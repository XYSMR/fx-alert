"""HDD 指標報表 supabase/reports/hdd_metrics.sql（issue #59）。

分母和組別看每人第一筆 exposed 事件（PR #54 review 後加入的事件）。

報表在 SQL Editor 以 postgres 身分執行；這裡放入測試資料，逐列核對算出來的數字和 PASS / FAIL。
測試在同一個交易裡執行，now() 固定，所以時間差都用「把 created_at 往前調」來做。
"""

from __future__ import annotations

import re
from pathlib import Path

import psycopg
import pytest

REPORT = Path(__file__).resolve().parents[2] / "supabase" / "reports" / "hdd_metrics.sql"
SINCE = "2000-01-01"  # 測試資料都在實驗期間內
READ_ONLY = "25006"  # read_only_sql_transaction


def report(db, since: str = SINCE) -> dict[tuple[str, str], tuple[str, str]]:
    """執行報表，回傳 {(metric, variant): (value, result)}。

    報表開頭把交易設成唯讀；放在 savepoint 裡執行、結束後還原，測試才能繼續放資料。
    """
    db.as_postgres()
    db.cur.execute("select set_config('hdd.since', %s, true)", (since,))
    db.cur.execute("savepoint report")
    db.cur.execute(REPORT.read_text("utf-8"))
    while db.cur.nextset():  # 最後一段 select 才是結果
        pass
    rows = db.cur.fetchall()
    db.cur.execute("rollback to savepoint report")
    return {(m, v): (value, result) for m, _flag, v, value, _target, result in rows}


def set_flag(db, flag: str, *, enabled: bool = True, rollout: int = 100) -> None:
    db.as_postgres()
    db.run(
        "update public.feature_flags set enabled = %s, rollout = %s where flag = %s",
        (enabled, rollout, flag),
    )


def event(db, user, name: str, flag: str, *, times: int = 1) -> None:
    db.as_user(user)
    for _ in range(times):
        db.run("insert into public.experiment_events (event, flag) values (%s, %s)", (name, flag))


def users(db, n: int, prefix: str = "u", *, exposed: str | None = None) -> list:
    """註冊 n 個使用者；exposed 給 flag 時，每人先記一筆 exposed（打開頁面）。"""
    us = [db.signup(f"{prefix}{i}@example.com") for i in range(n)]
    if exposed:
        for u in us:
            event(db, u, "exposed", exposed)
    return us


def signed_up_seconds_before_now(db, user, seconds: int) -> None:
    """模擬「第一次登入」在 seconds 秒前（第一個提醒在 now()）。"""
    db.as_postgres()
    db.run(
        "update public.profiles set created_at = now() - make_interval(secs => %s) "
        "where user_id = %s",
        (seconds, user),
    )


H1 = "H1 new users who created a trip"
H2 = "H2 median login -> first alert"
G2 = "Guardrail: alerts edited within 24 h"
G1 = "Guardrail: latest rate check"


# ---------------------------------------------------------------------------
# 沒有資料
# ---------------------------------------------------------------------------


def test_empty_database_reports_no_data(db):
    r = report(db)
    assert r[(H1, "on")] == ("0 / 0", "NO DATA")
    assert r[(H2, "on")] == ("- (0 of 0 new users)", "NO DATA")
    assert r[(G2, "on vs off")] == ("- vs -", "NO DATA")
    assert r[(G1, "-")] == ("no check yet", "NO DATA")


def test_report_blocks_writes_at_runtime(db):
    """報表後面被多貼一行寫入：整段在同一個唯讀交易裡，資料庫會拒絕（PR #60 review）。"""
    db.as_postgres()
    script = REPORT.read_text("utf-8") + "\nupdate public.feature_flags set enabled = false;"
    with pytest.raises(psycopg.Error) as exc, db.cur.connection.transaction():
        db.cur.execute(script)  # 沒有參數：報表裡的 % 不會被當成 placeholder
    assert exc.value.sqlstate == READ_ONLY, exc.value
    assert db.one("select enabled from public.feature_flags where flag = 'trip_plan'")


def test_report_is_read_only():
    """報表開頭設成唯讀，而且只有查詢；寫入語句只准出現在註解裡。"""
    code = "\n".join(line.split("--")[0] for line in REPORT.read_text("utf-8").splitlines()).lower()
    assert code.lstrip().startswith("set transaction read only;")
    for word in ("insert", "update", "delete", "truncate", "alter", "drop", "create", "grant"):
        assert not re.search(rf"\b{word}\b", code), word


# ---------------------------------------------------------------------------
# H1：ON 組建立旅程的比例
# ---------------------------------------------------------------------------


