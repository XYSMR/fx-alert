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
        ("delete from cron.job where jobname = 'fx-alert-dispatch-1605'", "fx-alert-dispatch-1605"),
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
