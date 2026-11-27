# Rollout 計畫：trip_plan、quick_target（A10 實驗）、web_push（手機推播）

這份文件說明兩個 feature flag 在正式專案怎麼一步步打開、什麼時候往下一階段、什麼時候拉 kill switch。
flag 的設計見 `supabase/migrations/20261123000100_feature_flags.sql`；指標報表見
`supabase/reports/hdd_metrics.sql`（issue #59）。

## 現況（2026-11-23 部署到正式專案）

| flag | enabled | rollout | 說明 |
|---|---|---|---|
| `trip_plan` | true | 100 | 已上線的功能，維持全開；flag 只當 kill switch |
| `quick_target` | false | 0 | 新功能，所有人看不到，等 rollout 開始 |

`admin`、`beta` 角色不受 rollout 影響（`allowed_roles`），但 `enabled = false` 時一樣關閉。

## 階段

只有 `quick_target` 分階段打開；`trip_plan` 已經是 100%。

| 階段 | 日期 | quick_target | 進下一階段的條件 |
|---|---|---|---|
| 0 staging | 已完成 | admin、beta 測試 | 證據腳本 10 項 PASS、TC1–TC8 通過 |
| 1 正式專案安全部署 | 2026-11-23 已完成 | `false / 0` | 3 天沒有新錯誤 |
| 2 小規模 | 2026-12-06 | `true / 20` | 至少 2 天；guardrail 沒有 FAIL |
| 3 擴大 | 2026-12-10 | `true / 50` | 至少 2 天；guardrail 沒有 FAIL |
| 4 Demo | 2026-12-15 | `true / 100` | 現場示範 ON / OFF 切換 |

從 20% 調到 50%，原本在 ON 組的人不會被換掉（分桶固定，`tests/test_flags.py` 有測試）。

### 每個階段要做的事

在正式專案的 SQL Editor：

```sql
-- 1. 打開到這個階段的比例（例：階段 2）
update public.feature_flags set enabled = true, rollout = 20 where flag = 'quick_target';

-- 2. 指標從這個階段開始算
select set_config('hdd.since', '2026-12-06 00:00+08', false);  -- 台灣時間，要寫 +08
-- 接著貼上 supabase/reports/hdd_metrics.sql 全文執行
```

每換一個階段就重設 `hdd.since`，各階段的指標分開看。
每個人的組別看他在這個 flag 的第一筆 `exposed` 事件（打開頁面時記錄，variant 由伺服器決定），
所以 rollout 比例改了以後，之前已經分好的組不會變。

## 每天看的指標

執行 `supabase/reports/hdd_metrics.sql`，每列的 `result` 是 PASS / FAIL / INFO / NO DATA。

| 指標 | 門檻 | 用途 |
|---|---|---|
| H1 新使用者建立旅程的比例 | ON 組 ≥ 30% | 假設是否成立 |
| H2 新使用者第一次登入 → 第一個提醒（中位數） | ON 組 < 60 秒 | 假設是否成立 |
| 24 小時內修改提醒的比例 | ON 組不高於 OFF 組 | guardrail |
| 最近一次匯率檢查 | 成功、0 封寄信錯誤 | guardrail |
| check job 執行時間 | < 2 分鐘 | guardrail，看 GitHub Actions 的執行紀錄 |

H1、H2 都只算實驗期間才註冊的新使用者：舊使用者可能在階段開始前就建過旅程，會被誤算成「沒建立」。
`trip_plan` 在正式專案是 100%，沒有 OFF 組可比；H1 的門檻是絕對的 30%，不需要對照組（PR #60 review）。

報表只算一般使用者（`role = 'user'`）：admin、beta 是組員的測試帳號，永遠在 ON 組，
算進去的話，組員自己測試就可能讓小樣本變成 PASS（PR #60 review）。

H1、H2 沒過門檻**不是** kill switch 的理由：它代表假設不成立，不代表功能有害。
樣本不夠時顯示 NO DATA（H1 少於 10 人、H2 少於 5 人），不做判斷。

## Kill switch

**什麼時候拉：**

- 匯率檢查連續兩次失敗，或出現寄信錯誤
- check job 超過 2 分鐘
- 「24 小時內修改提醒」guardrail 變成 FAIL
- flag 關閉時，API 寫入卻成功（TC3 在正式專案失敗）

**怎麼拉（有正式專案權限的組員）：**

```sql
update public.feature_flags set enabled = false where flag = 'quick_target';
-- 或 trip_plan：關閉後旅程變唯讀（20261123000300），已建立的資料不會被刪
```

使用者重新整理頁面就生效，不需要重新部署。拉完在群組通知，並開 issue 記錄原因和時間
（`feature_flags.updated_at` 會記下切換時間）。恢復時把 `enabled` 改回 `true`，rollout 從上一個穩定的比例重新開始。

## 已知限制

- **分母只算打開過頁面的人**：分母是有 `exposed` 事件的使用者（PR #54 review 後加入）。
  註冊了但沒打開過頁面的人不算進去；前端要在載入頁面時記錄 `exposed`，報表才有分母。
- **「修改提醒」是估計值**：排程改 `state` 時也會更新 `updated_at`，報表排除和 `state_changed_at` 同時的更新；
  使用者修改後又被排程改過的提醒會被漏算，已刪除的提醒也查不到，所以這個比例只會偏低。
