import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from fx_alert.rules import AlertState
from fx_alert.state_store import (
    CurrencyState,
    StateError,
    current,
    dumps_states,
    load_states,
    save_states,
    with_transition,
)

TW = timezone(timedelta(hours=8))
NOW = datetime(2026, 11, 9, 10, 7, tzinfo=TW)
ARMED, TRIG = AlertState.ARMED, AlertState.TRIGGERED


@pytest.fixture
def path(tmp_path):
    return tmp_path / "state.json"


def sample():
    return {
        "JPY": CurrencyState(TRIG, "2026-11-09T10:07:00+08:00", "0.2049"),
        "KRW": CurrencyState(ARMED, "2026-11-08T09:37:00+08:00", "0.0262"),
    }


# ---------- 讀取 ----------


def test_missing_file_means_first_run(path):
    assert load_states(path) == {}
    assert current(load_states(path), "JPY") is None


def test_round_trip(path):
    assert save_states(path, sample()) is True
    assert load_states(path) == sample()


@pytest.mark.parametrize(
    "content",
    [
        "",
        "{not json",
        "[]",
        '{"version": 2, "currencies": {}}',
        '{"version": 1}',
        '{"version": 1, "currencies": []}',
        '{"version": 1, "currencies": {"JPY": "armed"}}',
        '{"version": 1, "currencies": {"JPY": '
        '{"state": "sleeping", "changed_at": "x", "rate": "1"}}}',
        '{"version": 1, "currencies": {"JPY": {"state": "armed"}}}',
    ],
)
def test_corrupt_file_raises_instead_of_resetting(path, content):
    path.write_text(content, encoding="utf-8")
    with pytest.raises(StateError):
        load_states(path)


# ---------- 寫入 ----------


def test_output_is_deterministic_and_readable():
    text = dumps_states(sample())
    assert text == dumps_states(dict(reversed(list(sample().items()))))  # 順序不影響
    assert text.endswith("\n")
    data = json.loads(text)
    assert data["version"] == 1
    assert data["currencies"]["JPY"] == {
        "changed_at": "2026-11-09T10:07:00+08:00",
        "rate": "0.2049",
        "state": "triggered",
    }


def test_save_same_content_reports_no_change(path):
    assert save_states(path, sample()) is True
    mtime = path.stat().st_mtime_ns
    assert save_states(path, sample()) is False  # 不需要 commit
    assert path.stat().st_mtime_ns == mtime  # 而且沒有重寫檔案


def test_save_uses_lf_and_leaves_no_temp_file(path):
    save_states(path, sample())
    assert b"\r\n" not in path.read_bytes()
    assert [p.name for p in path.parent.iterdir()] == ["state.json"]


def test_save_creates_parent_folder(tmp_path):
    nested = tmp_path / "data" / "state.json"
    assert save_states(nested, sample()) is True
    assert nested.exists()


# ---------- 狀態轉換 ----------


def test_transition_records_time_and_rate():
    new = with_transition({}, "JPY", TRIG, D("0.2049"), NOW)
    assert new["JPY"] == CurrencyState(TRIG, "2026-11-09T10:07:00+08:00", "0.2049")


def test_same_state_keeps_original_entry():
    states = sample()
    later = NOW + timedelta(hours=3)
    assert with_transition(states, "JPY", TRIG, D("0.2010"), later) is states


def test_transition_does_not_mutate_input():
    states = sample()
    new = with_transition(states, "JPY", ARMED, D("0.2065"), NOW)
    assert states["JPY"].state is TRIG
    assert new["JPY"].state is ARMED
    assert new["KRW"] == states["KRW"]


def test_unchanged_run_does_not_touch_file(path):
    # 模擬：已通知後，匯率仍低於目標 → 狀態不變 → 檔案不變 → 不 commit
    states = with_transition({}, "JPY", TRIG, D("0.2049"), NOW)
    assert save_states(path, states) is True
    states = with_transition(load_states(path), "JPY", TRIG, D("0.2030"), NOW + timedelta(hours=1))
    assert save_states(path, states) is False
