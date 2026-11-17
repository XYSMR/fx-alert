"""migration 部署（staging／正式專案）：計畫、事前檢查檔、基準清單（不需要資料庫）。"""

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
    assert md.main(["push"]) == 2


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
    """正式專案和 staging 的基準清單共用 supabase/baseline_checks/。"""
    baseline = set(md.read_baseline()) | set(md.read_baseline(md.STAGING_BASELINE_FILE))
    files = sorted(md.BASELINE_CHECKS_DIR.glob("*.sql"))
    versions = [f.name[:14] for f in files]
    assert versions == sorted(baseline), "基準清單的每個版本都要有一個、而且只有一個物件檢查"
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


# ---------- apply ----------


def test_migration_file_and_name():
    path = md.migration_file("20261116000000")
    assert path.name == "20261116000000_currency_catalog.sql"
    assert md.migration_name(path) == "currency_catalog"
    with pytest.raises(DeployError, match="找不到"):
        md.migration_file("29991231000000")


def test_expected_plan_from_workflow(monkeypatch):
    monkeypatch.delenv("EXPECT_REPAIR", raising=False)
    monkeypatch.delenv("EXPECT_PENDING", raising=False)
    assert md.expected_plan() is None
    monkeypatch.setenv("EXPECT_REPAIR", "20261110020000 20261111000000")
    monkeypatch.setenv("EXPECT_PENDING", "")
    assert md.expected_plan() == Plan(("20261110020000", "20261111000000"), ())


def test_apply_on_actions_requires_the_plan(monkeypatch, capsys):
    """在 Actions 上沒有第 1 步的計畫就不寫入（避免手動跑 apply 跳過檢查）。"""
    monkeypatch.setenv("DB_URL", "postgresql://u:p@127.0.0.1:1/x")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.delenv("EXPECT_REPAIR", raising=False)
    monkeypatch.delenv("EXPECT_PENDING", raising=False)
    assert md.main(["apply"]) == 1
    assert "第 1 步的計畫" in capsys.readouterr().out


# ---------- staging ----------


def test_staging_baseline_is_the_seven_manual_versions():
    """staging 只有前 5 筆紀錄（2026-11-17 唯讀查詢），後面 7 個是手動套用、沒有紀錄的。"""
    staging = md.read_baseline(md.STAGING_BASELINE_FILE)
    local = md.local_versions()
    assert staging == [
        "20261110020000", "20261111000000", "20261116000000", "20261116040000",
        "20261116142000", "20261116153000", "20261116195500",
    ]  # fmt: skip
    assert set(staging) <= set(local)
    # 前 5 個 + 這 7 個 = 目前 repo 的全部（之後新增的交給 workflow 套用）
    assert local[:12] == local[:5] + staging


def test_target_and_baseline_file(monkeypatch):
    monkeypatch.delenv("FX_DEPLOY_TARGET", raising=False)
    assert md.target() == "production" and md.baseline_file() == md.BASELINE_FILE
    monkeypatch.setenv("FX_DEPLOY_TARGET", "staging")
    assert md.target() == "staging" and md.baseline_file() == md.STAGING_BASELINE_FILE
    monkeypatch.setenv("FX_DEPLOY_TARGET", "prod")
    with pytest.raises(DeployError, match="staging 或 production|production 或 staging"):
        md.target()


def test_missing_db_url_hint_names_the_staging_secret(monkeypatch, capsys):
    monkeypatch.delenv("DB_URL", raising=False)
    monkeypatch.setenv("FX_DEPLOY_TARGET", "staging")
    assert md.main(["plan"]) == 1
    out = capsys.readouterr().out
    assert "STAGING_DB_URL" in out and "PRODUCTION_DB_URL" not in out


def test_staging_allows_unknown_versions_but_production_does_not():
    """還沒合併的 PR 先套用到 staging：staging 只警告，其他版本照常套用。"""
    pr = "20261120000000"
    plan = make_plan([V1, V2, V3], recorded={V1, V2, pr}, baseline=[], allow_unknown=True)
    assert plan == Plan(repair=(), pending=(V3,), unknown=(pr,))
    with pytest.raises(DeployError, match="repo 沒有的版本"):
        make_plan([V1, V2, V3], recorded={V1, V2, pr}, baseline=[])


def test_unknown_newer_version_does_not_block_older_pending():
    """PR 的版本號比 main 新合併的還新：順序檢查不算 staging 多出來的版本。"""
    pr = "20261130000000"
    plan = make_plan([V1, V4], recorded={V1, pr}, baseline=[], allow_unknown=True)
    assert plan.pending == (V4,) and plan.unknown == (pr,)


def test_expected_plan_compares_only_write_steps():
    a = Plan(("x",), ("y",), unknown=("z",))
    assert a.steps() == Plan(("x",), ("y",)).steps()


def test_changed_versions_compares_saved_sql_with_repo():
    v = "20261116195500"
    current = md.migration_file(v).read_text(encoding="utf-8")
    local = md.local_versions()
    since = "20261110020000"
    assert md.changed_versions({v: [current]}, local, since) == []
    assert md.changed_versions({v: [current + "-- 改過\n"]}, local, since) == [v]
    # Supabase CLI 寫的舊紀錄（拆成多段）不比
    assert md.changed_versions({"20261109000000": ["a", "b"]}, local, since) == []
    assert md.changed_versions({v: ["x"]}, local, None) == []
    assert md.changed_versions({}, local, since) == []  # 還沒套用的不算
