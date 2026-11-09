import json
from datetime import datetime, timedelta
from decimal import Decimal as D

from fx_alert.config import TAIPEI
from fx_alert.history import (
    append_alert,
    append_quotes,
    read_alerts,
    read_last_check,
    read_quotes,
    write_last_check,
)

Q1 = datetime(2026, 11, 9, 9, 5)  # 兆豐給的牌告時間沒有時區
Q2 = datetime(2026, 11, 9, 11, 30)
RATES = {"KRW": D("0.0264"), "JPY": D("0.2121")}


def test_append_quotes_writes_monthly_csv(tmp_path):
    assert append_quotes(tmp_path, Q1, RATES) is True
    text = (tmp_path / "history" / "2026-11.csv").read_text(encoding="utf-8")
    assert text == (
        "quoted_at,currency,cash_sell\n"
        "2026-11-09T09:05:00+08:00,JPY,0.2121\n"
        "2026-11-09T09:05:00+08:00,KRW,0.0264\n"
    )


def test_same_quote_is_recorded_only_once(tmp_path):
    assert append_quotes(tmp_path, Q1, RATES) is True
    assert append_quotes(tmp_path, Q1, {"JPY": D("0.2121")}) is False
    assert append_quotes(tmp_path, Q2, RATES) is True
    assert len(read_quotes(tmp_path, Q1)) == 4


def test_no_quote_time_or_no_rates_records_nothing(tmp_path):
    assert append_quotes(tmp_path, None, RATES) is False
    assert append_quotes(tmp_path, Q1, {}) is False
    assert not (tmp_path / "history").exists()


def test_new_month_goes_to_new_file(tmp_path):
    append_quotes(tmp_path, datetime(2026, 10, 31, 16, 0), RATES)
    append_quotes(tmp_path, datetime(2026, 11, 2, 9, 5), RATES)
    assert sorted(p.name for p in (tmp_path / "history").iterdir()) == [
        "2026-10.csv",
        "2026-11.csv",
    ]


def test_read_quotes_filters_by_date_and_sorts(tmp_path):
    append_quotes(tmp_path, datetime(2026, 9, 1, 9, 0), RATES)  # 太舊
    append_quotes(tmp_path, Q2, RATES)
    append_quotes(tmp_path, Q1, RATES)  # 故意後寫
    since = datetime(2026, 10, 10, tzinfo=TAIPEI)
    quotes = read_quotes(tmp_path, since)
    assert [q.quoted_at.hour for q in quotes] == [9, 9, 11, 11]
    assert quotes[0].currency == "JPY" and quotes[0].cash_sell == D("0.2121")
    assert quotes[0].quoted_at.utcoffset() == timedelta(hours=8)


def test_read_quotes_without_history(tmp_path):
    assert read_quotes(tmp_path, Q1) == []


def test_alerts_round_trip_newest_first(tmp_path):
    t1 = datetime(2026, 11, 9, 10, 7, tzinfo=TAIPEI)
    append_alert(tmp_path, t1, "JPY", D("0.2049"), D("0.205"))
    append_alert(tmp_path, t1 + timedelta(days=2), "KRW", D("0.0249"), D("0.0250"))
    alerts = read_alerts(tmp_path)
    assert [a.currency for a in alerts] == ["KRW", "JPY"]
    assert alerts[1].rate == D("0.2049") and alerts[1].target == D("0.205")
    header = (tmp_path / "alerts.csv").read_text(encoding="utf-8").splitlines()[0]
    assert header == "sent_at,currency,rate,target"  # 沒有收件人欄位


def test_read_alerts_empty(tmp_path):
    assert read_alerts(tmp_path) == []


def test_last_check_only_stores_counts(tmp_path):
    at = datetime(2026, 11, 9, 15, 37, tzinfo=TAIPEI)
    write_last_check(tmp_path, at, ok=False, errors=2)
    data = read_last_check(tmp_path)
    assert data == {"at": "2026-11-09T15:37:00+08:00", "ok": False, "errors": 2}
    assert set(json.loads((tmp_path / "last_check.json").read_text())) == {"at", "ok", "errors"}


def test_last_check_missing(tmp_path):
    assert read_last_check(tmp_path) is None


def test_files_use_lf(tmp_path):
    append_quotes(tmp_path, Q1, RATES)
    append_alert(tmp_path, Q1, "JPY", D("0.2"), D("0.205"))
    write_last_check(tmp_path, Q1, ok=True, errors=0)
    for p in tmp_path.rglob("*.*"):
        assert b"\r\n" not in p.read_bytes(), p
