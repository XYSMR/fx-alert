# 手機推播（PWA＋Web Push）設計

狀態：PR 1（PWA 基礎）、PR 2（資料表）已合併並套用；PR 3（訂閱介面）進行中。推播本身在 PR 2～4，正式專案的開放排在 12/15 A10 Demo 之後。

## 已決定的事

| 項目 | 決定 |
|---|---|
| 推播和 Email | 推播是**額外的**，Email 照寄 |
| 鎖定畫面內容 | 只顯示幣別和目前價格，例如「日圓到價：0.2040」；**不顯示目標價** |
| 哪些提醒推播 | 到價提醒、換匯計畫提醒都推 |
| 時程 | PR 1～3 現在做；正式專案的 `web_push` 等 12/15 Demo 之後才開 |
| 技術 | 標準 service worker 推播；Declarative Web Push 之後再評估 |
| 測試裝置 | iPhone（iOS 27.2.1）、Android |

## PR 拆法

| PR | 內容 |
|---|---|
| 1 | PWA 基礎：`manifest.webmanifest`、圖示、`sw.js`（還不收推播）、`pwa.js`、加入主畫面說明 |
| 2 | 資料表 `push_subscriptions`、RLS、推播網址白名單、`web_push` flag（預設關閉） |
| 3 | 網頁訂閱介面：先顯示說明再問權限、裝置列表、設定頁的 Email／推播說明 |
| 4 | 寄送：check-rates 用 pywebpush 送推播、處理 404／410、每日上限、log 不洩漏 |
| 5 | 文件和 rollout |

PR 2 的 migration **只新增資料表、函式和一筆預設關閉的 flag**，不修改任何現有資料表、函式或 flag，
所以 Demo 用到的 `quick_target`、`trip_plan` 和 Email 提醒都不受影響。

## 資料表 `push_subscriptions`（PR 2）

migration：`supabase/migrations/20261128021121_push_subscriptions.sql`

| 誰 | 可以做什麼 |
|---|---|
| 使用者 | 看自己的裝置（不含加密金鑰）、改裝置名稱、刪除裝置 |
| 使用者新增或重新同步 | 只能呼叫 `save_push_subscription()`：要登入、`web_push` 對這個人是開的，否則 403 |
| 排程（service_role） | 讀金鑰、記錄成功時間和失敗次數、刪除失效的訂閱 |

- 每人最多 5 台裝置；重新同步已經有的裝置不算新增。
- 同一台裝置換人登入：同一個推播網址改歸新帳號，舊帳號的裝置名稱和成功紀錄不帶過去。
  推播網址只有那台裝置的瀏覽器知道，所以拿得到網址就代表在用那台裝置。
- `last_seen_at`：網頁每次打開都會更新。Apple 對失效的訂閱可能還是回 200，之後可以用它找出很久沒出現的裝置。
- `web_push` 關閉時：不能新增，已經有的裝置還在，看得到也刪得掉。
- 刪除帳號：裝置一起刪除。
- **只新增、不修改**：這個 migration 只新增推播自己的物件和一筆預設關閉的 flag，
  `tests/test_push_migration_scope.py` 逐句檢查，確保不會影響 Demo 用的 `trip_plan`、`quick_target` 和 Email 提醒。

## 網頁訂閱介面（PR 3）

設定頁「推播通知」區塊（`site/push.js`），`web_push` 對這個人開啟時才顯示：

- 依裝置顯示：可以開啟（「開啟推播」按鈕）、iPhone 還沒加到主畫面（教加入主畫面）、
  之前拒絕過（教到系統設定打開）、瀏覽器不支援、還沒設定 VAPID 公鑰。
