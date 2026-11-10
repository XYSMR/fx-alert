# fx-alert 匯率到價提醒

追蹤兆豐銀行日圓（JPY）、韓元（KRW）的**現金賣出價**，跌到目標價時寄 Email 通知。
同一次到價只通知一次；匯率回升超過「目標＋buffer」後再跌回，才會再通知。
全部跑在 GitHub Actions 和 GitHub Pages 上，完全免費、不需要自己的伺服器。

**儀表板：<https://xysmr.github.io/fx-alert/>**

## 運作方式

```
check-rates（台灣平日 09:07–16:07，每 30 分鐘）
  ├─ check   驗證 config → 抓兆豐匯率 → 狀態機判斷 → Gmail 寄信 → 資料 commit 到 state 分支
  └─ deploy  呼叫 deploy-pages：產生 data.json → 部署到 GitHub Pages

deploy-pages（也會在 push 前端／設定／程式到 main 時執行）
  驗證 config → 從 state 分支取資料 → build-site → 防外洩檢查 → 部署

ci（push 到 main 或開 PR）
  validate-config ＋ 防外洩檢查、ruff、pytest（Python 3.11 / 3.13）、前端單元測試、
  資料庫 RLS 測試（在 CI 裡啟動本機 Supabase）
```

### 到價規則

| 狀態 | 條件 | 結果 |
|---|---|---|
| 等待中 | 現價 ≤ 目標 | 寄信 → 已到價 |
| 已到價 | 現價 > 目標＋buffer | 回到等待中（不寄信） |
| 已到價 | 目標價被修改，且現價 > 新目標 | 回到等待中（不寄信） |
| 已到價 | 其他 | 不重複寄 |

- 第一次執行（沒有狀態紀錄）若已低於目標，也會寄通知。
- 寄信**成功**才更新狀態；失敗時狀態不變，下次執行再試。

## 修改目標價

在儀表板按「在 GitHub 修改目標價」，或直接編輯 [`config.yaml`](config.yaml)：

```yaml
currencies:
  JPY:
    target: "0.205"   # 現金賣出價 ≤ 這個數字就通知
    buffer: "0.001"   # 通知後要回升到 > target + buffer 才會再通知
```

commit 後頁面約 1 分鐘更新；到價判斷會在下一次排程檢查時套用新目標。
格式錯誤（不支援的幣別、目標價不是正數、buffer 超過目標價 10%、欄位打錯字…）時，
CI 會變紅，排程不會檢查，頁面也不會部署。

## 第一次設定

