"""public.active_rules：排程要檢查的規則（規則啟用 + Email 開著）。"""

from __future__ import annotations

DENIED = "42501"

SELECT = "select id, state, email from public.active_rules order by id"


def _active(db):
    db.as_service()
    return db.rows(SELECT)


def test_lists_enabled_rules_with_owner_email(db, alice, bob):
    a = db.add_rule(alice, "JPY", "0.205")
    b = db.add_rule(bob, "KRW", "0.025")
    assert _active(db) == [(a, "armed", "alice@example.com"), (b, "armed", "bob@example.com")]


def test_email_disabled_user_is_not_checked(db, alice, bob):
    db.add_rule(alice, "JPY", "0.205")
    db.add_rule(alice, "KRW", "0.025")
    b = db.add_rule(bob, "JPY", "0.205")
    db.as_user(alice)
    db.run("update public.profiles set email_enabled = false")
    assert [row[0] for row in _active(db)] == [b]


def test_disabled_rule_is_not_checked(db, alice):
    a = db.add_rule(alice, "JPY", "0.205")
    k = db.add_rule(alice, "KRW", "0.025")
    db.as_user(alice)
    db.run("update public.alert_rules set enabled = false where id = %s", (a,))
    assert [row[0] for row in _active(db)] == [k]


def test_reenabled_email_comes_back_as_armed(db, alice):
    """關閉期間不判斷；重新開啟後回到清單，而且是等待中（不補寄過時的通知）。"""
    a = db.add_rule(
        alice,
        "JPY",
        "0.205",
        state="triggered",
        state_changed_at="2026-11-01 10:00+08",
        state_rate="0.204",
        state_target="0.205",
    )
    db.as_user(alice)
    db.run("update public.profiles set email_enabled = false")
    assert _active(db) == []
    db.as_user(alice)
    db.run("update public.profiles set email_enabled = true")
    assert _active(db) == [(a, "armed", "alice@example.com")]


def test_users_cannot_read_view(db, alice):
    db.add_rule(alice, "JPY", "0.205")
    db.as_user(alice)
    db.fails(SELECT, sqlstate=DENIED)
    db.as_anon()
    db.fails(SELECT, sqlstate=DENIED)
