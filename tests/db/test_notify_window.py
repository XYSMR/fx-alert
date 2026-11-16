"""通知時段（migration 20261116142000）：預設 08:00–22:00、只能整點或半點、本人可改、排程讀得到。"""

from datetime import time

CHECK = "23514"
NO_PRIVILEGE = "42501"


def test_default_window_is_8_to_22(db, alice):
    db.as_user(alice)
    assert db.one("select notify_start, notify_end from public.profiles") == (
        time(8, 0),
        time(22, 0),
    )


def test_user_can_change_own_window(db, alice, bob):
    db.as_user(alice)
    assert db.run("update public.profiles set notify_start = '22:00', notify_end = '07:30'") == 1
    assert db.one("select notify_start, notify_end from public.profiles") == (
        time(22, 0),
        time(7, 30),
    )
    # 別人的看不到也改不到（RLS）
    db.as_user(bob)
    assert db.one("select notify_start from public.profiles") == time(8, 0)


def test_cannot_change_others_window(db, alice, bob):
    db.as_user(alice)
    assert (
        db.run("update public.profiles set notify_start = '00:00' where user_id = %s", (bob,)) == 0
    )
    db.as_service()
    assert db.one("select notify_start from public.profiles where user_id = %s", (bob,)) == time(
        8, 0
    )


def test_only_whole_or_half_hours(db, alice):
    db.as_user(alice)
    for bad in ("08:15", "08:00:30", "23:59"):
        db.fails("update public.profiles set notify_start = %s", (bad,), sqlstate=CHECK)
        db.fails("update public.profiles set notify_end = %s", (bad,), sqlstate=CHECK)
    assert db.run("update public.profiles set notify_start = '00:00', notify_end = '00:00'") == 1


def test_email_still_not_writable(db, alice):
    db.as_user(alice)
    db.fails("update public.profiles set email = 'x@example.com'", sqlstate=NO_PRIVILEGE)


def test_active_rules_includes_window(db, alice):
    db.add_rule(alice, "JPY", "0.2050")
    db.as_user(alice)
    db.run("update public.profiles set notify_start = '07:00', notify_end = '23:30'")
    db.as_service()
    assert db.rows("select currency, notify_start, notify_end from public.active_rules") == [
        ("JPY", time(7, 0), time(23, 30))
    ]
    # 欄位順序：舊欄位不變，新欄位加在最後
    cols = db.rows(
        "select column_name from information_schema.columns "
        "where table_schema = 'public' and table_name = 'active_rules' order by ordinal_position"
    )
    assert [c for (c,) in cols] == [
        "id",
        "user_id",
        "currency",
        "target",
        "effective_buffer",
        "state",
        "state_target",
        "email",
        "notify_start",
        "notify_end",
    ]
