// FX Alert 的 service worker。
//
// 目前（PR 1）只負責讓網站可以「加入主畫面」，之後的 PR 會在這裡加上推播（push、notificationclick）。
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

const SW_VERSION = "2026-11-28.1";

self.addEventListener("install", () => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

// 讓網頁可以問目前生效的是哪一版（pwa.js 的 activeVersion）
self.addEventListener("message", (event) => {
  if (event.data && event.data.type === "version" && event.ports && event.ports[0]) {
    event.ports[0].postMessage({ version: SW_VERSION });
  }
});
