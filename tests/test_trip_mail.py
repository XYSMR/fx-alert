"""出國換匯計畫：提醒信的內容與用語、store 的旅程讀寫、排程（run_check）的各種情境。"""

from __future__ import annotations

import io
import json
from datetime import date, datetime, time
from decimal import Decimal as D

import pytest

from fx_alert import main as main_mod
from fx_alert.bizdays import load_calendar
from fx_alert.config import TAIPEI, AppConfig, BusinessHours
from fx_alert.notifier import DryRunNotifier, EmailConfig, NotifyError
from fx_alert.notify_window import NotifyWindow
from fx_alert.store import (
    ReadOnlyStore,
    StoreError,
    SupabaseConfig,
    SupabaseStore,
    TripWatch,
)
from fx_alert.trip_mail import build_trip_message, estimate_twd, format_amount, short_date
from fx_alert.trip_rules import (
    CHEAP_FAR,
    FINAL,
    KINDS,
    LAST_CALL,
    NEAR_START,
    Decision,
    Sent,
    schedule,
)
from fx_alert.unsubscribe import UnsubscribeConfig

CAL = load_calendar()
NOW = datetime(2026, 11, 9, 10, 7, tzinfo=TAIPEI)  # 週一；兆豐 seed 有 11/09 之前的收盤價
EMAIL = EmailConfig(user="sender@example.com", password="x", to=())
UID = "0b6f3c1e-1111-4a1a-9a1a-000000000007"
URL = "https://abcdefghijklmnopqrst.supabase.co"
KEY = "sb_secret_TESTONLY"

FORBIDDEN = ["建議", "應該", "最佳", "推薦", "划算", "賺", "虧", "會漲", "會跌", "預測", "預估",
             "投資", "時機", "趁", "必", "保證會", "穩", "撿便宜", "抄底"]  # fmt: skip


# ---------------------------------------------------------------------------
# 信件
# ---------------------------------------------------------------------------


def message(kind, *, rank=None, window=None, rate=D("0.2049"), departure=date(2027, 1, 17),
            today=date(2026, 11, 20), remaining=D("30000"), unsub=None):  # fmt: skip
    d = Decision(kind, (departure - today).days, rank=rank, window=window, remaining=remaining)
    return build_trip_message(
        EMAIL,
        currency="JPY",
        decision=d,
        sch=schedule(departure, CAL),
        today=today,
        rate=rate,
        quoted_at=datetime(2026, 11, 20, 9, 30, tzinfo=TAIPEI),
        remaining=remaining,
        to=["alice@example.com"],
        unsubscribe=unsub,
    )


def test_cheap_message_explains_why():
    msg = message(CHEAP_FAR, rank=2, window=19)
    assert msg["Subject"] == "[FX Alert] 換匯計畫：日圓 0.2049，近 30 天第 2 低（距離出發 58 天）"
    body = msg.get_content()
    assert "在近 30 天（19 個營業日的收盤價）中是第 2 低" in body
    assert "距離出發還有 58 天" in body
    assert "還沒換：30,000 日圓（以現在的價格約 6,147 元台幣" in body
    assert "  12/24（四） 可以考慮分批換的提醒" in body
    assert "  1/8（五） 最後換匯提醒" in body
    assert "  1/14（四） 出發前提醒" in body
    assert "※ 僅供參考，不保證未來匯率走勢" in body
    assert msg["To"] == "alice@example.com"


def test_fixed_messages():
    near = message(NEAR_START, today=date(2026, 12, 24))
    assert near["Subject"] == "[FX Alert] 換匯計畫：距離出發 24 天，可以考慮分批換日圓"
    assert "可以考慮分批換，不用一次換完" in near.get_content()
    assert "12/24（四） 可以考慮分批換" not in near.get_content()  # 已過的不列
    final = message(FINAL, today=date(2027, 1, 8))
    assert final["Subject"] == "[FX Alert] 換匯計畫：日圓最後換匯提醒（距離出發 9 天）"
    last = message(LAST_CALL, today=date(2027, 1, 14))
    assert last["Subject"] == "[FX Alert] 換匯計畫：日圓出發前提醒（1/17（日） 出發）"
    assert "出發前最後一個營業日是 1/15（五）" in last.get_content()
    assert "接下來會在這些日子寄信" not in last.get_content()


def test_no_rate_still_sends_fixed_message():
    body = message(FINAL, today=date(2027, 1, 8), rate=None).get_content()
    assert "元台幣" not in body and "牌告時間" not in body
    assert "最後換匯提醒（不看價格）" in body


@pytest.mark.parametrize("kind", KINDS)
def test_wording_is_neutral(kind):
    msg = message(kind, rank=1, window=20)
    text = msg["Subject"] + msg.get_content()
    for w in FORBIDDEN:
        assert w not in text, f"{kind} 出現「{w}」"
    assert "僅供參考，不保證未來匯率走勢" in text


