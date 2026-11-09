import json
from datetime import datetime
from decimal import Decimal as D
from pathlib import Path

import pytest

from fx_alert import main as main_mod
from fx_alert.config import TAIPEI, AppConfig, BusinessHours
from fx_alert.fetcher import FetchError
from fx_alert.notifier import DryRunNotifier, EmailConfig, NotifyError
from fx_alert.rules import AlertState, Rule
from fx_alert.state_store import load_states, save_states

MONDAY_10AM = datetime(2026, 11, 9, 10, 7, tzinfo=TAIPEI)
SATURDAY = datetime(2026, 11, 14, 10, 7, tzinfo=TAIPEI)
EMAIL = EmailConfig(user="me@gmail.com", password="x", to=("me@gmail.com",))
ROOT = Path(__file__).resolve().parents[1]


def make_cfg(tmp_path, krw_target="0.0250"):
    from datetime import time

    return AppConfig(
        state_file=tmp_path / "state.json",
        hours=BusinessHours(weekdays_only=True, start=time(9, 0), end=time(16, 30)),
        rules=(
            Rule("JPY", target=D("0.205"), buffer=D("0.001")),
            Rule("KRW", target=D(krw_target), buffer=D("0.0002")),
        ),
    )


def mega_json(jpy="0.2105", krw="0.0260"):
    def item(code, ask):
        return {"currKey": f"{code}|00", "cash": {"bid": "0.01", "ask": ask}, "spot": {}}

    return json.dumps(
        {"updateTime": "2026/11/09 10:05:00", "rates": [item("JPY", jpy), item("KRW", krw)]}
    )


class FailingNotifier:
    def __init__(self, fail_for=("JPY",)):
        self.fail_for = fail_for
        self.sent = []

    def send(self, msg):
        if any(c in msg["Subject"] for c in self.fail_for):
            raise NotifyError("SMTP down")
        self.sent.append(msg)


def check(cfg, notifier, **kw):
    kw.setdefault("now", MONDAY_10AM)
    kw.setdefault("log", lambda s: None)
    return main_mod.run_check(cfg, notifier, EMAIL, **kw)


def subjects(n):
    return [m["Subject"] for m in n.sent]


# ---------- 主要情境 ----------


def test_first_run_already_below_target_notifies(tmp_path):
    cfg, n = make_cfg(tmp_path), DryRunNotifier()
    r = check(cfg, n, fetch=lambda: mega_json(jpy="0.2040"))
    assert r.notified == ["JPY"] and r.exit_code == 0
    assert subjects(n) == ["[FX Alert] JPY 到價：0.2040 ≤ 目標 0.205"]
    assert r.state_changed is True
    states = load_states(cfg.state_file)
    assert states["JPY"].state is AlertState.TRIGGERED
    assert states["KRW"].state is AlertState.ARMED


def test_second_run_still_below_does_not_notify_or_change_state(tmp_path):
    cfg, n = make_cfg(tmp_path), DryRunNotifier()
    check(cfg, n, fetch=lambda: mega_json(jpy="0.2040"))
    r = check(cfg, n, fetch=lambda: mega_json(jpy="0.2030"))
    assert r.notified == [] and r.state_changed is False
    assert len(n.sent) == 1


def test_rebound_then_drop_notifies_again(tmp_path):
    cfg, n = make_cfg(tmp_path), DryRunNotifier()
    for jpy in ["0.2040", "0.2062", "0.2045"]:
        check(cfg, n, fetch=lambda jpy=jpy: mega_json(jpy=jpy))
    assert len(n.sent) == 2


def test_above_target_no_notification(tmp_path):
    cfg, n = make_cfg(tmp_path), DryRunNotifier()
    r = check(cfg, n, fetch=lambda: mega_json())
    assert r.notified == [] and r.exit_code == 0
    assert r.state_changed is True  # 第一次執行：建立 armed 紀錄


def test_failed_send_keeps_state_and_retries(tmp_path):
    cfg = make_cfg(tmp_path)
    bad = FailingNotifier(fail_for=("JPY",))
    r = check(cfg, bad, fetch=lambda: mega_json(jpy="0.2040"))
    assert r.exit_code == 1 and "JPY 通知失敗" in r.errors[0]
    assert "JPY" not in load_states(cfg.state_file)  # 沒記成已通知
    good = DryRunNotifier()
    r = check(cfg, good, fetch=lambda: mega_json(jpy="0.2040"))
    assert r.notified == ["JPY"] and r.exit_code == 0


