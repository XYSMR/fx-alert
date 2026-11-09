from __future__ import annotations

import io
import json
from datetime import datetime, time
from decimal import Decimal as D

import pytest
import requests

from fx_alert import main as main_mod
from fx_alert.config import TAIPEI, AppConfig, BusinessHours
from fx_alert.history import read_alerts
from fx_alert.notifier import DryRunNotifier, EmailConfig
from fx_alert.rules import AlertState, Rule
from fx_alert.state_store import load_states
from fx_alert.store import FileStore, StoreError, SupabaseConfig, SupabaseStore, Watch

NOW = datetime(2026, 11, 9, 10, 7, tzinfo=TAIPEI)
ARMED, TRIG = AlertState.ARMED, AlertState.TRIGGERED
URL = "https://abcd.supabase.co"
KEY = "sb_secret_TESTONLY"


def make_cfg(tmp_path):
    return AppConfig(
        data_dir=tmp_path / "data",
        hours=BusinessHours(weekdays_only=True, start=time(9, 0), end=time(16, 30)),
        rules=(
            Rule("JPY", target=D("0.205"), buffer=D("0.001")),
            Rule("KRW", target=D("0.0250"), buffer=D("0.0002")),
        ),
    )


# ===========================================================================
# FileStore
# ===========================================================================


def test_filestore_first_run_has_no_state(tmp_path):
    store = FileStore(make_cfg(tmp_path), recipients=["me@example.com"])
    watches = store.load_watches()
    assert [(w.key, w.label, w.state, w.state_target) for w in watches] == [
        ("JPY", "JPY", None, None),
        ("KRW", "KRW", None, None),
    ]
    assert watches[0].recipients == ("me@example.com",)
    assert store.flush() is True  # 第一次一定會寫出 state.json


def test_filestore_corrupt_state_raises_store_error(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.state_file.parent.mkdir(parents=True)
    cfg.state_file.write_text("{broken", encoding="utf-8")
    with pytest.raises(StoreError):
        FileStore(cfg, recipients=[]).load_watches()


def test_filestore_record_alert_writes_csv_and_state(tmp_path):
    cfg = make_cfg(tmp_path)
    store = FileStore(cfg, recipients=[])
    jpy = store.load_watches()[0]
    store.record_alert(jpy, D("0.2040"), NOW)
    assert store.flush() is True
    state = load_states(cfg.state_file)["JPY"]
    assert (state.state, state.rate, state.target) == (TRIG, "0.2040", "0.205")
    assert [(a.currency, str(a.rate)) for a in read_alerts(cfg.data_dir)] == [("JPY", "0.2040")]

    # 重新讀取，狀態和觸發時的目標價都帶得回來
    jpy = FileStore(cfg, recipients=[]).load_watches()[0]
    assert (jpy.state, jpy.state_target) == (TRIG, D("0.205"))


def test_filestore_unchanged_state_is_not_rewritten(tmp_path):
    cfg = make_cfg(tmp_path)
    store = FileStore(cfg, recipients=[])
    for w in store.load_watches():
        store.record_transition(w, ARMED, D("1"), NOW)
    assert store.flush() is True
    store = FileStore(cfg, recipients=[])
    for w in store.load_watches():
        assert store.record_transition(w, ARMED, D("1"), NOW) is True
    assert store.flush() is False


def test_filestore_requires_load_first(tmp_path):
    store = FileStore(make_cfg(tmp_path), recipients=[])
    w = Watch("JPY", "JPY", Rule("JPY", D("0.2"), D("0")), None, None, ())
    with pytest.raises(StoreError):
        store.record_transition(w, ARMED, D("0.3"), NOW)
    assert store.flush() is False


# ===========================================================================
# SupabaseStore（用假的 HTTP session）
# ===========================================================================


class FakeResponse:
    def __init__(self, status=200, body=None, reason="OK"):
        self.status_code = status
        self.ok = 200 <= status < 300
        self.reason = reason
        self._body = body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kw):
        self.calls.append({"method": method, "url": url, **kw})
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def sb(*responses):
    session = FakeSession(*responses)
    return SupabaseStore(SupabaseConfig(URL, KEY), session=session), session


ROWS = [
    {
        "id": 7,
        "currency": "JPY",
        "target": "0.205000",
        "effective_buffer": "0.001025",
        "state": "triggered",
        "state_target": "0.210000",
        "profiles": {"email": "alice@example.com", "email_enabled": True},
    },
    {
        "id": 9,
        "currency": "KRW",
        "target": "0.025000",
        "effective_buffer": "0.000200",
        "state": "armed",
        "state_target": None,
        "profiles": {"email": "bob@example.com", "email_enabled": True},
    },
]


