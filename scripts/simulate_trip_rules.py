"""用兆豐一年份的每日收盤價模擬換匯計畫的提醒次數（每個營業日 16:00 檢查一次）。

    python scripts/simulate_trip_rules.py [--lead 60] [--since 2026-05-01] [--codes JPY,KRW,USD]

假設：每個營業日都可能是出發日；旅程在出發前 --lead 天建立；都沒有記錄換匯（剩餘金額不變）。
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import date, timedelta
from decimal import Decimal

from fx_alert.bizdays import load_calendar
from fx_alert.daily import read_seed, recent_stats
from fx_alert.trip_rules import KINDS, Sent, Trip, decide


def simulate(closes, cal, departure: date, lead: int):
    trip = Trip(departure, departure - timedelta(days=lead), Decimal(1))
    sent: list[Sent] = []
    for day in cal.between(trip.created_on, departure - timedelta(days=1)):
        price = closes.get(day)
        recent = recent_stats(closes, price, day) if price is not None else None
        d = decide(trip, day, recent, sent, cal)
        if d:
            sent.append(Sent(d.kind, day))
    return sent


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lead", type=int, default=60)
    ap.add_argument("--since", type=date.fromisoformat, default=date(2026, 3, 1))
    ap.add_argument("--codes", default="JPY,KRW,USD,EUR")
    args = ap.parse_args(argv)
    seed, cal = read_seed(), load_calendar()
    end = max(seed["USD"])
    for code in args.codes.split(","):
        closes = seed[code]
        deps = [d for d in cal.between(args.since, end)]
        per, kinds = [], Counter()
        for dep in deps:
            # 出發日之後的收盤價還不存在 → 只用 dep 之前的資料
            s = simulate({d: v for d, v in closes.items() if d < dep}, cal, dep, args.lead)
            per.append(len(s))
            kinds.update(x.kind for x in s)
        n = len(per)
        print(
            f"{code}：{n} 趟（出發日 {deps[0]}～{deps[-1]}，{args.lead} 天前建立）"
            f" 每趟平均 {sum(per) / n:.1f} 封、最少 {min(per)}、最多 {max(per)}"
        )
        print("  " + "、".join(f"{k} {kinds[k] / n:.2f}" for k in KINDS))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
