// node --test site/tests/*.test.js
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const p = require("../push.js");

const CASES = JSON.parse(fs.readFileSync(
  path.join(__dirname, "../../tests/fixtures/push_endpoints.json"), "utf8"));
const SRC = fs.readFileSync(path.join(__dirname, "../push.js"), "utf8");

const P256DH = "B" + "A".repeat(86);
const AUTH = "C".repeat(22);
const APPLE = CASES.allowed[0];

// ---------- 白名單（和資料庫、寄送端共用同一份案例） ----------

test("白名單：允許的推播服務都通過", () => {
  for (const url of CASES.allowed) assert.equal(p.isAllowedEndpoint(url), true, url);
});

test("白名單：拒絕的網址全部擋下（和資料庫的 is_allowed_push_endpoint 相同）", () => {
  for (const [reason, url] of Object.entries(CASES.denied)) {
    assert.equal(p.isAllowedEndpoint(url), false, reason);
  }
  const ok = "https://web.push.apple.com/" + "A".repeat(2048 - 27);
  assert.equal(p.isAllowedEndpoint(ok), true);
  assert.equal(p.isAllowedEndpoint(ok + "A"), false);
  for (const v of [null, undefined, 42, {}]) assert.equal(p.isAllowedEndpoint(v), false);
});

// ---------- 能不能開 ----------

const READY = {
  secure: true, hasServiceWorker: true, hasPushManager: true, hasNotification: true,
  permission: "default", iosNotInstalled: false,
};

test("pushStatus：iPhone 沒從主畫面打開 → 先教加入主畫面（即使瀏覽器缺 PushManager）", () => {
  assert.equal(p.pushStatus({ ...READY, iosNotInstalled: true }), "ios-install");
  assert.equal(p.pushStatus({ ...READY, iosNotInstalled: true, hasPushManager: false }), "ios-install");
});

test("pushStatus：不支援、拒絕過、可以開", () => {
  for (const missing of ["secure", "hasServiceWorker", "hasPushManager", "hasNotification"]) {
    assert.equal(p.pushStatus({ ...READY, [missing]: false }), "unsupported", missing);
  }
  assert.equal(p.pushStatus({ ...READY, permission: "denied" }), "denied");
  assert.equal(p.pushStatus(READY), "ready");
  assert.equal(p.pushStatus({ ...READY, permission: "granted" }), "ready");
});

// ---------- 訂閱資料 ----------

test("base64UrlToBytes：VAPID 公鑰轉成 65 bytes", () => {
  const raw = Buffer.alloc(65, 7);
  raw[0] = 4;
  const bytes = p.base64UrlToBytes(raw.toString("base64url"));
  assert.equal(bytes.length, 65);
  assert.deepEqual(Buffer.from(bytes), raw);
});

const fakeSub = (endpoint, keys = { p256dh: P256DH, auth: AUTH }) => ({ toJSON: () => ({ endpoint, keys }) });

test("subscriptionKeys：取出網址和兩個金鑰；格式不對或網址不在白名單就是 null", () => {
  assert.deepEqual(p.subscriptionKeys(fakeSub(APPLE)), { endpoint: APPLE, p256dh: P256DH, auth: AUTH });
  assert.equal(p.subscriptionKeys(fakeSub("https://evil.push.apple.com.attacker.com/x")), null);
  assert.equal(p.subscriptionKeys(fakeSub(APPLE, { p256dh: "short", auth: AUTH })), null);
  assert.equal(p.subscriptionKeys(fakeSub(APPLE, { p256dh: P256DH })), null);
  assert.equal(p.subscriptionKeys(fakeSub(APPLE, null)), null);
  assert.equal(p.subscriptionKeys(null), null);
});

test("deviceLabel：裝置列表上看得懂的名稱（最長 30 字，資料庫的限制）", () => {
  const cases = [
    ["Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)", 5, "iPhone"],
    ["Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)", 5, "iPad"],
    ["Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)", 0, "Mac"],
    ["Mozilla/5.0 (Linux; Android 15; Pixel 8) Mobile Safari", 5, "Android 手機"],
    ["Mozilla/5.0 (Linux; Android 15; SM-X710) Safari", 5, "Android 平板"],
    ["Mozilla/5.0 (Windows NT 10.0; Win64; x64)", 0, "Windows 電腦"],
    [undefined, undefined, "這台裝置"],
  ];
  for (const [ua, touch, want] of cases) {
    assert.equal(p.deviceLabel(ua, touch), want, ua);
    assert.ok(want.length <= 30);
  }
});