- **寄信錯誤只看最近一次**：`check_status` 只保留最後一筆，錯誤率的歷史要看 GitHub Actions。

---

# web_push（手機推播）

設計見 `docs/web-push.md`。和 A10 實驗分開：`web_push` 沒有實驗事件，HDD 報表只算一般使用者，
所以只開給 admin、beta 時不會影響 A10 的指標。

## 現況（2026-11-28）

| 項目 | 狀態 |
|---|---|
| 程式（PR 1～4） | 已合併：加到主畫面、資料表、訂閱介面、寄送 |
| 正式專案 `web_push` | `false / 0`（連 admin、beta 都關） |
| 正式專案金鑰 | 還沒設定（沒有私鑰 = 不推播；沒有公鑰 = 網頁顯示「還在設定中」） |
| 還沒做 | **Email 關閉的人只收推播**（要改 `active_rules`、`active_trips`，Demo 之後做） |

## 階段

| 階段 | 日期 | web_push | 進下一階段的條件 |
|---|---|---|---|
| 0 staging | 現在 | admin、beta（`supabase/staging/feature_flags.sql`） | 下方手動測試的電腦項目全部通過 |
| 0.5 正式專案只開給 admin | **PM 決定**（可以在 Demo 前） | `true / 0`，只有 admin、beta 看得到 | iPhone、Android 實機測試全部通過 |
| — Demo 期間不動 | 12/06～12/15 | 維持上一階段 | quick_target 的 rollout 期間不改 web_push，避免同時有兩個變化 |
| 1 補完 Email 關閉只推播 | 12/16 起 | 維持 | 該 PR 合併、staging 驗證：退訂 Email 後推播照送 |
| 2 小規模 | 補完後（約 12/18） | `true / 20` | 至少 2 天；健康報表沒有 FAIL、沒有 kill switch 條件 |
| 3 擴大 | 約 12/22 | `true / 50` | 同上 |
| 4 全部 | 約 12/29 | `true / 100` | 同上 |

階段 2～4 的日期是建議，依前一階段的結果調整。分桶和 quick_target 一樣固定，比例調高時已經開啟的人不會被關掉。

**開放正式專案前要先設定**（第一次進階段 0.5 時）：

1. 在自己的電腦產生正式專案的金鑰：`node scripts/vapid_keygen.js ../production.vapid`
2. 私鑰（檔案內容）→ Environment `production-runtime` 的 Secret `WEB_PUSH_PRIVATE_KEY`，然後刪掉檔案
3. 公鑰（畫面上印的）→ GitHub Variables 的 `WEB_PUSH_PUBLIC_KEY`，重新部署網站（deploy-pages）
4. 正式專案的 SQL Editor：

```sql
update public.feature_flags set enabled = true, rollout = 0 where flag = 'web_push';  -- 階段 0.5
update public.feature_flags set rollout = 20 where flag = 'web_push';                  -- 階段 2（之後 50、100）
```

## 每天看的指標

在 SQL Editor 執行 `supabase/reports/web_push_health.sql`（只讀、只有彙總數字，截圖可以公開）：

| 指標 | 門檻 |
|---|---|
| 失敗中的裝置（`fail_count > 0`）比例 | ≤ 20%（少於 5 台裝置時 NO DATA） |
| 每人最多幾台裝置、不在白名單的推播網址 | ≤ 5、0（資料庫已經擋住，應該永遠 PASS） |
| 裝置數、各推播服務、最近 24 小時送達、從沒送達、30 天沒打開 | 參考值 |

另外看 GitHub Actions 的 check-rates：

- log 裡「推播 N 台成功」「已失效、已移除」「失敗」的次數
- check job 執行時間仍然 < 2 分鐘（推播每台最多等 10 秒）

## Kill switch

**什麼時候拉：**

- 健康報表的「失敗中的裝置比例」FAIL（推播服務或金鑰出問題）
- 有人收到內容不對的推播，或在通知時段外收到推播
- 公開的 log 出現推播網址、金鑰或任何個人資料
- check job 因為推播超過 2 分鐘

**怎麼拉：**

```sql
update public.feature_flags set enabled = false where flag = 'web_push';
```

效果：下一次排程起完全不推播（寄送端連裝置都不讀），網頁的推播區塊隱藏，不能新增訂閱；
Email 不受影響，已經訂閱的裝置保留（使用者的資料不刪）。
更快的做法是刪掉 `production-runtime` 的 `WEB_PUSH_PRIVATE_KEY`：下一次排程就不推播，網頁不變。

拉完在群組通知，並開 issue 記錄原因和時間。恢復時把 `enabled` 改回 `true`，從上一個穩定的比例重新開始。

## 換金鑰（私鑰外洩，或定期更換）

1. 產生新的一組，私鑰換掉 `WEB_PUSH_PRIVATE_KEY`、公鑰換掉 `WEB_PUSH_PUBLIC_KEY` 並重新部署網站
2. 舊的訂閱用新私鑰送不出去（推播服務回 401／403）：使用者下次打開網站時，網頁會用新公鑰自動重新訂閱；
   一直沒打開的裝置連續失敗 10 次後自動刪除

