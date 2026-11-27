"""手機推播的寄送端（push.py）。"""

from __future__ import annotations

import base64
import http.server
import json
import threading
from datetime import datetime
from decimal import Decimal as D
from pathlib import Path

import pytest
import requests
from cryptography.hazmat.primitives.asymmetric import ec

from fx_alert import push
from fx_alert.config import TAIPEI
from fx_alert.push import (
    FAILED,
    GONE,
    OK,
    Device,
    Payload,
    PushConfig,
    PushConfigError,
    Pusher,
    WebPushSender,
    alert_payload,
    classify,
    is_allowed_endpoint,
    trip_payload,
)
from fx_alert.trip_rules import (
    CHEAP_FAR,
    CHEAP_NEAR,
    DISCLAIMER,
    FINAL,
    LAST_CALL,
    NEAR_START,
    Decision,
)

CASES = json.loads((Path(__file__).parent / "fixtures" / "push_endpoints.json").read_text("utf-8"))
NOW = datetime(2026, 11, 30, 10, 7, tzinfo=TAIPEI)


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def new_keys() -> tuple[str, str]:
    """(私鑰 43 字, 公鑰 87 字)，和 scripts/vapid_keygen.js 的格式相同"""
    key = ec.generate_private_key(ec.SECP256R1())
    priv = key.private_numbers().private_value.to_bytes(32, "big")
    nums = key.public_key().public_numbers()
    pub = b"\x04" + nums.x.to_bytes(32, "big") + nums.y.to_bytes(32, "big")
    return b64(priv), b64(pub)


def browser_keys() -> tuple[str, str]:
    """瀏覽器訂閱時給的 p256dh（65 bytes）和 auth（16 bytes）"""
    _, pub = new_keys()
    return pub, b64(b"0123456789abcdef")


def device(id_=1, user="u1", endpoint=None, fail_count=0) -> Device:
    p256dh, auth = browser_keys()
    return Device(
        id=id_,
        user_id=user,
        endpoint=endpoint or f"https://web.push.apple.com/dev-{id_}",
        p256dh=p256dh,
        auth=auth,
        fail_count=fail_count,
    )


# ---------------------------------------------------------------------------
# 白名單：和資料庫、網頁同一份案例
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("url", CASES["allowed"])
def test_allowed_endpoints(url):
    assert is_allowed_endpoint(url)


@pytest.mark.parametrize("reason", sorted(CASES["denied"]))
def test_denied_endpoints(reason):
    assert not is_allowed_endpoint(CASES["denied"][reason]), reason


def test_length_and_type_limits():
    ok = "https://web.push.apple.com/" + "A" * (2048 - 27)
    assert is_allowed_endpoint(ok)
    assert not is_allowed_endpoint(ok + "A")
    for v in (None, 1, b"https://web.push.apple.com/x"):
        assert not is_allowed_endpoint(v)


def test_sender_refuses_endpoint_outside_whitelist_without_sending():
    calls = []
    sender = WebPushSender(PushConfig(new_keys()[0]), webpush=lambda **kw: calls.append(kw))
    bad = device(endpoint="https://evil.push.apple.com.attacker.com/x")
    assert sender.send(bad, alert_payload("JPY", D("0.2040"))) == FAILED
    assert calls == []


# ---------------------------------------------------------------------------
# 狀態碼、不跟隨轉址
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "want"),
    [(200, OK), (201, OK), (202, OK), (404, GONE), (410, GONE), (400, FAILED), (401, FAILED),
     (403, FAILED), (413, FAILED), (429, FAILED), (500, FAILED), (503, FAILED), (None, FAILED)],
)  # fmt: skip
def test_classify(status, want):
    assert classify(status) == want


@pytest.mark.parametrize("status", CASES["redirect_statuses"])
def test_redirect_status_is_failure(status):
    assert classify(status) == FAILED


class _Redirector(http.server.BaseHTTPRequestHandler):
    hits: list[str] = []

    def do_POST(self):  # noqa: N802
        self.hits.append(self.path)
        if self.path == "/push":
            self.send_response(self.server.redirect_status)
            self.send_header("Location", "/internal-metadata")
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

    do_GET = do_POST  # noqa: N815

    def log_message(self, *args):
        pass


