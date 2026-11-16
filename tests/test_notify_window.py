"""通知時段：時段外到價不寄、狀態不變；時段開始後重新判斷，不補寄。"""

import dataclasses
import io
from datetime import datetime, time

import pytest
from test_store import (
    EMAIL,
    FakeResponse,
    MemoryStore,
    make_cfg,
    mega_json,
    sb,
    user_watch,
)

from fx_alert import main as main_mod
from fx_alert.config import TAIPEI
from fx_alert.notifier import DryRunNotifier
from fx_alert.notify_window import NotifyWindow, parse_window
from fx_alert.rules import AlertState
from fx_alert.store import ReadOnlyStore

DEFAULT = NotifyWindow(time(8, 0), time(22, 0))
NIGHT = NotifyWindow(time(22, 0), time(7, 0))  # 跨午夜
ALL_DAY = NotifyWindow(time(0, 0), time(0, 0))


def at(h, m=0):
    return datetime(2026, 11, 9, h, m, tzinfo=TAIPEI)


@pytest.mark.parametrize(
    ("window", "hour", "minute", "inside"),
    [
        (DEFAULT, 8, 0, True),  # 開始時間包含在內
        (DEFAULT, 7, 35, False),
        (DEFAULT, 21, 35, True),
        (DEFAULT, 22, 0, False),  # 結束時間不含
        (DEFAULT, 23, 5, False),
        (NIGHT, 23, 5, True),
        (NIGHT, 2, 35, True),
        (NIGHT, 7, 0, False),
        (NIGHT, 12, 5, False),
        (ALL_DAY, 3, 5, True),
    ],
)
def test_contains(window, hour, minute, inside):
    assert window.contains(at(hour, minute)) is inside


def test_contains_uses_taipei_time():
    from datetime import UTC

    # UTC 23:30 = 台灣 07:30（不在 08:00–22:00）；UTC 00:05 = 台灣 08:05
    assert DEFAULT.contains(datetime(2026, 11, 8, 23, 30, tzinfo=UTC)) is False
    assert DEFAULT.contains(datetime(2026, 11, 9, 0, 5, tzinfo=UTC)) is True


def test_label_and_parse():
    assert DEFAULT.label() == "08:00–22:00"
    assert ALL_DAY.label() == "全天"
    assert parse_window("22:00:00", "07:30:00") == NotifyWindow(time(22, 0), time(7, 30))
    assert parse_window(None, None) is None  # 還沒套用 migration：全天


def windowed(key, email, window, **kw):
    return dataclasses.replace(user_watch(key, email, **kw), window=window)


def run_at(tmp_path, store, now, notifier=None, logs=None, jpy="0.2040"):
    cfg = dataclasses.replace(make_cfg(tmp_path), hours=None)  # 24 小時檢查（第 6 步）的情況
    return main_mod.run_check(
        cfg,
        notifier or DryRunNotifier(out=io.StringIO()),
        EMAIL,
        now=now,
        fetch=lambda: mega_json(jpy=jpy),
        log=(logs.append if logs is not None else lambda s: None),
        store=store,
        force=True,
    )


def test_outside_window_does_not_send_or_change_state(tmp_path):
    """凌晨 02:35 到價：不寄、不寫狀態（還是等待中），log 不含 email。"""
    store = MemoryStore(
        [
            windowed("1", "alice@example.com", DEFAULT),  # 時段外
            windowed("2", "bob@example.com", NIGHT),  # 夜貓子：時段內 → 寄
        ]
    )
    n, logs = DryRunNotifier(out=io.StringIO()), []
    r = run_at(tmp_path, store, at(2, 35), n, logs)
    assert [m["To"] for m in n.sent] == ["bob@example.com"]
    assert r.outside_window == ["JPY 規則#1"]
    assert [k for k, _ in store.alerts] == ["2"]
    assert store.transitions == []  # 規則#1 狀態不變
    assert "JPY 規則#1: 已到價，但不在通知時段（台灣時間 08:00–22:00），時段開始後重新判斷" in logs
    assert not any("alice@" in line for line in logs)
    assert r.exit_code == 0


