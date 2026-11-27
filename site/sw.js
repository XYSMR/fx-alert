// FX Alert 的 service worker。
//
// 負責兩件事：讓網站可以「加入主畫面」（PR 1）、收推播並顯示通知（PR 3）。
//
// 推播內容（PR 4 的寄送端產生，JSON）：{"title": "日圓到價：0.2040", "body": "...", "url": "./#JPY", "tag": "rate-JPY"}
//   - 鎖定畫面看得到，所以寄送端不會放目標價、金額或 email。
//   - 每則推播一定要顯示通知：iPhone 會取消「收到推播卻沒顯示通知」的網站的推播權限。
//   - 點通知只會打開本站的網址，payload 裡的外部網址一律改成首頁。
//   - 推播網址換了（pushsubscriptionchange）不在這裡處理：service worker 沒有登入資訊，
//     網頁下次打開時會重新同步（push.js 的 resync）。
//
// 刻意不做的事：
//   - 沒有 fetch 處理、不做離線快取：匯率一定要是最新的，快取住反而會顯示舊價格。
//     沒有 fetch 處理也代表就算這個檔案出錯，網頁照樣直接從網路載入，不會被卡住。
//
// 更新方式（詳見 docs/web-push.md「sw.js 怎麼更新到使用者手機」）：
//   - 只要這個檔案的內容有任何一個位元組不同，瀏覽器就會安裝新版。
//   - 修改時請把 SW_VERSION 改成新的日期，方便從開發者工具確認手機上跑的是哪一版。
//   - install 時 skipWaiting、activate 時 clients.claim：新版馬上生效，不用等使用者關掉所有分頁。
//     這樣做是安全的，因為沒有快取，新舊版本之間沒有資料要搬。
"use strict";

const SW_VERSION = "2026-11-28.2";
const DEFAULT_TITLE = "FX Alert 匯率提醒";

/** 推播的 payload → 要顯示的通知；壞掉或空的 payload 也要顯示一則通知 */
function notificationFrom(data) {
  let msg = {};
  try {
    msg = data ? data.json() : {};
  } catch {
    msg = {};
  }
  const text = (v, max) => (typeof v === "string" && v.trim() ? v.trim().slice(0, max) : null);
  const title = text(msg && msg.title, 80) || DEFAULT_TITLE;
  const options = {
    body: text(msg && msg.body, 200) || "點開查看最新匯率",
    icon: "icons/icon-192.png",
    badge: "icons/icon-192.png",
    data: { url: safeUrl(msg && msg.url) },
  };
  const tag = text(msg && msg.tag, 64);
  if (tag) options.tag = tag;
  return { title, options };
}

/** 只開本站的網址；外部網址、javascript: 等一律改成首頁 */
function safeUrl(raw) {
  const scope = self.registration.scope;
  try {
    const url = new URL(typeof raw === "string" ? raw : "./", scope);
    if (url.origin === new URL(scope).origin && url.href.startsWith(scope)) return url.href;
  } catch {
    // 不是網址
  }
  return scope;
}

self.addEventListener("install", () => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener("push", (event) => {
  const { title, options } = notificationFrom(event.data);
  event.waitUntil(self.registration.showNotification(title, options));
});

// 點通知：已經開著網站就切過去，沒有就開新視窗
self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = safeUrl(event.notification.data && event.notification.data.url);
  event.waitUntil((async () => {
    const wins = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const w of wins) {
      if (w.url.startsWith(self.registration.scope) && "focus" in w) {
        await w.focus();
        return "navigate" in w ? w.navigate(url).catch(() => {}) : undefined;
      }
    }
    return self.clients.openWindow(url);
  })());
});

// 讓網頁可以問目前生效的是哪一版（pwa.js 的 activeVersion）
self.addEventListener("message", (event) => {
  if (event.data && event.data.type === "version" && event.ports && event.ports[0]) {
    event.ports[0].postMessage({ version: SW_VERSION });
  }
});
