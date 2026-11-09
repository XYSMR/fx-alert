from __future__ import annotations

import json
from datetime import UTC, datetime, time, timedelta, timezone
from decimal import Decimal as D

import pytest

from fx_alert import main as main_mod
from fx_alert.config import TAIPEI, AppConfig, BusinessHours
from fx_alert.history import append_alert
from fx_alert.migrate import (
    MigrationClient,
    MigrationError,
    Plan,
    PlannedNotification,
    PlannedRule,
    collect,
    compare,
    mask_email,
)
from fx_alert.rules import Rule
from fx_alert.store import StoreError, SupabaseConfig

URL = "https://abcdefghijklmnopqrst.supabase.co"
KEY = "sb_secret_TESTONLY"
UID = "00000000-0000-4000-8000-00000000000a"


def make_cfg(tmp_path):
    return AppConfig(
        data_dir=tmp_path / "data",
        hours=BusinessHours(weekdays_only=True, start=time(9, 0), end=time(16, 30)),
        rules=(
            Rule("JPY", target=D("0.205"), buffer=D("0.001")),
            Rule("KRW", target=D("0.0250"), buffer=D("0.0002")),
        ),
    )


def write_state(data_dir, currencies):
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "state.json").write_text(
        json.dumps({"version": 1, "currencies": currencies}), encoding="utf-8"
    )


# ===========================================================================
# collect：從 config.yaml、state.json、alerts.csv 讀出要搬的資料
# ===========================================================================


def test_collect_reads_all_three_sources(tmp_path):
    cfg = make_cfg(tmp_path)
    write_state(
        cfg.data_dir,
        {
            "JPY": {
                "state": "triggered",
                "changed_at": "2026-11-05T10:07:00+08:00",
                "rate": "0.2040",
                "target": "0.205",
            },
            # 舊格式：沒有 target
            "KRW": {"state": "armed", "changed_at": "2026-11-09T15:46:16+08:00", "rate": "0.0264"},
        },
    )
    sent = datetime(2026, 11, 5, 10, 7, tzinfo=TAIPEI)
    append_alert(cfg.data_dir, sent, "JPY", D("0.2040"), D("0.205"))
    append_alert(cfg.data_dir, sent, "USD", D("30"), D("31"))  # 不在 config 裡 → 不搬

    plan = collect(cfg, cfg.data_dir)
    jpy, krw = plan.rules
    assert (jpy.target, jpy.buffer, jpy.state) == (D("0.205"), D("0.001"), "triggered")
    assert jpy.state_changed_at == sent
    assert (jpy.state_rate, jpy.state_target) == (D("0.2040"), D("0.205"))
    assert (krw.target, krw.buffer, krw.state, krw.state_target) == (
        D("0.0250"),
        D("0.0002"),
        "armed",
        None,
    )
    assert [(n.currency, n.rate) for n in plan.notifications] == [("JPY", D("0.2040"))]


def test_collect_without_state_file_means_armed(tmp_path):
    cfg = make_cfg(tmp_path)
    plan = collect(cfg, cfg.data_dir)
    assert [(r.state, r.state_changed_at) for r in plan.rules] == [("armed", None)] * 2
    assert plan.notifications == ()


