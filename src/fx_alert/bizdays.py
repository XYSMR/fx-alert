"""銀行營業日（台灣）：換匯計畫用來決定「出發前最後一個營業日」這類日期。

規則：週一到週五是營業日，除了 seed/tw_bank_calendar.csv 列為 off 的日子；
列為 workday 的週末（補班日）也是營業日。

資料來源（見 seed/README.md）：
  * 國定假日、補假、補班：行政院人事行政總處「中華民國政府行政機關辦公日曆表」
    （data.gov.tw 資料集 14718，114～116 年版）。銀行照行政機關的放假日休息。
  * 颱風停班：臺北市宣布停止上班時，外匯市場休市、兆豐沒有 16:00 牌告。
    過去的日子從兆豐歷史匯率確認後補進來；未來的颱風沒辦法預先知道。

COVERED_FROM～COVERED_UNTIL 以外的日期只用「週一到週五」判斷（covers() 會回 False）。
2025-11-17～2026-11-16 已和兆豐實際有牌告的 243 天逐日比對過，完全一致（tests/test_bizdays.py）。
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

CALENDAR_PATH = Path(__file__).resolve().parents[2] / "seed" / "tw_bank_calendar.csv"
COVERED_FROM = date(2025, 11, 17)
COVERED_UNTIL = date(2027, 12, 31)


@dataclass(frozen=True)
class Calendar:
    off: frozenset[date] = field(default_factory=frozenset)
    workday: frozenset[date] = field(default_factory=frozenset)
    covered_from: date = COVERED_FROM
    covered_until: date = COVERED_UNTIL

    def covers(self, day: date) -> bool:
        return self.covered_from <= day <= self.covered_until

    def is_business_day(self, day: date) -> bool:
        if day in self.workday:
            return True
        return day.weekday() < 5 and day not in self.off

    def on_or_before(self, day: date) -> date:
        """day 當天或之前最近的營業日。"""
        while not self.is_business_day(day):
            day -= timedelta(days=1)
        return day

    def before(self, day: date, n: int = 1) -> date:
        """day 之前（不含 day）的第 n 個營業日。n=1：出發前最後一個營業日。"""
        for _ in range(n):
            day = self.on_or_before(day - timedelta(days=1))
        return day

    def between(self, start: date, end: date) -> list[date]:
        """start～end（含）之間的營業日。"""
        out, day = [], start
        while day <= end:
            if self.is_business_day(day):
                out.append(day)
            day += timedelta(days=1)
        return out


def load_calendar(path: Path = CALENDAR_PATH) -> Calendar:
    off, work = set(), set()
    with open(path, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            day = date.fromisoformat(row["date"])
            if row["kind"] == "off":
                off.add(day)
            elif row["kind"] == "workday":
                work.add(day)
            else:
                raise ValueError(f"未知的 kind：{row['kind']}（{row['date']}）")
    return Calendar(off=frozenset(off), workday=frozenset(work))
