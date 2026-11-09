"""public.record_alert：排程寄信成功後，狀態和通知紀錄要一起寫入。"""

from __future__ import annotations

from decimal import Decimal as D

import pytest

DENIED = "42501"
NOT_NULL = "23502"

CALL = "select public.record_alert(%s, %s, %s, '2026-11-09 10:07+08')"


def _rule(db, rule):
    db.as_service()
    return db.one(
        "select state, state_rate, state_target, "
        "state_changed_at = '2026-11-09 10:07+08'::timestamptz "
        "from public.alert_rules where id = %s",
        (rule,),
    )


def _notes(db, user):
    db.as_service()
    return db.rows(
        "select rule_id, currency, rate, target from public.notifications where user_id = %s",
        (user,),
    )


def test_writes_state_and_notification_together(db, alice):
    rule = db.add_rule(alice, "JPY", "0.205")
    db.as_service()
    note_id = db.one(CALL, (rule, "0.2040", "0.205"))
    assert note_id is not None
    assert _rule(db, rule) == ("triggered", D("0.204"), D("0.205"), True)
    assert _notes(db, alice) == [(rule, "JPY", D("0.204"), D("0.205"))]


def test_user_cannot_call_it(db, alice):
    rule = db.add_rule(alice, "JPY", "0.205")
    db.as_user(alice)
    db.fails(CALL, (rule, "0.2040", "0.205"), sqlstate=DENIED)
    db.as_anon()
    db.fails(CALL, (rule, "0.2040", "0.205"), sqlstate=DENIED)
    assert _rule(db, rule)[0] == "armed"
    assert _notes(db, alice) == []


def test_failure_midway_writes_nothing(db, alice):
    """狀態先更新、通知紀錄寫入失敗 → 整個交易回復，狀態也不會變。"""
    rule = db.add_rule(alice, "JPY", "0.205")
    db.as_service()
    db.fails(CALL, (rule, None, "0.205"), sqlstate=NOT_NULL)
    assert _rule(db, rule) == ("armed", None, None, None)
    assert _notes(db, alice) == []


def test_deleted_rule_returns_null(db, alice):
    rule = db.add_rule(alice, "JPY", "0.205")
    db.as_service()
    db.run("delete from public.alert_rules where id = %s", (rule,))
    assert db.one(CALL, (rule, "0.2040", "0.205")) is None
    assert _notes(db, alice) == []


def test_does_not_touch_other_rules(db, alice, bob):
    a = db.add_rule(alice, "JPY", "0.205")
    b = db.add_rule(bob, "JPY", "0.205")
    db.as_service()
    db.one(CALL, (a, "0.2040", "0.205"))
    assert _rule(db, b)[0] == "armed"
    assert _notes(db, bob) == []


# ---------------------------------------------------------------------------
# 排程檢查期間，使用者刪除帳號
# ---------------------------------------------------------------------------


def test_user_deleted_before_record_returns_null(db, alice, bob):
    """規則跟著帳號一起被刪除 → null，別人不受影響。"""
    a = db.add_rule(alice, "JPY", "0.205")
    b = db.add_rule(bob, "JPY", "0.205")
    db.as_postgres()
    db.run("delete from auth.users where id = %s", (alice,))
    db.as_service()
    assert db.one(CALL, (a, "0.2040", "0.205")) is None
    assert db.one(CALL, (b, "0.2040", "0.205")) is not None
    assert _notes(db, bob) == [(b, "JPY", D("0.204"), D("0.205"))]


def test_user_deleted_during_record_returns_null_and_rolls_back(db, alice):
    """刪除和 record_alert 同時進行：寫通知紀錄時帳號已不存在 → null，狀態也不改。

    用 session_replication_role 暫時關掉外鍵檢查，做出「規則還在、帳號已經不在」的瞬間。
    這需要 superuser；CI 的 Supabase 不一定允許，不允許就略過（上一個測試仍涵蓋主要情況）。
    """
    rule = db.add_rule(alice, "JPY", "0.205")
    db.as_postgres()
    try:
        db.run("set local session_replication_role = replica")
    except Exception:  # noqa: BLE001
        pytest.skip("這個資料庫不允許 session_replication_role")
    db.run("delete from public.profiles where user_id = %s", (alice,))
    db.run("set local session_replication_role = origin")
    db.as_service()
    assert db.one(CALL, (rule, "0.2040", "0.205")) is None
    assert _rule(db, rule) == ("armed", None, None, None)