def test_collect_rejects_corrupt_state(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.data_dir.mkdir(parents=True)
    (cfg.data_dir / "state.json").write_text("{broken", encoding="utf-8")
    with pytest.raises(MigrationError):
        collect(cfg, cfg.data_dir)


# ===========================================================================
# compare：Decimal 比數字、時間點比時間
# ===========================================================================

T8 = datetime(2026, 11, 5, 10, 7, tzinfo=TAIPEI)


def rule(**over):
    base = dict(
        currency="KRW",
        target=D("0.0250"),
        buffer=D("0.0002"),
        state="triggered",
        state_changed_at=T8,
        state_rate=D("0.0249"),
        state_target=D("0.0250"),
    )
    return PlannedRule(**{**base, **over})


def note(**over):
    base = dict(currency="KRW", rate=D("0.0249"), target=D("0.0250"), sent_at=T8)
    return PlannedNotification(**{**base, **over})


def test_compare_treats_equal_decimals_and_instants_as_same():
    """0.0250 和 0.025 相同；+08:00 和 UTC 寫法不同但是同一刻。"""
    expected = Plan((rule(),), (note(),))
    utc = T8.astimezone(UTC)
    actual = Plan(
        (
            rule(
                target=D("0.025"),
                buffer=D("0.000200"),
                state_rate=D("0.024900"),
                state_target=D("0.025000"),
                state_changed_at=utc,
            ),
        ),
        (note(rate=D("0.024900"), target=D("0.025"), sent_at=utc),),
    )
    assert compare(expected, actual) == []


def test_compare_other_timezone_same_instant():
    tokyo = timezone(timedelta(hours=9))
    assert (
        compare(Plan((rule(),), ()), Plan((rule(state_changed_at=T8.astimezone(tokyo)),), ())) == []
    )


@pytest.mark.parametrize(
    ("over", "field"),
    [
        ({"target": D("0.0251")}, "target"),
        ({"buffer": D("0.0003")}, "buffer"),
        ({"state": "armed"}, "state"),
        ({"state_changed_at": T8 + timedelta(seconds=1)}, "state_changed_at"),
        ({"state_rate": D("0.0248")}, "state_rate"),
        ({"state_target": None}, "state_target"),
    ],
)
def test_compare_detects_rule_differences(over, field):
    problems = compare(Plan((rule(),), ()), Plan((rule(**over),), ()))
    assert len(problems) == 1 and f"KRW {field}" in problems[0]


def test_compare_detects_notification_differences():
    exp = Plan((rule(),), (note(), note(sent_at=T8 - timedelta(days=1))))
    assert "筆數" in compare(exp, Plan((rule(),), (note(),)))[0]
    changed = Plan((rule(),), (note(), note(sent_at=T8 - timedelta(days=1), rate=D("0.0248"))))
    assert compare(exp, changed) == ["通知紀錄內容不一致"]
    # 順序不同不算不一致
    reordered = Plan((rule(),), tuple(reversed(exp.notifications)))
    assert compare(exp, reordered) == []


def test_compare_detects_missing_currency():
    problems = compare(Plan((rule(), rule(currency="JPY")), ()), Plan((rule(),), ()))
    assert "幣別不同" in problems[0]


# ===========================================================================
# MigrationClient（假的 HTTP）
# ===========================================================================


class Resp:
    def __init__(self, status=200, body=None, reason="OK"):
        self.status_code, self._body, self.reason = status, body, reason
        self.ok = 200 <= status < 300

    def json(self):
        return self._body


class Session:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def request(self, method, url, **kw):
        self.calls.append({"method": method, "url": url, **kw})
        return self.responses.pop(0)


def client(*responses):
    s = Session(*responses)
    return MigrationClient(SupabaseConfig(URL, KEY), session=s), s


def test_find_user_lowercases_email():
    c, s = client(Resp(body=[{"user_id": UID}]))
    assert c.find_user(" James@Gmail.com ") == UID
    assert s.calls[0]["params"]["email"] == "eq.james@gmail.com"
    assert s.calls[0]["headers"]["apikey"] == KEY
    assert "Authorization" not in s.calls[0]["headers"]


def test_find_user_not_found_tells_to_log_in_first():
    c, _ = client(Resp(body=[]))
    with pytest.raises(MigrationError, match="請先用 Google 登入"):
        c.find_user("x@example.com")


def test_import_sends_plan_and_replace_flag():
    c, s = client(Resp(body={"rules": 1, "notifications": 1}))
    plan = Plan((rule(),), (note(),))
    assert c.import_plan(UID, plan, replace=True) == {"rules": 1, "notifications": 1}
    body = s.calls[0]["json"]
    assert s.calls[0]["url"].endswith("/rest/v1/rpc/import_rules_for_user")
    assert body["p_replace"] is True and body["p_user_id"] == UID
    assert body["p_rules"][0]["target"] == "0.0250"
    assert body["p_rules"][0]["state_changed_at"] == "2026-11-05T10:07:00+08:00"


def test_import_already_has_rules_suggests_replace():
    c, _ = client(Resp(400, {"message": "fx_alert: user already has rules"}))
    with pytest.raises(MigrationError, match="--replace"):
        c.import_plan(UID, Plan((), ()), replace=False)


def test_fetch_parses_database_rows():
    c, _ = client(
        Resp(
            body=[
                {
                    "currency": "KRW",
                    "target": "0.025000",
                    "buffer": "0.000200",
                    "state": "triggered",
                    "state_changed_at": "2026-11-05T02:07:00+00:00",
                    "state_rate": "0.024900",
                    "state_target": "0.025000",
                }
            ]
        ),
        Resp(
            body=[
                {
                    "currency": "KRW",
                    "rate": "0.024900",
                    "target": "0.025000",
                    "sent_at": "2026-11-05T02:07:00+00:00",
                }
            ]
        ),
    )
    assert compare(Plan((rule(),), (note(),)), c.fetch(UID)) == []


# ===========================================================================
# CLI
# ===========================================================================


def test_secret_key_comes_only_from_environment(tmp_path, monkeypatch):
    """.env 檔不會被讀取：環境變數沒有設定就失敗。"""
    (tmp_path / ".env").write_text(
        f"SUPABASE_URL={URL}\nSUPABASE_SECRET_KEY={KEY}\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SECRET_KEY", raising=False)
    with pytest.raises(StoreError, match="缺少環境變數"):
        SupabaseConfig.from_env()


def test_no_dotenv_anywhere_in_source():
    from pathlib import Path

    src = Path(main_mod.__file__).parent
    for py in src.glob("*.py"):
        text = py.read_text(encoding="utf-8")
        assert "dotenv" not in text and '".env"' not in text, py


def run_cli(tmp_path, monkeypatch, *extra):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "currencies:\n  JPY: {target: '0.205', buffer: '0.001'}\n", encoding="utf-8"
    )
    data = tmp_path / "data"
    write_state(data, {})
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    args = ["migrate-me", "--email", "james@example.com", "--config", str(cfg_file)]
    return main_mod.main([*args, "--data-dir", str(data), *extra])