// ---------- 說明文字 ----------

test("開啟前的說明：通知種類、通知時段、鎖定畫面不顯示目標價、只問一次", () => {
  const lines = p.explainerLines({ emailOn: true, windowText: "08:00–22:00" });
  const text = lines.join("\n");
  assert.match(text, /到價提醒、換匯計畫提醒/);
  assert.match(text, /台灣時間 08:00–22:00/);
  assert.match(text, /不會顯示你的目標價/);
  assert.match(text, /Email 照常寄/);
  assert.match(text, /只會問這一次/);
  assert.match(text, /「設定」/);
});

test("開啟前的說明：Email 關閉時講清楚只會推播", () => {
  const text = p.explainerLines({ emailOn: false, windowText: "全天" }).join("\n");
  assert.match(text, /Email 通知目前是關閉的/);
  assert.doesNotMatch(text, /Email 照常寄/);
});

test("設定頁的 Email／推播關係：各管各的、互不影響、共用時段和規則", () => {
  const all = JSON.stringify(p.RELATION);
  assert.deepEqual(p.RELATION.head, ["", "Email 通知", "推播通知"]);
  assert.deepEqual(p.RELATION.rows.map((r) => r[0]), ["管什麼", "怎麼關", "互相影響"]);
  assert.match(all, /退訂 Email，推播照送/);
  assert.match(all, /關掉推播，Email 照寄/);
  assert.match(p.RELATION.shared, /通知時段/);
  assert.match(p.RELATION.shared, /刪除帳號/);
});

test("拒絕過權限：iPhone 教到「設定 → 通知」，其他裝置教網站設定", () => {
  assert.match(p.deniedHelp("ios"), /「設定」→「通知」→「FX Alert」/);
  assert.match(p.deniedHelp("other"), /網站設定/);
});

test("資料庫錯誤 → 使用者看得懂的訊息", () => {
  assert.match(p.saveErrorMessage({ code: "42501" }), /還沒開放/);
  assert.match(p.saveErrorMessage({ code: "23514", message: "每人最多 5 台裝置接收推播" }), /最多 5 台/);
  assert.match(p.saveErrorMessage({ code: "23514", message: "violates check constraint" }), /支援清單/);
  assert.match(p.saveErrorMessage(null), /稍後再試/);
});

// ---------- 這台裝置的紀錄、重新同步 ----------

function memoryStorage() {
  const m = new Map();
  return {
    getItem: (k) => (m.has(k) ? m.get(k) : null),
    setItem: (k, v) => m.set(k, String(v)),
    removeItem: (k) => m.delete(k),
  };
}

test("readDevice／writeDevice：只認同一個帳號；讀寫失敗不出錯", () => {
  const s = memoryStorage();
  p.writeDevice(s, { userId: "alice", endpoint: APPLE });
  assert.deepEqual(p.readDevice(s, "alice"), { userId: "alice", endpoint: APPLE });
  assert.equal(p.readDevice(s, "bob"), null);
  p.writeDevice(s, null);
  assert.equal(p.readDevice(s, "alice"), null);
  s.setItem(p.STORAGE_KEY, "{broken");
  assert.equal(p.readDevice(s, "alice"), null);
  const broken = { getItem() { throw new Error("blocked"); }, setItem() { throw new Error("blocked"); } };
  assert.equal(p.readDevice(broken, "alice"), null);
  assert.doesNotThrow(() => p.writeDevice(broken, { userId: "a", endpoint: APPLE }));
});