- 裝置列表：每台裝置的名稱、最後送達時間、「移除」；這台裝置是「關閉這台」。
- Email 和推播的關係表格（見下方）一直顯示在這個區塊裡。
- **每次打開網站都重新同步**（`resyncAction`）：
  - 瀏覽器有訂閱、資料庫也有 → 更新金鑰和 `last_seen_at`
  - 瀏覽器的訂閱不見了（例如 Apple 換了推播網址），資料庫還有 → 重新訂閱、換成新網址
  - 資料庫已經沒有（在別台裝置移除了）→ 不自動加回去
- **登出時停止這台裝置的推播**（刪除這台的訂閱並取消瀏覽器的訂閱），下一個登入的人要自己重新開啟。
- `sw.js` 收到推播一定顯示通知（iPhone 的規定），點通知只會打開本站的網址。

### VAPID 金鑰（你自己產生，正式專案和 staging 各一組）

```bash
node scripts/vapid_keygen.js ../staging.vapid      # 私鑰寫進檔案（不會印在畫面上），畫面只印公鑰
node scripts/vapid_keygen.js ../production.vapid
```

| 放哪裡 | staging | 正式專案 |
|---|---|---|
| 公鑰 | 本機 `WEB_PUSH_PUBLIC_KEY`（產生 config.js 時） | GitHub Variables `WEB_PUSH_PUBLIC_KEY` |
| 私鑰（檔案內容） | Environment `staging-runtime` 的 Secret `WEB_PUSH_PRIVATE_KEY` | Environment `production-runtime` 的 Secret `WEB_PUSH_PRIVATE_KEY` |

私鑰貼進 Secret 之後就刪掉 `.vapid` 檔案（`.gitignore` 已排除 `*.vapid`）。
換金鑰之後，所有裝置的舊訂閱都會失效，使用者下次打開網站時會自動用新公鑰重新訂閱。

### 給 PR 4（寄送端）的注意事項

- 目前 Email 關閉（`email_enabled = false`）的人，排程完全不檢查他的提醒。
  推播和 Email 互相獨立，所以 PR 4 要改成：Email 關閉但有推播裝置的人照樣檢查、只送推播。
- 推播內容的格式：`{"title": "日圓到價：0.2040", "body": "…", "url": "./#JPY", "tag": "rate-JPY"}`，
  不放目標價、金額或 email。

## 推播網址白名單（PR 2、PR 4）

瀏覽器訂閱推播時會給一個推播網址（endpoint），我們的排程之後會對這個網址發送請求。
如果不限制，任何登入的人都可以登記任意網址，讓 GitHub Actions 去連那個網址（SSRF）。

規則（資料庫的檢查限制和寄送程式各擋一次）：

1. 只接受 `https://`，不能有帳號密碼（`user@host`）、不能指定 port。
2. 主機名稱用**結尾比對**，而且要比對到網域的邊界：

   | 允許 | 比對方式 |
   |---|---|
   | `web.push.apple.com` 等 | 主機名稱以 `.push.apple.com` 結尾 |
   | `fcm.googleapis.com` | 完全相同 |
   | `updates.push.services.mozilla.com` | 完全相同 |
   | `*.notify.windows.com` | 以 `.notify.windows.com` 結尾 |

3. 寄送時**不跟隨轉址**：推播服務回 3xx 一律當成失敗，不會再去連 `Location` 的網址。

一定要被擋下的例子（完整清單在 `tests/fixtures/push_endpoints.json`，資料庫和寄送端共用）：

- `https://evil.push.apple.com.attacker.com/...`：包含 `push.apple.com`，但不是以它結尾
- `https://evilpush.apple.com/...`：結尾字串相同，但不在網域邊界上
- `http://web.push.apple.com/...`：不是 https
- `https://web.push.apple.com:8443/...`、`https://user@web.push.apple.com/...`
- 推播服務回 301／302／307／308 轉到其他網址

## 權限只能問一次（PR 3）

iPhone 上使用者如果在系統視窗按了「不允許」，網頁就沒辦法再跳出詢問，只能請使用者到「設定」裡自己打開。
所以按「開啟推播」時：

