"""Feature flags（migration 20261123000100、20261123000150）。

flag 表權限、is_feature_on()、伺服器端檢查、supabase/staging/ 的兩個腳本。

角色本身（預設 user、使用者改不了自己的角色）由 Task 2 的 tests/db/test_roles.py 測試；
is_feature_on() 透過 public.current_user_role() 讀角色。

對應 A10 的測試案例：
  TC2/TC4 flag 關閉（kill switch）→ 所有人關閉，改一列就生效
  TC3     flag 關閉時直接打 API 新增旅程 → 被 RLS 擋下（403）
  TC5     同一個使用者每次結果都一樣
  TC6     beta / admin 在 rollout 0 時仍然開啟
  TC7     使用者不能把自己改成 admin → tests/db/test_roles.py::test_user_cannot_promote_self
  TC8     沒登入 → 關閉
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from pathlib import Path

import pytest

DENIED = "42501"
CHECK = "23514"


def set_flag(db, flag, *, enabled=True, rollout=100, allowed_roles=("admin", "beta")) -> None:
    """模擬在 SQL Editor 切換 flag（postgres 身分）。"""
    db.as_postgres()
    db.run(
        "update public.feature_flags set enabled = %s, rollout = %s, allowed_roles = %s "
        "where flag = %s",
        (enabled, rollout, list(allowed_roles), flag),
    )


def set_role(db, user_id, role) -> None:
    """模擬在 SQL Editor 指定角色。"""
    db.as_postgres()
    db.run("update public.profiles set role = %s where user_id = %s", (role, user_id))


def is_on(db, flag) -> bool:
    return db.one("select public.is_feature_on(%s)", (flag,))


def as_uid(db, user_id: str) -> None:
    """用指定的 user_id 當作登入的使用者（不需要真的有這個帳號）。"""
    db.as_postgres()
    db.cur.execute(
        "select set_config('request.jwt.claims', %s, true)",
        (json.dumps({"sub": user_id, "role": "authenticated"}),),
    )
    db.cur.execute("set local role authenticated")


# ---------------------------------------------------------------------------
# 初始值、flag 表的權限
# ---------------------------------------------------------------------------


def test_production_defaults(db):
    """套用全部 migration 後 = 正式專案的預設值（PM 決定）：
    trip_plan 已上線維持全開（flag 只當 kill switch）；quick_target 關閉；
    web_push（手機推播，20261128021121）關閉，12/15 Demo 之後才開。"""
    db.as_anon()
    assert db.rows(
        "select flag, enabled, rollout, allowed_roles from public.feature_flags order by flag"
    ) == [
        ("quick_target", False, 0, ["admin", "beta"]),
        ("trip_plan", True, 100, ["admin", "beta"]),
        ("web_push", False, 0, ["admin", "beta"]),
    ]


def test_quick_target_is_off_for_everyone_by_default(db, alice, bob):
    """正式預設：連 admin、beta 都看不到 quick_target（enabled = false）。"""
    set_role(db, bob, "admin")
    for user in (alice, bob):
        db.as_user(user)
        assert not is_on(db, "quick_target")
        assert is_on(db, "trip_plan")


def test_everyone_can_read_flags_but_nobody_can_change_them_through_the_api(db, alice):
    for who in ("anon", "user"):
        db.as_anon() if who == "anon" else db.as_user(alice)
        assert db.one("select count(*) from public.feature_flags") == 3
        db.fails("update public.feature_flags set enabled = false", sqlstate=DENIED)
        db.fails("delete from public.feature_flags", sqlstate=DENIED)
        db.fails(
            "insert into public.feature_flags (flag, enabled) values ('evil', true)",
            sqlstate=DENIED,
        )


def test_flag_values_are_checked(db):
    db.as_postgres()
    db.fails("update public.feature_flags set rollout = 101", sqlstate=CHECK)
    db.fails("update public.feature_flags set rollout = -1", sqlstate=CHECK)
    db.fails("update public.feature_flags set allowed_roles = '{root}'", sqlstate=CHECK)


# ---------------------------------------------------------------------------
# is_feature_on()
# ---------------------------------------------------------------------------


def test_signed_out_is_off(db):  # TC8
    set_flag(db, "trip_plan", rollout=100)
    db.as_anon()
    assert not is_on(db, "trip_plan")


def test_unknown_flag_is_off(db, alice):
    db.as_user(alice)
    assert not is_on(db, "no_such_flag")


def test_kill_switch_turns_off_everyone_including_admin(db, alice, bob):  # TC2/TC4
    set_role(db, bob, "admin")
    set_flag(db, "trip_plan", rollout=100)
    for user in (alice, bob):
        db.as_user(user)
        assert is_on(db, "trip_plan")

    set_flag(db, "trip_plan", enabled=False, rollout=100)
    for user in (alice, bob):
        db.as_user(user)
        assert not is_on(db, "trip_plan")


@pytest.mark.parametrize(
    ("trip", "quick"), [(True, True), (True, False), (False, True), (False, False)]
)
def test_four_on_off_combinations_are_independent(db, alice, trip, quick):  # C1–C4
    set_flag(db, "trip_plan", enabled=trip, rollout=100)
    set_flag(db, "quick_target", enabled=quick, rollout=100)
    db.as_user(alice)
    assert (is_on(db, "trip_plan"), is_on(db, "quick_target")) == (trip, quick)


def test_beta_and_admin_are_on_at_rollout_0(db, alice, bob):  # TC6
    set_flag(db, "quick_target", rollout=0)
    db.as_user(alice)
    assert not is_on(db, "quick_target")
    for role in ("beta", "admin"):
        set_role(db, bob, role)
        db.as_user(bob)
        assert is_on(db, "quick_target"), role


def test_bucket_matches_python(db):  # TC5 + 和 src/fx_alert/flags.py 相同
    # bucket("trip_plan", "11111111-...") == 49（tests/test_flags.py）
    uid = "11111111-2222-3333-4444-555555555555"
    set_flag(db, "trip_plan", rollout=50)
    as_uid(db, uid)
    assert all(is_on(db, "trip_plan") for _ in range(10))
    set_flag(db, "trip_plan", rollout=49)
    as_uid(db, uid)
    assert not is_on(db, "trip_plan")


def test_rollout_ratio(db):
    set_flag(db, "quick_target", rollout=20)
    on = 0
    for _ in range(1000):
        as_uid(db, str(uuid.uuid4()))
        on += is_on(db, "quick_target")
    assert 140 < on < 260  # 20%，1000 人的誤差範圍


# ---------------------------------------------------------------------------
# 伺服器端檢查（TC3）：按鈕藏起來不算權限控管
# 用 restrictive policy 把 trips 的新增綁到 flag。原本的 "insert own trips" 是 permissive，
# 再加一條 permissive 是「或」的關係，擋不住；restrictive 才是「而且」。
# 這裡在交易裡建立 policy（測完 rollback）；正式的 policy 由 Task 2（Developer）的 migration 建立。
# ---------------------------------------------------------------------------


def add_trip_sql(db) -> tuple[str, tuple]:
    db.as_postgres()
    dep = db.one("select (now() at time zone 'Asia/Taipei')::date") + timedelta(days=40)
    return (
        "insert into public.trips (currency, departure, amount) values ('JPY', %s, 50000)",
        (dep,),
    )


def test_permissive_policy_does_not_block(db, alice):
    sql, params = add_trip_sql(db)
    # 正式的 restrictive policy（20261123000200）先在交易裡拿掉，只留 permissive 示範
    db.run('drop policy "trips only when trip_plan is on" on public.trips')
    db.run(
        "create policy flag_permissive on public.trips for insert to authenticated "
        "with check (public.is_feature_on('trip_plan'))"
    )
    set_flag(db, "trip_plan", enabled=False)
    db.as_user(alice)
    assert db.run(sql, params) == 1  # 還是新增成功：permissive 只是多一個「或」


def test_restrictive_policy_blocks_api_when_flag_is_off(db, alice):  # TC3
    sql, params = add_trip_sql(db)
    db.run(
        "create policy flag_restrictive on public.trips as restrictive for insert "
        "to authenticated with check (public.is_feature_on('trip_plan'))"
    )
    set_flag(db, "trip_plan", enabled=False)
    db.as_user(alice)
    db.fails(sql, params, sqlstate=DENIED)  # PostgREST 回 403

    set_flag(db, "trip_plan", enabled=True, rollout=100)
    db.as_user(alice)
    assert db.run(sql, params) == 1


# ---------------------------------------------------------------------------
# supabase/staging/ 的兩個腳本（在 staging 的 SQL Editor 手動執行，不是 migration）
# ---------------------------------------------------------------------------

STAGING = Path(__file__).resolve().parents[2] / "supabase" / "staging"


def flag_rows(db) -> list[tuple]:
    db.as_postgres()
    return db.rows(
        "select flag, enabled, rollout, allowed_roles from public.feature_flags order by flag"
    )


def test_staging_config_opens_new_features_to_admin_and_beta_only(db, alice, bob):
    db.as_postgres()
    db.cur.execute((STAGING / "feature_flags.sql").read_text("utf-8"))
    assert flag_rows(db) == [
        ("quick_target", True, 0, ["admin", "beta"]),
        ("trip_plan", True, 100, ["admin", "beta"]),
        ("web_push", True, 0, ["admin", "beta"]),
    ]
    set_role(db, bob, "beta")
    db.as_user(alice)
    assert not is_on(db, "quick_target")
    assert not is_on(db, "web_push")
    db.as_user(bob)
    assert is_on(db, "quick_target")
    assert is_on(db, "web_push")


def test_staging_evidence_script_passes_and_restores_flags(db):
    before = flag_rows(db)
    db.as_postgres()
    db.cur.execute((STAGING / "feature_flags_evidence.sql").read_text("utf-8"))
    while db.cur.nextset():  # 最後一段 select 才是結果表格
        pass
    results = db.cur.fetchall()
    assert [r[0] for r in results] == [
        "C1", "C2", "C3", "C4", "K1", "K2", "B1", "B2", "B3", "S1"
    ]  # fmt: skip
    assert all(r[-1] == "PASS" for r in results), results
    assert flag_rows(db) == before


def test_staging_scripts_are_not_migrations():
    """staging 腳本不能被部署 workflow 當成 migration 套用到正式專案。"""
    assert STAGING.is_dir()
    assert not (STAGING.parent / "migrations" / "feature_flags.sql").exists()
    for path in STAGING.glob("*.sql"):
        assert not path.name[:14].isdigit(), path