1. **Secrets**（Settings → Secrets and variables → Actions）：

   | Secret | 內容 |
   |---|---|
   | `SMTP_USER` | 寄件 Gmail 帳號 |
   | `SMTP_PASSWORD` | Gmail 應用程式密碼（帳號需開啟兩步驟驗證） |
   | `MAIL_TO` | 收件人，多個用逗號分隔 |
   | `HEALTHCHECK_PING_URL` | （選用）healthchecks.io 的 ping 網址，見[排程監控](#排程沒有執行時怎麼排查) |

2. **Pages**（Settings → Pages）：Source 選 **GitHub Actions**。
3. 到 Actions 手動執行一次 `check-rates`。

## 資料存放（`state` 分支）

| 檔案 | 內容 | 何時寫入 |
|---|---|---|
| `state.json` | 每個幣別目前是等待中或已到價、觸發時的目標價 | 狀態轉換時 |
| `history/YYYY-MM.csv` | 匯率走勢（`quoted_at,currency,cash_sell`） | 兆豐換牌時（同一份牌告只記一次） |
| `alerts.csv` | 到價通知紀錄（`sent_at,currency,rate,target`） | 寄信成功時 |
| `last_check.json` | 最後一次檢查的時間、是否成功、錯誤**數量** | 每次檢查 |

repo 是 public，所以這些檔案和頁面上的 `data.json` **不包含任何 email、錯誤訊息全文或 Secrets**。
`build-site` 不讀取任何 SMTP 相關的環境變數，部署前和 CI 也會再掃描一次。

## 排程沒有執行時怎麼排查

GitHub Actions 的 `schedule` 是「盡力而為」：官方文件寫明負載高時（尤其整點、UTC 00:00–08:00）會**延遲**，
負載夠高時排隊中的排程會被**直接丟掉**，而且被丟掉的排程**不會出現在任何紀錄裡，也不會顯示失敗**。
新建立、活動少的 repo 特別容易遇到。所以要靠外部監控發現「沒有執行」。

### 監控：healthchecks.io（免費方案）

每次排程檢查成功後，`check-rates` 會 ping 一次 healthchecks.io；失敗時 ping `/fail`。
超過時間沒收到 ping，healthchecks.io 會寄信通知。

1. 到 <https://healthchecks.io> 註冊（免費方案可監控 20 個工作），新增一個 check：
   - **Schedule**：選 **Cron**，填兩行
     ```
     7,37 9-15 * * 1-5
     7 16 * * 1-5
     ```
   - **Time Zone**：`Asia/Taipei`
   - **Grace Time**：`30 minutes`（下一次排程也沒來，就是約 1 小時沒有 ping → 寄信）
   - **Integrations**：Email（預設就是註冊的信箱）
2. 複製 check 的 **ping URL**，放到 GitHub **Secrets** 的 `HEALTHCHECK_PING_URL`。
   ⚠ 這個網址等同密碼（知道的人可以假裝「一切正常」），**不要**寫進 repo、issue 或截圖。
3. 到 Actions 手動跑一次 `check-rates`，healthchecks.io 的 check 應該變成綠色 **up**。

只有**真的檢查了**才 ping 成功：如果排程延遲太久、在台灣時間 09:00–16:30 以外才執行，程式會略過檢查
（workflow 仍是綠燈，log 會出現「不在檢查時段內，略過檢查；不通知監控」），這時**不 ping**，監控才會發現該跑的時段沒有檢查。
`dry_run` 也不會 ping；沒設定 `HEALTHCHECK_PING_URL` 時只會略過，不會讓排程失敗。

### 收到 DOWN 通知時，照順序檢查

1. **workflow 是不是被停用了**：打開 Actions → `check-rates`，上方出現「This workflow was disabled」就按 **Enable workflow**。
   公開 repo **60 天沒有任何活動**時，GitHub 會自動停用排程。
2. **有沒有排程紀錄**：Actions → `check-rates`，在搜尋框輸入 `event:schedule`。
   - **有紀錄、但是紅的**：點進去看哪一步失敗。最前面的 **Show trigger** 會印出觸發方式、cron 和實際開始時間。
     常見原因：`Validate config.yaml` 設定檔格式錯誤；`Check rates` 兆豐 API 或 Supabase 暫時連不上（下一次通常就好了）；
     Secrets 被改錯（Supabase 回 401）。
   - **有紀錄、但開始時間晚很多**：GitHub 排程延遲，資料只是晚一點更新。
   - **完全沒有紀錄**：排程被 GitHub 丟掉了。先手動補跑（下一步），連續好幾次都這樣，再考慮改用外部觸發。
3. **時間有沒有算錯**：cron 用 **UTC**。`7,37 1-7 * * 1-5` ＝ 台灣平日 09:07–15:37，另一行 `7 8 * * 1-5` ＝ 16:07。
   程式在台灣時間 09:00–16:30 以外會自動略過（不算錯誤，也不會 commit）。
4. **排程只看 `main` 上的 workflow 檔**：在其他分支改 cron 不會生效。
5. **補救**：
   - Actions → `check-rates` → **Run workflow**（`force` 保持勾選）立刻補跑一次。
   - 如果懷疑 GitHub 沒有登記到排程（例如剛停用又啟用過），push 一個修改 `.github/workflows/check-rates.yml` 的 commit 到 `main`，
     讓 GitHub 重新讀取排程。
6. **確認恢復**：`state` 分支出現新的 `data: … 台灣時間` commit，healthchecks.io 的 check 回到 **up**。

## 本機開發

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1                 # macOS / Linux: source .venv/bin/activate
python -m pip install -e ".[dev]"

python -m pytest                           # Python 測試
python -m ruff check .                     # lint
node --test site/tests/app.test.js site/tests/account.test.js site/tests/unsubscribe.test.js  # 前端測試
python -m fx_alert site-config            # 產生 site/config.js（預設登入功能關閉）
# 資料庫 RLS 測試（tests/db）需要 Supabase CLI + Docker，沒有的話會自動略過、交給 CI 跑

python -m fx_alert validate-config         # 驗證 config.yaml
python -m fx_alert check --dry-run --force # 實際抓匯率，但不寄信；資料寫到 data/
# 多人版（Supabase）：只讀不寫，需要 SUPABASE_URL、SUPABASE_SECRET_KEY 環境變數
# python -m fx_alert check --mode multi --dry-run --force
python -m fx_alert build-site              # 產生 site/data.json
python -m http.server 8000 -d site         # 開 http://localhost:8000/
```

前端也可以用範例資料預覽（範例中的走勢是假資料，用 `python scripts/make_samples.py` 重新產生）：

| 網址參數 | 情境 |
|---|---|
| `?data=samples/waiting.json` | 兩個幣別都在等待中 |
| `?data=samples/triggered.json` | 日圓已低於目標、已寄通知 |
| `?data=samples/rebound.json` | 日圓已通知過，但回升到目標和回升門檻之間 |
| `?data=samples/empty.json` | 還沒有任何資料 |
| `?data=samples/error.json` | 最後一次檢查失敗 |
| 加上 `&now=2026-11-14T12:00` | 把「現在」設成指定的台灣時間（例如週末，看「非檢查時段」的狀態列） |

> Windows 若出現「應用程式控制原則已封鎖此檔案」，把 `pytest`、`ruff` 改成 `python -m pytest`、`python -m ruff`。

## 專案結構

```
src/fx_alert/
  fetcher.py      下載兆豐匯率 JSON
  parser.py       解析成 Decimal（缺值 = None）
  rules.py        到價狀態機（純函式）
  state_store.py  讀寫 state.json
  store.py        規則與狀態的存放處：FileStore（單人版）／SupabaseStore（多人版）
  unsubscribe.py  一鍵退訂的 HMAC 簽章與連結
  migrate.py      單人版 → 多人版的搬家工具（migrate-me）
  history.py      走勢、通知紀錄、最後檢查
  notifier.py     Gmail SMTP / dry-run
  config.py       讀取並驗證 config.yaml、檢查時段
  build_site.py   產生前端用的 data.json（含距離目標 %、30 天最高最低）
  main.py         CLI：check / validate-config / build-site
site/
  index.html  style.css  app.js     儀表板（純 HTML/JS，無建置步驟）
  account.js                        多人版：Google 登入、我的提醒（AUTH_ENABLED 開啟時才啟用）
  unsubscribe.html  unsubscribe.js  退訂確認頁
  vendor/chart.umd.min.js           Chart.js 4.6.1（MIT）
  samples/                          本機預覽用的範例資料（不部署；scripts/make_samples.py 產生）
  tests/app.test.js                 前端單元測試（不部署）
.github/workflows/
  ci.yml  check-rates.yml  deploy-pages.yml
  staging-check.yml                 手動：用 staging Supabase 跑多人版（預設只讀；勾 send 會真的寄信）
supabase/
  config.toml                       本機 Supabase 設定（CI 用）
  migrations/                       多人版資料表、權限、RLS
  functions/unsubscribe/            一鍵退訂 Edge Function（Deno）
  functions/delete-account/         刪除帳號 Edge Function（Deno）
tests/db/                           RLS 測試（需要 SUPABASE_DB_URL）
docs/multi-user.md                  多人版設計與開發步驟
```

## 資料來源

[兆豐銀行牌告匯率](https://www.megabank.com.tw/personal-banking/foreign-exchange/exchange-rate/exchange-rate)。
原本規劃使用臺灣銀行，但其牌告匯率網站有機器人驗證，程式無法合規取得，因此改用兆豐銀行
（四家候選銀行中唯一提供韓元現鈔報價者）。頁面上的匯率僅供參考，實際以銀行公告為準。