@pytest.mark.parametrize("status", CASES["redirect_statuses"])
def test_sender_session_never_follows_redirects(status):
    """真的起一個會轉址的伺服器：送出端只收到 3xx，不會去連 Location 的網址"""
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Redirector)
    srv.redirect_status = status
    _Redirector.hits = []
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        session = push._NoRedirectSession()
        resp = session.post(f"http://127.0.0.1:{srv.server_port}/push", data=b"x", timeout=5)
        assert resp.status_code == status
        assert _Redirector.hits == ["/push"]  # 沒有連到 /internal-metadata
        # 呼叫的人就算要求跟隨也一樣不跟隨
        resp = session.post(
            f"http://127.0.0.1:{srv.server_port}/push", data=b"x", timeout=5, allow_redirects=True
        )
        assert resp.status_code == status
        assert _Redirector.hits == ["/push", "/push"]
    finally:
        srv.shutdown()


def test_sender_passes_no_redirect_session_and_ttl():
    seen = {}

    class R:
        status_code = 201

    def fake(**kw):
        seen.update(kw)
        return R()

    sender = WebPushSender(PushConfig(new_keys()[0]), webpush=fake)
    assert sender.send(device(), alert_payload("JPY", D("0.2040"))) == OK
    assert isinstance(seen["requests_session"], push._NoRedirectSession)
    assert seen["ttl"] == push.TTL_SECONDS
    assert seen["timeout"] == push.TIMEOUT_SECONDS


def test_sender_turns_exceptions_into_outcomes():
    class Exc(Exception):
        def __init__(self, status):
            super().__init__("Push failed: https://web.push.apple.com/secret-endpoint")
            self.response = type("R", (), {"status_code": status})()

    for status, want in ((410, GONE), (302, FAILED), (403, FAILED)):

        def boom(status=status, **kw):
            raise Exc(status)

        assert (
            WebPushSender(PushConfig(new_keys()[0]), webpush=boom).send(
                device(), alert_payload("JPY", D("0.2"))
            )
            == want
        )

    def offline(**kw):
        raise requests.ConnectionError("down")

    sender = WebPushSender(PushConfig(new_keys()[0]), webpush=offline)
    assert sender.send(device(), alert_payload("JPY", D("0.2"))) == FAILED


# ---------------------------------------------------------------------------
# 真的用 pywebpush 加密、簽章（只把網路換掉）
# ---------------------------------------------------------------------------


def _jwt_claims(authorization: str) -> dict:
    token = authorization.split("t=")[1].split(",")[0]
    body = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))


def test_real_pywebpush_signs_each_endpoint_with_its_own_audience(monkeypatch):
    """pywebpush 會把第一個網址的 aud 寫回 claims；我們每次都用新的 dict，
    所以送到 Apple、Google 的簽章 aud 各自正確（重用 dict 時 Google 會收到 Apple 的 aud 而拒絕）。
    私鑰用 vapid_keygen.js 的格式（43 字的 raw key）。"""
    posts = []

    def fake_request(self, method, url, **kw):
        assert kw["allow_redirects"] is False
        posts.append((url, kw["headers"], kw["data"]))
        r = requests.Response()
        r.status_code = 201
        return r

    monkeypatch.setattr(requests.Session, "request", fake_request)
    priv, pub = new_keys()
    sender = WebPushSender(PushConfig(priv))
    apple = device(1, endpoint="https://web.push.apple.com/abc")
    google = device(2, endpoint="https://fcm.googleapis.com/fcm/send/xyz:APA91")
    payload = alert_payload("JPY", D("0.2040"))
    assert sender.send(apple, payload) == OK
    assert sender.send(google, payload) == OK
    assert [u for u, _, _ in posts] == [apple.endpoint, google.endpoint]
    for (_url, headers, body), aud in zip(
        posts, ["https://web.push.apple.com", "https://fcm.googleapis.com"], strict=True
    ):
        claims = _jwt_claims(headers["Authorization"])
        assert claims["aud"] == aud
        assert claims["sub"] == push.DEFAULT_SUBJECT
        assert f"k={pub}" in headers["Authorization"]
        assert headers["TTL"] == str(push.TTL_SECONDS)
        assert headers["Content-Encoding"] == "aes128gcm"
        assert payload.title.encode() not in body  # 內容是加密的


