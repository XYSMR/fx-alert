from __future__ import annotations

import io
from datetime import datetime, time
from decimal import Decimal as D
from email import message_from_bytes
from email.policy import default as default_policy

import pytest

from fx_alert import main as main_mod
from fx_alert.config import TAIPEI, AppConfig, BusinessHours
from fx_alert.notifier import DryRunNotifier, EmailConfig, build_alert_message
from fx_alert.rules import Rule
from fx_alert.store import Watch
from fx_alert.unsubscribe import (
    UnsubscribeConfig,
    UnsubscribeError,
    UnsubscribeLinks,
    sign,
    verify,
)

# 和 supabase/functions/unsubscribe/handler.test.ts 共用的測試向量：兩邊必須算出一樣的簽章
VECTOR_SECRET = "fx-alert-test-vector-secret-0123456789"
VECTOR_USER = "00000000-0000-4000-8000-000000000001"
VECTOR_SIG = "AjoUefqQcmpwV0VAbKi-BoBMszBZ6bicHXfBpHuWxDQ"

PROJECT = "https://abcdefghijklmnopqrst.supabase.co"
ENV = {"UNSUBSCRIBE_SECRET": VECTOR_SECRET, "SUPABASE_URL": PROJECT}


# ---------- 簽章 ----------


def test_signature_matches_shared_vector():
    assert sign(VECTOR_SECRET, VECTOR_USER) == VECTOR_SIG


def test_signature_is_stable_so_old_links_keep_working():
    """沒有 token 表、也沒有過期時間：舊信裡的連結和今天產生的完全一樣。"""
    old_link = UnsubscribeConfig.from_env(ENV).links(VECTOR_USER)
    # ……幾個月後，同一個密鑰再寄一封新信
    new_link = UnsubscribeConfig.from_env(dict(ENV)).links(VECTOR_USER)
    assert old_link == new_link
    assert verify(VECTOR_SECRET, VECTOR_USER, VECTOR_SIG)


def test_user_id_is_case_insensitive():
    assert sign(VECTOR_SECRET, VECTOR_USER.upper()) == VECTOR_SIG


@pytest.mark.parametrize(
    ("secret", "user", "sig"),
    [
        (VECTOR_SECRET, "00000000-0000-4000-8000-000000000002", VECTOR_SIG),  # 換成別人
        (VECTOR_SECRET, VECTOR_USER, VECTOR_SIG[:-1] + "A"),  # 竄改簽章
        (VECTOR_SECRET, VECTOR_USER, ""),
        ("another-secret-of-sufficient-length-000", VECTOR_USER, VECTOR_SIG),  # 換過密鑰
        (VECTOR_SECRET, "not-a-uuid", VECTOR_SIG),
    ],
)
def test_verify_rejects(secret, user, sig):
    assert verify(secret, user, sig) is False


def test_sign_rejects_non_uuid():
    with pytest.raises(UnsubscribeError):
        sign(VECTOR_SECRET, "alice@example.com")


# ---------- 設定 ----------


def test_links():
    links = UnsubscribeConfig.from_env(ENV).links(VECTOR_USER)
    assert links.page_url == (
        "https://xysmr.github.io/fx-alert/unsubscribe.html"
        f"?u={VECTOR_USER}&s={VECTOR_SIG}&p=abcdefghijklmnopqrst"
    )
    assert (
        links.one_click_url == f"{PROJECT}/functions/v1/unsubscribe?u={VECTOR_USER}&s={VECTOR_SIG}"
    )


def test_secret_not_in_repr():
    assert VECTOR_SECRET not in repr(UnsubscribeConfig.from_env(ENV))


@pytest.mark.parametrize(
    "env",
    [
        {"SUPABASE_URL": PROJECT},
        {**ENV, "UNSUBSCRIBE_SECRET": "too-short"},
        {**ENV, "SUPABASE_URL": "https://evil.example.com"},
        {**ENV, "SUPABASE_URL": ""},
        {**ENV, "FX_SITE_URL": "http://xysmr.github.io/fx-alert/"},
    ],
)
def test_from_env_rejects(env):
    with pytest.raises(UnsubscribeError):
        UnsubscribeConfig.from_env(env)


def test_custom_site_url_gets_trailing_slash():
    cfg = UnsubscribeConfig.from_env({**ENV, "FX_SITE_URL": "https://example.org/fx"})
    assert cfg.links(VECTOR_USER).page_url.startswith("https://example.org/fx/unsubscribe.html?")


# ---------- 信件 ----------

EMAIL = EmailConfig(user="sender@example.com", password="x", to=("me@example.com",))
JPY = Rule("JPY", target=D("0.205"), buffer=D("0.001"))
LINKS = UnsubscribeLinks(page_url="https://site/unsub?u=1", one_click_url="https://fn/unsub?u=1")


def _roundtrip(msg):
    return message_from_bytes(msg.as_bytes(), policy=default_policy)


