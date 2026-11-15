"""只有資料有變（或成功／失敗改變）才 commit state 分支、部署網頁。"""

import subprocess

from fx_alert import publish

OK = {"at": "2026-11-16T09:35:00+08:00", "ok": True, "errors": 0}
OK_BEFORE = {"at": "2026-11-16T09:05:00+08:00", "ok": True, "errors": 0}
FAIL = {"at": "2026-11-16T09:35:00+08:00", "ok": False, "errors": 1}


def test_new_quotes_are_published():
    ok, reason = publish.decide(["history/2026-11-16.csv", "last_check.json"], OK, OK_BEFORE)
    assert ok is True and "history/2026-11-16.csv" in reason


def test_only_last_check_time_is_not_published():
    ok, reason = publish.decide(["last_check.json"], OK, OK_BEFORE)
    assert ok is False and "不 commit、不部署" in reason


def test_nothing_changed():
    assert publish.decide([], OK, OK)[0] is False


def test_failure_and_recovery_are_published():
    assert publish.decide(["last_check.json"], FAIL, OK_BEFORE)[0] is True
    assert publish.decide(["last_check.json"], OK, FAIL)[0] is True
    assert publish.decide(["last_check.json"], FAIL, FAIL)[0] is False  # 一直失敗：不必每次部署


def test_first_commit_is_published():
    assert publish.decide(["last_check.json"], OK, None)[0] is True


def test_history_conversion_is_published():
    changed = ["history/2026-11.csv", "history/2026-11-09.csv", "history/2026-11-10.csv"]
    assert publish.decide(changed, OK, OK_BEFORE)[0] is True


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_main_reads_git_index(tmp_path, monkeypatch, capsys):
    """在真的 git repo 裡：只改 last_check.json 的時間 → false；加了走勢檔 → true。"""
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "bot@invalid")
    _git(tmp_path, "config", "user.name", "bot")
    (tmp_path / "last_check.json").write_text(
        '{"at": "2026-11-16T09:05:00+08:00", "ok": true, "errors": 0}', "utf-8"
    )
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "first")
    monkeypatch.chdir(tmp_path)

    (tmp_path / "last_check.json").write_text(
        '{"at": "2026-11-16T09:35:00+08:00", "ok": true, "errors": 0}', "utf-8"
    )
    _git(tmp_path, "add", "-A")
    publish.main()
    assert capsys.readouterr().out.strip() == "false"

    (tmp_path / "history").mkdir()
    (tmp_path / "history" / "2026-11-16.csv").write_text("quoted_at,currency,cash_sell\n", "utf-8")
    _git(tmp_path, "add", "-A")
    publish.main()
    assert capsys.readouterr().out.strip() == "true"


def test_publish_uses_only_standard_library():
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(publish))
    mods = {
        (n.module if isinstance(n, ast.ImportFrom) else a.name).split(".")[0]
        for n in ast.walk(tree)
        if isinstance(n, ast.Import | ast.ImportFrom)
        for a in (n.names if isinstance(n, ast.Import) else [n])
    }
    assert mods <= {"__future__", "json", "subprocess", "sys"}
