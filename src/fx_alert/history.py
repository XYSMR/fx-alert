"""歷史資料：匯率走勢、到價通知紀錄、最後一次檢查結果。

全部放在資料資料夾（GitHub Actions 上就是 state 分支）：

    data/
    ├── state.json            （state_store.py 管）
    ├── history/2026-11.csv   每月一檔；同一份牌告（quoted_at）只記一次
    ├── alerts.csv            寄信成功才記一筆（不含收件人）
    └── last_check.json       最後一次檢查的時間、是否成功、錯誤數量（不含錯誤訊息）

這些檔案在 public repo 公開，所以「絕對不寫入」email、錯誤訊息全文或任何環境變數。
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from fx_alert.config import TAIPEI

HISTORY_DIR = "history"
ALERTS_FILE = "alerts.csv"
LAST_CHECK_FILE = "last_check.json"

HISTORY_FIELDS = ["quoted_at", "currency", "cash_sell"]
ALERT_FIELDS = ["sent_at", "currency", "rate", "target"]


@dataclass(frozen=True)
class Quote:
    quoted_at: datetime
    currency: str
    cash_sell: Decimal


@dataclass(frozen=True)
class Alert:
    sent_at: datetime
    currency: str
    rate: Decimal
    target: Decimal


def _aware(dt: datetime) -> datetime:
    """兆豐給的牌告時間沒有時區（台灣時間），補上 +08:00。"""
    return dt.replace(tzinfo=TAIPEI) if dt.tzinfo is None else dt.astimezone(TAIPEI)


def _iso(dt: datetime) -> str:
    return _aware(dt).isoformat(timespec="seconds")


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _append_csv(path: Path, fields: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    new_file = not path.exists()
    with open(path, "a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, lineterminator="\n")
        if new_file:
            w.writeheader()
        w.writerows(rows)


# ---------- 匯率走勢 ----------


def append_quotes(data_dir: Path, quoted_at: datetime | None, rates: dict[str, Decimal]) -> bool:
    """記錄一份牌告。同一個 quoted_at 已經記過就不重複記。回傳是否有新增。"""
    if quoted_at is None or not rates:
        return False  # 沒有牌告時間就無法去重，乾脆不記
    stamp = _iso(quoted_at)
    path = data_dir / HISTORY_DIR / f"{_aware(quoted_at):%Y-%m}.csv"
    if any(row["quoted_at"] == stamp for row in _read_csv(path)):
        return False
    rows = [
        {"quoted_at": stamp, "currency": code, "cash_sell": str(rate)}
        for code, rate in sorted(rates.items())
    ]
    _append_csv(path, HISTORY_FIELDS, rows)
    return True


def read_quotes(data_dir: Path, since: datetime) -> list[Quote]:
    """讀出 since 之後（含）的所有牌告，依時間排序。"""
    since = _aware(since)
    folder = data_dir / HISTORY_DIR
    if not folder.exists():
        return []
    quotes = []
    for path in sorted(folder.glob("*.csv")):
        if path.stem < f"{since:%Y-%m}":
            continue  # 整個月份都太舊，不用讀
        for row in _read_csv(path):
            at = datetime.fromisoformat(row["quoted_at"])
            if at >= since:
                quotes.append(Quote(at, row["currency"], Decimal(row["cash_sell"])))
    return sorted(quotes, key=lambda q: (q.quoted_at, q.currency))


def first_quote_at(data_dir: Path) -> datetime | None:
    """最早的一筆牌告時間（只讀最舊的月份檔）；沒有資料回傳 None。"""
    folder = data_dir / HISTORY_DIR
    files = sorted(folder.glob("*.csv")) if folder.exists() else []
    for path in files:
        times = [datetime.fromisoformat(row["quoted_at"]) for row in _read_csv(path)]
        if times:
            return min(_aware(t) for t in times)
    return None


# ---------- 到價通知紀錄 ----------


def append_alert(
    data_dir: Path, sent_at: datetime, currency: str, rate: Decimal, target: Decimal
) -> None:
    row = {"sent_at": _iso(sent_at), "currency": currency, "rate": str(rate), "target": str(target)}
    _append_csv(data_dir / ALERTS_FILE, ALERT_FIELDS, [row])


def read_alerts(data_dir: Path) -> list[Alert]:
    """所有通知紀錄，最新的在前面。"""
    alerts = [
        Alert(
            datetime.fromisoformat(r["sent_at"]),
            r["currency"],
            Decimal(r["rate"]),
            Decimal(r["target"]),
        )
        for r in _read_csv(data_dir / ALERTS_FILE)
    ]
    return sorted(alerts, key=lambda a: a.sent_at, reverse=True)


# ---------- 最後一次檢查 ----------


def write_last_check(data_dir: Path, at: datetime, ok: bool, errors: int) -> None:
    """記錄這次檢查的結果。只記錯誤「數量」，不記訊息（訊息可能含 email 等資訊）。"""
    data_dir.mkdir(parents=True, exist_ok=True)
    payload = {"at": _iso(at), "ok": ok, "errors": errors}
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    with open(data_dir / LAST_CHECK_FILE, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def read_last_check(data_dir: Path) -> dict | None:
    path = data_dir / LAST_CHECK_FILE
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))
