"""歷史資料：匯率走勢、到價通知紀錄、最後一次檢查結果。

全部放在資料資料夾（GitHub Actions 上就是 state 分支）：

    data/
    ├── state.json            （state_store.py 管）
    ├── history/2026-11-16.csv 每天一檔（台灣日期）；同一份牌告（quoted_at）只記一次
    │                          舊格式 history/2026-11.csv（每月一檔）照樣讀得到，
    │                          convert_monthly_history() 會把它拆成每天一檔
    ├── alerts.csv            寄信成功才記一筆（不含收件人）
    └── last_check.json       最後一次檢查的時間、是否成功、錯誤數量（不含錯誤訊息）
                              多人版另外寫到 Supabase 的 check_status；
                              state 分支只在資料有變時 commit

為什麼一天一檔：18 種幣別 × 每 30 分鐘，月份檔一個月會有好幾萬列，每次 commit 都改同一個大檔；
每天一檔，commit 只動當天的小檔，build-site 也只讀最近 30 天的檔案。

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


def _is_monthly(path: Path) -> bool:
    """history/2026-11.csv（舊格式，每月一檔）；每天一檔是 history/2026-11-16.csv。"""
    return len(path.stem) == 7


def _day_path(data_dir: Path, at: datetime) -> Path:
    return data_dir / HISTORY_DIR / f"{_aware(at):%Y-%m-%d}.csv"


def _write_csv(path: Path, fields: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    tmp.replace(path)


def append_quotes(data_dir: Path, quoted_at: datetime | None, rates: dict[str, Decimal]) -> bool:
    """記錄一份牌告到當天的檔案。同一個 quoted_at 已經記過就不重複記。回傳是否有新增。"""
    if quoted_at is None or not rates:
        return False  # 沒有牌告時間就無法去重，乾脆不記
    stamp = _iso(quoted_at)
    path = _day_path(data_dir, quoted_at)
    if any(row["quoted_at"] == stamp for row in _read_csv(path)):
        return False
    rows = [
        {"quoted_at": stamp, "currency": code, "cash_sell": str(rate)}
        for code, rate in sorted(rates.items())
    ]
    _append_csv(path, HISTORY_FIELDS, rows)
    return True


def convert_monthly_history(data_dir: Path) -> list[str]:
    """把舊格式（每月一檔）拆成每天一檔，然後刪掉月份檔。回傳轉換了哪些月份。

    和已經存在的每日檔合併：同一個（quoted_at, 幣別）只留一筆，依時間排序。
    執行幾次結果都一樣；資料筆數不會變（拆完會核對，對不上就不刪月份檔）。
    """
    folder = data_dir / HISTORY_DIR
    if not folder.exists():
        return []
    converted = []
    for month in sorted(p for p in folder.glob("*.csv") if _is_monthly(p)):
        rows = _read_csv(month)
        by_day: dict[str, list[dict[str, str]]] = {}
        for row in rows:
            day = f"{_aware(datetime.fromisoformat(row['quoted_at'])):%Y-%m-%d}"
            by_day.setdefault(day, []).append(row)
        expected = set()
        for day, new_rows in sorted(by_day.items()):
            path = folder / f"{day}.csv"
            merged = {(r["quoted_at"], r["currency"]): r for r in _read_csv(path)}
            for r in new_rows:
                key = (r["quoted_at"], r["currency"])
                merged.setdefault(key, {k: r[k] for k in HISTORY_FIELDS})
                expected.add(key)
            ordered = sorted(
                merged.values(),
                key=lambda r: (datetime.fromisoformat(r["quoted_at"]), r["currency"]),
            )
            _write_csv(path, HISTORY_FIELDS, ordered)
        written = {
            (r["quoted_at"], r["currency"])
            for day in by_day
            for r in _read_csv(folder / f"{day}.csv")
        }
        if not expected <= written:
            raise RuntimeError(f"拆分 {month.name} 後資料對不上，保留原檔")
        month.unlink()
        converted.append(month.stem)
    return converted


def _history_files(data_dir: Path) -> list[Path]:
    """依日期排序（月份檔 2026-11.csv 當作 2026-11-00，排在 11 月的每日檔前面）。"""
    folder = data_dir / HISTORY_DIR
    if not folder.exists():
        return []
    return sorted(folder.glob("*.csv"), key=lambda p: p.stem + "-00" if _is_monthly(p) else p.stem)


def read_quotes(data_dir: Path, since: datetime) -> list[Quote]:
    """讀出 since 之後（含）的所有牌告，依時間排序。每天一檔、每月一檔（舊格式）都讀得到。"""
    since = _aware(since)
    seen = set()
    quotes = []
    for path in _history_files(data_dir):
        oldest = f"{since:%Y-%m}" if _is_monthly(path) else f"{since:%Y-%m-%d}"
        if path.stem < oldest:
            continue  # 整個檔案都太舊，不用讀
        for row in _read_csv(path):
            at = datetime.fromisoformat(row["quoted_at"])
            key = (row["quoted_at"], row["currency"])
            if at >= since and key not in seen:
                seen.add(key)
                quotes.append(Quote(at, row["currency"], Decimal(row["cash_sell"])))
    return sorted(quotes, key=lambda q: (q.quoted_at, q.currency))


def first_quote_at(data_dir: Path) -> datetime | None:
    """最早的一筆牌告時間；沒有資料回傳 None。"""
    files = _history_files(data_dir)
    earliest = None
    for path in files:
        times = [_aware(datetime.fromisoformat(row["quoted_at"])) for row in _read_csv(path)]
        if times:
            first = min(times)
            earliest = first if earliest is None else min(earliest, first)
            if not _is_monthly(path):
                break  # 每天一檔：第一個有資料的檔案就是最早的
    return earliest


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
