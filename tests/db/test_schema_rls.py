"""多人版資料表與 RLS：每個人只能看、改自己的資料；狀態欄位只有排程能寫。"""

from __future__ import annotations

from decimal import Decimal as D

DENIED = "42501"  # 沒有權限，或違反 RLS 的 with check
UNIQUE = "23505"
FOREIGN_KEY = "23503"
CHECK = "23514"
GENERATED = "428C9"  # 不能寫入 generated column

USER_TABLES = ("profiles", "alert_rules", "notifications")


# ---------------------------------------------------------------------------
# 全域檢查
# ---------------------------------------------------------------------------


def test_every_public_table_has_rls(db):
    """以後新增的表忘了開 RLS，這個測試會抓到。"""
    db.as_postgres()
    missing = db.rows(
        "select c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace "
        "where n.nspname = 'public' and c.relkind = 'r' and not c.relrowsecurity"
    )
    assert missing == []


# ---------------------------------------------------------------------------
# 沒登入（anon）
# ---------------------------------------------------------------------------


def test_anon_can_read_currencies(db):
    db.as_anon()
    assert db.rows("select code from public.currencies order by code") == [("JPY",), ("KRW",)]


def test_anon_cannot_write_currencies(db):
    db.as_anon()
    db.fails("insert into public.currencies values ('USD', '美元')", sqlstate=DENIED)


def test_anon_cannot_read_personal_tables(db, alice):
    db.add_rule(alice, "JPY", "0.205")
    db.as_anon()
    for table in USER_TABLES:
        db.fails(f"select * from public.{table}", sqlstate=DENIED)


def test_authenticated_cannot_write_currencies(db, alice):
    db.as_user(alice)
    db.fails("insert into public.currencies values ('USD', '美元')", sqlstate=DENIED)


# ---------------------------------------------------------------------------
# profiles
# ---------------------------------------------------------------------------


def test_signup_creates_profile(db, alice):
    db.as_service()
    row = db.one(
        "select email, email_enabled, email_disabled_at from public.profiles where user_id = %s",
        (alice,),
    )
    assert row == ("alice@example.com", True, None)


def test_auth_email_change_syncs_profile(db, alice):
    db.as_postgres()
    db.run("update auth.users set email = 'alice.new@example.com' where id = %s", (alice,))
    db.as_service()
    assert db.one("select email from public.profiles where user_id = %s", (alice,)) == (
        "alice.new@example.com"
    )


def test_user_sees_only_own_profile(db, alice, bob):
    db.as_user(alice)
    assert db.rows("select email from public.profiles") == [("alice@example.com",)]


def test_user_can_toggle_own_email_enabled(db, alice):
    db.as_user(alice)
    assert db.run("update public.profiles set email_enabled = false") == 1
    assert db.one("select email_enabled from public.profiles") is False


def test_user_cannot_change_own_email(db, alice):
    """不能自己改 email，否則可以把通知導到別人的信箱。"""
    db.as_user(alice)
    db.fails("update public.profiles set email = 'evil@example.com'", sqlstate=DENIED)


def test_user_cannot_touch_other_profile(db, alice, bob):
    db.as_user(alice)
    sql = "update public.profiles set email_enabled = false where user_id = %s"
    assert db.run(sql, (bob,)) == 0
    db.as_service()
    assert db.one("select email_enabled from public.profiles where user_id = %s", (bob,)) is True


def test_user_cannot_insert_or_delete_profiles(db, alice, bob):
    db.as_user(alice)
    db.fails(
        "insert into public.profiles (user_id, email) values (gen_random_uuid(), 'x@example.com')",
        sqlstate=DENIED,
    )
    db.fails("delete from public.profiles", sqlstate=DENIED)


# ---------------------------------------------------------------------------
# alert_rules：讀寫自己的
# ---------------------------------------------------------------------------


def test_user_creates_rule_for_self(db, alice, bob):
    db.as_user(alice)
    db.run("insert into public.alert_rules (currency, target) values ('JPY', 0.205)")
    db.as_service()
    owner, state = db.one("select user_id, state from public.alert_rules where currency = 'JPY'")
    assert owner == alice
    assert state == "armed"


def test_user_cannot_create_rule_for_someone_else(db, alice, bob):
    db.as_user(alice)
    db.fails(
        "insert into public.alert_rules (user_id, currency, target) values (%s, 'JPY', 0.205)",
        (bob,),
        sqlstate=DENIED,
    )


def test_user_sees_only_own_rules(db, alice, bob):
    db.add_rule(alice, "JPY", "0.205")
    db.add_rule(bob, "JPY", "0.210")
    db.add_rule(bob, "KRW", "0.025")
    db.as_user(alice)
    assert db.rows("select currency, target from public.alert_rules") == [("JPY", D("0.205"))]
    db.as_user(bob)
    assert len(db.rows("select id from public.alert_rules")) == 2


def test_user_can_update_and_delete_own_rule(db, alice):
    db.add_rule(alice, "JPY", "0.205")
    db.as_user(alice)
    assert db.run("update public.alert_rules set target = 0.2, buffer = 0.002") == 1
    assert db.run("update public.alert_rules set enabled = false") == 1
    assert db.run("delete from public.alert_rules") == 1