def test_multi_user_message_has_unsubscribe_headers_and_link():
    msg = _roundtrip(build_alert_message(EMAIL, JPY, D("0.2"), None, unsubscribe=LINKS))
    assert msg["List-Unsubscribe"] == "<https://fn/unsub?u=1>"
    assert msg["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert "取消訂閱：https://site/unsub?u=1" in msg.get_content()


def test_single_user_message_has_no_unsubscribe():
    msg = build_alert_message(EMAIL, JPY, D("0.2"), None)
    assert msg["List-Unsubscribe"] is None
    assert "取消訂閱" not in msg.get_content()


# ---------- run_check ----------

NOW = datetime(2026, 11, 9, 10, 7, tzinfo=TAIPEI)


class OneWatchStore:
    def __init__(self, user_id):
        self.watch = Watch("1", "規則#1", JPY, None, None, ("alice@example.com",), user_id=user_id)

    def load_watches(self):
        return [self.watch]

    def count_sent_since(self, since):
        return 0

    def record_alert(self, w, rate, now):
        return True

    def record_transition(self, w, s, rate, now):
        return True

    def flush(self):
        return True


def _mega():
    return (
        '{"updateTime": "2026/11/09 10:05:00", "rates": ['
        '{"currKey": "JPY|00", "cash": {"bid": "0.19", "ask": "0.2040"}, "spot": {}}]}'
    )


def _run(tmp_path, store, unsubscribe, logs):
    n = DryRunNotifier(out=io.StringIO())
    r = main_mod.run_check(
        AppConfig(
            data_dir=tmp_path / "data",
            hours=BusinessHours(weekdays_only=True, start=time(9, 0), end=time(16, 30)),
            rules=(JPY,),
        ),
        n,
        EMAIL,
        now=NOW,
        fetch=_mega,
        log=logs.append,
        store=store,
        unsubscribe=unsubscribe,
    )
    return r, n


def test_run_check_adds_links_for_each_user(tmp_path):
    logs = []
    cfg = UnsubscribeConfig.from_env(ENV)
    r, n = _run(tmp_path, OneWatchStore(VECTOR_USER), cfg, logs)
    (msg,) = n.sent
    assert msg["List-Unsubscribe"] == f"<{cfg.links(VECTOR_USER).one_click_url}>"
    assert r.exit_code == 0
    # 連結等於「可以替這個人退訂」，不能出現在公開的 log
    assert not any(VECTOR_SIG in line for line in logs)


def test_run_check_without_config_sends_plain_mail(tmp_path):
    r, n = _run(tmp_path, OneWatchStore(VECTOR_USER), None, [])
    assert n.sent[0]["List-Unsubscribe"] is None


def test_bad_user_id_skips_that_mail(tmp_path):
    r, n = _run(tmp_path, OneWatchStore("not-a-uuid"), UnsubscribeConfig.from_env(ENV), [])
    assert n.sent == []
    assert r.errors and "退訂連結" in r.errors[0]


# ---------- unsubscribe-check：確認 GitHub Secret 和 Edge Function 一致 ----------


def _probe_cfg():
    from fx_alert.unsubscribe import UnsubscribeConfig

    return UnsubscribeConfig(
        secret="k" * 40, supabase_url="https://abcdefghijklmnopqrst.supabase.co"
    )


@pytest.mark.parametrize(
    "status,body,ok,text",
    [
        (200, {"ok": True, "status": "no_account"}, True, "通過"),
        (403, {"ok": False, "error": "invalid_link"}, False, "簽章不符"),
        (500, {"ok": False, "error": "not_configured"}, False, "沒有設定 UNSUBSCRIBE_SECRET"),
        (500, {"ok": False, "error": "update_failed"}, False, "FX_SECRET_KEY"),
        # 不該發生：測試帳號不存在
        (200, {"ok": True, "status": "unsubscribed"}, False, "回應 200"),
        (404, None, False, "回應 404"),
    ],
)
def test_check_unsubscribe_results(status, body, ok, text):
    from fx_alert.main import check_unsubscribe

    calls = []
    result = check_unsubscribe(_probe_cfg(), lambda url: calls.append(url) or (status, body))
    assert result[0] is ok and text in result[1]
    (url,) = calls
    assert "u=00000000-0000-4000-8000-000000000000" in url  # 不存在的帳號，不會退訂任何人
    assert "/functions/v1/unsubscribe?" in url


def test_check_unsubscribe_never_prints_link_or_signature(monkeypatch, capsys):
    from fx_alert import main as main_mod
    from fx_alert.unsubscribe import sign

    secret = "k" * 40
    monkeypatch.setenv("UNSUBSCRIBE_SECRET", secret)
    monkeypatch.setenv("SUPABASE_URL", "https://abcdefghijklmnopqrst.supabase.co")
    monkeypatch.setattr(main_mod, "_post_json", lambda url: (403, {"error": "invalid_link"}))
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert main_mod.main(["unsubscribe-check"]) == 1
    out = "".join(capsys.readouterr())
    assert "簽章不符" in out and "::error::" in out
    sig = sign(secret, "00000000-0000-4000-8000-000000000000")
    for leaked in (sig, secret, "functions/v1", "abcdefghijklmnopqrst"):
        assert leaked not in out


def test_check_unsubscribe_connection_error():
    from fx_alert.main import check_unsubscribe

    def boom(url):
        raise ConnectionError(f"cannot reach {url}")

    ok, text = check_unsubscribe(_probe_cfg(), boom)
    assert not ok and text == "連不到 Edge Function：ConnectionError"
