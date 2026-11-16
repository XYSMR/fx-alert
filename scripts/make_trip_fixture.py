"""產生 tests/fixtures/trip_schedules.json：Python（trip_rules.schedule）算出的提醒日期。

網頁（site/trips.js）用同一份日曆照同樣的規則算，site/tests/trips.test.js 逐筆比對，
tests/test_trip_rules.py 檢查這份檔案和目前的 Python 程式一致（改了規則要重新產生）。

    python scripts/make_trip_fixture.py
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

from fx_alert.bizdays import calendar_json, load_calendar
from fx_alert.trip_rules import schedule

OUT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "trip_schedules.json"
FIRST, LAST = date(2026, 11, 17), date(2027, 12, 31)


def build() -> dict:
    cal = load_calendar()
    cases, day = [], FIRST
    while day <= LAST:
        s = schedule(day, cal)
        cases.append(
            [
                day.isoformat(),
                s.near_on.isoformat(),
                s.final_on.isoformat(),
                s.last_call.isoformat(),
            ]
        )
        day += timedelta(days=1)
    return {"calendar": calendar_json(cal), "cases": cases}


def main() -> int:
    OUT.write_text(json.dumps(build(), ensure_ascii=False, indent=0) + "\n", encoding="utf-8")
    print(f"寫入 {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
