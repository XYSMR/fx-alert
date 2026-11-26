# Rollout 計畫：trip_plan、quick_target（A10 實驗）

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
select set_config('hdd.since', '2026-12-06', false);
-- 接著貼上 supabase/reports/hdd_metrics.sql 全文執行
```

每換一個階段就重設 `hdd.since`，各階段的指標分開看。
每個人的組別看他在這個 flag 的第一筆 `exposed` 事件（打開頁面時記錄，variant 由伺服器決定），
所以 rollout 比例改了以後，之前已經分好的組不會變。

## 每天看的指標

執行 `supabase/reports/hdd_metrics.sql`，每列的 `result` 是 PASS / FAIL / INFO / NO DATA。

| 指標 | 門檻 | 用途 |
|---|---|---|
| H1 建立旅程的使用者比例 | ON 組 ≥ 30% | 假設是否成立 |
| H2 新使用者第一次登入 → 第一個提醒（中位數） | ON 組 < 60 秒 | 假設是否成立 |
| 24 小時內修改提醒的比例 | ON 組不高於 OFF 組 | guardrail |
| 最近一次匯率檢查 | 成功、0 封寄信錯誤 | guardrail |
| check job 執行時間 | < 2 分鐘 | guardrail，看 GitHub Actions 的執行紀錄 |

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