def test_cli_dry_run_needs_no_network(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("SUPABASE_SECRET_KEY", raising=False)
    assert run_cli(tmp_path, monkeypatch, "--dry-run") == 0
    out = capsys.readouterr().out
    assert "jam***@example.com" in out and "JPY: 目標 0.205" in out and "不寫入" in out


def test_cli_refuses_in_github_actions(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    args = ["migrate-me", "--email", "james@example.com", "--dry-run"]
    assert main_mod.main(args) == 1
    assert "@" not in capsys.readouterr().out


class FakeClient:
    stored: Plan | None = None
    tamper = False

    def __init__(self, cfg):
        pass

    def find_user(self, email):
        return UID

    def import_plan(self, user_id, plan, *, replace):
        FakeClient.stored = plan
        return {"rules": len(plan.rules), "notifications": len(plan.notifications)}

    def fetch(self, user_id):
        p = FakeClient.stored
        if FakeClient.tamper:
            p = Plan((rule(currency="JPY", target=D("0.2")),), ())
        return p


@pytest.mark.parametrize(("tamper", "code"), [(False, 0), (True, 1)])
def test_cli_verifies_after_import(tmp_path, monkeypatch, capsys, tamper, code):
    import fx_alert.migrate as migrate_mod

    monkeypatch.setattr(migrate_mod, "MigrationClient", FakeClient)
    monkeypatch.setenv("SUPABASE_URL", URL)
    monkeypatch.setenv("SUPABASE_SECRET_KEY", KEY)
    FakeClient.tamper = tamper
    assert run_cli(tmp_path, monkeypatch) == code
    captured = capsys.readouterr()
    if tamper:
        assert "比對不一致" in captured.err
    else:
        assert "比對完全一致" in captured.out


def test_mask_email():
    assert mask_email("james470706@gmail.com") == "jam***@gmail.com"
    assert mask_email("nope") == "***"