def test_h1_counts_distinct_users_not_rows(db):
    set_flag(db, "trip_plan", rollout=100)
    us = users(db, 10, exposed="trip_plan")
    for u in us[:3]:
        event(db, u, "trip_created", "trip_plan")
    event(db, us[0], "trip_created", "trip_plan", times=20)  # 同一人重複送，不灌水
    assert report(db)[(H1, "on")] == ("3 / 10 = 30.0%", "PASS")


def test_h1_below_threshold_fails(db):
    set_flag(db, "trip_plan", rollout=100)
    us = users(db, 10, exposed="trip_plan")
    for u in us[:2]:
        event(db, u, "trip_created", "trip_plan")
    assert report(db)[(H1, "on")] == ("2 / 10 = 20.0%", "FAIL")


def test_h1_needs_ten_exposed_users(db):
    set_flag(db, "trip_plan", rollout=100)
    us = users(db, 9, exposed="trip_plan")
    for u in us:
        event(db, u, "trip_created", "trip_plan")
    assert report(db)[(H1, "on")][1] == "NO DATA"


def test_h1_kill_switch_moves_everyone_to_off(db):
    set_flag(db, "trip_plan", enabled=False, rollout=100)
    users(db, 4, exposed="trip_plan")
    r = report(db)
    assert r[(H1, "on")] == ("0 / 0", "NO DATA")
    assert r[(H1, "off")] == ("0 / 4 = 0.0%", "INFO")


def test_users_who_never_opened_the_page_are_not_in_the_denominator(db):
    set_flag(db, "trip_plan", rollout=100)
    seen = users(db, 10, "seen", exposed="trip_plan")
    users(db, 30, "never")  # 沒有 exposed 事件
    for u in seen[:3]:
        event(db, u, "trip_created", "trip_plan")
    assert report(db)[(H1, "on")] == ("3 / 10 = 30.0%", "PASS")


def test_group_is_fixed_when_first_exposed(db):
    """先在 OFF 組被曝光，之後 rollout 改成 100%：仍算在 OFF 組（看第一筆 exposed）。"""
    set_flag(db, "trip_plan", enabled=False)
    us = users(db, 4, exposed="trip_plan")
    set_flag(db, "trip_plan", rollout=100)
    for u in us:
        event(db, u, "exposed", "trip_plan")
        event(db, u, "trip_created", "trip_plan")
    r = report(db)
    assert r[(H1, "off")] == ("4 / 4 = 100.0%", "INFO")
    assert r[(H1, "on")] == ("0 / 0", "NO DATA")


def test_h1_only_counts_users_who_signed_up_during_the_experiment(db):
    """舊使用者可能在實驗開始前就建過旅程，不能被算成「沒建立」（PR #60 review）。"""
    set_flag(db, "trip_plan", rollout=100)
    old = users(db, 5, "old", exposed="trip_plan")
    db.as_postgres()
    for u in old:
        db.run("update public.profiles set created_at = '1999-01-01' where user_id = %s", (u,))
    new = users(db, 10, "new", exposed="trip_plan")
    for u in new[:3]:
        event(db, u, "trip_created", "trip_plan")
    assert report(db)[(H1, "on")] == ("3 / 10 = 30.0%", "PASS")  # 不是 3 / 15 = 20% FAIL


def test_events_before_since_are_ignored(db):
    set_flag(db, "trip_plan", rollout=100)
    us = users(db, 10, exposed="trip_plan")
    for u in us[:5]:
        event(db, u, "trip_created", "trip_plan")
    assert report(db, since="2999-01-01")[(H1, "on")] == ("0 / 0", "NO DATA")


# ---------------------------------------------------------------------------
# H2：新使用者第一次登入到第一個提醒
# ---------------------------------------------------------------------------


def test_h2_median_of_first_alert_per_user(db):
    set_flag(db, "quick_target", rollout=100)
    us = users(db, 5, exposed="quick_target")
    for u, secs in zip(us, (10, 20, 30, 40, 200), strict=True):
        signed_up_seconds_before_now(db, u, secs)
        event(db, u, "alert_saved", "quick_target")
    # 中間那位之後又存了 5 個提醒（1 小時後）：只算第一個，中位數仍是 30 秒
    event(db, us[2], "alert_saved", "quick_target", times=5)
    db.as_postgres()
    db.run(
        "update public.experiment_events set created_at = now() + interval '1 hour' "
        "where user_id = %s and event = 'alert_saved' and id > (select min(id) "
        "from public.experiment_events where user_id = %s and event = 'alert_saved')",
        (us[2], us[2]),
    )
    assert report(db)[(H2, "on")] == ("30.0 s (5 of 5 new users)", "PASS")


def test_h2_slow_median_fails(db):
    set_flag(db, "quick_target", rollout=100)
    for u in users(db, 5, exposed="quick_target"):
        signed_up_seconds_before_now(db, u, 90)
        event(db, u, "alert_saved", "quick_target")
    assert report(db)[(H2, "on")] == ("90.0 s (5 of 5 new users)", "FAIL")


