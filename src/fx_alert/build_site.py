"""產生前端用的 data.json。

    python -m fx_alert build-site [--config config.yaml] [--data-dir data] [--out site/data.json]

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
from fx_alert.history import read_alerts, read_last_check, read_quotes
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


def distance_pct(rate: Decimal, target: Decimal) -> float:
    """(現價 − 目標) ÷ 目標 × 100，四捨五入到小數 2 位。正數 = 還要跌多少 %。"""
    pct = (rate - target) / target * 100
    return float(pct.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _repo() -> str:
    # GitHub Actions 會提供 GITHUB_REPOSITORY（例如 XYSMR/fx-alert）；只取這一個公開的值
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    return repo if repo.count("/") == 1 else DEFAULT_REPO


def build_data(
    cfg: AppConfig, data_dir: Path, *, now: datetime, repo: str | None = None
) -> dict[str, Any]:
    repo = repo or _repo()
    states = load_states(data_dir / "state.json")
    quotes = read_quotes(data_dir, since=now - timedelta(days=HISTORY_DAYS))

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

    return {
        "schema": SCHEMA_VERSION,
        "generated_at": _iso(now),
        "last_check": read_last_check(data_dir),
        "quoted_at": _iso(quotes[-1].quoted_at) if quotes else None,
        "source": {"bank": "兆豐銀行", "rate_type": BANK_LABEL.split()[-1], "url": RATE_PAGE},
        "repo_url": f"https://github.com/{repo}",
        "edit_config_url": f"https://github.com/{repo}/edit/main/config.yaml",
        "currencies": currencies,
        "alerts": alerts,
    }


def write_data_json(data: dict[str, Any], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