1. 先顯示網頁自己的說明：會收到哪些通知（到價提醒、換匯計畫提醒）、只在通知時段內送、
   鎖定畫面只顯示幣別和價格、Email 照常寄。
2. 使用者在說明裡按「好，開啟」，才呼叫 `Notification.requestPermission()`，跳出系統詢問。
   按「先不要」就什麼都不問，之後還可以再按。
3. 如果權限已經是「拒絕」，不顯示開啟按鈕，改說明怎麼到 iPhone「設定 → 通知 → FX Alert」打開。
4. iPhone 上如果不是從主畫面打開，先顯示「加入主畫面」的說明，不顯示開啟按鈕（Safari 分頁裡不能訂閱推播）。

## Email 退訂和推播開關的關係（PR 3，會寫在設定頁上）

| | Email 通知 | 推播通知 |
|---|---|---|
| 管什麼 | 寄到你信箱的到價提醒和換匯計畫提醒 | 送到「這台裝置」的到價提醒和換匯計畫提醒 |
| 怎麼關 | 設定頁的 Email 開關，或信裡的退訂連結 | 設定頁的推播開關（每台裝置分開），或手機系統設定 |
| 互相影響 | **不會**。按信裡的退訂只關 Email，推播照送 | **不會**。關掉推播，Email 照寄 |

兩者共用的規則：

- 通知時段：兩種都只在你設定的通知時段內送。
- 提醒規則：到價條件、換匯計畫的提醒日是同一套判斷，同一則提醒 Email 和推播同時送。
- 刪除帳號：Email 設定和所有裝置的推播訂閱一起刪除。
- 兩種都關掉時，提醒規則還在，只是不會通知你；網站上照樣看得到到價狀態。

## sw.js 怎麼更新到使用者手機

service worker 一旦裝在手機上，就算網站更新了，手機上跑的也可能還是舊版。這個專案用以下方式讓手機盡快換成新版：

1. **瀏覽器會自己檢查**：每次打開網站、每次 `register()`，瀏覽器都會重新下載 `sw.js` 比對，
   只要有任何一個位元組不同就安裝新版。收到推播時，如果距離上次檢查超過 24 小時也會檢查。
2. **跳過 HTTP 快取**：`pwa.js` 註冊時設定 `updateViaCache: "none"`，檢查時一律跟 GitHub Pages 要最新的檔案，
   不會被 Pages 的 10 分鐘快取擋住。
3. **從背景切回來也檢查**：iPhone 的網頁 App 常常是從背景叫回來，而不是重新載入。
   `pwa.js` 在頁面重新顯示時呼叫 `registration.update()`，最多每 10 分鐘一次。
4. **新版馬上生效**：`sw.js` 在 install 時 `skipWaiting()`、activate 時 `clients.claim()`，
   不用等使用者關掉所有分頁。因為 `sw.js` 不做快取，新舊版本之間沒有資料要搬，馬上切換是安全的。
5. **看得出是哪一版**：`sw.js` 裡有 `SW_VERSION`，每次修改都要改成新的日期（例如 `2026-11-28.1`）。
   在手機上可以用 `await FxPwa.activeVersion()`（Safari 網頁檢查器或 Chrome 遠端偵錯）確認目前生效的版本。

萬一發布了有問題的 `sw.js`：

- `sw.js` 沒有攔截網路請求，就算有錯，網頁本身照樣從網路正常載入。
- 修好後直接部署新版，上面的機制會在使用者下次打開或切回網站時換掉舊版。
- 真的需要整個停用時，可以發布一版只做 `self.registration.unregister()` 的 `sw.js`。

## 加入主畫面

- iPhone／iPad：用 Safari 打開網站 → 「分享」→「加入主畫面」，之後從主畫面的圖示打開。
  推播只能在這個模式下開啟。
- Android、電腦：瀏覽器選單裡的「安裝應用程式」或「加到主畫面」；不加也可以收推播。
- 網站頁尾有「加到手機主畫面」的說明，從主畫面打開時不顯示。