def test_h2_only_counts_users_who_signed_up_during_the_experiment(db):
    set_flag(db, "quick_target", rollout=100)
    old = users(db, 3, "old", exposed="quick_target")
    db.as_postgres()
    for u in old:
        db.run("update public.profiles set created_at = '1999-01-01' where user_id = %s", (u,))
    users(db, 2, "new", exposed="quick_target")
    assert report(db)[(H2, "on")] == ("- (0 of 2 new users)", "NO DATA")


def test_team_accounts_are_not_counted(db):
    """admin、beta 永遠在 ON 組，是組員的測試帳號：不算進分母，也不算進分子（PR #60 review）。"""
    set_flag(db, "trip_plan", rollout=100)
    real = users(db, 10, "real", exposed="trip_plan")
    team = users(db, 5, "team")
    db.as_postgres()
    for u, role in zip(team, ("beta", "beta", "beta", "admin", "admin"), strict=True):
        db.run("update public.profiles set role = %s where user_id = %s", (role, u))
    for u in team:
        event(db, u, "exposed", "trip_plan")
        event(db, u, "trip_created", "trip_plan")
    event(db, real[0], "trip_created", "trip_plan")
    assert report(db)[(H1, "on")] == ("1 / 10 = 10.0%", "FAIL")  # 不是 6 / 15 = 40% PASS


def test_beta_testing_alone_cannot_pass_h2(db):
    """rollout 0% 時只有 beta 在 ON 組；他們存提醒再快也不會出現在報表裡。"""
    set_flag(db, "quick_target", rollout=0)
    team = users(db, 5, "team")
    db.as_postgres()
    for u in team:
        db.run("update public.profiles set role = 'beta' where user_id = %s", (u,))
    for u in team:
        event(db, u, "exposed", "quick_target")
        signed_up_seconds_before_now(db, u, 10)
        event(db, u, "alert_saved", "quick_target")
    assert report(db)[(H2, "on")] == ("- (0 of 0 new users)", "NO DATA")


# ---------------------------------------------------------------------------
# Guardrails
# ---------------------------------------------------------------------------


def rules(db, owners, *, edited: int, by_scheduler: int = 0) -> None:
    """每人一個提醒；前 edited 個「使用者改過」，接著 by_scheduler 個只被排程改過。"""
    for i, u in enumerate(owners):
        rule = db.add_rule(u, "JPY", "0.2")
        db.as_postgres()
        if i < edited:  # 建立 1 小時後被修改（trigger 把 updated_at 設成 now()）
            db.run(
                "update public.alert_rules set created_at = now() - interval '1 hour' "
                "where id = %s",
                (rule,),
            )
        elif i < edited + by_scheduler:  # 排程改 state：state_changed_at 和 updated_at 同時
            db.run(
                "update public.alert_rules set created_at = now() - interval '1 hour', "
                "state = 'triggered', state_changed_at = now() where id = %s",
                (rule,),
            )


def split_groups(db, n_on: int, n_off: int):
    """一般使用者分成兩組：rollout 100% 時曝光的在 ON 組，改成 0% 後才曝光的在 OFF 組。"""
    set_flag(db, "quick_target", rollout=100)
    on = users(db, n_on, "on", exposed="quick_target")
    set_flag(db, "quick_target", rollout=0)
    off = users(db, n_off, "off", exposed="quick_target")
    return on, off


def test_edited_alerts_guardrail_passes_when_on_is_not_higher(db):
    on, off = split_groups(db, 5, 5)
    rules(db, on, edited=1)
    rules(db, off, edited=2)
    assert report(db)[(G2, "on vs off")] == ("20.0% vs 40.0%", "PASS")


def test_edited_alerts_guardrail_fails_when_on_is_higher(db):
    on, off = split_groups(db, 5, 5)
    rules(db, on, edited=3)
    rules(db, off, edited=1)
    assert report(db)[(G2, "on vs off")] == ("60.0% vs 20.0%", "FAIL")


def test_scheduler_state_changes_are_not_user_edits(db):
    on, off = split_groups(db, 5, 5)
    rules(db, on, edited=0, by_scheduler=5)
    rules(db, off, edited=0)
    assert report(db)[(G2, "on vs off")] == ("0.0% vs 0.0%", "PASS")


def test_latest_check_guardrail(db):
    db.as_postgres()
    db.run("insert into public.check_status (checked_at, ok, errors) values (now(), true, 0)")
    assert report(db)[(G1, "-")][1] == "PASS"
    db.run("update public.check_status set ok = true, errors = 2")
    assert report(db)[(G1, "-")][1] == "FAIL"
