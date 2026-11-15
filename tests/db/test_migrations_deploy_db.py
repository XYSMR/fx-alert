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
