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
    assert row == [("RESTRICTIVE", "INSERT", "{authenticated}")]  # 其餘五種寫入見下方


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


# ---------------------------------------------------------------------------
# 20261123000300：flag 關閉 = 唯讀（旅程、換匯紀錄都不能新增、修改、刪除）
# ---------------------------------------------------------------------------


def make_trip_with_exchange(db, user) -> tuple[int, int]:
    """flag 開著的時候建立一趟旅程和一筆換匯紀錄，回傳 (trip_id, exchange_id)。"""
    sql, params = insert_trip(db)
    set_flag(db, enabled=True)
    db.as_user(user)
    db.cur.execute(sql + " returning id", params)
    trip_id = db.cur.fetchone()[0]
    db.cur.execute(
        "insert into public.trip_exchanges (trip_id, amount) values (%s, 10000) returning id",
        (trip_id,),
    )
    return trip_id, db.cur.fetchone()[0]


def counts(db, user) -> tuple[int, int]:
    db.as_user(user)
    return (
        db.one("select count(*) from public.trips"),
        db.one("select count(*) from public.trip_exchanges"),
    )


def test_all_six_write_policies_are_restrictive(db):
    db.as_postgres()
    rows = db.rows(
        "select tablename, cmd from pg_policies where schemaname = 'public' "
        "and tablename in ('trips', 'trip_exchanges') and permissive = 'RESTRICTIVE' "
        "order by 1, 2"
    )
    assert rows == [
        ("trip_exchanges", "DELETE"),
        ("trip_exchanges", "INSERT"),
        ("trip_exchanges", "UPDATE"),
        ("trips", "DELETE"),
        ("trips", "INSERT"),
        ("trips", "UPDATE"),
    ]


def test_flag_off_trip_update_is_denied(db, alice):
    trip_id, _ = make_trip_with_exchange(db, alice)
    set_flag(db, enabled=False)
    db.as_user(alice)
    db.fails("update public.trips set amount = 1 where id = %s", (trip_id,), sqlstate=DENIED)
    assert db.one("select amount from public.trips where id = %s", (trip_id,)) == 50000


def test_flag_off_trip_delete_removes_nothing(db, alice):
    """刪除被 USING 擋住：不報錯（PostgREST 回 200/204），但 0 列被刪，資料還在。"""
    trip_id, _ = make_trip_with_exchange(db, alice)
    set_flag(db, enabled=False)
    db.as_user(alice)
    assert db.run("delete from public.trips where id = %s", (trip_id,)) == 0
    assert counts(db, alice) == (1, 1)


def test_flag_off_exchange_insert_is_denied(db, alice):
    trip_id, _ = make_trip_with_exchange(db, alice)
    set_flag(db, enabled=False)
    db.as_user(alice)
    db.fails(
        "insert into public.trip_exchanges (trip_id, amount) values (%s, 5000)",
        (trip_id,),
        sqlstate=DENIED,
    )
    assert counts(db, alice) == (1, 1)


def test_flag_off_exchange_update_is_denied(db, alice):
    _, ex_id = make_trip_with_exchange(db, alice)
    set_flag(db, enabled=False)
    db.as_user(alice)
    db.fails("update public.trip_exchanges set amount = 1 where id = %s", (ex_id,), sqlstate=DENIED)


def test_flag_off_exchange_delete_removes_nothing(db, alice):
    _, ex_id = make_trip_with_exchange(db, alice)
    set_flag(db, enabled=False)
    db.as_user(alice)
    assert db.run("delete from public.trip_exchanges where id = %s", (ex_id,)) == 0
    assert counts(db, alice) == (1, 1)


def test_flag_on_all_writes_work(db, alice):
    trip_id, ex_id = make_trip_with_exchange(db, alice)
    db.as_user(alice)
    assert db.run("update public.trips set amount = 60000 where id = %s", (trip_id,)) == 1
    assert db.run("update public.trip_exchanges set amount = 2 where id = %s", (ex_id,)) == 1
    assert db.run("delete from public.trip_exchanges where id = %s", (ex_id,)) == 1
    assert db.run("delete from public.trips where id = %s", (trip_id,)) == 1
    assert counts(db, alice) == (0, 0)


def test_flag_off_service_role_and_account_deletion_still_work(db, alice):
    """排程（service_role）不受影響；刪除帳號時旅程照樣連帶刪除（cascade 不經過 RLS）。"""
    make_trip_with_exchange(db, alice)
    set_flag(db, enabled=False)
    db.as_service()
    assert db.one("select count(*) from public.trips where user_id = %s", (alice,)) == 1
    db.as_postgres()
    db.run("delete from auth.users where id = %s", (alice,))
    assert db.one("select count(*) from public.trips where user_id = %s", (alice,)) == 0
