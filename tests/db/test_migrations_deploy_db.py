"""事前檢查在真正的 Postgres 上：只回傳列數，而且是唯讀交易。"""

import os

import psycopg
import pytest

from fx_alert import migrations_deploy as md

DB_URL = os.environ["SUPABASE_DB_URL"]  # conftest 沒設定時整個資料夾不收集


@pytest.fixture
def conn():
    c = md.connect(DB_URL)
    yield c
    c.close()


def test_precheck_counts_rows(conn):
    assert md.run_precheck(conn, "select 1 where false") == 0
    assert md.run_precheck(conn, "select generate_series(1, 3)") == 3


def test_precheck_runs_read_only(conn):
    """事前檢查就算不小心寫了 update，也會被唯讀交易擋下，資料不會變。"""
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        md.run_precheck(conn, "update public.currencies set name_zh = name_zh")
    conn.rollback()


def test_real_prechecks_run_on_current_schema(conn):
    """repo 裡的每個事前檢查都能在目前的 schema 上執行（欄位名稱沒打錯）。"""
    for path in sorted(md.PRECHECKS_DIR.glob("*.sql")):
        assert md.run_precheck(conn, path.read_text(encoding="utf-8")) == 0, path.name


def test_baseline_object_checks_pass_on_current_schema(conn):
    """基準清單的每個版本，物件檢查在目前的 schema 上都通過。"""
    for version in md.read_baseline():
        path = md.baseline_check_path(version)
        assert md.run_object_check(conn, path.read_text(encoding="utf-8")) == [], path.name


@pytest.mark.parametrize(
    ("break_it", "expected"),
    [
        ("select cron.unschedule('fx-alert-dispatch')", "cron job fx-alert-dispatch"),
        ("drop function private.dispatch_check_rates()", "private.dispatch_check_rates()"),
    ],
)
def test_missing_object_is_reported(break_it, expected):
    """物件不存在（在一個最後 rollback 的交易裡刪掉）→ 檢查回報那一項，不會補紀錄。"""
    c = psycopg.connect(DB_URL)
    try:
        sql = md.baseline_check_path("20261110020000").read_text(encoding="utf-8")
        with c.cursor() as cur:
            cur.execute(break_it)
            cur.execute(sql)
            missing = md.failed_items(cur.fetchall())
        assert any(expected in item for item in missing), missing
    finally:
        c.rollback()
        c.close()


def test_check_query_error_counts_as_missing(conn):
    missing = md.run_object_check(conn, "select 'x' as item, true as ok from no_such_table")
    assert len(missing) == 1 and "檢查查詢出錯" in missing[0]


# ---------- apply：寫入（每個測試都在最後 rollback 的交易裡） ----------


@pytest.fixture
def wconn():
    c = md.connect(DB_URL, read_only=False)
    with c.cursor() as cur:  # 本機替身沒有這張表；CI 的 Supabase 已經有，這行什麼都不做
        cur.execute("create schema if not exists supabase_migrations")
        cur.execute(
            "create table if not exists supabase_migrations.schema_migrations "
            "(version text primary key, statements text[], name text)"
        )
    yield c
    c.rollback()
    c.close()


def test_apply_runs_sql_and_records_like_cli(wconn):
    sql = (
        "create table public.zz_deploy_test (id int);\n"
        "do $$ begin insert into public.zz_deploy_test values (1); end $$;\n"
    )
    assert md.history_has_statements(wconn)
    md.apply_migration(wconn, "29991231000000", "zz_test", sql, with_statements=True)
    with wconn.cursor() as cur:
        cur.execute("select count(*) from public.zz_deploy_test")
        assert cur.fetchone()[0] == 1
        cur.execute(
            "select name, statements from supabase_migrations.schema_migrations "
            "where version = '29991231000000'"
        )
        assert cur.fetchone() == ("zz_test", [sql])


def test_failed_migration_leaves_nothing(wconn):
    """migration 中途出錯：連同前面成功的語句和紀錄一起 rollback，不會只套用一半。"""
    sql = "create table public.zz_half (id int);\nselect 1 / 0;\n"
    with pytest.raises(psycopg.errors.DivisionByZero):
        with (
            wconn.transaction()
        ):  # savepoint：只 rollback 這個 migration（apply 是整個交易 rollback）
            md.apply_migration(wconn, "29991231000001", "zz_half", sql, with_statements=True)
    with wconn.cursor() as cur:
        cur.execute("select to_regclass('public.zz_half')")
        assert cur.fetchone()[0] is None
        cur.execute(
            "select count(*) from supabase_migrations.schema_migrations "
            "where version = '29991231000001'"
        )
        assert cur.fetchone()[0] == 0


def test_checks_stay_read_only_on_a_writable_connection(wconn):
    """apply 用可寫入的連線，但物件檢查、事前檢查仍然在唯讀交易裡。"""
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        md.run_precheck(wconn, "update public.currencies set name_zh = name_zh")
    wconn.rollback()
    assert md.run_object_check(wconn, "select 'x' as item, true as ok") == []


def test_baseline_check_before_24h_migration():
    """24 小時的 migration 還沒套用（舊的兩個工作都在）也通過；16:05 不見又沒併入 → 回報。"""
    c = psycopg.connect(DB_URL)
    try:
        sql = md.baseline_check_path("20261110020000").read_text(encoding="utf-8")
        with c.cursor() as cur:
            # 回到 24 小時之前的排程
            cur.execute(
                "select cron.schedule('fx-alert-dispatch', '5,35 1-7 * * 1-5', "
                "'select private.dispatch_check_rates()')"
            )
            cur.execute(
                "select cron.schedule('fx-alert-dispatch-1605', '5 8 * * 1-5', "
                "'select private.dispatch_check_rates()')"
            )
            cur.execute(sql)
            assert md.failed_items(cur.fetchall()) == []
            cur.execute("select cron.unschedule('fx-alert-dispatch-1605')")
            cur.execute(sql)
            assert md.failed_items(cur.fetchall()) == [
                "cron job fx-alert-dispatch-1605（或已併入 24 小時排程）"
            ]
    finally:
        c.rollback()
        c.close()
