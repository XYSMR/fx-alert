"""public.import_rules_for_user：單人版 → 多人版的搬家。"""

from __future__ import annotations

import json
from decimal import Decimal as D

DENIED = "42501"
ALREADY = "P0001"
NO_PROFILE = "P0002"
CHECK = "23514"

RULES = [
    {
        "currency": "JPY",
        "target": "0.205",
        "buffer": "0.001",
        "state": "triggered",
        "state_changed_at": "2026-11-05T10:07:00+08:00",
        "state_rate": "0.2040",
        "state_target": "0.205",
    },
    {
        "currency": "KRW",
        "target": "0.0250",
        "buffer": "0.0002",
        "state": "armed",
        "state_changed_at": "2026-11-09T15:46:16+08:00",
        "state_rate": "0.0264",
        "state_target": None,
    },
]
NOTES = [
    {
        "currency": "JPY",
        "rate": "0.2040",
        "target": "0.205",
        "sent_at": "2026-11-05T10:07:00+08:00",
    },
    {
        "currency": "JPY",
        "rate": "0.2045",
        "target": "0.205",
        "sent_at": "2026-10-20T13:37:00+08:00",
    },
]

CALL = "select public.import_rules_for_user(%s, %s::jsonb, %s::jsonb, %s)"


def _import(db, user, rules=RULES, notes=NOTES, replace=False):
    db.as_service()
    return db.one(CALL, (user, json.dumps(rules), json.dumps(notes), replace))


def _counts(db, user):
    db.as_service()
    return (
        db.one("select count(*) from public.alert_rules where user_id = %s", (user,)),
        db.one("select count(*) from public.notifications where user_id = %s", (user,)),
    )


def test_imports_rules_and_notifications(db, alice):
    assert _import(db, alice) == {"rules": 2, "notifications": 2}
    db.as_service()
    rows = db.rows(
        "select currency, target, buffer, effective_buffer, state, state_rate, state_target, "
        "state_changed_at = %s::timestamptz from public.alert_rules where user_id = %s "
        "order by currency",
        ("2026-11-05T02:07:00Z", alice),
    )
    assert rows[0] == (
        "JPY",
        D("0.205"),
        D("0.001"),
        D("0.001"),
        "triggered",
        D("0.204"),
        D("0.205"),
        True,
    )
    assert rows[1][:7] == ("KRW", D("0.025"), D("0.0002"), D("0.0002"), "armed", D("0.0264"), None)
    # 通知紀錄對到同一個人的規則
    linked = db.one(
        "select count(*) from public.notifications n join public.alert_rules r on r.id = n.rule_id "
        "where n.user_id = %s and r.user_id = %s",
        (alice, alice),
    )
    assert linked == 2


def test_refuses_when_user_already_has_rules(db, alice):
    _import(db, alice)
    db.as_service()
    db.fails(CALL, (alice, json.dumps(RULES), json.dumps(NOTES), False), sqlstate=ALREADY)
    assert _counts(db, alice) == (2, 2)


def test_replace_twice_keeps_counts(db, alice):
    """--replace 執行兩次：規則和通知紀錄都不會重複。"""
    _import(db, alice)
    assert _import(db, alice, replace=True) == {"rules": 2, "notifications": 2}
    assert _import(db, alice, replace=True) == {"rules": 2, "notifications": 2}
    assert _counts(db, alice) == (2, 2)


def test_replace_only_touches_that_user(db, alice, bob):
    _import(db, alice)
    _import(db, bob)
    _import(db, alice, replace=True)
    assert _counts(db, bob) == (2, 2)


def test_failure_midway_writes_nothing(db, alice):
    """第二條規則不合法（buffer 超過 10%）→ 整個交易回復，連 --replace 的刪除也回復。"""
    _import(db, alice)
    bad = [RULES[0], {**RULES[1], "buffer": "0.01"}]
    db.as_service()
    db.fails(CALL, (alice, json.dumps(bad), json.dumps(NOTES), True), sqlstate=CHECK)
    assert _counts(db, alice) == (2, 2)


def test_requires_existing_profile(db):
    db.as_service()
    db.fails(
        CALL,
        ("00000000-0000-4000-8000-0000000000ff", json.dumps(RULES), json.dumps(NOTES), False),
        sqlstate=NO_PROFILE,
    )


def test_users_cannot_call_it(db, alice):
    db.as_user(alice)
    db.fails(CALL, (alice, json.dumps(RULES), json.dumps(NOTES), False), sqlstate=DENIED)
    db.as_anon()
    db.fails(CALL, (alice, json.dumps(RULES), json.dumps(NOTES), False), sqlstate=DENIED)
    assert _counts(db, alice) == (0, 0)
