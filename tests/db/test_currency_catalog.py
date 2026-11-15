"""全部 18 種幣別、價格對齊報價單位 0.0001（migration 20261116000000）。"""

from decimal import Decimal as D

import pytest

from fx_alert.currencies import CATALOG, auto_buffer

CHECK = "23514"
FOREIGN_KEY = "23503"
NO_PRIVILEGE = "42501"


def test_currencies_table_matches_catalog(db):
    """資料庫和 currencies.py 是同一份清單：代碼、中文名稱、牌告順序、常用順序。"""
    db.as_anon()
    rows = db.rows(
        "select code, name_zh, sort_order, popular_rank from public.currencies order by sort_order"
    )
    assert rows == [(c.code, c.name_zh, c.sort_order, c.popular_rank) for c in CATALOG]


def test_spot_only_currencies_are_not_supported(db, alice):
    db.as_user(alice)
    for code in ("ZAR", "SEK"):
        db.fails(
            "insert into public.alert_rules (currency, target) values (%s, 2.0)",
            (code,),
            sqlstate=FOREIGN_KEY,
        )


def test_user_can_set_rules_for_new_currencies(db, alice):
    db.as_user(alice)
    for code, target in (("USD", "32.5"), ("VND", "0.0015"), ("IDR", "0.0021"), ("EUR", "36.8")):
        assert (
            db.run(
                "insert into public.alert_rules (currency, target) values (%s, %s)", (code, target)
            )
            == 1
        )


@pytest.mark.parametrize(
    ("code", "target"),
    [("VND", "0.0015"), ("IDR", "0.0021"), ("KRW", "0.0245"), ("JPY", "0.205"), ("USD", "32.5")],
)
def test_auto_buffer_matches_python_and_is_on_tick(db, alice, code, target):
    """自動回升門檻：0.5% 四捨五入到 0.0001、至少 0.0001；和 Python、網頁算法一致。

    越南盾目標 0.0015 → 0.0001（原本是 0.0000075，回升價格會變成 0.0015075）。
    """
    db.as_user(alice)
    db.run("insert into public.alert_rules (currency, target) values (%s, %s)", (code, target))
    buf, rearm = db.one(
        "select effective_buffer, target + effective_buffer from public.alert_rules"
    )
    assert buf == auto_buffer(D(target))
    assert rearm == rearm.quantize(D("0.0001"))  # 不會出現 0.0015075


def test_target_must_be_on_tick(db, alice):
    db.as_user(alice)
    db.fails(
        "insert into public.alert_rules (currency, target) values ('VND', 0.00155)",
        sqlstate=CHECK,
    )
    db.fails(
        "insert into public.alert_rules (currency, target) values ('JPY', 0.20505)",
        sqlstate=CHECK,
    )


@pytest.mark.parametrize("buffer", ["0", "0.00005", "0.00015"])
def test_buffer_must_be_at_least_one_tick_and_on_tick(db, alice, buffer):
    db.as_user(alice)
    db.fails(
        "insert into public.alert_rules (currency, target, buffer) values ('VND', 0.0015, %s)",
        (buffer,),
        sqlstate=CHECK,
    )


def test_tiny_target_still_allows_one_tick_buffer(db, alice):
    """越南盾目標 0.0010：10% 只有 0.0001，仍然可以設一個報價單位，但不能更多。"""
    db.as_user(alice)
    ok = "insert into public.alert_rules (currency, target, buffer) values ('VND', 0.0010, 0.0001)"
    assert db.run(ok) == 1
    db.fails("update public.alert_rules set buffer = 0.0002 where currency = 'VND'", sqlstate=CHECK)


def test_active_rules_view_still_service_role_only(db, alice):
    db.add_rule(alice, "VND", "0.0015")
    db.as_service()
    assert db.rows("select currency, target, effective_buffer from public.active_rules") == [
        ("VND", D("0.0015"), D("0.0001"))
    ]
    for who in (db.as_anon, lambda: db.as_user(alice)):
        who()
        db.fails("select * from public.active_rules", sqlstate=NO_PRIVILEGE)


def test_new_currency_columns_are_read_only(db, alice):
    for who in (db.as_anon, lambda: db.as_user(alice)):
        who()
        db.fails(
            "update public.currencies set sort_order = 99 where code = 'USD'", sqlstate=NO_PRIVILEGE
        )
        db.fails(
            "insert into public.currencies (code, name_zh, sort_order) "
            "values ('ZAR', '南非幣', 19)",
            sqlstate=NO_PRIVILEGE,
        )
