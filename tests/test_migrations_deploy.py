"""正式專案 migration 部署：計畫、事前檢查檔、基準清單（不需要資料庫）。"""

import re

import pytest

from fx_alert import migrations_deploy as md
from fx_alert.migrations_deploy import DeployError, Plan, make_plan

V1, V2, V3, V4 = "20261109000000", "20261110000000", "20261111000000", "20261116000000"


def test_plan_repairs_baseline_and_pushes_new():
    """正式專案現況：前兩筆有紀錄、第三筆手動套用沒紀錄、第四筆是新的。"""
    plan = make_plan([V1, V2, V3, V4], recorded={V1, V2}, baseline=[V1, V2, V3])
    assert plan == Plan(repair=(V3,), pending=(V4,))


def test_plan_nothing_to_do():
    assert make_plan([V1, V2], recorded={V1, V2}, baseline=[V1]) == Plan((), ())


def test_plan_refuses_unknown_remote_versions():
    with pytest.raises(DeployError, match="repo 沒有的版本：20261120000000"):
        make_plan([V1], recorded={V1, "20261120000000"}, baseline=[])


def test_plan_refuses_out_of_order_pending():
    """待套用的版本比已套用的還舊：可能是分支合併順序錯了，不自動套用。"""
    with pytest.raises(DeployError, match="順序可能錯亂"):
        make_plan([V1, V2, V3], recorded={V1, V3}, baseline=[])


def test_plan_refuses_baseline_not_in_repo():
    with pytest.raises(DeployError, match="基準清單"):
        make_plan([V1], recorded=set(), baseline=[V1, V2])


# ---------- repo 裡的檔案 ----------


def test_real_migrations_and_baseline_are_consistent():
    local = md.local_versions()
    baseline = md.read_baseline()
    assert baseline == local[: len(baseline)]  # 基準清單是最早的那幾個，順序一致
    # 2026-11-16 確認過的 7 個版本；之後的 migration 一律交給 workflow，不加進基準清單
    assert baseline[-1] == "20261111000000" and len(baseline) == 7


def test_every_precheck_matches_a_migration_and_is_select_only():
    local = set(md.local_versions())
    for path in md.PRECHECKS_DIR.glob("*.sql"):
        m = re.match(r"^(\d{14})_[a-z0-9_]+\.sql$", path.name)
        assert m and m.group(1) in local, f"{path.name} 沒有對應的 migration"
        sql = re.sub(r"--.*", "", path.read_text(encoding="utf-8")).strip().lower()
        assert sql.startswith(("select", "with")), f"{path.name} 必須是一段 select"
        assert ";" not in sql.rstrip(";"), f"{path.name} 只能有一段 SQL"
        for word in ("insert", "update", "delete", "drop", "alter", "truncate", "grant"):
            assert not re.search(rf"\b{word}\b", sql), f"{path.name} 不能有 {word}"


def test_read_baseline_rejects_bad_lines(tmp_path):
    p = tmp_path / "b.txt"
    p.write_text("# 說明\n20261109000000  # 註解\n\n", encoding="utf-8")
    assert md.read_baseline(p) == ["20261109000000"]
    p.write_text("2026-11-09\n", encoding="utf-8")
    with pytest.raises(DeployError, match="不是版本號"):
        md.read_baseline(p)


def test_local_versions_rejects_bad_names(tmp_path):
    (tmp_path / "add_table.sql").write_text("select 1", encoding="utf-8")
    with pytest.raises(DeployError, match="檔名格式不對"):
        md.local_versions(tmp_path)


def test_precheck_path(tmp_path):
    (tmp_path / f"{V4}_currency_catalog.sql").write_text("select 1", encoding="utf-8")
    assert md.precheck_path(V4, tmp_path).name == f"{V4}_currency_catalog.sql"
    assert md.precheck_path(V3, tmp_path) is None


# ---------- 連線字串不外洩 ----------


def test_missing_db_url_gives_setup_hint(monkeypatch, capsys):
    monkeypatch.delenv("DB_URL", raising=False)
    assert md.main(["plan"]) == 1
    assert "PRODUCTION_DB_URL" in capsys.readouterr().out


def test_password_is_masked_in_actions_log(monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    md.mask_password("postgresql://postgres.abc:p%40ss@aws-0.pooler.supabase.com:5432/postgres")
    out = capsys.readouterr().out.splitlines()
    assert "::add-mask::p%40ss" in out and "::add-mask::p@ss" in out


def test_bad_command():
    assert md.main(["apply"]) == 2


class _FakeCursor:
    def __init__(self, exists, rows):
        self.exists, self.rows, self.queries = exists, rows, []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql):
        self.queries.append(sql)

    def fetchone(self):
        return (self.exists,)

    def fetchall(self):
        return [(v,) for v in self.rows]


class _FakeConn:
    def __init__(self, cur):
        self.cur = cur

    def cursor(self):
        return self.cur


def test_recorded_versions_requires_supabase_history_table():
    assert md.recorded_versions(_FakeConn(_FakeCursor(True, [V1, V2]))) == {V1, V2}
    with pytest.raises(DeployError, match="schema_migrations"):
        md.recorded_versions(_FakeConn(_FakeCursor(False, [])))


# ---------- 補紀錄前：物件必須真的存在 ----------


def test_every_baseline_version_has_one_object_check():
    baseline = md.read_baseline()
    files = sorted(md.BASELINE_CHECKS_DIR.glob("*.sql"))
    versions = [f.name[:14] for f in files]
    assert versions == baseline, "基準清單的每個版本都要有一個、而且只有一個物件檢查"
    for path in files:
        sql = re.sub(r"--.*", "", path.read_text(encoding="utf-8")).strip().lower()
        assert sql.startswith(("select", "with")), f"{path.name} 必須是一段 select"
        assert ";" not in sql.rstrip(";"), f"{path.name} 只能有一段 SQL"
        for word in ("insert", "update", "delete", "drop", "alter", "truncate", "grant"):
            assert not re.search(rf"\b{word}\b", sql), f"{path.name} 不能有 {word}"
        assert " as item" in sql and " as ok" in sql, f"{path.name} 要回傳 (item, ok)"


def test_pg_cron_object_check_covers_function_and_both_jobs():
    sql = md.baseline_check_path("20261110020000").read_text(encoding="utf-8")
    for name in (
        "private.dispatch_check_rates()",
        "private.dispatch_inputs(text)",
        "jobname = 'fx-alert-dispatch'",
        "jobname = 'fx-alert-dispatch-1605'",
        "net",
    ):
        assert name in sql, name


def test_failed_items():
    assert md.failed_items([("a", True), ("b", True)]) == []
    assert md.failed_items([("a", True), ("cron job x", False), ("c", None)]) == ["cron job x", "c"]
    assert md.failed_items([]) == ["（檢查沒有回傳任何一列）"]  # 沒有結果不能當作通過
