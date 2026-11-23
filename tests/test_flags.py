import pytest

from fx_alert.flags import bucket, is_on

ON_20 = {"f": {"enabled": True, "rollout": 20, "allowed_roles": ["admin", "beta"]}}
USERS = [f"user-{i}" for i in range(10_000)]


def flags(enabled=True, rollout=20, allowed_roles=("admin", "beta")):
    return {"f": {"enabled": enabled, "rollout": rollout, "allowed_roles": list(allowed_roles)}}


# --- kill switch / 關閉的情況 -------------------------------------------------


def test_kill_switch_turns_off_everyone():
    off = flags(enabled=False, rollout=100)
    assert not any(is_on("f", u, off) for u in USERS[:1000])


def test_kill_switch_also_turns_off_admin_and_beta():
    off = flags(enabled=False, rollout=100)
    assert not is_on("f", "user-1", off, role="admin")
    assert not is_on("f", "user-1", off, role="beta")


def test_unknown_flag_is_off():
    assert not is_on("no_such_flag", "user-1", ON_20)


@pytest.mark.parametrize("user_id", [None, ""])
def test_signed_out_is_off(user_id):
    assert not is_on("f", user_id, flags(rollout=100))


def test_missing_rollout_defaults_to_off():
    assert not any(is_on("f", u, {"f": {"enabled": True}}) for u in USERS[:1000])


# --- rollout 分桶 -------------------------------------------------------------


def test_sticky_same_user_same_result():
    first = [is_on("f", u, ON_20) for u in USERS[:500]]
    for _ in range(10):  # TC5：重新整理 10 次結果都一樣
        assert [is_on("f", u, ON_20) for u in USERS[:500]] == first


def test_rollout_ratio_is_about_right():
    on = sum(is_on("f", u, ON_20) for u in USERS)
    assert 1800 < on < 2200  # 20% ± 2%


def test_rollout_0_and_100():
    assert not any(is_on("f", u, flags(rollout=0)) for u in USERS[:1000])
    assert all(is_on("f", u, flags(rollout=100)) for u in USERS[:1000])


def test_raising_rollout_keeps_existing_users_on():
    # 20% → 50%：原本開著的人不會被關掉（只會多人，不會換人）
    at_20 = {u for u in USERS if is_on("f", u, flags(rollout=20))}
    at_50 = {u for u in USERS if is_on("f", u, flags(rollout=50))}
    assert at_20 <= at_50


def test_flags_are_bucketed_independently():
    a = {u for u in USERS if bucket("trip_plan", u) < 20}
    b = {u for u in USERS if bucket("quick_target", u) < 20}
    assert a != b
    assert 200 < len(a & b) < 600  # 約 20% × 20% = 4%


def test_bucket_range():
    assert {bucket("f", u) for u in USERS} == set(range(100))


# --- role（TC6）---------------------------------------------------------------


@pytest.mark.parametrize("role", ["admin", "beta"])
def test_allowed_role_is_on_even_at_rollout_0(role):
    assert is_on("f", "user-1", flags(rollout=0), role=role)


def test_normal_user_role_does_not_bypass_rollout():
    assert not any(is_on("f", u, flags(rollout=0), role="user") for u in USERS[:1000])


# --- 和 SQL public.is_feature_on() 一致 ----------------------------------------
# 期望值是在 PostgreSQL 16 執行
#   select ('x' || substr(md5(flag || ':' || uid), 1, 8))::bit(32)::bigint % 100
# 得到的結果。改了任何一邊的算法，這裡就會失敗。


@pytest.mark.parametrize(
    ("flag", "user_id", "expected"),
    [
        ("trip_plan", "11111111-2222-3333-4444-555555555555", 49),
        ("trip_plan", "0b5e8c1a-7d3f-4e21-9a6b-2c4d8e0f1a3b", 96),
        ("trip_plan", "00000000-0000-0000-0000-000000000000", 97),
        ("quick_target", "ffffffff-0000-0000-0000-000000000000", 15),
        ("quick_target", "9f2c4e6a-1b3d-4f5a-8c7e-0d2b4a6c8e1f", 99),
    ],
)
def test_bucket_matches_postgres(flag, user_id, expected):
    assert bucket(flag, user_id) == expected