def test_user_cannot_update_or_delete_other_rule(db, alice, bob):
    rule = db.add_rule(bob, "JPY", "0.210")
    db.as_user(alice)
    assert db.run("update public.alert_rules set target = 0.1 where id = %s", (rule,)) == 0
    assert db.run("delete from public.alert_rules where id = %s", (rule,)) == 0
    db.as_service()
    assert db.one("select target from public.alert_rules where id = %s", (rule,)) == D("0.210")


def test_user_cannot_move_rule_to_other_user(db, alice, bob):
    db.add_rule(alice, "JPY", "0.205")
    db.as_user(alice)
    db.fails("update public.alert_rules set user_id = %s", (bob,), sqlstate=DENIED)


def test_user_cannot_change_rule_currency(db, alice):
    """換幣別等於另一條規則，請刪掉再新增，避免沿用舊狀態。"""
    db.add_rule(alice, "JPY", "0.205")
    db.as_user(alice)
    db.fails("update public.alert_rules set currency = 'KRW'", sqlstate=DENIED)


# ---------------------------------------------------------------------------
# alert_rules：state_* 只有排程能寫
# ---------------------------------------------------------------------------


def test_user_cannot_write_state_columns(db, alice):
    db.add_rule(alice, "JPY", "0.205")
    db.as_user(alice)
    for col, value in [
        ("state", "'triggered'"),
        ("state_changed_at", "now()"),
        ("state_rate", "0.2"),
        ("state_target", "0.2"),
    ]:
        db.fails(f"update public.alert_rules set {col} = {value}", sqlstate=DENIED)


def test_user_cannot_insert_with_state(db, alice):
    db.as_user(alice)
    db.fails(
        "insert into public.alert_rules (currency, target, state) "
        "values ('JPY', 0.205, 'triggered')",
        sqlstate=DENIED,
    )


def test_service_role_can_write_state(db, alice):
    rule = db.add_rule(alice, "JPY", "0.205")
    db.as_service()
    n = db.run(
        "update public.alert_rules set state = 'triggered', state_changed_at = now(), "
        "state_rate = 0.204, state_target = 0.205 where id = %s",
        (rule,),
    )
    assert n == 1


# ---------------------------------------------------------------------------
# alert_rules：資料檢查
# ---------------------------------------------------------------------------


def test_one_rule_per_currency_per_user(db, alice, bob):
    db.add_rule(alice, "JPY", "0.205")
    db.add_rule(bob, "JPY", "0.205")  # 不同人可以
    db.as_user(alice)
    db.fails(
        "insert into public.alert_rules (currency, target) values ('JPY', 0.2)", sqlstate=UNIQUE
    )


def test_unknown_currency_rejected(db, alice):
    db.as_user(alice)
    db.fails(
        "insert into public.alert_rules (currency, target) values ('XXX', 1)", sqlstate=FOREIGN_KEY
    )


def test_target_must_be_positive(db, alice):
    db.as_user(alice)
    for bad in ("0", "-0.1"):
        db.fails(
            "insert into public.alert_rules (currency, target) values ('JPY', %s)",
            (bad,),
            sqlstate=CHECK,
        )


def test_buffer_limits(db, alice):
    db.as_user(alice)
    db.fails(
        "insert into public.alert_rules (currency, target, buffer) values ('JPY', 0.2, 0.021)",
        sqlstate=CHECK,
    )  # 超過目標價的 10%
    db.fails(
        "insert into public.alert_rules (currency, target, buffer) values ('JPY', 0.2, -0.001)",
        sqlstate=CHECK,
    )
    ok = "insert into public.alert_rules (currency, target, buffer) values ('JPY', 0.2, 0.02)"
    assert db.run(ok) == 1


def test_buffer_defaults_to_half_percent_and_follows_target(db, alice):
    db.as_user(alice)
    db.run("insert into public.alert_rules (currency, target) values ('JPY', 0.2)")
    assert db.one("select buffer, effective_buffer from public.alert_rules") == (None, D("0.001"))
    db.run("update public.alert_rules set target = 0.21")
    assert db.one("select effective_buffer from public.alert_rules") == D("0.00105")
    # 進階設定填了 buffer 就用填的
    db.run("update public.alert_rules set buffer = 0.003")
    assert db.one("select effective_buffer from public.alert_rules") == D("0.003")


def test_effective_buffer_cannot_be_written(db, alice):
    db.add_rule(alice, "JPY", "0.205")
    db.as_service()
    db.fails("update public.alert_rules set effective_buffer = 1", sqlstate=GENERATED)


# ---------------------------------------------------------------------------
# 重新開啟 → 重設為等待中（不補寄關閉期間的通知）
# ---------------------------------------------------------------------------

TRIGGERED = {
    "state": "triggered",
    "state_changed_at": "2026-11-01 10:00+08",
    "state_rate": "0.204",
    "state_target": "0.205",
}


