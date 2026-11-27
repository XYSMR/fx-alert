"""每日收盤價：判斷「目前價格在近 30 天是第幾低」用（出國換匯計畫）。

收盤價 = 兆豐「營業日最後一次匯率（約 16:00）」的現金賣出價。來源分兩段：

  * 2026-11-16（含）以前：兆豐網站「歷史匯率」下載的官方資料，存在 repo 的
    seed/mega_daily_close.csv（一次性回補，見 seed/README.md）
  * 之後：我們自己記錄的牌告（state 分支 history/）。兆豐每個營業日 16:04 左右會掛一次牌告，
    剛好是 16:05 那次檢查抓到的；取「台灣時間 16:00–16:10 之間最後一份」當收盤價。
    和兆豐官方收盤比對（11/09–11/16 的日圓、韓元）：12 天裡 10 天一致，2 天差 1 個報價單位。

營業日：有收盤價的日子就是兆豐的營業日（週末、國定假日兆豐不會在 16:00 掛新牌告）。
這只能判斷「過去」；未來的營業日（例如出發前最後一個營業日）另外處理（見換匯計畫的規則）。

    python -m fx_alert.daily compare [--data-dir data]   # 我們的收盤 vs 兆豐官方（重疊的日子）
"""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

from fx_alert.config import TAIPEI
from fx_alert.history import Quote, read_quotes
from fx_alert.paths import repo_file

SEED_PATH = repo_file("seed/mega_daily_close.csv")
CLOSE_FROM = time(16, 0)
CLOSE_UNTIL = time(16, 10, 59)
WINDOW_DAYS = 30  # 「近 30 天」= 日曆天
MIN_CLOSES = 15  # 少於 15 個營業日就不判斷「相對便宜」（百分位太不穩定）

Closes = dict[str, dict[date, Decimal]]


def read_seed(path: Path = SEED_PATH) -> Closes:
    """seed/mega_daily_close.csv（date + 每個幣別一欄）→ {幣別: {日期: 收盤價}}。"""
    out: Closes = {}
    # 檔案不在就直接失敗：以前是回傳空的，部署時找不到檔案也不會發現（近 30 天只剩自己記錄的幾天）
    with open(path, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            day = date.fromisoformat(row.pop("date"))
            for code, value in row.items():
                if value:
                    out.setdefault(code, {})[day] = Decimal(value)
    return out


def derive_closes(quotes: list[Quote]) -> Closes:
    """我們記錄的牌告 → 每天的收盤價（台灣時間 16:00–16:10 之間最後一份）。"""
    out: Closes = {}
    for q in sorted(quotes, key=lambda q: q.quoted_at):
        local = q.quoted_at.astimezone(TAIPEI)
        if CLOSE_FROM <= local.time() <= CLOSE_UNTIL:
            out.setdefault(q.currency, {})[local.date()] = q.cash_sell
    return out


def daily_closes(data_dir: Path, *, seed_path: Path = SEED_PATH, until: datetime) -> Closes:
    """兆豐官方（seed）＋ seed 之後我們自己算的收盤價。同一天兩邊都有時用兆豐官方的。"""
    seed = read_seed(seed_path)
    seed_end = max((d for days in seed.values() for d in days), default=None)
    since = (
        datetime.combine(seed_end + timedelta(days=1), time(0), TAIPEI)
        if seed_end
        else until - timedelta(days=WINDOW_DAYS + 1)
    )
    derived = derive_closes([q for q in read_quotes(data_dir, since) if q.quoted_at <= until])
    out: Closes = {code: dict(days) for code, days in seed.items()}
    for code, days in derived.items():
        merged = out.setdefault(code, {})
        for d, v in days.items():
            merged.setdefault(d, v)
    return out


@dataclass(frozen=True)
class Recent:
    """近 30 天收盤價的統計。lower：收盤價比 price 低的天數（嚴格小於）。"""

    days: int
    lower: int
    since: date
    enough: bool

    def as_dict(self) -> dict:
        return {
            "days": self.days,
            "lower": self.lower,
            "since": self.since.isoformat(),
            "enough": self.enough,
        }


def recent_stats(
    closes: dict[date, Decimal], price: Decimal, today: date, window_days: int = WINDOW_DAYS
) -> Recent:
    """近 window_days 個日曆天（含 today）的收盤價裡，有幾天比 price 低。"""
    start = today - timedelta(days=window_days - 1)
    window = [v for d, v in closes.items() if start <= d <= today]
    return Recent(
        days=len(window),
        lower=sum(1 for v in window if v < price),
        since=start,
        enough=len(window) >= MIN_CLOSES,
    )


def business_days(closes: Closes) -> set[date]:
    """有收盤價的日子 = 兆豐的營業日（過去）。用美元判斷（每個營業日都有）。"""
    return set(closes.get("USD", {}))


# ---------- 比對工具 ----------


def compare(data_dir: Path, seed_path: Path = SEED_PATH) -> list[tuple[str, date, str, str]]:
    """我們自己算的收盤 vs 兆豐官方（兩邊都有的日子），只列不一致的：(幣別, 日期, 兆豐, 我們)。"""
    seed = read_seed(seed_path)
    first = min((d for days in seed.values() for d in days), default=None)
    if first is None:
        return []
    ours = derive_closes(read_quotes(data_dir, datetime.combine(first, time(0), TAIPEI)))
    diffs = []
    for code, days in ours.items():
        for d, v in sorted(days.items()):
            official = seed.get(code, {}).get(d)
            if official is not None and official != v:
                diffs.append((code, d, str(official), str(v)))
    return diffs


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m fx_alert.daily")
    p.add_argument("command", choices=["compare"])
    p.add_argument("--data-dir", type=Path, default=Path("data"))
    args = p.parse_args(argv)
    seed = read_seed()
    first = min((d for days in seed.values() for d in days), default=None)
    ours = derive_closes(
        read_quotes(args.data_dir, datetime.combine(first or date.today(), time(0), TAIPEI))
    )
    overlap = sum(1 for code, days in ours.items() for d in days if d in seed.get(code, {}))
    diffs = compare(args.data_dir)
    print(f"重疊的日子（幣別 × 日期）：{overlap} 筆，不一致 {len(diffs)} 筆")
    for code, d, official, mine in diffs:
        print(f"  {code} {d}：兆豐 {official}，我們 {mine}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
