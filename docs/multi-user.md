# 多人版設計（Supabase）

單人版：設定在 `config.yaml`、狀態在 state 分支、收件人在 `MAIL_TO` Secret。
多人版：每個人用 Google 登入，在網頁上設定自己的目標價，到價寄到自己的信箱。

## 原則

- **公開資料留在 git，個人資料只在 Supabase。** 匯率走勢、`last_check.json` 繼續放 state 分支並發布成 `data.json`；
  email、目標價、狀態、通知紀錄永遠不進 git，`data.json` 也不再包含目標價。
- **前端只放 publishable key，安全靠 RLS。** secret key 只放 GitHub Secrets（排程）和 Edge Function secrets。
- **`rules.py` 不變。** 排程對「每個人的每條規則」呼叫同一個 `decide()`。

## 資料表（`supabase/migrations/`）

| 表 | 用途 | 一般使用者（authenticated） | 沒登入（anon） | 排程（service_role） |
|---|---|---|---|---|
| `currencies` | 可追蹤的幣別 | 讀 | 讀 | 全部 |
| `profiles` | 每人一列；Google 第一次登入時由 trigger 建立 | 讀自己的；只能改 `email_enabled` | — | 全部 |
| `alert_rules` | 每人每幣別一條規則 + 狀態 | 讀 / 刪自己的；新增、修改只限 `target`、`buffer`、`enabled`（新增時另含 `currency`） | — | 全部 |
| `notifications` | 通知紀錄 | 讀自己的 | — | 全部 |

重點規則（都有測試，見 `tests/db/test_schema_rls.py`）：

- `state`、`state_changed_at`、`state_rate`、`state_target` 只有排程能寫。
- `buffer` 留空 = 自動，目標價的 0.5%；`effective_buffer` 由資料庫計算、跟著目標價變，排程用它。
  設定頁把 buffer 放在可展開的「進階設定」。
- **Email 關閉期間**：排程不判斷、不更新狀態（第 3 步實作與測試）。
  **重新開啟時**：這個人的所有規則重設為等待中，不補寄關閉期間的通知。
  單一規則「停用 → 啟用」也一樣。重設後如果現價已低於目標，下一次排程會寄**當下**的通知。
- **刪除帳號**：Edge Function 用 secret key 刪除 `auth.users` 的那一列 →
  `profiles` → `alert_rules`、`notifications` 全部連帶刪除。
- 改目標價不在資料庫處理，沿用 `rules.decide(prev_target=state_target)`。

## 一鍵退訂（HMAC，沒有 token 表）

- 簽章：`HMAC-SHA256(UNSUBSCRIBE_SECRET, "fx-alert:unsubscribe:v1:" + user_id)`，base64url（不補 `=`）。
  同一個人的簽章永遠相同，所以**舊信的連結一直有效**；換密鑰會讓所有舊連結失效。
  Python（`src/fx_alert/unsubscribe.py`）和 Edge Function（`supabase/functions/unsubscribe/handler.ts`）
  用同一組測試向量確保算法一致。
- 密鑰放兩個地方：GitHub Secrets（排程產生連結）、Edge Function secrets（驗證）。
- 信裡有兩種連結：
  - 內文：`https://xysmr.github.io/fx-alert/unsubscribe.html?u=<user_id>&s=<簽章>&p=<專案代號>`，
    打開只顯示按鈕，按下才 POST 給 Edge Function。
  - `List-Unsubscribe: <https://<專案>.supabase.co/functions/v1/unsubscribe?u=…&s=…>` 和
    `List-Unsubscribe-Post: List-Unsubscribe=One-Click`，信箱的「取消訂閱」按鈕會直接 POST。
- Edge Function **只接受 POST**（GET 回 405）：信箱的連結掃描器會自動打開連結，不能因此退訂。
  不檢查 JWT（`verify_jwt = false`），身分由簽章驗證。
- 限制：用個人 Gmail 經 SMTP 寄信時，Gmail 的 DKIM 簽章不一定涵蓋 List-Unsubscribe 標頭，
  所以信箱的「取消訂閱」按鈕不一定出現；內文連結一定可用。要完整支援需改用自有網域的寄信服務。
- 本機產生某人的連結（測試用；拒絕在 GitHub Actions 執行，避免連結進公開 log）：
  `python -m fx_alert unsubscribe-link --user-id <uuid>`（需要 `UNSUBSCRIBE_SECRET`、`SUPABASE_URL`）

## 開發步驟（每步一個分支、一個 PR）

