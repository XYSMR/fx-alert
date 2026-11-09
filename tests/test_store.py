from __future__ import annotations

import io
import json
from datetime import datetime, time
from decimal import Decimal as D

import pytest
import requests

from fx_alert import main as main_mod
from fx_alert.config import TAIPEI, AppConfig, BusinessHours
from fx_alert.history import append_alert, read_alerts
from fx_alert.notifier import DryRunNotifier, EmailConfig, NotifyError
from fx_alert.rules import AlertState, Rule
from fx_alert.state_store import load_states
from fx_alert.store import (
    FileStore,
    ReadOnlyStore,
    StoreError,
    SupabaseConfig,
    SupabaseStore,
    Watch,
)

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


def test_filestore_counts_alerts_since(tmp_path):
    cfg = make_cfg(tmp_path)
    store = FileStore(cfg, recipients=[])
    assert store.count_sent_since(NOW) == 0  # 還沒有 alerts.csv
    for at in [
        "2026-11-08T16:00:00+08:00",
        "2026-11-09T00:00:00+08:00",
        "2026-11-09T09:30:00+08:00",
    ]:
        append_alert(cfg.data_dir, datetime.fromisoformat(at), "JPY", D("0.2"), D("0.205"))
    day_start = NOW.replace(hour=0, minute=0)
    assert store.count_sent_since(day_start) == 2


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
    def __init__(self, status=200, body=None, reason="OK", headers=None):
        self.status_code = status
        self.ok = 200 <= status < 300
        self.reason = reason
        self._body = body
        self.headers = headers or {}

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
        "user_id": "0b6f3c1e-1111-4a1a-9a1a-000000000007",
        "currency": "JPY",
        "target": "0.205000",
        "effective_buffer": "0.001025",
        "state": "triggered",
        "state_target": "0.210000",
        "email": "alice@example.com",
    },
    {
        "id": 9,
        "user_id": "0b6f3c1e-2222-4a1a-9a1a-000000000009",
        "currency": "KRW",
        "target": "0.025000",
        "effective_buffer": "0.000200",
        "state": "armed",
        "state_target": None,
        "email": "bob@example.com",
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
    # active_rules 已經排除停用的規則和 Email 關閉的人（tests/db 有測）
    assert call["url"] == f"{URL}/rest/v1/active_rules"
    assert call["headers"]["apikey"] == KEY
    assert "Authorization" not in call["headers"]  # secret key 不能當 JWT 用
    assert "target::text" in call["params"]["select"]  # 數字用文字取回，避免浮點誤差
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
    assert store.record_alert(_watch(), D("0.2040"), NOW) is True
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


def test_record_alert_returns_false_when_rule_was_deleted():
    """資料庫的 record_alert 回傳 null：規則或帳號在檢查期間被刪除。"""
    store, _ = sb(FakeResponse(body=None))
    assert store.record_alert(_watch(), D("0.2040"), NOW) is False
    assert store.flush() is False


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


def test_count_sent_since():
    store, session = sb(FakeResponse(206, [{"id": 1}], headers={"Content-Range": "0-0/12"}))
    assert store.count_sent_since(NOW) == 12
    (call,) = session.calls
    assert call["url"] == f"{URL}/rest/v1/notifications"
    assert call["params"]["sent_at"] == "gte.2026-11-09T10:07:00+08:00"
    assert call["headers"]["Prefer"] == "count=exact"


def test_count_sent_since_zero_and_missing_header():
    store, _ = sb(FakeResponse(200, [], headers={"Content-Range": "*/0"}))
    assert store.count_sent_since(NOW) == 0
    store, _ = sb(FakeResponse(200, []))
    with pytest.raises(StoreError):
        store.count_sent_since(NOW)


# ---- ReadOnlyStore（多人版 --dry-run）----


def test_read_only_store_reads_but_never_writes():
    inner, session = sb(FakeResponse(body=ROWS), FakeResponse(headers={"Content-Range": "*/3"}))
    logs = []
    store = ReadOnlyStore(inner, log=logs.append)
    jpy, krw = store.load_watches()
    assert store.count_sent_since(NOW) == 3
    store.record_alert(krw, D("0.0240"), NOW)
    assert store.record_transition(jpy, ARMED, D("0.2200"), NOW) is True
    assert store.flush() is False
    assert [c["method"] for c in session.calls] == ["GET", "GET"]  # 沒有任何寫入
    assert len(logs) == 2 and all("@" not in line for line in logs)


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

    def __init__(self, watches, fail_alert_for=(), fail_load=False, sent_today=0, deleted=()):
        self.watches = watches
        self.deleted = deleted  # 檢查期間被刪除的規則（帳號已刪除）
        self.sent_today = sent_today
        self.count_since = None
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
        if w.key in self.deleted:
            return False
        self.alerts.append((w.key, rate))
        return True

    def record_transition(self, w, new_state, rate, now):
        if w.key in self.deleted:
            return False
        self.transitions.append((w.key, new_state))
        return True

    def flush(self):
        return bool(self.alerts or self.transitions)

    def count_sent_since(self, since):
        self.count_since = since
        return self.sent_today


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


# ---- 每日寄信上限 ----


def test_daily_limit_defers_extra_notifications(tmp_path):
    store = MemoryStore(
        [user_watch(str(i), f"u{i}@example.com") for i in (1, 2, 3)], sent_today=398
    )
    n = DryRunNotifier(out=io.StringIO())
    r = run(tmp_path, store, n)  # 上限 400，今天已寄 398 → 只能再寄 2 封
    assert r.notified == ["JPY 規則#1", "JPY 規則#2"]
    assert r.deferred == ["JPY 規則#3"]
    assert [k for k, _ in store.alerts] == ["1", "2"]  # 規則#3 的狀態不更新，下次再試
    assert len(n.sent) == 2
    assert r.exit_code == 0  # 延後不算錯誤
    assert store.count_since == NOW.replace(hour=0, minute=0)  # 從台灣時間今天 00:00 算


def test_daily_limit_reached_sends_nothing(tmp_path):
    store = MemoryStore([user_watch("1", "a@example.com")], sent_today=400)
    logs = []
    r = run(tmp_path, store, logs=logs)
    assert r.notified == [] and r.deferred == ["JPY 規則#1"] and store.alerts == []
    assert any("延後 1 封" in line for line in logs)


def test_failed_send_does_not_use_up_daily_quota(tmp_path):
    class FailFirst:
        def __init__(self):
            self.sent = []

        def send(self, msg):
            if not self.sent and msg["To"] == "a@example.com":
                self.sent.append(None)
                raise NotifyError("boom")
            self.sent.append(msg)

    store = MemoryStore(
        [user_watch("1", "a@example.com"), user_watch("2", "b@example.com")], sent_today=399
    )
    r = run(tmp_path, store, FailFirst())
    assert r.notified == ["JPY 規則#2"] and r.deferred == []


def test_count_failure_stops_sending(tmp_path):
    class NoCount(MemoryStore):
        def count_sent_since(self, since):
            raise StoreError("Supabase 回應 503：down")

    n = DryRunNotifier(out=io.StringIO())
    r = run(tmp_path, NoCount([user_watch("1", "a@example.com")]), n)
    assert r.exit_code == 1 and n.sent == []


# ---- 錯誤訊息遮蔽 email ----


def test_send_error_with_address_is_redacted(tmp_path):
    class Refused:
        def send(self, msg):
            raise NotifyError(f"寄信失敗：SMTPRecipientsRefused: {{'{msg['To']}': (550, b'no')}}")

    store = MemoryStore([user_watch("1", "alice@example.com")])
    r = run(tmp_path, store, Refused())
    assert r.errors == [
        "JPY 規則#1 通知失敗，狀態不更新：寄信失敗：SMTPRecipientsRefused: {'***': (550, b'no')}"
    ]


# ---- 檢查期間有人刪除帳號 ----


def test_deleted_user_during_check_is_skipped_without_failing(tmp_path):
    """規則讀出來之後、寫入之前，alice 刪除了帳號：只略過她，bob 照常，整次排程成功。"""
    store = MemoryStore(
        [
            user_watch("1", "alice@example.com"),  # 到價 → 寄信後記錄時發現已刪除
            user_watch("2", "bob@example.com"),  # 到價 → 正常記錄
            user_watch("3", "alice@example.com", target="0.200", state=AlertState.TRIGGERED),
        ],
        deleted=("1", "3"),
    )
    logs = []
    r = run(tmp_path, store, logs=logs)
    assert r.exit_code == 0 and r.errors == []
    assert [k for k, _ in store.alerts] == ["2"]
    assert any("JPY 規則#1: 規則已不存在" in line for line in logs)
    assert any("JPY 規則#3: 規則在檢查期間被變更或刪除" in line for line in logs)
    assert all("@" not in line for line in logs)


def test_supabase_store_deleted_rule_end_to_end(tmp_path):
    """SupabaseStore 對真正的回應格式：讀到 2 條規則，其中 1 條在寫入時已被刪除。"""
    rows = [
        {**ROWS[0], "id": 1, "state": "armed", "target": "0.300000", "state_target": None},
        {
            **ROWS[0],
            "id": 2,
            "state": "armed",
            "target": "0.300000",
            "state_target": None,
            "email": "bob@example.com",
        },
    ]
    session = FakeSession(
        FakeResponse(body=rows),  # load_watches
        FakeResponse(200, [], headers={"Content-Range": "*/0"}),  # count_sent_since
        FakeResponse(body=None),  # record_alert #1 → null（帳號已刪除）
        FakeResponse(body=42),  # record_alert #2
    )
    store = SupabaseStore(SupabaseConfig(URL, KEY), session=session)
    n = DryRunNotifier(out=io.StringIO())
    logs = []
    r = main_mod.run_check(
        make_cfg(tmp_path),
        n,
        EMAIL,
        now=NOW,
        fetch=lambda: mega_json(),
        log=logs.append,
        store=store,
    )
    assert r.exit_code == 0 and r.errors == []
    assert r.notified == ["JPY 規則#1", "JPY 規則#2"]
    assert any("規則#1: 規則已不存在" in line for line in logs)
    assert r.state_changed is True
