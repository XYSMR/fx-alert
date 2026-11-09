import io
import smtplib
from datetime import datetime
from decimal import Decimal as D

import pytest

from fx_alert import notifier
from fx_alert.notifier import (
    ConfigError,
    DryRunNotifier,
    EmailConfig,
    NotifyError,
    SmtpNotifier,
    build_alert_message,
    build_test_message,
)
from fx_alert.rules import Rule

ENV = {
    "SMTP_USER": "me@gmail.com",
    "SMTP_PASSWORD": "abcd efgh ijkl mnop",
    "MAIL_TO": "me@gmail.com, friend@example.com",
}
JPY = Rule("JPY", target=D("0.205"), buffer=D("0.001"))
QUOTED = datetime(2026, 11, 9, 10, 7, 30)


@pytest.fixture
def cfg():
    return EmailConfig.from_env(ENV)


# ---------- 設定 ----------


def test_config_from_env(cfg):
    assert cfg.user == "me@gmail.com"
    assert cfg.password == "abcdefghijklmnop"  # 空格被去掉
    assert cfg.to == ("me@gmail.com", "friend@example.com")
    assert (cfg.host, cfg.port) == ("smtp.gmail.com", 465)


def test_password_never_in_repr(cfg):
    assert "abcd" not in repr(cfg)


@pytest.mark.parametrize("missing", ["SMTP_USER", "SMTP_PASSWORD", "MAIL_TO"])
def test_missing_env_raises(missing):
    env = {**ENV, missing: "  "}
    with pytest.raises(ConfigError, match=missing):
        EmailConfig.from_env(env)


def test_bad_port_raises():
    with pytest.raises(ConfigError, match="SMTP_PORT"):
        EmailConfig.from_env({**ENV, "SMTP_PORT": "abc"})


# ---------- 信件內容 ----------


def test_alert_message_content(cfg):
    msg = build_alert_message(cfg, JPY, D("0.2049"), QUOTED)
    assert msg["Subject"] == "[FX Alert] JPY 到價：0.2049 ≤ 目標 0.205"
    assert msg["From"] == "me@gmail.com"
    assert msg["To"] == "me@gmail.com, friend@example.com"
    body = msg.get_content()
    assert "目前匯率：0.2049" in body
    assert "目標匯率：≤ 0.205" in body
    assert "2026-11-09 10:07" in body
    assert "> 0.206" in body  # 告訴使用者什麼時候會再通知
    assert "abcdefghijklmnop" not in msg.as_string()


def test_alert_message_without_quote_time(cfg):
    body = build_alert_message(cfg, JPY, D("0.2049"), None).get_content()
    assert "銀行未提供" in body


# ---------- 寄送（用假的 SMTP） ----------


class FakeSMTP:
    instances: list["FakeSMTP"] = []

    def __init__(self, host, port, timeout=None, context=None, fail_on=None, exc=None):
        self.host, self.port, self.timeout, self.context = host, port, timeout, context
        self.logged_in = None
        self.sent = []
        self.fail_on, self.exc = fail_on, exc
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def login(self, user, password):
        if self.fail_on == "login":
            raise self.exc
        self.logged_in = (user, password)

    def send_message(self, msg):
        if self.fail_on == "send":
            raise self.exc
        self.sent.append(msg)


def factory(**kw):
    FakeSMTP.instances.clear()
    return lambda host, port, **k: FakeSMTP(host, port, **k, **kw)


def test_smtp_send_logs_in_and_sends(cfg):
    msg = build_test_message(cfg)
    SmtpNotifier(cfg, smtp_factory=factory()).send(msg)
    smtp = FakeSMTP.instances[0]
    assert (smtp.host, smtp.port) == ("smtp.gmail.com", 465)
    assert smtp.timeout == notifier.TIMEOUT_SECONDS
    assert smtp.context is not None  # 有驗證 TLS 憑證
    assert smtp.logged_in == ("me@gmail.com", "abcdefghijklmnop")
    assert smtp.sent == [msg]


def test_auth_failure_gives_helpful_error(cfg):
    exc = smtplib.SMTPAuthenticationError(535, b"Username and Password not accepted")
    with pytest.raises(NotifyError, match="應用程式密碼"):
        SmtpNotifier(cfg, smtp_factory=factory(fail_on="login", exc=exc)).send(
            build_test_message(cfg)
        )


@pytest.mark.parametrize(
    "exc",
    [
        smtplib.SMTPRecipientsRefused({"x@y": (550, b"no")}),
        smtplib.SMTPServerDisconnected("bye"),
        TimeoutError("timed out"),
        OSError("network unreachable"),
    ],
)
def test_send_failures_become_notify_error(cfg, exc):
    with pytest.raises(NotifyError):
        SmtpNotifier(cfg, smtp_factory=factory(fail_on="send", exc=exc)).send(
            build_test_message(cfg)
        )


def test_connection_failure_becomes_notify_error(cfg):
    def boom(*a, **k):
        raise ConnectionRefusedError("refused")

    with pytest.raises(NotifyError):
        SmtpNotifier(cfg, smtp_factory=boom).send(build_test_message(cfg))


def test_dry_run_records_without_sending(cfg):
    out = io.StringIO()
    n = DryRunNotifier(out=out)
    msg = build_alert_message(cfg, JPY, D("0.2049"), QUOTED)
    n.send(msg)
    assert n.sent == [msg]
    assert "[dry-run]" in out.getvalue()
    assert "JPY 到價" in out.getvalue()


# ---------- 手動測試指令 ----------


def test_cli_requires_test_flag(capsys):
    assert notifier._main([]) == 2


def test_cli_reports_missing_config(monkeypatch, capsys):
    for k in ENV:
        monkeypatch.delenv(k, raising=False)
    assert notifier._main(["--test"]) == 1
    assert "缺少環境變數" in capsys.readouterr().err