| # | 分支 | 內容 |
|---|---|---|
| 1 | `feat/db-schema` | 資料表、欄位權限、RLS、trigger、CI 的本機 Supabase 測試 |
| 2 | `feat/rule-store` | 狀態存取抽成介面：`FileStore`（現在）、`SupabaseStore`（新） |
| 3 | `feat/multi-user-check` | `--mode single/multi`；規則從 view `active_rules` 讀（排除 Email 關閉的人與停用的規則）；每日寄信上限 `email.daily_limit`；多人版 `--dry-run` 只讀；錯誤訊息遮蔽 email；手動的 `staging-check` workflow |
| 4 | `feat/unsubscribe` | HMAC 簽章、Edge Function（Deno 測試）、確認頁、退訂 header；測試舊信連結仍有效、竄改的簽章被拒 |
| 5 | `feat/frontend-auth` | Google 登入、我的提醒、我的通知紀錄、Email 開關；`AUTH_ENABLED` 開關（見下方） |
| 6 | `feat/delete-account` | 刪除帳號 Edge Function（Auth 驗證 token、最近 10 分鐘內登入、確認文字）＋前端；排程與刪除同時發生時只略過該使用者；已刪除帳號點舊退訂連結顯示「已經不會再收到通知」；staging-check 的 `send` 選項 |
| 7 | `feat/migrate-me` | 搬家工具 `migrate-me` + `import_rules_for_user`（同一個交易；`--replace` 連通知紀錄一起覆蓋）；在 staging 演練 |
| 8 | `chore/cutover` | 依下方「第 8 步：切換流程」執行：排程改 `--mode multi`、`data.json` 拿掉目標價與通知紀錄、`AUTH_ENABLED=true`、保活、隱私說明 |

### 第 5 步：前端登入（`site/account.js`）

**登入功能開關**（GitHub → Settings → Secrets and variables → Actions → **Variables**）：

| Variable | 說明 |
|---|---|
| `AUTH_ENABLED` | `true` 才開啟；其他值或沒設定 = 關閉 |
| `SUPABASE_URL` | `https://<專案代號>.supabase.co` |
| `SUPABASE_PUBLISHABLE_KEY` | `sb_publishable_…`（本來就公開；`sb_secret_…` 會被拒絕） |

部署時 `python -m fx_alert site-config` 產生 `config.js`：

- **關閉**：`config.js` 只有 `{"authEnabled": false}`，不含任何 Supabase 網址或金鑰；
  頁面和單人版完全一樣（目標價、通知紀錄來自 `data.json`），不出現登入按鈕，不載入 supabase-js。
- **開啟**：沒登入只顯示公開行情；登入後從 Supabase 讀自己的目標價、狀態、通知紀錄（RLS 保證只拿得到自己的）。

正式網站在第 8 步切換前維持關閉；第 5 步的登入功能在本機驗證。

設定頁：
- 卡片用語和介面改版一致（「你的目標 0.205」）。
- 進階設定的回升門檻用白話說明：「匯率回升到 0.2061 以上，才會再提醒你」（銀行報價的位數，
  比「目標 + 門檻」大的最小報價，和排程的「匯率 > 目標 + 門檻」一致）。
- 目標價和現價差超過 30% 時，存檔前再確認一次（防止打錯小數點）。
- 新增提醒的幣別選單只列出還沒設定過的幣別。

### 第 6 步：刪除帳號（`supabase/functions/delete-account`）

- 只接受 POST；必須帶使用者自己的登入 token，由 Supabase Auth 驗證是誰；**只刪除 token 本人**，不接受請求裡的 user_id。
- **最近 10 分鐘內登入過**才可以刪（看 token 的 `amr` 登入時間）；否則回 `reauth_required`，
  前端提示「為了安全，請重新登入後再刪除」並顯示「重新登入」。
- 確認文字「刪除我的帳號」：前後端用同一套規則（NFC 正規化、去掉前後空白），
  測試案例放在 `confirm_cases.json`，前端和 Edge Function 的測試都讀這個檔案。
- 刪除 `auth.users` 後，`profiles`、`alert_rules`、`notifications` 由資料庫連帶刪除。

和刪除有關的邊界情況：

| 情況 | 行為 |
|---|---|
| 排程讀出規則後、寫入前，使用者刪除帳號 | `record_alert` 回傳 null（外鍵錯誤也會被接住並回復），排程只記一行 log、略過這個人；其他人照常，整次排程不算失敗。信可能已經寄出 |
| 已刪除帳號的人點舊信的退訂連結 | Edge Function 回 200 `no_account`，確認頁顯示「你已經不會再收到通知」 |
| 另一台裝置還登入著 | 讀不到自己的 profile、或寫入時外鍵錯誤 → 顯示「帳號已不存在，請重新登入」並自動登出 |

