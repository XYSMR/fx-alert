// node --test site/tests/*.test.js
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const pwa = require("../pwa.js");

const SITE = path.join(__dirname, "..");
const read = (f) => fs.readFileSync(path.join(SITE, f), "utf8");

/** PNG 的寬高（IHDR） */
function pngSize(file) {
  const buf = fs.readFileSync(path.join(SITE, file));
  assert.equal(buf.toString("hex", 0, 8), "89504e470d0a1a0a", `${file} 不是 PNG`);
  return [buf.readUInt32BE(16), buf.readUInt32BE(20)];
}

/** 在假的 service worker 環境執行 sw.js，回傳註冊的事件和呼叫紀錄 */
function loadServiceWorker() {
  const listeners = {};
  const calls = [];
  const self = {
    addEventListener(type, fn) {
      (listeners[type] ||= []).push(fn);
    },
    skipWaiting() {
      calls.push("skipWaiting");
      return Promise.resolve();
    },
    clients: {
      claim() {
        calls.push("claim");
        return Promise.resolve();
      },
    },
  };
  vm.runInNewContext(read("sw.js"), { self });
  return { listeners, calls };
}

// ---------- manifest ----------

test("manifest：從網站根目錄開啟、全螢幕、相對路徑（GitHub Pages 在子路徑底下）", () => {
  const m = JSON.parse(read("manifest.webmanifest"));
  assert.equal(m.start_url, "./");
  assert.equal(m.scope, "./");
  assert.equal(m.id, "./");
  assert.equal(m.display, "standalone");
  assert.equal(m.lang, "zh-Hant-TW");
  assert.ok(m.short_name.length <= 12, "主畫面圖示下的名稱太長會被截斷");
  for (const icon of m.icons) assert.ok(!icon.src.startsWith("/"), `${icon.src} 不能用絕對路徑`);
});

test("manifest：圖示檔案存在、尺寸和宣告的一樣，而且有 maskable 版本", () => {
  const m = JSON.parse(read("manifest.webmanifest"));
  for (const icon of m.icons) {
    const [w, h] = pngSize(icon.src);
    assert.equal(`${w}x${h}`, icon.sizes, icon.src);
  }
  const sizes = m.icons.filter((i) => i.purpose === "any").map((i) => i.sizes);
  assert.deepEqual(sizes.sort(), ["192x192", "512x512"]);
  assert.ok(m.icons.some((i) => i.purpose === "maskable"));
  assert.deepEqual(pngSize("icons/apple-touch-icon.png"), [180, 180]);
});

// ---------- sw.js ----------

