# fx-alert 匯率到價提醒

追蹤兆豐銀行日圓（JPY）、韓元（KRW）的**現金賣出價**，跌到目標價時寄 Email 通知。
同一次到價只通知一次；匯率回升超過「目標＋buffer」後再跌回，才會再通知。
完全免費，跑在 GitHub Actions 上。

## 運作方式

- `check-rates` workflow：台灣平日 09:07–16:07 每 30 分鐘執行一次
- 抓兆豐銀行牌告匯率 API → 狀態機判斷 → Gmail SMTP 寄信
- 每個幣別的狀態（待命／已通知）存在 `state` 分支的 `state.json`，有變化才 commit
- `ci` workflow：push 到 main 或開 PR 時跑 ruff 和 pytest

## 設定

`config.yaml`：幣別、目標價、buffer、檢查時段（不含任何機密）。改完 push 到 main 就生效。

GitHub repo → Settings → Secrets and variables → Actions，新增：

| Secret | 內容 |
|---|---|
| `SMTP_USER` | 寄件 Gmail 帳號 |
| `SMTP_PASSWORD` | Gmail 應用程式密碼（需開啟兩步驟驗證） |
| `MAIL_TO` | 收件人，多個用逗號分隔 |

## 本機開發

```bash
python -m venv .venv
.venv\Scripts\Activate.ps1          # macOS / Linux: source .venv/bin/activate
python -m pip install -e ".[dev]"

python -m pytest                     # 測試
python -m ruff check .               # lint
python -m fx_alert check --dry-run --force   # 實際抓匯率，但不寄信
```

## 專案結構

```
src/fx_alert/
  fetcher.py      下載兆豐匯率 JSON
  parser.py       解析成 Decimal（缺值 = None）
  rules.py        到價狀態機（純函式）
  state_store.py  讀寫 state.json
  notifier.py     Gmail SMTP / dry-run
  config.py       讀取 config.yaml、檢查時段
  main.py         串起所有步驟的 CLI
```