### 第 7 步：搬家工具（`python -m fx_alert migrate-me`）

```powershell
# 只在自己的電腦執行（會印出 email，在 GitHub Actions 裡會拒絕）
# secret key 只從環境變數讀，不讀也不寫任何 .env 檔
$env:SUPABASE_URL = "https://<專案代號>.supabase.co"
$env:SUPABASE_SECRET_KEY = "sb_secret_..."
python -m fx_alert migrate-me --email <你的 Gmail> --data-dir <state 分支的資料夾> --dry-run
python -m fx_alert migrate-me --email <你的 Gmail> --data-dir <state 分支的資料夾>
```

- 目標價、buffer 照 `config.yaml` 原值搬（不改成自動 0.5%）；狀態、時間、觸發時的目標價來自 `state.json`；通知紀錄來自 `alerts.csv`。
- 寫入在同一個交易裡；帳號已經有提醒就拒絕，`--replace` 會在同一個交易裡先刪掉他的通知紀錄和規則再寫入（執行幾次結果都一樣）。
- 寫完重新讀回來比對：數字用 Decimal（0.0250 = 0.025），時間轉成時間點（+08:00 和 UTC 是同一刻就相同）。

### 第 8 步：切換流程（草稿，第 8 步開始前再確認一次）

**切換前**

- [ ] ⚠ **提醒使用者：決定韓元目標價**，更新 `config.yaml` 並合併（搬家會用這個值）
- [ ] 正式專案設定完成：migrations、兩個 Edge Function（Verify JWT 關閉）、`UNSUBSCRIBE_SECRET`、Google 登入、Site URL / Redirect URLs
- [ ] GitHub Secrets：`SUPABASE_URL`、`SUPABASE_SECRET_KEY`、`UNSUBSCRIBE_SECRET`（正式專案的值）
- [ ] GitHub Variables：`SUPABASE_URL`、`SUPABASE_PUBLISHABLE_KEY`（正式專案）；`AUTH_ENABLED` 先維持未設定

**切換**

1. Actions → check-rates → **Disable workflow**（暫停單人版排程，state 分支從這一刻起不再變動）
2. 本機用 Google 登入一次正式專案（建立 profile）
3. 取出 state 分支：`git fetch origin state` → `git worktree add ../fx-state origin/state`
4. `migrate-me --dry-run` 確認內容 → 正式搬家 → 比對必須完全一致
5. 合併 `chore/cutover` PR（check-rates 改 `--mode multi`；`data.json` 不再含目標價與通知紀錄）
6. Variables 設 `AUTH_ENABLED=true` → 重新部署 Pages → 確認沒登入看不到目標價、登入後看得到
7. Enable check-rates → 手動 Run 一次 → 確認 log 只有規則編號、狀態正確

**切換後**

- [ ] **重新產生正式專案的 secret key**（Supabase → API Keys → 新增一把、更新 GitHub Secrets `SUPABASE_SECRET_KEY`、確認排程正常後刪除舊的）。搬家時這把 key 曾在本機環境變數出現過。
- [ ] state 分支的 `state.json`、`alerts.csv` **保留不刪**（退回方案要用）
- [ ] `MAIL_TO` 穩定運作一週後再刪（退回方案要用）

**退回方案（切回單人模式）**

1. Actions → check-rates → Disable workflow
2. Variables：`AUTH_ENABLED` 刪除或設為 `false`（頁面立刻回到單人版顯示，不出現登入）
3. 在 GitHub 對 `chore/cutover` 的合併按 **Revert**，開 PR 合併（check-rates 回到單人模式；`config.yaml` 的目標價仍在）
4. 確認 `MAIL_TO` 還在（不在就加回）
5. 狀態：state 分支停在切換那一刻。如果多人版期間有到價通知，對照 Supabase 的狀態手動更新 `state.json`；不更新的話，最壞情況是同一次到價再寄一封
6. Enable check-rates → 手動 Run 一次（建議先勾 dry_run）→ 確認 log 與頁面正常

### 待辦（第 8 步之後）

- [ ] migration 和 Edge Function 改由 GitHub Actions 部署（`supabase db push`、`supabase functions deploy`），
      並用 **GitHub Environments**：`staging` 自動部署、`production` 需要使用者核准
- [ ] Actions 升級（Node 20 淘汰、ubuntu-latest 換版的警告）