test("sw.js：不攔截網路請求、不做離線快取（匯率必須是最新的）", () => {
  const { listeners } = loadServiceWorker();
  assert.equal(listeners.fetch, undefined);
  const src = read("sw.js");
  assert.doesNotMatch(src, /caches\.|addEventListener\(\s*["']fetch/);
});

test("sw.js：新版安裝後立刻生效（skipWaiting＋clients.claim）", async () => {
  const { listeners, calls } = loadServiceWorker();
  listeners.install.forEach((fn) => fn({ waitUntil() {} }));
  const pending = [];
  listeners.activate.forEach((fn) => fn({ waitUntil: (p) => pending.push(p) }));
  await Promise.all(pending);
  assert.deepEqual(calls, ["skipWaiting", "claim"]);
});

test("sw.js：有版本號，網頁可以問目前是哪一版", () => {
  const m = read("sw.js").match(/const SW_VERSION = "([^"]+)";/);
  assert.ok(m, "sw.js 要有 SW_VERSION");
  assert.match(m[1], /^\d{4}-\d{2}-\d{2}\.\d+$/);
  const { listeners } = loadServiceWorker();
  const replies = [];
  const port = { postMessage: (msg) => replies.push(msg) };
  listeners.message.forEach((fn) => fn({ data: { type: "version" }, ports: [port] }));
  listeners.message.forEach((fn) => fn({ data: { type: "other" }, ports: [port] }));
  assert.equal(JSON.stringify(replies), JSON.stringify([{ version: m[1] }])); // 不同 vm context 的物件
});

// ---------- pwa.js：裝置判斷 ----------

const UA = {
  iphone: "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Version/18.0 Mobile/15E148 Safari/604.1",
  ipadOs: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Version/18.0 Safari/605.1.15",
  android: "Mozilla/5.0 (Linux; Android 15; Pixel 8) AppleWebKit/537.36 Chrome/131.0 Mobile Safari/537.36",
};

test("isIos：iPhone、iPadOS（假裝成 Mac 但有觸控）算 iOS；Mac 和 Android 不算", () => {
  assert.equal(pwa.isIos(UA.iphone, 5), true);
  assert.equal(pwa.isIos(UA.ipadOs, 5), true);
  assert.equal(pwa.isIos(UA.ipadOs, 0), false); // 真的 Mac
  assert.equal(pwa.isIos(UA.android, 5), false);
  assert.equal(pwa.isIos(undefined, undefined), false);
});

function fakeWin({ ua = UA.iphone, touch = 5, standaloneMedia = false, navStandalone } = {}) {
  return {
    matchMedia: (q) => ({ matches: q === "(display-mode: standalone)" && standaloneMedia }),
    navigator: { userAgent: ua, maxTouchPoints: touch, standalone: navStandalone },
  };
}

test("installHint：已從主畫面打開就不顯示；iPhone 和其他裝置顯示不同說明", () => {
  assert.equal(pwa.installHint(fakeWin()), "ios");
  assert.equal(pwa.installHint(fakeWin({ ua: UA.android })), "other");
  assert.equal(pwa.installHint(fakeWin({ standaloneMedia: true })), null);
  assert.equal(pwa.installHint(fakeWin({ navStandalone: true })), null); // iOS 舊版的判斷方式
  const noMedia = fakeWin();
  noMedia.matchMedia = () => {
    throw new Error("不支援");
  };
  assert.equal(pwa.installHint(noMedia), "ios");
});

// ---------- pwa.js：註冊與更新 ----------

function fakeSwWindow({ registerFails = false } = {}) {
  const docListeners = {};
  const registered = [];
  const reg = { updates: 0, update() { this.updates += 1; return Promise.resolve(); } };
  const win = {
    navigator: {
      serviceWorker: {
        register(url, opts) {
          registered.push([url, opts]);
          return registerFails ? Promise.reject(new Error("blocked")) : Promise.resolve(reg);
        },
      },
    },
    document: {
      visibilityState: "visible",
      addEventListener: (type, fn) => (docListeners[type] = fn),
    },
  };
  return { win, reg, registered, docListeners };
}

test("register：用 sw.js、scope ./，檢查更新時跳過 HTTP 快取", async () => {
  const { win, registered } = fakeSwWindow();
  await pwa.register(win, () => 0);
  assert.deepEqual(registered, [["sw.js", { scope: "./", updateViaCache: "none" }]]);
});

test("register：瀏覽器不支援或註冊失敗時回傳 null，不影響網站其他功能", async () => {
  assert.equal(await pwa.register({ navigator: {} }), null);
  const { win } = fakeSwWindow({ registerFails: true });
  const warn = console.warn;
  console.warn = () => {};
  try {
    assert.equal(await pwa.register(win, () => 0), null);
  } finally {
    console.warn = warn;
  }
});

test("register：從背景切回來時檢查 sw.js，但最多每 10 分鐘一次", async () => {
  const { win, reg, docListeners } = fakeSwWindow();
  let now = 0;
  await pwa.register(win, () => now);
  const back = () => docListeners.visibilitychange();

  now = 5 * 60 * 1000;
  back();
  assert.equal(reg.updates, 0, "剛註冊完不用馬上再檢查");

  now = 10 * 60 * 1000;
  back();
  assert.equal(reg.updates, 1);

  win.document.visibilityState = "hidden";
  now = 60 * 60 * 1000;
  back();
  assert.equal(reg.updates, 1, "切到背景時不檢查");

  win.document.visibilityState = "visible";
  back();
  assert.equal(reg.updates, 2);
});

test("shouldCheckUpdate：從沒檢查過，或距離上次滿 10 分鐘", () => {
  assert.equal(pwa.shouldCheckUpdate(null, 0), true);
  assert.equal(pwa.shouldCheckUpdate(0, pwa.UPDATE_INTERVAL_MS - 1), false);
  assert.equal(pwa.shouldCheckUpdate(0, pwa.UPDATE_INTERVAL_MS), true);
});

// ---------- index.html ----------

test("index.html：有 manifest、iOS 圖示，pwa.js 最後載入，CSP 沒有放寬", () => {
  const html = read("index.html");
  assert.match(html, /<link rel="manifest" href="manifest\.webmanifest">/);
  assert.match(html, /<link rel="apple-touch-icon" href="icons\/apple-touch-icon\.png">/);
  const scripts = [...html.matchAll(/<script src="([^"]+)"/g)].map((m) => m[1]);
  assert.equal(scripts.at(-1), "pwa.js");
  const csp = html.match(/Content-Security-Policy"\s+content="([^"]+)"/)[1];
  assert.match(csp, /default-src 'self';/);
  assert.match(csp, /script-src 'self';/);
  assert.doesNotMatch(csp, /worker-src|manifest-src|unsafe-eval/);
});

test("index.html：加入主畫面的說明預設隱藏，兩種裝置各一段", () => {
  const html = read("index.html");
  assert.match(html, /<details class="install-help" id="install-help" hidden>/);
  const kinds = [...html.matchAll(/data-install="(\w+)" hidden/g)].map((m) => m[1]);
  assert.deepEqual(kinds, ["ios", "other"]);
  assert.match(html, /「加入主畫面」/);
});

test("showInstallHelp：只顯示符合裝置的那一段；從主畫面打開時整段保持隱藏", () => {
  function fakeDoc() {
    const paras = ["ios", "other"].map((k) => ({ hidden: true, getAttribute: () => k }));
    const box = { hidden: true, querySelectorAll: () => paras };
    return { box, paras, document: { getElementById: (id) => (id === "install-help" ? box : null) } };
  }
  const a = fakeDoc();
  pwa.showInstallHelp({ ...fakeWin({ ua: UA.android }), document: a.document });
  assert.equal(a.box.hidden, false);
  assert.deepEqual(a.paras.map((p) => p.hidden), [true, false]);

  const b = fakeDoc();
  pwa.showInstallHelp({ ...fakeWin({ standaloneMedia: true }), document: b.document });
  assert.equal(b.box.hidden, true);
});
