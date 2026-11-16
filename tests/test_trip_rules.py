from datetime import date, timedelta
from decimal import Decimal

from fx_alert.bizdays import load_calendar
from fx_alert.daily import Recent
from fx_alert.trip_rules import (
    CHEAP_FAR,
    CHEAP_NEAR,
    FINAL,
    LAST_CALL,
    NEAR_START,
    Sent,
    Trip,
    decide,
    schedule,
)

CAL = load_calendar()
D = date(2027, 1, 17)  # 週日出發：D-21、D-7 都落在週末


def rec(lower, days=20):
    return Recent(days=days, lower=lower, since=date(2000, 1, 1), enough=days >= 15)


def trip(created=date(2026, 11, 16), remaining="1000"):
    return Trip(D, created, Decimal(remaining))


def test_schedule_shifts_to_business_days():
    s = schedule(D, CAL)
    assert s.near_on == date(2026, 12, 24)  # D-21 = 12/27 週日；12/25 行憲紀念日 → 12/24
    assert s.final_on == date(2027, 1, 8)  # D-7 = 1/10 週日 → 1/8 週五
    assert s.last_call == date(2027, 1, 14)


def test_schedule_lunar_new_year():
    s = schedule(date(2027, 2, 11), CAL)
    # D-7 = 2/4 小年夜 → 2/3；但最晚是倒數第 3 個營業日 2/1
    assert (s.final_on, s.last_call) == (date(2027, 2, 1), date(2027, 2, 2))
    assert s.near_on == date(2027, 1, 21)


def test_far_cheap_with_cooldown():
    t, day = trip(), date(2026, 11, 20)
    d = decide(t, day, rec(2), [], CAL)
    assert d.kind == CHEAP_FAR and d.rank == 3 and d.window == 20
    assert decide(t, day, rec(5), [], CAL) is None  # 25% > 20%
    assert decide(t, day + timedelta(days=4), rec(0), [Sent(CHEAP_FAR, day)], CAL) is None
    assert decide(t, day + timedelta(days=7), rec(0), [Sent(CHEAP_FAR, day)], CAL).kind == CHEAP_FAR


def test_not_enough_history_or_no_quote():
    assert decide(trip(), date(2026, 11, 20), rec(0, days=10), [], CAL) is None
    assert decide(trip(), date(2026, 11, 20), None, [], CAL) is None


def test_weekend_and_holiday_never_send():
    assert decide(trip(), date(2026, 11, 21), rec(0), [], CAL) is None
    assert decide(trip(), date(2026, 12, 25), rec(0), [], CAL) is None


def test_near_start_once_then_p40():
    s = schedule(D, CAL)
    assert decide(trip(), s.near_on, rec(19), [], CAL).kind == NEAR_START
    sent = [Sent(NEAR_START, s.near_on)]
    assert decide(trip(), date(2026, 12, 28), rec(0), sent, CAL) is None  # 冷卻 5 天未滿
    later = date(2027, 1, 5)
    assert decide(trip(), later, rec(8), sent, CAL).kind == CHEAP_NEAR
    assert decide(trip(), later, rec(9), sent, CAL) is None  # 45% > 40%


def test_final_and_last_call_regardless_of_price():
    s = schedule(D, CAL)
    assert decide(trip(), s.final_on, rec(19), [], CAL).kind == FINAL
    assert decide(trip(), s.last_call, rec(19), [Sent(FINAL, s.final_on)], CAL).kind == LAST_CALL
    done = [Sent(FINAL, s.final_on), Sent(LAST_CALL, s.last_call)]
    assert decide(trip(), CAL.before(D, 1), rec(0), done, CAL) is None  # 最後階段不寄價格提醒
    assert decide(trip(), D, rec(0), [], CAL) is None


def test_missed_fixed_mail_caught_up_next_business_day():
    s = schedule(D, CAL)
    nxt = date(2027, 1, 11)  # final_on 1/8 週五沒寄到 → 下週一補
    assert s.final_on == date(2027, 1, 8)
    assert decide(trip(), nxt, rec(19), [], CAL).kind == FINAL


def test_one_mail_per_day():
    day = date(2026, 11, 20)
    assert decide(trip(), day, rec(0), [Sent(CHEAP_FAR, day)], CAL) is None


def test_created_late_skips_passed_milestones():
    s = schedule(D, CAL)
    late = trip(created=date(2027, 1, 4))
    assert decide(late, date(2027, 1, 5), rec(19), [], CAL) is None  # 不補「分批」
    assert decide(late, date(2027, 1, 5), rec(2), [], CAL).kind == CHEAP_NEAR
    very_late = trip(created=date(2027, 1, 13))
    assert decide(very_late, date(2027, 1, 13), rec(0), [], CAL) is None  # 最後階段：不補 FINAL
    assert decide(very_late, s.last_call, rec(19), [], CAL).kind == LAST_CALL


def test_finished_trip_stops():
    assert decide(trip(remaining="0"), date(2026, 11, 20), rec(0), [], CAL) is None


def test_simulation_script_runs(capsys):
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "scripts" / "simulate_trip_rules.py"
    spec = importlib.util.spec_from_file_location("sim", path)
    sim = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sim)
    assert sim.main(["--codes", "JPY", "--since", "2026-10-01"]) == 0
    out = capsys.readouterr().out
    assert "JPY：" in out and "last_call 1.00" in out


def test_js_fixture_matches_python():
    """site/tests/trips.test.js 用這份檔案比對前端算的提醒日期；規則改了要重新產生。"""
    import importlib.util
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("fx", root / "scripts" / "make_trip_fixture.py")
    fx = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fx)
    saved = json.loads((root / "tests" / "fixtures" / "trip_schedules.json").read_text("utf-8"))
    assert saved == fx.build(), "請執行 python scripts/make_trip_fixture.py"
