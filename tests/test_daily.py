"""每日收盤價：兆豐官方回補（seed/）＋ 我們自己的資料（16:00–16:10 最後一份）。"""

import csv
import hashlib
from datetime import date, datetime
from decimal import Decimal as D

from fx_alert.config import TAIPEI
from fx_alert.currencies import CODES
from fx_alert.daily import (
    MIN_CLOSES,
    SEED_PATH,
    business_days,
    compare,
    daily_closes,
    derive_closes,
    read_seed,
    recent_stats,
)
from fx_alert.history import Quote, append_quotes


def tpe(*args):
    return datetime(*args, tzinfo=TAIPEI)


# ---------- seed/mega_daily_close.csv（一次性回補的兆豐官方資料） ----------


def test_seed_file_is_the_downloaded_data():
    """內容和 2026-11-16 從兆豐下載、在瀏覽器裡算的 SHA-256 一致（改了就要更新 seed/README.md）。"""
    digest = hashlib.sha256(SEED_PATH.read_bytes()).hexdigest()
    assert digest == "9f20a862d5c8c3c07c254c17b5aba3aaa3f27667745cedd05c9a961bdc17b37e"


def test_seed_covers_all_currencies_for_one_year():
    seed = read_seed()
    assert set(seed) == set(CODES)  # 18 種，不含只有即期的南非幣、瑞典幣
    for code, days in seed.items():
        assert len(days) == 243, code
        assert min(days) == date(2025, 11, 17) and max(days) == date(2026, 11, 16)
        # 價格都是牌告的 4 位小數
        assert all(v == v.quantize(D("0.0001")) and v > 0 for v in days.values()), code


def test_seed_has_no_weekends_and_same_days_for_every_currency():
    seed = read_seed()
    days = set(seed["USD"])
    assert all(set(v) == days for v in seed.values())
    assert all(d.weekday() < 5 for d in days)
    # 國定假日沒有收盤價（例如 2026 春節 2/16–2/20、雙十節 10/9 補假）
    assert date(2026, 2, 17) not in days and date(2026, 10, 9) not in days
    assert date(2026, 10, 8) in days


def test_seed_header_is_plain_csv():
    with open(SEED_PATH, encoding="utf-8", newline="") as f:
        header = next(csv.reader(f))
    assert header[0] == "date" and header[1:] == list(CODES)


# ---------- 我們自己的收盤價 ----------


def q(at, code, v):
    return Quote(at, code, D(v))


def test_derive_closes_takes_last_quote_between_1600_and_1610():
    quotes = [
        q(tpe(2026, 11, 17, 15, 34, 56), "JPY", "0.2095"),
        q(tpe(2026, 11, 17, 16, 4, 12), "JPY", "0.2096"),  # 收盤
        q(tpe(2026, 11, 17, 18, 4, 12), "JPY", "0.2090"),  # 夜間牌告：不算
        q(tpe(2026, 11, 21, 3, 5, 0), "JPY", "0.2080"),  # 週六半夜：沒有收盤
    ]
    assert derive_closes(quotes) == {"JPY": {date(2026, 11, 17): D("0.2096")}}


def test_daily_closes_prefers_official_seed_then_ours(tmp_path):
    seed = tmp_path / "seed.csv"
    seed.write_text("date,JPY\n2026-11-16,0.2099\n2026-11-13,0.2091\n", encoding="utf-8")
    # 11/16 我們記錄的是 0.2098（和官方差一格）→ 用官方的；11/17 只有我們的 → 用我們的
    append_quotes(tmp_path, tpe(2026, 11, 16, 16, 4, 12), {"JPY": D("0.2098")})
    append_quotes(tmp_path, tpe(2026, 11, 17, 16, 4, 12), {"JPY": D("0.2101")})
    closes = daily_closes(tmp_path, seed_path=seed, until=tpe(2026, 11, 17, 20, 0))
    assert closes["JPY"] == {
        date(2026, 11, 13): D("0.2091"),
        date(2026, 11, 16): D("0.2099"),
        date(2026, 11, 17): D("0.2101"),
    }


def test_compare_lists_only_differences(tmp_path):
    seed = tmp_path / "seed.csv"
    seed.write_text("date,JPY,KRW\n2026-11-13,0.2091,0.0265\n", encoding="utf-8")
    append_quotes(tmp_path, tpe(2026, 11, 13, 16, 4, 11), {"JPY": D("0.2090"), "KRW": D("0.0265")})
    assert compare(tmp_path, seed) == [("JPY", date(2026, 11, 13), "0.2091", "0.2090")]


# ---------- 近 30 天統計 ----------


def test_recent_stats_counts_lower_days():
    closes = {date(2026, 11, d): D(v) for d, v in [(2, "0.2100"), (3, "0.2090"), (4, "0.2110")]}
    r = recent_stats(closes, D("0.2100"), date(2026, 11, 4))
    assert (r.days, r.lower, r.since, r.enough) == (3, 1, date(2026, 10, 6), False)
    assert recent_stats(closes, D("0.2000"), date(2026, 11, 4)).lower == 0


def test_recent_stats_on_real_data():
    """11/16 日圓收盤 0.2099：近 30 天 19 個營業日裡有 7 天比它低。"""
    jpy = read_seed()["JPY"]
    r = recent_stats(jpy, D("0.2099"), date(2026, 11, 16))
    assert r.days == 19 and r.lower == 7 and r.enough
    assert r.days >= MIN_CLOSES


def test_window_excludes_future_and_old_days():
    jpy = read_seed()["JPY"]
    r = recent_stats(jpy, D("0.2099"), date(2026, 11, 9))
    assert r.since == date(2026, 10, 11) and r.days == 19


def test_business_days_from_closes():
    days = business_days(read_seed())
    assert len(days) == 243
    assert date(2026, 11, 14) not in days  # 週六