# ---- 設定 ----


def test_config_from_env():
    cfg = SupabaseConfig.from_env({"SUPABASE_URL": URL + "/", "SUPABASE_SECRET_KEY": f" {KEY} "})
    assert (cfg.url, cfg.secret_key) == (URL, KEY)
    assert KEY not in repr(cfg)


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"SUPABASE_URL": URL},
        {"SUPABASE_SECRET_KEY": KEY},
        {"SUPABASE_URL": "http://abcd.supabase.co", "SUPABASE_SECRET_KEY": KEY},
    ],
)
def test_config_from_env_rejects_bad_values(env):
    with pytest.raises(StoreError):
        SupabaseConfig.from_env(env)


# ---- 讀取規則 ----


def test_load_watches_request():
    store, session = sb(FakeResponse(body=ROWS))
    store.load_watches()
    (call,) = session.calls
    assert call["method"] == "GET"
    assert call["url"] == f"{URL}/rest/v1/alert_rules"
    assert call["headers"]["apikey"] == KEY
    assert "Authorization" not in call["headers"]  # secret key 不能當 JWT 用
    params = call["params"]
    assert params["enabled"] == "is.true"
    assert params["profiles.email_enabled"] == "is.true"  # Email 關閉的人不檢查
    assert "target::text" in params["select"]  # 數字用文字取回，避免浮點誤差
    assert call["timeout"] > 0


def test_load_watches_parses_rows_exactly():
    store, _ = sb(FakeResponse(body=ROWS))
    jpy, krw = store.load_watches()
    assert jpy.key == "7"
    assert jpy.rule == Rule("JPY", target=D("0.205"), buffer=D("0.001025"))
    assert (jpy.state, jpy.state_target) == (TRIG, D("0.21"))
    assert jpy.recipients == ("alice@example.com",)
    assert (krw.state, krw.state_target) == (ARMED, None)


def test_labels_never_contain_email():
    """label 會進 GitHub Actions 的公開 log。"""
    store, _ = sb(FakeResponse(body=ROWS))
    for w in store.load_watches():
        assert "@" not in w.label
        assert "@" not in repr(w)


@pytest.mark.parametrize(
    "response",
    [
        FakeResponse(401, {"message": "Invalid API key"}, "Unauthorized"),
        FakeResponse(500, ValueError("not json"), "Server Error"),
        requests.ConnectionError(f"cannot reach {URL} with {KEY}"),
        FakeResponse(body=ValueError("not json")),
        FakeResponse(body=[{"id": 1}]),  # 缺欄位
        FakeResponse(body=[{**ROWS[0], "target": "abc"}]),
    ],
)
def test_load_watches_errors_become_store_error_without_secrets(response):
    store, _ = sb(response)
    with pytest.raises(StoreError) as exc_info:
        store.load_watches()
    assert KEY not in str(exc_info.value)
    assert URL not in str(exc_info.value)


# ---- 寫入 ----


def _watch(state=ARMED, key="7"):
    return Watch(key, f"JPY 規則#{key}", Rule("JPY", D("0.205"), D("0.001")), state, None, ())


def test_record_alert_calls_rpc():
    store, session = sb(FakeResponse(body=123))
    store.record_alert(_watch(), D("0.2040"), NOW)
    (call,) = session.calls
    assert call["method"] == "POST"
    assert call["url"] == f"{URL}/rest/v1/rpc/record_alert"
    assert call["json"] == {
        "p_rule_id": 7,
        "p_rate": "0.2040",
        "p_target": "0.205",
        "p_sent_at": "2026-11-09T10:07:00+08:00",
    }
    assert store.flush() is True


def test_record_alert_failure_raises():
    store, _ = sb(FakeResponse(500, {"message": "boom"}, "Server Error"))
    with pytest.raises(StoreError, match="500"):
        store.record_alert(_watch(), D("0.2040"), NOW)
    assert store.flush() is False


def test_record_transition_same_state_sends_nothing():
    store, session = sb()
    assert store.record_transition(_watch(ARMED), ARMED, D("0.21"), NOW) is True
    assert session.calls == []
    assert store.flush() is False


def test_record_transition_only_if_state_unchanged_since_read():
    store, session = sb(FakeResponse(body=[{"id": 7}]))
    assert store.record_transition(_watch(TRIG), ARMED, D("0.2100"), NOW) is True
    (call,) = session.calls
    assert call["method"] == "PATCH"
    assert call["params"] == {"id": "eq.7", "state": "eq.triggered"}
    assert call["json"] == {
        "state": "armed",
        "state_changed_at": "2026-11-09T10:07:00+08:00",
        "state_rate": "0.2100",
        "state_target": "0.205",
    }
    assert store.flush() is True