def test_unsubscribe_header_and_link():
    unsub = UnsubscribeConfig(secret="s" * 32, supabase_url=URL)
    msg = message(CHEAP_FAR, rank=1, window=20, unsub=unsub.links(UID))
    assert msg["List-Unsubscribe"].startswith(f"<{URL}/functions/v1/unsubscribe?")
    assert msg["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert "取消訂閱：https://xysmr.github.io/fx-alert/unsubscribe.html?" in msg.get_content()


def test_helpers():
    assert short_date(date(2027, 1, 17)) == "1/17（日）"
    assert format_amount(D("50000.00")) == "50,000"
    assert format_amount(D("1234.50")) == "1,234.5"
    assert estimate_twd(D("1000000"), D("0.0235")) == "23,500"
    assert estimate_twd(D("1"), None) is None


# ---------------------------------------------------------------------------
# SupabaseStore
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code = status
        self.ok = 200 <= status < 300
        self.reason = "x"
        self._body = body
        self.headers = headers or {}

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kw):
        self.calls.append({"method": method, "url": url, **kw})
        return self.responses.pop(0)


TRIP_ROW = {
    "id": 12,
    "user_id": UID,
    "currency": "JPY",
    "departure": "2026-12-20",
    "remaining": "30000.00",
    "created_on": "2026-11-01",
    "email": "alice@example.com",
    "notify_start": "08:00:00",
    "notify_end": "22:00:00",
    "sent": [{"kind": "cheap_far", "on": "2026-11-02"}],
}


def test_load_trips_parses_rows():
    session = FakeSession(FakeResponse(body=[TRIP_ROW]))
    (t,) = SupabaseStore(SupabaseConfig(URL, KEY), session=session).load_trips()
    assert session.calls[0]["url"] == f"{URL}/rest/v1/active_trips"
    assert "remaining::text" in session.calls[0]["params"]["select"]
    assert (t.key, t.label, t.departure, t.created_on, t.remaining) == (
        "12", "JPY 旅程#12", date(2026, 12, 20), date(2026, 11, 1), D("30000"))  # fmt: skip
    assert t.sent == (Sent("cheap_far", date(2026, 11, 2)),)
    assert t.window == NotifyWindow(time(8), time(22))
    assert "@" not in t.label and "@" not in repr(t) and "30000" not in repr(t)


def test_load_trips_when_table_missing():
    missing = FakeResponse(404, {"code": "PGRST205", "message": "Could not find the table"})
    store = SupabaseStore(SupabaseConfig(URL, KEY), session=FakeSession(missing))
    assert store.load_trips() == []
    assert store.trips_supported is False


def test_load_trips_other_error_raises():
    store = SupabaseStore(SupabaseConfig(URL, KEY), session=FakeSession(FakeResponse(500, {})))
    with pytest.raises(StoreError):
        store.load_trips()


def trip_watch(**kw):
    base = dict(key="12", label="JPY 旅程#12", currency="JPY", departure=date(2026, 12, 20),
                created_on=date(2026, 11, 1), remaining=D("30000"), sent=(),
                recipients=("alice@example.com",), user_id=UID, window=None)  # fmt: skip
    return TripWatch(**{**base, **kw})


def test_record_trip_notice_payload_and_conflict():
    d = Decision(CHEAP_FAR, 41, rank=1, window=19, remaining=D("30000"))
    session = FakeSession(FakeResponse(201), FakeResponse(409, {"message": "duplicate key"}))
    store = SupabaseStore(SupabaseConfig(URL, KEY), session=session)
    assert store.record_trip_notice(trip_watch(), d, D("0.2040"), NOW) is True
    assert session.calls[0]["json"] == {
        "trip_id": 12, "user_id": UID, "kind": "cheap_far", "sent_on": "2026-11-09",
        "sent_at": "2026-11-09T10:07:00+08:00", "rate": "0.2040", "rank": 1,
        "window_days": 19, "days_left": 41, "remaining": "30000",
    }  # fmt: skip
    assert store.record_trip_notice(trip_watch(), d, D("0.2040"), NOW) is False
    assert store.flush() is False  # 換匯提醒不影響 state 分支的 commit


def test_read_only_store_does_not_write_trip_notices():
    session = FakeSession(FakeResponse(body=[TRIP_ROW]))
    logs = []
    store = ReadOnlyStore(SupabaseStore(SupabaseConfig(URL, KEY), session=session), log=logs.append)
    (t,) = store.load_trips()
    assert store.record_trip_notice(t, Decision(FINAL, 7), None, NOW) is True
    assert [c["method"] for c in session.calls] == ["GET"]
    assert logs == ["[dry-run] JPY 旅程#12: 不寫入換匯提醒 final"]


# ---------------------------------------------------------------------------
# run_check
# ---------------------------------------------------------------------------


def make_cfg(tmp_path, daily_limit=50):
    return AppConfig(
        data_dir=tmp_path / "data",
        hours=BusinessHours(weekdays_only=False, start=time(0, 0), end=time(23, 59)),
        rules=(),
        daily_limit=daily_limit,
    )


def mega_json(jpy="0.2040", when="2026/11/09 10:05:00"):
    item = {"currKey": "JPY|00", "cash": {"bid": "0.01", "ask": jpy}, "spot": {}}
    return json.dumps({"updateTime": when, "rates": [item]})


