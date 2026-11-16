"""排程去重（fx_alert.gate）：20 分鐘內已成功檢查過，自動觸發就略過。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from fx_alert import gate
from fx_alert.config import TAIPEI

NOW = datetime(2026, 11, 11, 1, 7, tzinfo=UTC)  # 台灣 09:07（GitHub 排程的時間）


def last(minutes_ago: float, ok: bool = True, tz: str = "+08:00") -> dict:
    at = (NOW - timedelta(minutes=minutes_ago)).astimezone(
        datetime.fromisoformat(f"2026-01-01T00:00:00{tz}").tzinfo
    )
    return {"at": at.isoformat(timespec="seconds"), "ok": ok, "errors": 0 if ok else 1}


AUTOMATED = [("schedule", ""), ("workflow_dispatch", "pg_cron")]


@pytest.mark.parametrize(("event", "source"), AUTOMATED)
def test_recent_success_skips_automated_runs(event, source):
    run, reason = gate.decide(last(2), NOW, event=event, source=source)
    assert run is False
    assert "略過" in reason and "不 ping 監控、不部署" in reason


@pytest.mark.parametrize(("event", "source"), AUTOMATED)
def test_window_boundary_is_20_minutes(event, source):
    assert gate.decide(last(20), NOW, event=event, source=source)[0] is False
    assert gate.decide(last(20.1), NOW, event=event, source=source)[0] is True
    assert gate.decide(last(30), NOW, event=event, source=source)[0] is True  # 下一個排程


@pytest.mark.parametrize("source", ["manual", "", "something"])
def test_manual_runs_always_check(source):
    run, reason = gate.decide(last(1), NOW, event="workflow_dispatch", source=source)
    assert run is True and "手動" in reason


@pytest.mark.parametrize(("event", "source"), AUTOMATED)
@pytest.mark.parametrize(
    "record",
    [
        None,
        {},
        {"at": "not a time", "ok": True},
        {"at": "2026-11-11T09:05:00", "ok": True},  # 沒有時區
        "just a string",
        [1, 2, 3],
    ],
)
def test_missing_or_bad_record_checks(event, source, record):
    assert gate.decide(record, NOW, event=event, source=source)[0] is True


@pytest.mark.parametrize(("event", "source"), AUTOMATED)
def test_recent_failure_checks_again(event, source):
    assert gate.decide(last(2, ok=False), NOW, event=event, source=source)[0] is True


def test_timezone_does_not_matter():
    for tz in ("+08:00", "+00:00", "-05:00"):
        assert gate.decide(last(5, tz=tz), NOW, event="schedule", source="")[0] is False


def test_small_clock_skew_counts_as_recent_but_far_future_does_not():
    assert gate.decide(last(-3), NOW, event="schedule", source="")[0] is False
    assert gate.decide(last(-60), NOW, event="schedule", source="")[0] is True


# ---------- CLI：寫 GITHUB_OUTPUT、永遠回傳 0 ----------


def run_cli(tmp_path, monkeypatch, record, *args):
    out = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    path = tmp_path / "last_check.json"
    if record is not None:
        path.write_text(record if isinstance(record, str) else json.dumps(record), "utf-8")
    code = gate.main(["--last-check", str(path), *args], now=NOW)
    lines = out.read_text("utf-8").splitlines() if out.exists() else []
    return code, dict(line.split("=", 1) for line in lines)


def test_cli_skip(tmp_path, monkeypatch):
    code, out = run_cli(tmp_path, monkeypatch, last(2), "--event", "schedule")
    assert code == 0 and out["run"] == "false" and "略過" in out["reason"]


def test_cli_run(tmp_path, monkeypatch):
    code, out = run_cli(
        tmp_path, monkeypatch, last(40), "--event", "workflow_dispatch", "--source", "pg_cron"
    )
    assert code == 0 and out["run"] == "true"


@pytest.mark.parametrize("content", [None, "{broken", ""])
def test_cli_missing_or_broken_file_checks(tmp_path, monkeypatch, content):
    code, out = run_cli(tmp_path, monkeypatch, content, "--event", "schedule")
    assert code == 0 and out["run"] == "true"


def test_cli_bad_arguments_still_exit_zero_and_check(tmp_path, monkeypatch):
    out = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    assert gate.main(["--no-such-flag"], now=NOW) == 0
    assert "run=true" in out.read_text("utf-8")


def test_cli_notice_in_actions(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    path = tmp_path / "last_check.json"
    path.write_text(json.dumps(last(2)), "utf-8")
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    assert gate.main(["--last-check", str(path), "--event", "schedule"], now=NOW) == 0
    assert "::notice::" in capsys.readouterr().out


def test_gate_uses_only_standard_library():
    """workflow 不安裝套件就直接執行 gate：不能 import 任何第三方套件或 fx_alert 其他模組。"""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(gate))
    mods = {
        (n.module if isinstance(n, ast.ImportFrom) else a.name).split(".")[0]
        for n in ast.walk(tree)
        if isinstance(n, ast.Import | ast.ImportFrom)
        for a in (n.names if isinstance(n, ast.Import) else [n])
    }
    allowed = {"__future__", "argparse", "json", "os", "sys", "datetime", "pathlib", "urllib"}
    assert mods <= allowed


# ---------- 讀 Supabase 的 check_status（讀不到就用 state 分支） ----------


def test_from_check_status():
    rows = [{"checked_at": "2026-11-16T01:05:00+00:00", "ok": True, "errors": 0}]
    assert gate.from_check_status(rows) == {
        "at": "2026-11-16T01:05:00+00:00",
        "ok": True,
        "errors": 0,
    }
    assert gate.from_check_status([]) is None  # 還沒有任何檢查
    assert gate.from_check_status({"message": "relation does not exist"}) is None
    assert gate.from_check_status([{"ok": True}]) is None


def test_newest_prefers_latest_record():
    supa = {"at": "2026-11-16T01:05:00+00:00", "ok": True}  # 09:05 台灣時間
    state = {"at": "2026-11-13T22:10:11+08:00", "ok": True}  # state 分支只在資料有變時更新
    assert gate.newest(supa, state) is supa
    assert gate.newest(None, state) is state
    assert gate.newest({"at": "壞掉"}, state) is state
    assert gate.newest(None, None) is None


def test_fetch_check_status_without_config_or_network_returns_none():
    assert gate.fetch_check_status("", "") is None
    # 連不到（保留網段，不會真的連出去）：回傳 None，不丟例外
    assert gate.fetch_check_status("http://192.0.2.1", "sb_publishable_x", timeout=0.2) is None


def test_main_uses_supabase_record(tmp_path, monkeypatch, capsys):
    """Supabase 說 5 分鐘前剛檢查過 → 略過；state 分支的舊紀錄不影響。"""
    old = tmp_path / "last_check.json"
    old.write_text('{"at": "2026-11-13T22:10:11+08:00", "ok": true, "errors": 0}', "utf-8")
    monkeypatch.setattr(
        gate,
        "fetch_check_status",
        lambda url, key: {"at": "2026-11-16T09:05:30+08:00", "ok": True, "errors": 0},
    )
    out = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    now = datetime(2026, 11, 16, 9, 7, tzinfo=TAIPEI)
    gate.main(["--last-check", str(old), "--event", "schedule"], now=now)
    assert "run=false" in out.read_text("utf-8")
    assert "Supabase check_status 讀得到" in capsys.readouterr().out


def test_main_falls_back_to_state_branch(tmp_path, monkeypatch, capsys):
    """Supabase 讀不到（例如專案暫停）：用 state 分支的紀錄；太舊就照常檢查。"""
    old = tmp_path / "last_check.json"
    old.write_text('{"at": "2026-11-13T22:10:11+08:00", "ok": true, "errors": 0}', "utf-8")
    monkeypatch.setattr(gate, "fetch_check_status", lambda url, key: None)
    out = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    gate.main(
        ["--last-check", str(old), "--event", "schedule"],
        now=datetime(2026, 11, 16, 9, 7, tzinfo=TAIPEI),
    )
    assert "run=true" in out.read_text("utf-8")
    assert "讀不到，改用 state 分支" in capsys.readouterr().out