def test_one_currency_failing_send_does_not_block_other(tmp_path):
    cfg = make_cfg(tmp_path, krw_target="0.0265")
    n = FailingNotifier(fail_for=("JPY",))
    r = check(cfg, n, fetch=lambda: mega_json(jpy="0.2040", krw="0.0260"))
    assert r.notified == ["KRW"]
    states = load_states(cfg.state_file)
    assert states["KRW"].state is AlertState.TRIGGERED
    assert "JPY" not in states


def test_missing_currency_in_feed_is_error_but_others_continue(tmp_path):
    cfg, n = make_cfg(tmp_path), DryRunNotifier()
    feed = json.dumps({"rates": [{"currKey": "JPY|08", "cash": {"ask": "0.2040"}}]})
    r = check(cfg, n, fetch=lambda: feed)
    assert r.notified == ["JPY"]
    assert r.exit_code == 1 and "KRW" in r.errors[0]


# ---------- 不該動狀態的情況 ----------


def test_outside_hours_is_skipped(tmp_path):
    cfg, n = make_cfg(tmp_path), DryRunNotifier()

    def must_not_fetch():
        raise AssertionError("不應該抓銀行網站")

    r = check(cfg, n, now=SATURDAY, fetch=must_not_fetch)
    assert r.skipped and r.exit_code == 0
    assert not cfg.state_file.exists()


def test_force_ignores_hours(tmp_path):
    cfg, n = make_cfg(tmp_path), DryRunNotifier()
    r = check(cfg, n, now=SATURDAY, force=True, fetch=lambda: mega_json(jpy="0.2040"))
    assert r.notified == ["JPY"]


@pytest.mark.parametrize(
    "fetch_exc_or_text",
    [FetchError("timeout"), "<!DOCTYPE html>", "{}"],
)
def test_fetch_or_parse_error_leaves_state_untouched(tmp_path, fetch_exc_or_text):
    cfg, n = make_cfg(tmp_path), DryRunNotifier()
    check(cfg, n, fetch=lambda: mega_json(jpy="0.2040"))
    before = cfg.state_file.read_bytes()

    def fetch():
        if isinstance(fetch_exc_or_text, Exception):
            raise fetch_exc_or_text
        return fetch_exc_or_text

    r = check(cfg, n, fetch=fetch)
    assert r.exit_code == 1
    assert cfg.state_file.read_bytes() == before


def test_corrupt_state_stops_everything(tmp_path):
    cfg, n = make_cfg(tmp_path), DryRunNotifier()
    cfg.state_file.write_text("{broken", encoding="utf-8")
    r = check(cfg, n, fetch=lambda: mega_json(jpy="0.2040"))
    assert r.exit_code == 1 and n.sent == []


# ---------- CLI ----------


def test_cli_dry_run_end_to_end(tmp_path, monkeypatch, capsys):
    (tmp_path / "config.yaml").write_text(
        (ROOT / "config.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    monkeypatch.setattr(main_mod, "fetch_rates_json", lambda: mega_json(jpy="0.2040"))
    out_file = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out_file))
    for k in ("SMTP_USER", "SMTP_PASSWORD", "MAIL_TO"):
        monkeypatch.delenv(k, raising=False)  # dry-run 不需要帳密

    code = main_mod.main(
        ["check", "--dry-run", "--force", "--config", str(tmp_path / "config.yaml")]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "[dry-run]" in out and "JPY 到價" in out
    assert out_file.read_text(encoding="utf-8") == "state_changed=true\n"
    assert load_states(tmp_path / "state.json")["JPY"].state is AlertState.TRIGGERED


def test_cli_without_email_env_fails_fast(tmp_path, monkeypatch, capsys):
    for k in ("SMTP_USER", "SMTP_PASSWORD", "MAIL_TO"):
        monkeypatch.delenv(k, raising=False)
    code = main_mod.main(["check", "--config", str(ROOT / "config.yaml")])
    assert code == 1
    assert "缺少環境變數" in capsys.readouterr().err


def test_cli_bad_config(tmp_path, capsys):
    bad = tmp_path / "c.yaml"
    bad.write_text("currencies: {}", encoding="utf-8")
    assert main_mod.main(["check", "--dry-run", "--config", str(bad)]) == 1
    assert "設定錯誤" in capsys.readouterr().err


def test_state_file_written_by_run_is_loadable(tmp_path):
    cfg = make_cfg(tmp_path)
    save_states(cfg.state_file, {})
    check(cfg, DryRunNotifier(), fetch=lambda: mega_json(jpy="0.2040"))
    assert load_states(cfg.state_file)["JPY"].rate == "0.2040"