class TripStore:
    def __init__(self, trips, sent_today=0, trip_sent_today=0, record_ok=True, fail_record=False):
        self.trips = trips
        self.sent_today, self.trip_sent_today = sent_today, trip_sent_today
        self.record_ok, self.fail_record = record_ok, fail_record
        self.notices = []

    def load_watches(self):
        return []

    def count_sent_since(self, since):
        return self.sent_today

    def count_trip_sent_since(self, since):
        return self.trip_sent_today

    def load_trips(self):
        return self.trips

    def record_trip_notice(self, t, d, rate, now):
        if self.fail_record:
            raise StoreError("Supabase 回應 500：boom")
        self.notices.append((t.key, d.kind, rate))
        return self.record_ok

    def flush(self):
        return False


def run(tmp_path, store, *, notifier=None, fetch=None, now=NOW, logs=None, daily_limit=50):
    return main_mod.run_check(
        make_cfg(tmp_path, daily_limit),
        notifier or DryRunNotifier(out=io.StringIO()),
        EMAIL,
        now=now,
        fetch=fetch or (lambda: mega_json()),
        log=(logs.append if logs is not None else lambda s: None),
        store=store,
    )


def test_cheap_far_is_sent_and_recorded(tmp_path):
    store = TripStore([trip_watch()])
    n = DryRunNotifier(out=io.StringIO())
    logs = []
    r = run(tmp_path, store, notifier=n, logs=logs)
    assert r.exit_code == 0
    assert r.trip_notified == ["JPY 旅程#12: cheap_far"]
    assert store.notices == [("12", "cheap_far", D("0.2040"))]
    (msg,) = n.sent
    assert msg["To"] == "alice@example.com"
    assert "近 30 天第 1 低（距離出發 41 天）" in msg["Subject"]
    # 公開的 log 不能有 email、金額
    assert all("@" not in line and "30000" not in line and "30,000" not in line for line in logs)


def test_price_alert_needs_todays_quote(tmp_path):
    """牌告還是昨天的（早上 8 點）：不判斷相對便宜。"""
    store = TripStore([trip_watch()])
    r = run(tmp_path, store, fetch=lambda: mega_json(when="2026/11/06 16:04:00"))
    assert r.trip_notified == [] and store.notices == []


def test_fixed_reminder_without_fresh_quote(tmp_path):
    # 11/16（一）出發：最後提醒 = min(D-7 = 11/09, 倒數第 3 個營業日 11/11) = 11/09
    store = TripStore([trip_watch(departure=date(2026, 11, 16))])
    r = run(tmp_path, store, fetch=lambda: mega_json(jpy="0.2300", when="2026/11/06 16:04:00"))
    assert r.trip_notified == ["JPY 旅程#12: final"]


def test_outside_window_waits(tmp_path):
    store = TripStore([trip_watch(window=NotifyWindow(time(12), time(22)))])
    logs = []
    r = run(tmp_path, store, logs=logs)
    assert r.trip_notified == [] and store.notices == []
    assert any("不在通知時段" in line for line in logs)


def test_daily_limit_counts_both_kinds(tmp_path):
    store = TripStore([trip_watch()], sent_today=3, trip_sent_today=2)
    r = run(tmp_path, store, daily_limit=5)
    assert r.deferred == ["JPY 旅程#12"] and store.notices == []
    assert r.exit_code == 0


def test_already_sent_today_or_cooldown(tmp_path):
    store = TripStore([trip_watch(sent=(Sent(CHEAP_FAR, date(2026, 11, 5)),))])
    assert run(tmp_path, store).trip_notified == []  # 7 天冷卻


def test_send_failure_is_error_and_not_recorded(tmp_path):
    class Boom:
        def send(self, msg):
            raise NotifyError("寄信失敗：alice@example.com refused")

    store = TripStore([trip_watch()])
    r = run(tmp_path, store, notifier=Boom())
    assert store.notices == []
    assert r.errors == ["JPY 旅程#12 換匯提醒寄送失敗：寄信失敗：*** refused"]


def test_record_conflict_is_only_a_note(tmp_path):
    logs = []
    r = run(tmp_path, TripStore([trip_watch()], record_ok=False), logs=logs)
    assert r.exit_code == 0
    assert "JPY 旅程#12: 提醒紀錄已存在或旅程剛被刪除，略過記錄" in logs


def test_record_failure_is_error(tmp_path):
    r = run(tmp_path, TripStore([trip_watch()], fail_record=True))
    assert r.errors == ["JPY 旅程#12 換匯提醒紀錄寫入失敗：Supabase 回應 500：boom"]


def test_weekend_sends_nothing(tmp_path):
    sat = datetime(2026, 11, 14, 10, 7, tzinfo=TAIPEI)
    store = TripStore([trip_watch()])
    r = run(tmp_path, store, now=sat, fetch=lambda: mega_json(when="2026/11/14 10:05:00"))
    assert r.trip_notified == []


def test_store_without_trips_is_untouched(tmp_path):
    class Plain(TripStore):
        load_trips = None

    r = run(tmp_path, Plain([]))
    assert r.exit_code == 0 and r.trip_notified == []
