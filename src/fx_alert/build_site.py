"""產生前端用的 data.json。

    python -m fx_alert build-site [--config config.yaml] [--data-dir data] [--out site/data.json]
                                  [--public]

--public（多人版，部署時 AUTH_ENABLED=true 就會加）：只輸出公開行情。
拿掉每個幣別的目標價、buffer、距離目標、狀態，也不輸出通知紀錄和「編輯 config.yaml」連結。
多人版的目標價和通知紀錄只存在 Supabase，登入後才看得到自己的。

只讀兩個地方：config.yaml（目標價等設定）和資料資料夾（state 分支的內容）。
完全不讀環境變數，所以 Secrets 不可能出現在輸出裡；測試另外再檢查一次。

計算都在這裡做完（距離目標幾 %、狀態、30 天範圍），前端只負責顯示。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from fx_alert.config import CURRENCY_NAMES, TAIPEI, AppConfig
from fx_alert.history import first_quote_at, read_alerts, read_last_check, read_quotes
from fx_alert.notifier import BANK_LABEL, RATE_PAGE
from fx_alert.rules import AlertState
from fx_alert.state_store import load_states

SCHEMA_VERSION = 1
HISTORY_DAYS = 30
MAX_ALERTS = 20
DEFAULT_REPO = "XYSMR/fx-alert"

STATUS = {AlertState.ARMED: "waiting", AlertState.TRIGGERED: "triggered"}


def _iso(dt: datetime) -> str:
    return dt.astimezone(TAIPEI).isoformat(timespec="seconds")


# 主要排程是 Supabase pg_cron 在每個整點後第 5、35 分觸發（migration 的 cron "5,35 ..."；
# GitHub schedule 的 :07、:37 只是備援），前端用來顯示「下次更新：週一 09:05」。
# tests/test_build_site.py 會檢查和 migration 一致。
SCHEDULE_OFFSET_MINUTES = 5


def distance_pct(rate: Decimal, target: Decimal) -> float:
    """還要跌多少 %：(現價 − 目標) ÷ 現價 × 100。

    除以「現價」，意思是「從現在的價格還要再跌多少」。保留小數 3 位，
    讓前端能判斷「不到 0.1%」；顯示時再四捨五入到 1 位。負數 = 已低於目標。
    """
    pct = (rate - target) / rate * 100
    return float(pct.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP))


def _extreme(series: list, pick) -> dict[str, str] | None:
    """30 天內的最高或最低價；同樣的價格出現多次時取最早的那一次。"""
    if not series:
        return None
    best = pick(series, key=lambda q: q.cash_sell)
    first = next(q for q in series if q.cash_sell == best.cash_sell)
    return {"rate": str(first.cash_sell), "at": _iso(first.quoted_at)}


def _add_minutes(t, minutes: int):
    return (datetime.combine(datetime(2000, 1, 1), t) + timedelta(minutes=minutes)).time()


def _repo() -> str:
    # GitHub Actions 會提供 GITHUB_REPOSITORY（例如 XYSMR/fx-alert）；只取這一個公開的值
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    return repo if repo.count("/") == 1 else DEFAULT_REPO


# 多人版公開頁面不能出現的欄位（每個幣別）；前端 site/app.js 的 PERSONAL_FIELDS 也是這些
PERSONAL_FIELDS = (
    "target",
    "buffer",
    "rearm_above",
    "distance_pct",
    "status",
    "status_since",
)


def public_problems(data: dict[str, Any]) -> list[str]:
    """公開版 data.json 的檢查：回傳不該出現的東西（空的 = 沒問題）。"""
    problems = [
        f"{c.get('code')}.{k}"
        for c in data.get("currencies", [])
        for k in PERSONAL_FIELDS
        if k in c
    ]
    if data.get("alerts"):
        problems.append("alerts")
    if "edit_config_url" in data:
        problems.append("edit_config_url")
    return problems


def build_data(
    cfg: AppConfig,
    data_dir: Path,
    *,
    now: datetime,
    repo: str | None = None,
    public: bool = False,
) -> dict[str, Any]:
    repo = repo or _repo()
    states = load_states(data_dir / "state.json")
    quotes = read_quotes(data_dir, since=now - timedelta(days=HISTORY_DAYS))

    # 最早的一筆牌告：資料不滿 30 天時，前端顯示「資料從 11/09 開始累積」
    since = first_quote_at(data_dir)

    by_code: dict[str, list] = {}
    for q in quotes:
        by_code.setdefault(q.currency, []).append(q)

    currencies = []
    for rule in cfg.rules:
        series = by_code.get(rule.currency, [])
        latest = series[-1] if series else None
        entry = states.get(rule.currency)
        currencies.append(
            {
                "code": rule.currency,
                "name": CURRENCY_NAMES.get(rule.currency, rule.currency),
                "rate": str(latest.cash_sell) if latest else None,
                "rate_at": _iso(latest.quoted_at) if latest else None,
                "target": str(rule.target),
                "buffer": str(rule.buffer),
                "rearm_above": str(rule.rearm_above),
                "distance_pct": distance_pct(latest.cash_sell, rule.target) if latest else None,
                "status": STATUS[entry.state] if entry else "waiting",
                "status_since": entry.changed_at if entry else None,
                "high": _extreme(series, max),
                "low": _extreme(series, min),
                "history": [[_iso(q.quoted_at), str(q.cash_sell)] for q in series],
            }
        )

    alerts = [
        {
            "sent_at": _iso(a.sent_at),
            "currency": a.currency,
            "rate": str(a.rate),
            "target": str(a.target),
        }
        for a in read_alerts(data_dir)[:MAX_ALERTS]
    ]

    data = {
        "schema": SCHEMA_VERSION,
        "generated_at": _iso(now),
        "last_check": read_last_check(data_dir),
        "quoted_at": _iso(quotes[-1].quoted_at) if quotes else None,
        "history_days": HISTORY_DAYS,
        "history_since": _iso(since) if since else None,
        "source": {"bank": "兆豐銀行", "rate_type": BANK_LABEL.split()[-1], "url": RATE_PAGE},
        "repo_url": f"https://github.com/{repo}",
        "edit_config_url": f"https://github.com/{repo}/edit/main/config.yaml",
        "actions_url": f"https://github.com/{repo}/actions/workflows/check-rates.yml",
        # 前端用來判斷「資料是不是太久沒更新」：只有在檢查時段內才算過期
        "business_hours": {
            "weekdays_only": cfg.hours.weekdays_only,
            "start": f"{cfg.hours.start:%H:%M}",
            "end": f"{cfg.hours.end:%H:%M}",
            "first_check": f"{_add_minutes(cfg.hours.start, SCHEDULE_OFFSET_MINUTES):%H:%M}",
        },
        "currencies": currencies,
        "alerts": alerts,
    }
    if public:
        for c in currencies:
            for k in PERSONAL_FIELDS:
                del c[k]
        data["alerts"] = []
        del data["edit_config_url"]
    return data


def write_data_json(data: dict[str, Any], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