# ---------------------------------------------------------------------------
# 推播內容（鎖定畫面看得到）
# ---------------------------------------------------------------------------


def test_alert_payload_shows_currency_and_price_only():
    p = alert_payload("JPY", D("0.2040"))
    assert p.title == "日圓到價：0.2040"
    assert p.url == "./#JPY"
    assert p.tag == "rate-JPY"
    assert "目標" not in p.title + p.body


def decision(kind, rank=None, days=30):
    return Decision(kind=kind, days_left=days, rank=rank, window=20 if rank else None)


@pytest.mark.parametrize("kind", [CHEAP_FAR, NEAR_START, CHEAP_NEAR, FINAL, LAST_CALL])
def test_trip_payloads_are_neutral_and_have_no_amount_or_date(kind):
    p = trip_payload(
        "JPY", decision(kind, rank=3 if "cheap" in kind else None, days=7), D("0.2040")
    )
    text = p.title + p.body
    assert "日圓" in p.title
    for word in [
        "建議",
        "應該",
        "最佳",
        "推薦",
        "划算",
        "賺",
        "虧",
        "會漲",
        "會跌",
        "預測",
        "預估",
    ]:
        assert word not in text, word
    assert "/" not in text and "元" not in text  # 沒有出發日、沒有金額
    if kind in (CHEAP_FAR, CHEAP_NEAR):
        assert "第 3 低" in p.title
        assert DISCLAIMER in p.body


def test_final_payload_matches_plan_example():
    assert (
        trip_payload("JPY", decision(FINAL, days=7), D("0.2")).title
        == "日圓最後換匯提醒（距離出發 7 天）"
    )


def test_payload_json_is_small_and_utf8():
    p = trip_payload("VND", decision(CHEAP_FAR, rank=1, days=99), D("0.001234"))
    data = p.to_json().encode()
    assert len(data) < 1000  # Web Push 上限約 4 KB
    assert json.loads(data) == {"title": p.title, "body": p.body, "url": p.url, "tag": p.tag}


# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------


def test_default_subject_is_accepted_by_py_vapid():
    """py_vapid 只接受 mailto: 或不帶路徑的 https 網址；不合格時每一則推播都會失敗"""
    import py_vapid

    assert py_vapid._check_sub(push.DEFAULT_SUBJECT)


def test_config_absent_means_no_push():
    assert PushConfig.from_env({}) is None
    assert PushConfig.from_env({"WEB_PUSH_PRIVATE_KEY": "  "}) is None


def test_config_valid_and_key_hidden_from_repr():
    priv, _ = new_keys()
    cfg = PushConfig.from_env({"WEB_PUSH_PRIVATE_KEY": priv + "\n"})
    assert cfg.private_key == priv and cfg.subject == push.DEFAULT_SUBJECT
    assert priv not in repr(cfg)


@pytest.mark.parametrize(
    ("env", "msg"),
    [
        ({"WEB_PUSH_PRIVATE_KEY": new_keys()[1]}, "43 字"),  # 公鑰設錯地方
        ({"WEB_PUSH_PRIVATE_KEY": "-----BEGIN EC PRIVATE KEY-----"}, "43 字"),
        ({"WEB_PUSH_PRIVATE_KEY": "x" * 43, "WEB_PUSH_SUBJECT": "me"}, "https:// 或 mailto:"),
        (
            {
                "WEB_PUSH_PRIVATE_KEY": "x" * 43,
                "WEB_PUSH_SUBJECT": "https://xysmr.github.io/fx-alert/",
            },
            "根網址",
        ),
    ],
)
def test_config_errors(env, msg):
    with pytest.raises(PushConfigError, match=msg):
        PushConfig.from_env(env)


# ---------------------------------------------------------------------------
# Pusher：flag、上限、失效清理、log
# ---------------------------------------------------------------------------

FLAG_ON = {"web_push": {"flag": "web_push", "enabled": True, "rollout": 100,
                        "allowed_roles": ["admin", "beta"]}}  # fmt: skip