def test_window_opens_and_still_below_target_sends(tmp_path):
    """08:05（時段開始後第一次檢查）還在目標以下 → 寄。"""
    store = MemoryStore([windowed("1", "alice@example.com", DEFAULT, state=AlertState.ARMED)])
    n = DryRunNotifier(out=io.StringIO())
    run_at(tmp_path, store, at(8, 5), n)
    assert len(n.sent) == 1 and [k for k, _ in store.alerts] == ["1"]


def test_window_opens_but_rate_recovered_does_not_send(tmp_path):
    """夜裡到價、08:05 已經回升到目標以上 → 不寄、繼續等待（不補寄）。"""
    store = MemoryStore([windowed("1", "alice@example.com", DEFAULT, state=AlertState.ARMED)])
    n = DryRunNotifier(out=io.StringIO())
    run_at(tmp_path, store, at(8, 5), n, jpy="0.2060")
    assert n.sent == [] and store.alerts == []
    assert store.transitions == [("1", AlertState.ARMED)]


def test_no_window_means_all_day(tmp_path):
    """單人版、或 migration 還沒套用（window=None）：照舊全天都寄。"""
    store = MemoryStore([user_watch("1", "alice@example.com")])
    n = DryRunNotifier(out=io.StringIO())
    run_at(tmp_path, store, at(2, 35), n)
    assert len(n.sent) == 1


def test_outside_window_does_not_use_daily_limit(tmp_path):
    """時段外的不算寄信額度；也不會被當成「超過上限延後」。"""
    store = MemoryStore([windowed("1", "alice@example.com", DEFAULT)], sent_today=10_000)
    r = run_at(tmp_path, store, at(2, 35))
    assert r.deferred == [] and r.outside_window == ["JPY 規則#1"]


# ---------- Supabase：讀通知時段；欄位還不存在時退回全天 ----------

ROW = {
    "id": 7,
    "user_id": "u1",
    "currency": "JPY",
    "target": "0.205000",
    "effective_buffer": "0.001000",
    "state": "armed",
    "state_target": None,
    "email": "alice@example.com",
}


def test_load_watches_reads_window():
    store, session = sb(
        FakeResponse(body=[{**ROW, "notify_start": "22:00:00", "notify_end": "07:00:00"}])
    )
    [w] = store.load_watches()
    assert w.window == NIGHT
    assert "notify_start::text,notify_end::text" in session.calls[0]["params"]["select"]
    assert store.window_supported is True


def test_load_watches_before_migration_falls_back_to_all_day():
    store, session = sb(
        FakeResponse(
            400, {"code": "42703", "message": "column active_rules.notify_start does not exist"}
        ),
        FakeResponse(body=[ROW]),
    )
    [w] = store.load_watches()
    assert w.window is None
    assert store.window_supported is False
    assert "notify_start" not in session.calls[1]["params"]["select"]
    assert ReadOnlyStore(store).window_supported is False


def test_other_errors_are_not_swallowed():
    from fx_alert.store import StoreError

    store, _ = sb(FakeResponse(503, {"message": "down"}))
    with pytest.raises(StoreError):
        store.load_watches()


def test_main_logs_note_when_window_columns_missing(tmp_path):
    store, _ = sb(
        FakeResponse(
            400, {"code": "42703", "message": "column active_rules.notify_start does not exist"}
        ),
        FakeResponse(body=[]),
        FakeResponse(200, [], headers={"Content-Range": "*/0"}),
        FakeResponse(201, None),  # record_check
    )
    logs = []
    r = run_at(tmp_path, store, at(10, 5), logs=logs)
    assert r.exit_code == 0
    assert "注意：資料庫還沒有通知時段欄位（migration 還沒套用），這次全部當作全天" in logs