def _rule_state(db, rule):
    db.as_service()
    return db.one(
        "select state, state_rate, state_target, state_changed_at is not null "
        "from public.alert_rules where id = %s",
        (rule,),
    )


def test_disabling_email_records_time_and_keeps_state(db, alice):
    rule = db.add_rule(alice, "JPY", "0.205", **TRIGGERED)
    db.as_user(alice)
    db.run("update public.profiles set email_enabled = false")
    assert db.one("select email_disabled_at is not null from public.profiles") is True
    # 關閉這個動作本身不改狀態；之後排程也不會碰它
    assert _rule_state(db, rule) == ("triggered", D("0.204"), D("0.205"), True)


def test_reenabling_email_resets_all_rules_to_armed(db, alice, bob):
    a_jpy = db.add_rule(alice, "JPY", "0.205", **TRIGGERED)
    a_krw = db.add_rule(alice, "KRW", "0.025", **TRIGGERED)
    b_jpy = db.add_rule(bob, "JPY", "0.205", **TRIGGERED)

    db.as_user(alice)
    db.run("update public.profiles set email_enabled = false")
    db.run("update public.profiles set email_enabled = true")

    assert db.one("select email_disabled_at from public.profiles") is None
    assert _rule_state(db, a_jpy) == ("armed", None, None, True)
    assert _rule_state(db, a_krw) == ("armed", None, None, True)
    # 別人的規則不受影響
    assert _rule_state(db, b_jpy) == ("triggered", D("0.204"), D("0.205"), True)


def test_setting_email_enabled_to_same_value_changes_nothing(db, alice):
    rule = db.add_rule(alice, "JPY", "0.205", **TRIGGERED)
    db.as_user(alice)
    db.run("update public.profiles set email_enabled = true")
    assert _rule_state(db, rule) == ("triggered", D("0.204"), D("0.205"), True)


def test_reenabling_rule_resets_it_to_armed(db, alice):
    rule = db.add_rule(alice, "JPY", "0.205", **TRIGGERED)
    other = db.add_rule(alice, "KRW", "0.025", **TRIGGERED)
    db.as_user(alice)
    db.run("update public.alert_rules set enabled = false where id = %s", (rule,))
    assert _rule_state(db, rule)[0] == "triggered"  # 停用本身不改狀態
    db.as_user(alice)
    db.run("update public.alert_rules set enabled = true where id = %s", (rule,))
    assert _rule_state(db, rule) == ("armed", None, None, True)
    assert _rule_state(db, other)[0] == "triggered"


def test_changing_target_keeps_state_for_rules_py(db, alice):
    """改目標價不在資料庫處理，由 rules.decide(prev_target=state_target) 判斷。"""
    rule = db.add_rule(alice, "JPY", "0.205", **TRIGGERED)
    db.as_user(alice)
    db.run("update public.alert_rules set target = 0.2")
    assert _rule_state(db, rule) == ("triggered", D("0.204"), D("0.205"), True)


# ---------------------------------------------------------------------------
# notifications
# ---------------------------------------------------------------------------


def test_user_sees_only_own_notifications(db, alice, bob):
    rule = db.add_rule(alice, "JPY", "0.205")
    db.add_notification(alice, rule, "JPY")
    db.add_notification(bob, None, "KRW")
    db.as_user(alice)
    assert db.rows("select currency from public.notifications") == [("JPY",)]


def test_user_cannot_write_notifications(db, alice):
    db.add_notification(alice, None, "JPY")
    db.as_user(alice)
    db.fails(
        "insert into public.notifications (user_id, currency, rate, target) "
        "values (%s, 'JPY', 0.2, 0.2)",
        (alice,),
        sqlstate=DENIED,
    )
    db.fails("update public.notifications set rate = 0", sqlstate=DENIED)
    db.fails("delete from public.notifications", sqlstate=DENIED)


def test_deleting_rule_keeps_notification_history(db, alice):
    rule = db.add_rule(alice, "JPY", "0.205")
    note = db.add_notification(alice, rule, "JPY")
    db.as_user(alice)
    db.run("delete from public.alert_rules")
    assert db.one("select rule_id from public.notifications where id = %s", (note,)) is None


# ---------------------------------------------------------------------------
# 刪除帳號：Edge Function 會刪 auth.users，相關資料全部連帶刪除
# ---------------------------------------------------------------------------


def test_deleting_auth_user_removes_all_personal_data(db, alice, bob):
    a_rule = db.add_rule(alice, "JPY", "0.205")
    db.add_notification(alice, a_rule, "JPY")
    b_rule = db.add_rule(bob, "JPY", "0.205")
    db.add_notification(bob, b_rule, "JPY")

    db.as_postgres()
    db.run("delete from auth.users where id = %s", (alice,))

    db.as_service()
    for table in USER_TABLES:
        left = db.one(f"select count(*) from public.{table} where user_id = %s", (alice,))
        assert left == 0, table
        kept = db.one(f"select count(*) from public.{table} where user_id = %s", (bob,))
        assert kept == 1, table


def test_user_cannot_delete_auth_users(db, alice):
    db.as_user(alice)
    db.fails("delete from auth.users", sqlstate=DENIED)