class FakeStore:
    def __init__(self, flags=FLAG_ON, devices=(), roles=None, sent_today=1, fail_load=False):
        self.flags, self.devices, self.roles = flags, list(devices), roles or {}
        self.sent_today, self.fail_load = sent_today, fail_load
        self.loads, self.results = 0, []

    def load_push_targets(self):
        self.loads += 1
        if self.fail_load:
            raise RuntimeError("db down")
        devices = self.devices if self.flags.get("web_push", {}).get("enabled") else []
        return self.flags, devices, self.roles

    def count_user_sent_since(self, user_id, since):
        assert since == NOW.replace(hour=0, minute=0, second=0, microsecond=0)
        return self.sent_today

    def record_push_result(self, d, outcome, now):
        self.results.append((d.id, outcome))


class FakeSender:
    def __init__(self, outcomes=None):
        self.outcomes, self.sent = outcomes or {}, []

    def send(self, d, payload):
        self.sent.append((d.id, payload.title))
        return self.outcomes.get(d.id, OK)


def run(store, sender=None, user="u1", times=1):
    lines = []
    sender = sender or FakeSender()
    p = Pusher(store, sender, now=NOW, log=lines.append)
    for _ in range(times):
        p.notify(user, alert_payload("JPY", D("0.2040")), "規則#7")
    return sender, lines


def test_nothing_is_loaded_until_something_is_pushed():
    store = FakeStore(devices=[device()])
    Pusher(store, FakeSender(), now=NOW)
    assert store.loads == 0


def test_kill_switch_and_missing_flag_send_nothing():
    for flags in ({}, {"web_push": {**FLAG_ON["web_push"], "enabled": False}}):
        sender, lines = run(FakeStore(flags=flags, devices=[device()]))
        assert sender.sent == [] and lines == []


def test_rollout_zero_only_admin_and_beta():
    flags = {"web_push": {**FLAG_ON["web_push"], "rollout": 0}}
    sender, _ = run(FakeStore(flags=flags, devices=[device()], roles={"u1": "user"}))
    assert sender.sent == []
    sender, _ = run(FakeStore(flags=flags, devices=[device()], roles={"u1": "beta"}))
    assert sender.sent == [(1, "日圓到價：0.2040")]


def test_no_user_or_no_devices():
    sender, lines = run(FakeStore(devices=[device(user="someone-else")]))
    assert sender.sent == [] and lines == []
    sender, _ = run(FakeStore(devices=[device()]), user=None)
    assert sender.sent == []


def test_daily_limit_per_user():
    sender, lines = run(FakeStore(devices=[device()], sent_today=push.PER_USER_DAILY_LIMIT))
    assert len(sender.sent) == 1
    sender, lines = run(FakeStore(devices=[device()], sent_today=push.PER_USER_DAILY_LIMIT + 1))
    assert sender.sent == []
    assert lines == [f"規則#7: 今天的推播已達上限 {push.PER_USER_DAILY_LIMIT} 則，這次不推播"]


def test_results_are_recorded_and_gone_devices_removed():
    devices = [device(1), device(2), device(3), device(4, fail_count=push.MAX_FAILURES - 1)]
    store = FakeStore(devices=devices)
    sender = FakeSender({2: GONE, 3: FAILED, 4: FAILED})
    _, lines = run(store, sender, times=2)
    assert store.results[:4] == [(1, "ok"), (2, "delete"), (3, "failed"), (4, "delete")]
    assert lines[0] == "規則#7: 推播 1 台成功、1 台已失效、已移除、2 台失敗"
    # 第二則：刪掉的不再送；失敗的那台累計失敗次數
    assert [i for i, _ in sender.sent[4:]] == [1, 3]
    assert store.results[4:] == [(1, "ok"), (3, "failed")]


def test_failures_never_raise_and_load_failure_is_logged_once():
    store = FakeStore(devices=[device()], fail_load=True)
    sender, lines = run(store, times=3)
    assert store.loads == 1 and sender.sent == []
    assert lines == ["注意：讀不到推播設定（RuntimeError），這次不推播，Email 照常"]

    class Boom(FakeSender):
        def send(self, d, payload):
            raise ValueError("https://web.push.apple.com/secret")

    _, lines = run(FakeStore(devices=[device()]), Boom())
    assert lines == ["規則#7: 推播失敗（ValueError），Email 已照常寄出"]


