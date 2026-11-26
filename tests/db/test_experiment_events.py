"""HDD 指標事件表 experiment_events（20261127000000，issue #53）。"""

from __future__ import annotations

DENIED = "42501"
CHECK = "23514"
INSERT = "insert into public.experiment_events (event, flag) values (%s, %s)"


def set_flag(db, flag: str, *, enabled: bool, rollout: int = 100) -> None:
    db.as_postgres()
    db.run(
        "update public.feature_flags set enabled = %s, rollout = %s where flag = %s",
        (enabled, rollout, flag),
    )


def stored(db) -> list[tuple]:
    db.as_service()
    return db.rows("select user_id, event, flag, variant from public.experiment_events order by id")


def test_user_can_insert_own_event(db, alice):
    db.as_user(alice)
    assert db.run(INSERT, ("trip_created", "trip_plan")) == 1
    assert [r[:3] for r in stored(db)] == [(alice, "trip_created", "trip_plan")]


def test_user_cannot_read_events(db, alice):
    """連自己的事件也讀不到：指標只在 service_role 算。"""
    db.as_user(alice)
    db.run(INSERT, ("alert_saved", "quick_target"))
    db.fails("select * from public.experiment_events", sqlstate=DENIED)


def test_user_cannot_write_for_someone_else(db, alice, bob):
    db.as_user(alice)
    db.fails(
        "insert into public.experiment_events (user_id, event, flag) values (%s, %s, %s)",
        (bob, "trip_created", "trip_plan"),
        sqlstate=DENIED,
    )


def test_user_cannot_update_or_delete(db, alice):
    db.as_user(alice)
    db.run(INSERT, ("trip_created", "trip_plan"))
    db.fails("update public.experiment_events set event = 'alert_saved'", sqlstate=DENIED)
    db.fails("delete from public.experiment_events", sqlstate=DENIED)


def test_anon_cannot_insert(db):
    db.as_anon()
    db.fails(INSERT, ("trip_created", "trip_plan"), sqlstate=DENIED)


def test_unknown_event_or_flag_rejected(db, alice):
    db.as_user(alice)
    db.fails(INSERT, ("clicked_everything", "trip_plan"), sqlstate=CHECK)
    db.fails(INSERT, ("trip_created", "dark_mode"), sqlstate=CHECK)


def test_event_must_match_its_experiment(db, alice):
    """H1 的事件不能被記到 H2（反之亦然），否則指標會算錯。"""
    db.as_user(alice)
    db.fails(INSERT, ("trip_created", "quick_target"), sqlstate=CHECK)
    db.fails(INSERT, ("quick_target_clicked", "trip_plan"), sqlstate=CHECK)
    db.fails(INSERT, ("alert_saved", "trip_plan"), sqlstate=CHECK)


def test_exposed_is_recorded_for_both_arms(db, alice):
    """分母：ON、OFF 兩組都會記 exposed，variant 由伺服器決定。"""
    set_flag(db, "trip_plan", enabled=True)
    db.as_user(alice)
    db.run(INSERT, ("exposed", "trip_plan"))
    set_flag(db, "trip_plan", enabled=False)
    db.as_user(alice)
    db.run(INSERT, ("exposed", "trip_plan"))
    db.run(INSERT, ("exposed", "quick_target"))
    assert [r[1:] for r in stored(db)] == [
        ("exposed", "trip_plan", "on"),
        ("exposed", "trip_plan", "off"),
        ("exposed", "quick_target", "off"),
    ]
