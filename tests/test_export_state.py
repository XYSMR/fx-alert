"""export-state：多人版 → 單人版的反向匯出（退回方案用）。"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from decimal import Decimal as D

import pytest

from fx_alert import main as main_mod
from fx_alert.config import TAIPEI, AppConfig, BusinessHours
from fx_alert.export_state import describe_export, plan_export, write_export
from fx_alert.history import ALERTS_FILE, append_alert, read_alerts
from fx_alert.migrate import Plan, PlannedNotification, PlannedRule, collect
from fx_alert.rules import AlertState, Rule
from fx_alert.state_store import load_states, save_states, with_transition
from fx_alert.store import FileStore

URL = "https://abcdefghijklmnopqrst.supabase.co"
KEY = "sb_secret_TESTONLY"
UID = "00000000-0000-4000-8000-00000000000a"
T0 = datetime(2026, 11, 5, 10, 7, tzinfo=TAIPEI)


def make_cfg(tmp_path):
    return AppConfig(
        data_dir=tmp_path / "data",
        hours=BusinessHours(weekdays_only=True, start=time(9, 0), end=time(16, 30)),
        rules=(
            Rule("JPY", target=D("0.205"), buffer=D("0.001")),
            Rule("KRW", target=D("0.0250"), buffer=D("0.0002")),
        ),
    )


def single_mode_files(cfg):
    """用單人版自己的寫法（with_transition + append_alert）做出 state.json、alerts.csv。"""
    states = with_transition({}, "JPY", AlertState.TRIGGERED, D("0.2040"), T0, target=D("0.205"))
    states = with_transition(
        states, "KRW", AlertState.ARMED, D("0.0264"), T0 + timedelta(days=4), target=D("0.0250")
    )
    save_states(cfg.state_file, states)
    append_alert(cfg.data_dir, T0, "JPY", D("0.2040"), D("0.205"))


def snapshot(cfg):
    return (cfg.state_file.read_bytes(), (cfg.data_dir / ALERTS_FILE).read_bytes())


def watches(cfg):
    return [
        (w.key, w.state, w.state_target, w.rule.target) for w in FileStore(cfg, []).load_watches()
    ]


def rule(**kw):
    base = {
        "currency": "JPY",
        "target": D("0.205"),
        "buffer": D("0.001"),
        "state": "armed",
        "state_changed_at": None,
        "state_rate": None,
        "state_target": None,
    }
    return PlannedRule(**{**base, **kw})


# ===========================================================================
# 來回一趟：單人版檔案 → 搬進資料庫 → 匯出 → 單人版看到的一模一樣
# ===========================================================================


def test_round_trip_is_byte_identical(tmp_path):
    cfg = make_cfg(tmp_path)
    single_mode_files(cfg)
    before, before_watches = snapshot(cfg), watches(cfg)

    db = collect(cfg, cfg.data_dir)  # 搬家時寫進資料庫的內容 = 資料庫讀回來的內容
    plan = plan_export(db, cfg, cfg.data_dir)
    assert not plan.has_changes
    write_export(plan, cfg.data_dir)

    assert snapshot(cfg) == before
    assert watches(cfg) == before_watches


def test_round_trip_survives_utc_timestamps_and_trailing_zeros(tmp_path):
    """資料庫回傳 UTC 時間、數字多了尾端的 0：仍然視為相同，不改檔案。"""
    cfg = make_cfg(tmp_path)
    single_mode_files(cfg)
    before = snapshot(cfg)
    db = collect(cfg, cfg.data_dir)
    utc_rules = tuple(
        PlannedRule(
            r.currency,
            r.target,
            r.buffer,
            r.state,
            r.state_changed_at.astimezone(UTC),
            r.state_rate,
            r.state_target,
        )  # fmt: skip
        for r in db.rules
    )
    utc_notes = tuple(
        PlannedNotification(n.currency, D(f"{n.rate}00"), n.target, n.sent_at.astimezone(UTC))
        for n in db.notifications
    )
    plan = plan_export(Plan(utc_rules, utc_notes), cfg, cfg.data_dir)
    assert not plan.has_changes
    write_export(plan, cfg.data_dir)
    assert snapshot(cfg) == before


# ===========================================================================
# 多人版期間有變化 → 寫回檔案，單人版不會重複寄信
# ===========================================================================


def test_trigger_during_multi_user_period_is_written_back(tmp_path):
    cfg = make_cfg(tmp_path)
    single_mode_files(cfg)
    sent = datetime(2026, 11, 20, 2, 30, 15, 123456, tzinfo=UTC)  # 資料庫有微秒
    db = Plan(
        rules=(
            rule(
                currency="JPY",
                state="triggered",
                state_changed_at=T0,
                state_rate=D("0.2040"),
                state_target=D("0.205"),
            ),
            rule(
                currency="KRW",
                target=D("0.0250"),
                buffer=D("0.0002"),
                state="triggered",
                state_changed_at=sent,
                state_rate=D("0.0248"),
                state_target=D("0.0250"),
            ),
        ),
        notifications=(
            PlannedNotification("JPY", D("0.2040"), D("0.205"), T0),  # 搬家時帶過去的
            PlannedNotification("KRW", D("0.0248"), D("0.0250"), sent),  # 多人版寄的
        ),
    )
    plan = plan_export(db, cfg, cfg.data_dir)
    assert plan.state_changes == ("KRW：armed → triggered（2026-11-20T10:30:15+08:00）",)
    assert [n.currency for n in plan.new_alerts] == ["KRW"]
    write_export(plan, cfg.data_dir)

    krw = load_states(cfg.state_file)["KRW"]
    assert (krw.state, krw.changed_at, krw.rate, krw.target) == (
        AlertState.TRIGGERED,
        "2026-11-20T10:30:15+08:00",
        "0.0248",
        "0.0250",
    )
    # 單人版讀到的是「已通知」→ 下次檢查不會再寄
    assert ("KRW", AlertState.TRIGGERED, D("0.0250"), D("0.0250")) in watches(cfg)
    assert len(read_alerts(cfg.data_dir)) == 2


def test_running_twice_changes_nothing(tmp_path):
    cfg = make_cfg(tmp_path)
    single_mode_files(cfg)
    sent = datetime(2026, 11, 20, 2, 30, 15, 999999, tzinfo=UTC)
    db = Plan(
        rules=(
            rule(
                state="armed",
                state_changed_at=sent,
                state_rate=D("0.2070"),
                state_target=D("0.205"),
            ),  # fmt: skip
        ),
        notifications=(PlannedNotification("JPY", D("0.2040"), D("0.205"), sent),),
    )
    write_export(plan_export(db, cfg, cfg.data_dir), cfg.data_dir)
    after_first = snapshot(cfg)

    second = plan_export(db, cfg, cfg.data_dir)
    assert not second.has_changes
    write_export(second, cfg.data_dir)
    assert snapshot(cfg) == after_first
    assert len(read_alerts(cfg.data_dir)) == 2


def test_duplicate_notifications_in_db_are_written_once(tmp_path):
    cfg = make_cfg(tmp_path)
    single_mode_files(cfg)
    at = datetime(2026, 11, 21, 1, 0, 0, tzinfo=UTC)
    note = PlannedNotification("JPY", D("0.2030"), D("0.205"), at)
    same_second = PlannedNotification("JPY", D("0.20300"), D("0.205"), at.replace(microsecond=5))
    plan = plan_export(Plan((), (note, same_second)), cfg, cfg.data_dir)
    assert len(plan.new_alerts) == 1


def test_new_alerts_are_appended_in_time_order(tmp_path):
    cfg = make_cfg(tmp_path)
    single_mode_files(cfg)
    later = PlannedNotification("KRW", D("0.0249"), D("0.025"), T0 + timedelta(days=9))
    earlier = PlannedNotification("JPY", D("0.2030"), D("0.205"), T0 + timedelta(days=8))
    write_export(plan_export(Plan((), (later, earlier)), cfg, cfg.data_dir), cfg.data_dir)
    rows = (cfg.data_dir / ALERTS_FILE).read_text(encoding="utf-8").splitlines()
    assert [r.split(",")[1] for r in rows[1:]] == ["JPY", "JPY", "KRW"]


# ===========================================================================
# 以 config.yaml 為準的情況
# ===========================================================================


def test_unknown_currency_in_db_is_skipped_with_warning(tmp_path):
    cfg = make_cfg(tmp_path)
    single_mode_files(cfg)
    before = snapshot(cfg)
    db = Plan(
        rules=(
            rule(
                currency="USD",
                target=D("31"),
                state="triggered",
                state_changed_at=T0,
                state_rate=D("30.5"),
                state_target=D("31"),
            ),  # fmt: skip
        ),
        notifications=(PlannedNotification("USD", D("30.5"), D("31"), T0),),
    )
    plan = plan_export(db, cfg, cfg.data_dir)
    assert not plan.has_changes
    assert any(w.startswith("USD：") for w in plan.warnings)
    write_export(plan, cfg.data_dir)
    assert snapshot(cfg) == before


def test_currency_missing_in_db_keeps_file_and_warns(tmp_path):
    cfg = make_cfg(tmp_path)
    single_mode_files(cfg)
    before = snapshot(cfg)
    plan = plan_export(Plan((), ()), cfg, cfg.data_dir)
    assert not plan.has_changes
    assert {w.split("：")[0] for w in plan.warnings} == {"JPY", "KRW"}
    write_export(plan, cfg.data_dir)
    assert snapshot(cfg) == before


def test_never_transitioned_in_db_keeps_file_entry(tmp_path):
    cfg = make_cfg(tmp_path)
    single_mode_files(cfg)
    plan = plan_export(Plan((rule(state="armed"),), ()), cfg, cfg.data_dir)
    assert plan.states["JPY"].state is AlertState.TRIGGERED  # 檔案原本的內容


def test_target_changed_on_web_only_warns(tmp_path):
    cfg = make_cfg(tmp_path)
    single_mode_files(cfg)
    db = collect(cfg, cfg.data_dir)
    jpy = db.rules[0]
    edited = PlannedRule("JPY", D("0.200"), jpy.buffer, jpy.state, jpy.state_changed_at,
                         jpy.state_rate, jpy.state_target)  # fmt: skip
    plan = plan_export(Plan((edited, db.rules[1]), db.notifications), cfg, cfg.data_dir)
    assert not plan.has_changes
    assert any("JPY" in w and "0.200" in w and "config.yaml" in w for w in plan.warnings)


def test_missing_files_start_from_empty(tmp_path):
    cfg = make_cfg(tmp_path)
    db = Plan(
        rules=(
            rule(
                state="triggered",
                state_changed_at=T0,
                state_rate=D("0.204"),
                state_target=D("0.205"),
            ),
        ),  # fmt: skip
        notifications=(PlannedNotification("JPY", D("0.204"), D("0.205"), T0),),
    )
    write_export(plan_export(db, cfg, cfg.data_dir), cfg.data_dir)
    assert load_states(cfg.state_file)["JPY"].state is AlertState.TRIGGERED
    assert len(read_alerts(cfg.data_dir)) == 1


def test_describe_lists_changes_and_warnings(tmp_path):
    cfg = make_cfg(tmp_path)
    single_mode_files(cfg)
    note = PlannedNotification("KRW", D("0.0249"), D("0.025"), T0 + timedelta(days=9))
    text = "\n".join(describe_export(plan_export(Plan((), (note,)), cfg, cfg.data_dir)))
    assert "state.json 不用改" in text
    assert "alerts.csv 會補上 1 筆" in text and "KRW 匯率 0.0249" in text
    assert "⚠ JPY：資料庫沒有" in text


# ===========================================================================
# CLI
# ===========================================================================


class FakeClient:
    db: Plan = Plan((), ())

    def __init__(self, cfg):
        pass

    def find_user(self, email):
        return UID

    def fetch(self, user_id):
        assert user_id == UID
        return FakeClient.db


def run_cli(tmp_path, monkeypatch, *extra):
    import fx_alert.migrate as migrate_mod

    monkeypatch.setattr(migrate_mod, "MigrationClient", FakeClient)
    monkeypatch.setenv("SUPABASE_URL", URL)
    monkeypatch.setenv("SUPABASE_SECRET_KEY", KEY)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "currencies:\n  JPY: {target: '0.205', buffer: '0.001'}\n", encoding="utf-8"
    )
    args = ["export-state", "--email", "james@example.com", "--config", str(cfg_file)]
    return main_mod.main([*args, "--data-dir", str(tmp_path / "data"), *extra])


@pytest.fixture
def triggered_db():
    FakeClient.db = Plan(
        rules=(
            rule(
                state="triggered",
                state_changed_at=T0,
                state_rate=D("0.204"),
                state_target=D("0.205"),
            ),
        ),  # fmt: skip
        notifications=(PlannedNotification("JPY", D("0.204"), D("0.205"), T0),),
    )
    yield
    FakeClient.db = Plan((), ())


def test_cli_dry_run_writes_nothing(tmp_path, monkeypatch, capsys, triggered_db):
    assert run_cli(tmp_path, monkeypatch, "--dry-run") == 0
    out = capsys.readouterr().out
    assert "jam***@example.com" in out and "james@" not in out
    assert "JPY：（沒有紀錄） → triggered" in out and "不寫入" in out
    assert not (tmp_path / "data").exists()


def test_cli_writes_then_is_idempotent(tmp_path, monkeypatch, capsys, triggered_db):
    assert run_cli(tmp_path, monkeypatch) == 0
    assert "已寫入" in capsys.readouterr().out
    first = (tmp_path / "data" / "state.json").read_bytes()
    assert run_cli(tmp_path, monkeypatch) == 0
    assert "不用寫入" in capsys.readouterr().out
    assert (tmp_path / "data" / "state.json").read_bytes() == first
    assert len(read_alerts(tmp_path / "data")) == 1


def test_cli_refuses_in_github_actions(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert main_mod.main(["export-state", "--email", "james@example.com", "--dry-run"]) == 1
    captured = capsys.readouterr()
    assert "@" not in captured.out + captured.err


def test_cli_needs_secret_from_env(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("SUPABASE_SECRET_KEY", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("currencies:\n  JPY: {target: '0.205'}\n", encoding="utf-8")
    args = ["export-state", "--email", "a@b.c", "--config", str(cfg_file), "--dry-run"]
    assert main_mod.main(args) == 1
    assert "匯出失敗" in capsys.readouterr().err


def test_written_files_contain_no_email(tmp_path, monkeypatch, triggered_db):
    assert run_cli(tmp_path, monkeypatch) == 0
    for path in (tmp_path / "data").rglob("*"):
        if path.is_file():
            assert "@" not in path.read_text(encoding="utf-8"), path
