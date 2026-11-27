"""公開 log 不能出現使用者的個人資料（GitHub Actions 的 log 在 public repo 任何人都看得到）。

走真正的 CLI（python -m fx_alert check --mode multi）：資料庫回應、寄信都換成假的，
資料裡放容易辨認的目標價、buffer、email、user_id、通知時段、外幣金額、出發日，
連寄信失敗的錯誤訊息（帶 email）也跑一次，檢查 stdout + stderr 裡都找不到。
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal as D

import pytest

from fx_alert import main as main_mod
from fx_alert.config import TAIPEI
from fx_alert.notifier import NotifyError
from fx_alert.store import SupabaseConfig, SupabaseStore

NOW = datetime(2026, 11, 9, 10, 7, tzinfo=TAIPEI)  # 週一
URL = "https://abcdefghijklmnopqrst.supabase.co"

# 這些值都不能出現在 log 裡
TARGET, BUFFER = "0.2077", "0.0013"
ALICE, CAROL = "secret.alice@example.com", "fail.carol@example.com"
ALICE_ID = "0b6f3c1e-1111-4a1a-9a1a-000000000001"
CAROL_ID = "0b6f3c1e-3333-4a1a-9a1a-000000000003"
WINDOW = ("12:00:00", "13:30:00")  # 不在時段內的那個人
AMOUNT = "98765.43"
DEPARTURE = "2026-12-21"  # 距離出發 42 天
SECRETS = [TARGET, "0.207", BUFFER, ALICE, CAROL, "@", ALICE_ID, CAROL_ID, "12:00", "13:30",
           "98765", "98,765", DEPARTURE, "12/21", "42 天"]  # fmt: skip


def rule(id_, email, user_id, window=("00:00:00", "00:00:00")):
    return {
        "id": id_,
        "user_id": user_id,
        "currency": "JPY",
        "target": TARGET + "00",
        "effective_buffer": BUFFER + "00",
        "state": "armed",
        "state_target": None,
        "email": email,
        "notify_start": window[0],
        "notify_end": window[1],
    }


TRIP = {
    "id": 12,
    "user_id": ALICE_ID,
    "currency": "JPY",
    "departure": DEPARTURE,
    "remaining": AMOUNT,
    "created_on": "2026-11-01",
    "email": ALICE,
    "notify_start": "00:00:00",
    "notify_end": "00:00:00",
    "sent": [],
}


class Resp:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code, self.ok, self.reason = status, 200 <= status < 300, "x"
        self._body, self.headers = body, headers or {}

    def json(self):
        return self._body


# 手機推播：推播網址、金鑰也不能出現在 log 裡
ENDPOINT = "https://web.push.apple.com/ENDPOINT-SECRET-TOKEN"
P256DH, AUTH = "B" + "P" * 86, "A" * 22
PUSH_SECRETS = ["ENDPOINT-SECRET-TOKEN", "push.apple.com", P256DH, AUTH]


class Session:
    """依網址回應：讀規則、讀旅程、計數、寫入都成功。"""

    def request(self, method, url, **kw):
        path = url.split("/rest/v1/")[1]
        if method == "GET" and path == "feature_flags":
            return Resp(body=[{"flag": "web_push", "enabled": True, "rollout": 100,
                               "allowed_roles": ["admin", "beta"]}])  # fmt: skip
        if method == "GET" and path == "push_subscriptions":
            return Resp(body=[{"id": 5, "user_id": ALICE_ID, "endpoint": ENDPOINT,
                               "p256dh": P256DH, "auth": AUTH, "fail_count": 0}])  # fmt: skip
        if method == "GET" and path == "profiles":
            return Resp(body=[{"user_id": ALICE_ID, "role": "user"}])
        if method == "GET" and path == "active_rules":
            return Resp(body=[rule(1, ALICE, ALICE_ID), rule(2, ALICE, ALICE_ID, WINDOW),
                              rule(3, CAROL, CAROL_ID)])  # fmt: skip
        if method == "GET" and path == "active_trips":
            return Resp(body=[TRIP])
        if method == "GET":  # 今天寄了幾封
            return Resp(200, [], headers={"Content-Range": "*/0"})
        if path == "rpc/record_alert":
            return Resp(body=1)
        return Resp(201)


class Notifier:
    """寄給 carol 的信失敗，錯誤訊息帶 email（和 smtplib 的 SMTPRecipientsRefused 一樣）。"""

    def __init__(self, cfg):
        self.sent = []

    def send(self, msg):
        if CAROL in str(msg["To"]):
            raise NotifyError(f"寄信失敗：SMTPRecipientsRefused: {{'{CAROL}': (550, b'no')}}")
        self.sent.append(msg)


class FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW


def mega_json():
    item = {"currKey": "JPY|00", "cash": {"bid": "0.01", "ask": "0.2040"}, "spot": {}}
    return json.dumps({"updateTime": "2026/11/09 10:05:00", "rates": [item]})


@pytest.fixture
def cli(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f'data_dir: "{tmp_path / "data"}"\n'
        'business_hours: {weekdays_only: false, start: "00:00", end: "23:59"}\n'
        'currencies:\n  JPY: {target: "0.1", buffer: "0.001"}\n',
        encoding="utf-8",
    )
    env = {
        "SUPABASE_URL": URL,
        "SUPABASE_SECRET_KEY": "sb_secret_TESTONLY",
        "SMTP_USER": "sender@example.com",
        "SMTP_PASSWORD": "x",
        "UNSUBSCRIBE_SECRET": "s" * 40,
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    fake_store = SupabaseStore(SupabaseConfig(URL, "k"), session=Session())
    monkeypatch.setattr(main_mod, "SupabaseStore", lambda c: fake_store)
    monkeypatch.setattr(main_mod, "SmtpNotifier", Notifier)
    monkeypatch.setattr(main_mod, "fetch_rates_json", mega_json)
    monkeypatch.setattr(main_mod, "datetime", FrozenDatetime)
    return ["check", "--mode", "multi", "--config", str(cfg)]


def test_check_output_has_no_personal_data(cli, capsys):
    code = main_mod.main(cli)
    out = capsys.readouterr()
    text = out.out + out.err
    # 確認情境真的有跑到：寄出、時段外、寄信失敗、換匯提醒
    assert "規則#1: armed → triggered：到價" in text
    assert "規則#2: 不在通知時段，時段開始後重新判斷" in text
    assert "規則#3 通知失敗" in text and code == 1
    assert "旅程#12: 寄出換匯提醒 cheap_far" in text
    for secret in SECRETS:
        assert secret not in text, f"log 出現了「{secret}」"


def test_dry_run_output_has_no_personal_data(cli, capsys):
    main_mod.main([*cli, "--dry-run"])
    text = "".join(capsys.readouterr())
    assert "[dry-run] 不寄出：收件人 1 位" in text
    for secret in SECRETS:
        assert secret not in text, f"log 出現了「{secret}」"


def test_validate_config_prints_no_targets(capsys):
    main_mod.main(["validate-config"])
    out = capsys.readouterr().out
    assert "config.yaml 的規則" in out
    for value in ("0.205", "0.0245", "0.001", "0.0002"):
        assert value not in out


def test_labels_are_numbers_only():
    from fx_alert.store import _row_to_trip, _row_to_watch

    assert _row_to_watch(rule(7, ALICE, ALICE_ID)).label == "規則#7"
    assert _row_to_trip(TRIP).label == "旅程#12"
    assert D(TARGET)  # 只是確認測試資料是合法數字


def test_push_output_has_no_personal_data(cli, capsys, monkeypatch):
    """推播開啟：推播服務的錯誤訊息帶推播網址，log 裡也只能看到「規則#N：推播…」"""
    monkeypatch.setenv("WEB_PUSH_PRIVATE_KEY", "k" * 43)

    class Exc(Exception):
        def __init__(self):
            super().__init__(f"Push failed: 410 Gone {ENDPOINT} {P256DH}")
            self.response = type("R", (), {"status_code": 410})()

    def fake_webpush(**kw):
        raise Exc()

    import pywebpush

    monkeypatch.setattr(pywebpush, "webpush", fake_webpush)
    assert main_mod.main(cli) == 1  # carol 的信寄失敗（和上面相同）
    out = capsys.readouterr()
    text = out.out + out.err
    assert "規則#1: 推播 0 台成功、1 台已失效、已移除" in text
    assert "旅程#12: 推播" not in text  # 同一台已經移除，不會再送
    for secret in SECRETS + PUSH_SECRETS:
        assert secret not in text, secret