def test_record_failure_does_not_stop_other_devices():
    class Flaky(FakeStore):
        def record_push_result(self, d, outcome, now):
            if d.id == 1:
                raise RuntimeError("write failed")
            super().record_push_result(d, outcome, now)

    store = Flaky(devices=[device(1), device(2)])
    sender, lines = run(store)
    assert [i for i, _ in sender.sent] == [1, 2]
    assert lines == ["規則#7: 推播 2 台成功、1 台的結果沒有寫進資料庫"]


def test_log_lines_never_contain_endpoint_or_keys():
    d = device(1, endpoint="https://web.push.apple.com/VERY-SECRET-ENDPOINT")
    store = FakeStore(devices=[d, device(2)])
    _, lines = run(store, FakeSender({2: GONE}))
    text = "\n".join(lines)
    for secret in ("VERY-SECRET-ENDPOINT", "push.apple.com", d.p256dh, d.auth, "u1"):
        assert secret not in text


def test_payload_type_is_frozen():
    p = Payload("t", "b", "./", "x")
    with pytest.raises(AttributeError):
        p.title = "changed"


# ---------------------------------------------------------------------------
# 和主流程的接法：Email 寄出並記錄之後才推播
# ---------------------------------------------------------------------------


class _MemStore:
    """最小的多人版 store：一條日圓規則（等待中）"""

    def __init__(self, record_ok=True, window=None):
        from fx_alert.rules import Rule
        from fx_alert.store import Watch

        self.watch = Watch(
            key="7",
            label="規則#7",
            rule=Rule("JPY", D("0.2100"), D("0.001")),
            state=None,
            state_target=None,
            recipients=("someone@example.com",),
            user_id="u1",
            window=window,
        )
        self.record_ok = record_ok

    def load_watches(self):
        return [self.watch]

    def count_sent_since(self, since):
        return 0

    def record_alert(self, w, rate, now):
        return self.record_ok

    def record_transition(self, *a):
        return True

    def flush(self):
        return True


class _RecPusher:
    def __init__(self):
        self.calls = []

    def notify(self, user_id, payload, label):
        self.calls.append((user_id, payload.title, label))


def _run(tmp_path, store, notifier=None):
    from fx_alert.main import run_check
    from fx_alert.notifier import EmailConfig

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        f'data_dir: "{tmp_path / "data"}"\n'
        'business_hours: {weekdays_only: false, start: "00:00", end: "23:59"}\n'
        'currencies:\n  JPY: {target: "0.1", buffer: "0.001"}\n',
        encoding="utf-8",
    )
    from fx_alert.config import load_config

    item = {"currKey": "JPY|00", "cash": {"bid": "0.01", "ask": "0.2040"}, "spot": {}}
    board = json.dumps({"updateTime": "2026/11/30 10:05:00", "rates": [item]})
    pusher = _RecPusher()

    class Sent:
        def send(self, msg):
            pass

    result = run_check(
        load_config(cfg_file),
        notifier or Sent(),
        EmailConfig(user="s@example.com", password="", to=()),
        now=NOW,
        fetch=lambda: board,
        log=lambda line: None,
        store=store,
        pusher=pusher,
    )
    return result, pusher


def test_push_after_email_is_sent_and_recorded(tmp_path):
    result, pusher = _run(tmp_path, _MemStore())
    assert result.notified == ["規則#7"]
    assert pusher.calls == [("u1", "日圓到價：0.2040", "規則#7")]


def test_no_push_when_email_fails(tmp_path):
    from fx_alert.notifier import NotifyError

    class Fail:
        def send(self, msg):
            raise NotifyError("smtp down")

    result, pusher = _run(tmp_path, _MemStore(), Fail())
    assert result.errors and pusher.calls == []


def test_no_push_when_rule_was_deleted_meanwhile(tmp_path):
    _, pusher = _run(tmp_path, _MemStore(record_ok=False))
    assert pusher.calls == []


def test_no_push_outside_notify_window(tmp_path):
    from datetime import time

    from fx_alert.notify_window import NotifyWindow

    result, pusher = _run(tmp_path, _MemStore(window=NotifyWindow(time(12, 0), time(13, 0))))
    assert result.outside_window == ["規則#7"] and pusher.calls == []
