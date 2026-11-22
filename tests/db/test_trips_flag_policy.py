"""trips 綁 trip_plan flag 的 restrictive policy（20261123000200）：TC3 的伺服器端檢查。"""

from __future__ import annotations

from datetime import timedelta

DENIED = "42501"
POLICY = "trips only when trip_plan is on"


def set_flag(db, *, enabled: bool, rollout: int = 100) -> None:
    db.as_postgres()
    db.run(
        "update public.feature_flags set enabled = %s, rollout = %s where flag = 'trip_plan'",
        (enabled, rollout),
    )


def insert_trip(db) -> tuple[str, tuple]:
    db.as_postgres()
    dep = db.one("select (now() at time zone 'Asia/Taipei')::date") + timedelta(days=40)
    return (
        "insert into public.trips (currency, departure, amount) values ('JPY', %s, 50000)",
        (dep,),
    )


def test_policy_is_restrictive_insert_only(db):
    db.as_postgres()
    row = db.rows(
        "select permissive, cmd, roles::text from pg_policies "
        "where schemaname = 'public' and tablename = 'trips' and policyname = %s",
        (POLICY,),
    )
    assert row == [("RESTRICTIVE", "INSERT", "{authenticated}")]


def test_flag_on_user_can_add_trip(db, alice):
    sql, params = insert_trip(db)
    set_flag(db, enabled=True)
    db.as_user(alice)
    assert db.run(sql, params) == 1


def test_flag_off_direct_api_insert_is_denied(db, alice):  # TC3
    sql, params = insert_trip(db)
    set_flag(db, enabled=False)
    db.as_user(alice)
    db.fails(sql, params, sqlstate=DENIED)  # PostgREST 回 403


def test_outside_rollout_is_denied_but_beta_is_allowed(db, alice):
    sql, params = insert_trip(db)
    set_flag(db, enabled=True, rollout=0)
    db.as_user(alice)
    db.fails(sql, params, sqlstate=DENIED)

    db.as_postgres()
    db.run("update public.profiles set role = 'beta' where user_id = %s", (alice,))
    db.as_user(alice)
    assert db.run(sql, params) == 1


def test_existing_trips_stay_visible_when_flag_is_off(db, alice):
    sql, params = insert_trip(db)
    set_flag(db, enabled=True)
    db.as_user(alice)
    db.run(sql, params)

    set_flag(db, enabled=False)
    db.as_user(alice)
    assert db.one("select count(*) from public.trips") == 1


def test_still_cannot_add_trip_for_someone_else(db, alice, bob):
    """restrictive 是「而且」：flag 開著也不能替別人新增（原本的 permissive policy 還在）。"""
    db.as_postgres()
    dep = db.one("select (now() at time zone 'Asia/Taipei')::date") + timedelta(days=40)
    set_flag(db, enabled=True)
    db.as_user(alice)
    db.fails(
        "insert into public.trips (user_id, currency, departure, amount) "
        "values (%s, 'JPY', %s, 50000)",
        (bob, dep),
        sqlstate=DENIED,
    )
