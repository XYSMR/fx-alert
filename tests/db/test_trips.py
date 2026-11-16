"""出國換匯計畫（migration 20261116195500）：旅程、換匯紀錄、提醒紀錄的權限和限制。"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal as D

DENIED = "42501"
CHECK = "23514"
FOREIGN_KEY = "23503"
UNIQUE = "23505"


def today(db) -> date:
    db.as_postgres()
    return db.one("select (now() at time zone 'Asia/Taipei')::date")


def add_trip(db, user, days=40, currency="JPY", amount="50000", name=None) -> int:
    dep = today(db) + timedelta(days=days)
    db.as_user(user)
    return db.one(
        "insert into public.trips (currency, departure, amount, name) "
        "values (%s, %s, %s, %s) returning id",
        (currency, dep, amount, name),
    )


def add_exchange(db, user, trip_id, amount="20000", rate=None) -> int:
    db.as_user(user)
    return db.one(
        "insert into public.trip_exchanges (trip_id, amount, rate) "
        "values (%s, %s, %s) returning id",
        (trip_id, amount, rate),
    )


def add_notice(db, trip_id, kind="cheap_far", days_ago=0):
    db.as_service()
    uid = db.one("select user_id from public.trips where id = %s", (trip_id,))
    on = today(db) - timedelta(days=days_ago)
    db.as_service()
    db.run(
        "insert into public.trip_notifications "
        "(trip_id, user_id, kind, sent_on, rate, rank, window_days, days_left, remaining) "
        "values (%s, %s, %s, %s, 0.2050, 2, 20, 30, 50000)",
        (trip_id, uid, kind, on),
    )


# ---------------------------------------------------------------------------
# 旅程
# ---------------------------------------------------------------------------


def test_user_creates_and_sees_only_own_trips(db, alice, bob):
    add_trip(db, alice, name="東京")
    add_trip(db, bob)
    db.as_user(alice)
    assert db.rows("select currency, amount, name from public.trips") == [
        ("JPY", D("50000.00"), "東京")
    ]
    db.as_anon()
    db.fails("select * from public.trips", sqlstate=DENIED)


def test_departure_must_be_tomorrow_to_one_year(db, alice):
    t = today(db)
    db.as_user(alice)
    sql = "insert into public.trips (currency, departure, amount) values ('JPY', %s, 1000)"
    for bad in (t, t - timedelta(days=1), t + timedelta(days=366)):
        db.fails(sql, (bad,), sqlstate=CHECK)
    assert db.run(sql, (t + timedelta(days=1),)) == 1
    assert db.run(sql, (t + timedelta(days=365),)) == 1


def test_at_most_five_upcoming_trips(db, alice, bob):
    for i in range(5):
        add_trip(db, alice, days=10 + i)
    dep = today(db) + timedelta(days=50)
    db.as_user(alice)
    db.fails(
        "insert into public.trips (currency, departure, amount) values ('KRW', %s, 1)",
        (dep,),
        sqlstate=CHECK,
    )
    add_trip(db, bob)  # 別人不受影響
    # 已經出發的不算（直接用 postgres 把一趟改成過去）
    db.as_postgres()
    db.run(
        "update public.trips set departure = departure - 400 where id = "
        "(select min(id) from public.trips where user_id = %s)",
        (alice,),
    )
    add_trip(db, alice, days=60)


def test_user_cannot_set_user_id_or_change_departure(db, alice, bob):
    trip = add_trip(db, alice)
    db.as_user(alice)
    db.fails(
        "insert into public.trips (user_id, currency, departure, amount) "
        "values (%s, 'JPY', current_date + 10, 1)",
        (bob,),
        sqlstate=DENIED,
    )
    db.fails("update public.trips set departure = departure + 1", sqlstate=DENIED)
    db.fails("update public.trips set currency = 'KRW'", sqlstate=DENIED)
    sql = "update public.trips set amount = 80000, name = '大阪' where id = %s"
    assert db.run(sql, (trip,)) == 1


def test_user_cannot_touch_others_trip(db, alice, bob):
    trip = add_trip(db, bob)
    db.as_user(alice)
    assert db.run("update public.trips set amount = 1 where id = %s", (trip,)) == 0
    assert db.run("delete from public.trips where id = %s", (trip,)) == 0


def test_amount_and_name_limits(db, alice):
    sql = "insert into public.trips (currency, departure, amount, name) values ('VND', %s, %s, %s)"
    dep = today(db) + timedelta(days=30)
    db.as_user(alice)
    db.fails(sql, (dep, "0", None), sqlstate=CHECK)
    db.fails(sql, (dep, "1000000000000", None), sqlstate=CHECK)
    db.fails(sql, (dep, "1", ""), sqlstate=CHECK)
    db.fails(sql, (dep, "1", "x" * 31), sqlstate=CHECK)
    assert db.run(sql, (dep, "50000000", "越南")) == 1  # 越南盾金額大


# ---------------------------------------------------------------------------
# 換匯紀錄
# ---------------------------------------------------------------------------


def test_exchanges_and_progress(db, alice):
    trip = add_trip(db, alice)
    add_exchange(db, alice, trip, "20000", "0.2061")
    add_exchange(db, alice, trip, "10000")
    db.as_user(alice)
    assert db.one(
        "select exchanged, remaining, exchange_count from public.trip_progress where id = %s",
        (trip,),
    ) == (D("30000.00"), D("20000.00"), 2)


def test_over_exchange_remaining_is_zero(db, alice):
    trip = add_trip(db, alice, amount="1000")
    add_exchange(db, alice, trip, "1500")
    db.as_user(alice)
    assert db.one("select remaining from public.trip_progress") == D("0")


def test_cannot_add_exchange_to_others_trip(db, alice, bob):
    trip = add_trip(db, bob)
    db.as_user(alice)
    db.fails(
        "insert into public.trip_exchanges (trip_id, amount) values (%s, 100)",
        (trip,),
        sqlstate=FOREIGN_KEY,
    )
    db.fails(
        "insert into public.trip_exchanges (trip_id, user_id, amount) values (%s, %s, 100)",
        (trip, bob),
        sqlstate=DENIED,
    )


def test_exchanges_visible_only_to_owner(db, alice, bob):
    trip = add_trip(db, alice)
    add_exchange(db, alice, trip)
    db.as_user(bob)
    assert db.rows("select * from public.trip_exchanges") == []
    assert db.rows("select * from public.trip_progress") == []


def test_exchange_limit_50(db, alice):
    trip = add_trip(db, alice)
    db.as_user(alice)
    for _ in range(50):
        db.run("insert into public.trip_exchanges (trip_id, amount) values (%s, 1)", (trip,))
    db.fails(
        "insert into public.trip_exchanges (trip_id, amount) values (%s, 1)",
        (trip,),
        sqlstate=CHECK,
    )


def test_exchange_rate_limits(db, alice):
    trip = add_trip(db, alice)
    db.as_user(alice)
    sql = "insert into public.trip_exchanges (trip_id, amount, rate) values (%s, 1, %s)"
    db.fails(sql, (trip, "0"), sqlstate=CHECK)
    db.fails(sql, (trip, "1000"), sqlstate=CHECK)


def test_delete_trip_cascades(db, alice):
    trip = add_trip(db, alice)
    add_exchange(db, alice, trip)
    add_notice(db, trip)
    db.as_user(alice)
    assert db.run("delete from public.trips where id = %s", (trip,)) == 1
    db.as_service()
    assert db.one("select count(*) from public.trip_exchanges") == 0
    assert db.one("select count(*) from public.trip_notifications") == 0


def test_delete_user_cascades(db, alice):
    trip = add_trip(db, alice)
    add_exchange(db, alice, trip)
    add_notice(db, trip)
    db.as_postgres()
    db.run("delete from auth.users where id = %s", (alice,))
    for table in ("trips", "trip_exchanges", "trip_notifications"):
        assert db.one(f"select count(*) from public.{table}") == 0


# ---------------------------------------------------------------------------
# 提醒紀錄
# ---------------------------------------------------------------------------


def test_only_service_writes_notifications(db, alice):
    trip = add_trip(db, alice)
    db.as_user(alice)
    db.fails(
        "insert into public.trip_notifications (trip_id, user_id, kind, sent_on, days_left, "
        "remaining) values (%s, %s, 'final', current_date, 7, 1)",
        (trip, alice),
        sqlstate=DENIED,
    )
    add_notice(db, trip)
    db.as_user(alice)
    assert db.rows("select kind, rank from public.trip_notifications") == [("cheap_far", 2)]
    db.fails("delete from public.trip_notifications", sqlstate=DENIED)


def test_one_per_day_and_fixed_kinds_once(db, alice):
    trip = add_trip(db, alice)
    add_notice(db, trip, "near_start", days_ago=3)
    add_notice(db, trip, "cheap_near", days_ago=0)
    db.fails(  # 同一天第二封
        "insert into public.trip_notifications (trip_id, user_id, kind, sent_on, days_left, "
        "remaining) select id, user_id, 'final', (now() at time zone 'Asia/Taipei')::date, 7, 1 "
        "from public.trips",
        sqlstate=UNIQUE,
    )
    db.fails(  # 固定提醒第二次
        "insert into public.trip_notifications (trip_id, user_id, kind, sent_on, days_left, "
        "remaining) select id, user_id, 'near_start', (now() at time zone 'Asia/Taipei')::date "
        "- 1, 7, 1 from public.trips",
        sqlstate=UNIQUE,
    )
    add_notice(db, trip, "cheap_near", days_ago=1)  # 價格提醒可以多次（不同天）


def test_notification_kind_checked(db, alice):
    trip = add_trip(db, alice)
    db.as_service()
    db.fails(
        "insert into public.trip_notifications (trip_id, user_id, kind, sent_on, days_left, "
        "remaining) values (%s, %s, 'buy_now', current_date, 7, 1)",
        (trip, alice),
        sqlstate=CHECK,
    )


# ---------------------------------------------------------------------------
# 排程用的 active_trips
# ---------------------------------------------------------------------------


def test_active_trips_for_scheduler(db, alice, bob):
    trip = add_trip(db, alice, amount="1000")
    done = add_trip(db, alice, days=20, amount="500")
    add_exchange(db, alice, done, "500")  # 換完了
    add_notice(db, trip, "cheap_far", days_ago=2)
    add_trip(db, bob)
    db.as_user(bob)
    db.run("update public.profiles set email_enabled = false")  # 關掉通知的人不列
    db.as_service()
    rows = db.rows(
        "select id, remaining, created_on, email, sent from public.active_trips order by id"
    )
    t = today(db)
    db.as_service()
    assert rows == [
        (
            trip,
            D("1000.00"),
            t,
            "alice@example.com",
            [{"kind": "cheap_far", "on": (t - timedelta(days=2)).isoformat()}],
        )
    ]


def test_active_trips_excludes_departed(db, alice):
    trip = add_trip(db, alice)
    db.as_postgres()
    db.run("update public.trips set departure = (now() at time zone 'Asia/Taipei')::date")
    db.as_service()
    assert db.rows("select id from public.active_trips where id = %s", (trip,)) == []


def test_active_trips_service_only(db, alice):
    add_trip(db, alice)
    db.as_user(alice)
    db.fails("select * from public.active_trips", sqlstate=DENIED)
    db.as_anon()
    db.fails("select * from public.trip_progress", sqlstate=DENIED)