def test_record_transition_skipped_when_changed_meanwhile():
    """例如使用者剛好重新開啟 Email，資料庫已把規則重設為等待中。"""
    store, _ = sb(FakeResponse(body=[]))
    assert store.record_transition(_watch(TRIG), ARMED, D("0.2100"), NOW) is False
    assert store.flush() is False


# ===========================================================================
# run_check + 自訂 store（多人）
# ===========================================================================

EMAIL = EmailConfig(user="sender@example.com", password="x", to=("single@example.com",))


def mega_json(jpy="0.2040", krw="0.0260"):
    def item(code, ask):
        return {"currKey": f"{code}|00", "cash": {"bid": "0.01", "ask": ask}, "spot": {}}

    return json.dumps(
        {"updateTime": "2026/11/09 10:05:00", "rates": [item("JPY", jpy), item("KRW", krw)]}
    )


class MemoryStore:
    """記錄呼叫的假 store。"""

    def __init__(self, watches, fail_alert_for=(), fail_load=False):
        self.watches = watches
        self.fail_alert_for = fail_alert_for
        self.fail_load = fail_load
        self.alerts, self.transitions = [], []

    def load_watches(self):
        if self.fail_load:
            raise StoreError("Supabase 回應 503：down")
        return self.watches

    def record_alert(self, w, rate, now):
        if w.key in self.fail_alert_for:
            raise StoreError("Supabase 回應 500：boom")
        self.alerts.append((w.key, rate))

    def record_transition(self, w, new_state, rate, now):
        self.transitions.append((w.key, new_state))
        return True

    def flush(self):
        return bool(self.alerts or self.transitions)


def user_watch(key, email, target="0.205", state=None):
    return Watch(key, f"JPY 規則#{key}", Rule("JPY", D(target), D("0.001")), state, None, (email,))


def run(tmp_path, store, notifier=None, logs=None):
    return main_mod.run_check(
        make_cfg(tmp_path),
        notifier or DryRunNotifier(out=io.StringIO()),
        EMAIL,
        now=NOW,
        fetch=lambda: mega_json(),
        log=(logs.append if logs is not None else lambda s: None),
        store=store,
    )


def test_each_user_gets_own_email(tmp_path):
    store = MemoryStore(
        [
            user_watch("1", "alice@example.com"),  # 0.2040 ≤ 0.205 → 通知
            user_watch("2", "bob@example.com", target="0.200"),  # 還沒到價
            user_watch("3", "carol@example.com", target="0.210"),  # 通知
        ]
    )
    n = DryRunNotifier(out=io.StringIO())
    r = run(tmp_path, store, n)
    assert [m["To"] for m in n.sent] == ["alice@example.com", "carol@example.com"]
    assert r.notified == ["JPY 規則#1", "JPY 規則#3"]
    assert [k for k, _ in store.alerts] == ["1", "3"]
    assert store.transitions == [("2", ARMED)]
    assert r.exit_code == 0


def test_store_load_failure_stops_before_fetch(tmp_path):
    fetched = []
    store = MemoryStore([], fail_load=True)
    r = main_mod.run_check(
        make_cfg(tmp_path),
        DryRunNotifier(),
        EMAIL,
        now=NOW,
        fetch=lambda: fetched.append(1) or mega_json(),
        log=lambda s: None,
        store=store,
    )
    assert r.exit_code == 1 and fetched == []


def test_record_failure_for_one_rule_does_not_block_others(tmp_path):
    store = MemoryStore(
        [user_watch("1", "alice@example.com"), user_watch("2", "bob@example.com")],
        fail_alert_for=("1",),
    )
    r = run(tmp_path, store)
    assert r.notified == ["JPY 規則#1", "JPY 規則#2"]  # 兩封都寄了
    assert [k for k, _ in store.alerts] == ["2"]
    assert r.errors == ["JPY 規則#1 狀態寫入失敗：Supabase 回應 500：boom"]


def test_logs_and_errors_never_contain_email(tmp_path):
    store = MemoryStore(
        [user_watch("1", "alice@example.com"), user_watch("2", "bob@example.com")],
        fail_alert_for=("2",),
    )
    logs = []
    r = run(tmp_path, store, logs=logs)
    assert logs and r.errors
    for line in logs + r.errors:
        assert "@" not in line, line