test("resyncAction：每次打開網站的同步方式", () => {
  const row = (endpoint) => ({ endpoint });
  const stored = { userId: "u", endpoint: APPLE };
  const NEW = "https://web.push.apple.com/new";
  const cases = [
    // 沒允許通知：什麼都不做
    [{ permission: "default", currentEndpoint: APPLE, stored, rows: [row(APPLE)] }, "none"],
    // 正常：更新金鑰和 last_seen_at
    [{ permission: "granted", currentEndpoint: APPLE, stored, rows: [row(APPLE)] }, "save"],
    [{ permission: "granted", currentEndpoint: APPLE, stored: null, rows: [row(APPLE)] }, "save"],
    // 瀏覽器的訂閱不見了，但資料庫還有：重新訂閱
    [{ permission: "granted", currentEndpoint: null, stored, rows: [row(APPLE)] }, "resubscribe"],
    // 在別台裝置移除了：不自動加回去
    [{ permission: "granted", currentEndpoint: APPLE, stored, rows: [] }, "forget"],
    [{ permission: "granted", currentEndpoint: null, stored, rows: [] }, "forget"],
    // 這台的訂閱屬於別的帳號（換人登入）：等使用者自己按開啟
    [{ permission: "granted", currentEndpoint: APPLE, stored: null, rows: [] }, "none"],
    // 瀏覽器有新的訂閱、資料庫還是舊的：不自動處理（不確定是不是同一個人）
    [{ permission: "granted", currentEndpoint: NEW, stored, rows: [row(APPLE)] }, "none"],
    // 換過 VAPID 金鑰：用新公鑰重新訂閱（舊訂閱送不出去）
    [{ permission: "granted", currentEndpoint: APPLE, stored, rows: [row(APPLE)], keyMatches: false }, "rekey"],
    [{ permission: "granted", currentEndpoint: APPLE, stored, rows: [], keyMatches: false }, "forget"],
  ];
  for (const [input, want] of cases) assert.equal(p.resyncAction(input), want, JSON.stringify(input));
});

test("keyMatches：比對訂閱用的公鑰；瀏覽器沒提供時當作相同", () => {
  const raw = Buffer.alloc(65, 9);
  raw[0] = 4;
  const key = raw.toString("base64url");
  const other = Buffer.from(raw).fill(8, 1).toString("base64url");
  const sub = { options: { applicationServerKey: new Uint8Array(raw).buffer } };
  assert.equal(p.keyMatches(sub, key), true);
  assert.equal(p.keyMatches(sub, other), false);
  assert.equal(p.keyMatches({ options: {} }, key), true);
  assert.equal(p.keyMatches({}, key), true);
  assert.equal(p.keyMatches(sub, null), true);
});

// ---------- 程式結構 ----------

test("權限只在按「好，開啟」時詢問，而且在任何 await 之前（iPhone 要求直接由使用者操作觸發）", () => {
  const calls = SRC.match(/Notification\.requestPermission\(/g) || [];
  assert.equal(calls.length, 1, "只能有一個地方詢問權限");
  const body = SRC.slice(SRC.indexOf("function enable("));
  const end = body.indexOf("\n  }\n");
  const fn = body.slice(0, end);
  assert.ok(fn.indexOf("requestPermission") < fn.indexOf("await"), "requestPermission 要在第一個 await 之前");
  // 呼叫 enable 的只有說明裡的「好，開啟」
  assert.deepEqual(SRC.match(/enable\(ok\)/g), ["enable(ok)"]);
  assert.match(SRC, /"push-enable", onclick: onEnable/);
});

test("新增訂閱只透過 save_push_subscription（資料庫檢查 flag），不直接 insert", () => {
  assert.match(SRC, /rpc\("save_push_subscription"/);
  assert.doesNotMatch(SRC, /\.insert\(/);
  assert.doesNotMatch(SRC, /\.upsert\(/);
});

test("沒有讀取加密金鑰或 user_id（使用者沒有這些欄位的權限）", () => {
  const selects = [...SRC.matchAll(/\.select\("([^"]+)"\)/g)].map((m) => m[1]);
  assert.deepEqual(selects, ["id,endpoint,label,created_at,last_success_at"]);
});

test("瀏覽器裡只往全域放 FxPush；web_push 沒開時不顯示區塊", () => {
  const sandbox = { document: {}, FxFlags: { isOn: () => false } };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  const before = new Set(Object.keys(sandbox));
  vm.runInContext(SRC, sandbox);
  const added = Object.keys(sandbox).filter((k) => !before.has(k));
  assert.deepEqual(added, ["FxPush"]);
  assert.deepEqual(Object.keys(sandbox.FxPush).sort(), ["forgetThisDevice", "section"]);
  assert.equal(sandbox.FxPush.section({}), null);
});

test("index.html：push.js 在 account.js 之前載入", () => {
  const html = fs.readFileSync(path.join(__dirname, "../index.html"), "utf8");
  const scripts = [...html.matchAll(/<script src="([^"]+)"/g)].map((m) => m[1]);
  assert.ok(scripts.indexOf("push.js") >= 0);
  assert.ok(scripts.indexOf("push.js") < scripts.indexOf("account.js"));
});
