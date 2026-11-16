import hashlib
from datetime import date

from fx_alert.bizdays import CALENDAR_PATH, COVERED_FROM, Calendar, load_calendar
from fx_alert.daily import business_days, read_seed

CAL = load_calendar()


def test_matches_mega_business_days_exactly():
    mega = business_days(read_seed())
    assert set(CAL.between(COVERED_FROM, date(2026, 11, 16))) == mega
    assert len(mega) == 243


def test_holidays_and_weekends():
    assert not CAL.is_business_day(date(2026, 2, 17))  # 春節
    assert not CAL.is_business_day(date(2026, 10, 10))  # 週六
    assert not CAL.is_business_day(date(2026, 10, 23))  # 颱風
    assert CAL.is_business_day(date(2026, 10, 22))
    assert not CAL.is_business_day(date(2027, 2, 9))  # 補假


def test_before_and_on_or_before():
    # 2027-02-11（週四）出發：2/4～2/10 春節連假
    assert CAL.before(date(2027, 2, 11), 1) == date(2027, 2, 3)
    assert CAL.before(date(2027, 2, 11), 2) == date(2027, 2, 2)
    assert CAL.on_or_before(date(2026, 12, 27)) == date(2026, 12, 24)  # 週日 → 12/25 假日 → 12/24


def test_makeup_workday_counts():
    cal = Calendar(workday=frozenset({date(2027, 3, 6)}))
    assert cal.is_business_day(date(2027, 3, 6))


def test_kinds_are_valid_and_covered():
    text = CALENDAR_PATH.read_text(encoding="utf-8")
    assert text.startswith("date,kind,note\n")
    for line in text.strip().splitlines()[1:]:
        d, kind, _ = line.split(",", 2)
        assert kind in ("off", "workday")
        assert CAL.covers(date.fromisoformat(d))
    assert len(hashlib.sha256(text.encode()).hexdigest()) == 64
