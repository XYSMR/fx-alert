from datetime import UTC, datetime, time, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

from fx_alert.config import TAIPEI, ConfigFileError, load_config, parse_config

ROOT = Path(__file__).resolve().parents[1]


def base(**over):
    data = {
        "data_dir": "data",
        "business_hours": {"weekdays_only": True, "start": "09:00", "end": "16:30"},
        "currencies": {"JPY": {"target": "0.205", "buffer": "0.001"}},
    }
    data.update(over)
    return data


def test_repo_config_yaml_is_valid():
    cfg = load_config(ROOT / "config.yaml")
    assert [r.currency for r in cfg.rules] == ["JPY", "KRW"]
    jpy = cfg.rules[0]
    assert (jpy.target, jpy.buffer) == (D("0.205"), D("0.001"))
    assert cfg.data_dir == ROOT / "data"
    assert cfg.state_file == ROOT / "data" / "state.json"


def test_yaml_floats_become_exact_decimals(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("currencies:\n  JPY:\n    target: 0.205\n    buffer: 0.001\n", encoding="utf-8")
    rule = load_config(p).rules[0]
    assert rule.target == D("0.205") and rule.buffer == D("0.001")


def test_default_buffer_is_zero():
    assert parse_config(base(currencies={"JPY": {"target": "0.2"}})).rules[0].buffer == D("0")


# ---------- CI 用的格式驗證：失敗就不能合併、不能部署 ----------


@pytest.mark.parametrize(
    ("data", "msg"),
    [
        ([], "最外層"),
        (base(state_fle="x"), "不認得的欄位：state_fle"),
        (base(currencies={}), "至少"),
        # 幣別代碼
        (base(currencies={"XYZ": {"target": "1"}}), "不支援的幣別代碼：XYZ"),
        (base(currencies={"jpy": {"target": "0.2"}}), "要大寫"),
        (base(currencies={"JPYY": {"target": "0.2"}}), "不支援"),
        # 目標價
        (base(currencies={"JPY": {}}), "target"),
        (base(currencies={"JPY": {"target": "abc"}}), "不是合法的數字"),
        (base(currencies={"JPY": {"target": "NaN"}}), "不是合法的數字"),
        (base(currencies={"JPY": {"target": True}}), "必須是數字"),
        (base(currencies={"JPY": {"target": None}}), "必須是數字"),
        (base(currencies={"JPY": {"target": "0"}}), "大於 0"),
        (base(currencies={"JPY": {"target": "-0.2"}}), "大於 0"),
        # buffer
        (base(currencies={"JPY": {"target": "0.2", "buffer": "-0.001"}}), "負數"),
        (base(currencies={"JPY": {"target": "0.205", "buffer": "0.03"}}), "10%"),
        (base(currencies={"JPY": {"target": "0.2", "bufer": "0.001"}}), "不認得的欄位：bufer"),
        # 檢查時段
        (base(business_hours={"start": "9點"}), "HH:MM"),
        (base(business_hours={"start": "17:00", "end": "09:00"}), "早於"),
        (base(business_hours={"weekdays_only": "yes"}), "true 或 false"),
        (base(business_hours={"weekday_only": True}), "不認得的欄位"),
    ],
)
def test_invalid_config(data, msg):
    with pytest.raises(ConfigFileError, match=msg):
        parse_config(data)


def test_buffer_exactly_10_percent_is_allowed():
    rule = parse_config(base(currencies={"JPY": {"target": "0.2", "buffer": "0.02"}})).rules[0]
    assert rule.buffer == D("0.02")


def test_duplicate_currency_in_yaml_is_rejected(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(
        "currencies:\n  JPY:\n    target: '0.205'\n  JPY:\n    target: '0.3'\n", encoding="utf-8"
    )
    with pytest.raises(ConfigFileError, match="重複的欄位 JPY"):
        load_config(p)


def test_invalid_yaml_file(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("currencies: [unclosed", encoding="utf-8")
    with pytest.raises(ConfigFileError, match="YAML"):
        load_config(p)


def test_missing_file(tmp_path):
    with pytest.raises(ConfigFileError, match="讀不了"):
        load_config(tmp_path / "nope.yaml")


# ---------- 檢查時段（台灣時間） ----------

HOURS = parse_config(base()).hours


def tw(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=TAIPEI)


@pytest.mark.parametrize(
    ("when", "expected"),
    [
        (tw(2026, 11, 9, 9, 0), True),  # 週一 09:00（含）
        (tw(2026, 11, 9, 16, 30), True),  # 16:30（含）
        (tw(2026, 11, 9, 16, 31), False),
        (tw(2026, 11, 9, 8, 59), False),
        (tw(2026, 11, 13, 12, 0), True),  # 週五
        (tw(2026, 11, 14, 12, 0), False),  # 週六
        (tw(2026, 11, 15, 12, 0), False),  # 週日
    ],
)
def test_business_hours(when, expected):
    assert HOURS.contains(when) is expected


def test_business_hours_converts_from_utc():
    # GitHub Actions 的時間是 UTC：01:07 UTC = 台灣 09:07
    assert HOURS.contains(datetime(2026, 11, 9, 1, 7, tzinfo=UTC))
    assert not HOURS.contains(datetime(2026, 11, 9, 0, 59, tzinfo=UTC))


def test_weekends_allowed_when_configured():
    hours = parse_config(base(business_hours={"weekdays_only": False})).hours
    assert hours.contains(tw(2026, 11, 14, 12, 0))
    assert hours.start == time(9, 0)


def test_taipei_is_utc_plus_8():
    assert TAIPEI.utcoffset(None) == timedelta(hours=8)


# ---------- 每日寄信上限 ----------


def test_daily_limit_default_and_repo_value():
    assert parse_config(base()).daily_limit == 400
    assert load_config(ROOT / "config.yaml").daily_limit == 400
    assert parse_config(base(email={"daily_limit": 50})).daily_limit == 50


@pytest.mark.parametrize(
    "email",
    [
        {"daily_limit": 0},
        {"daily_limit": 2001},
        {"daily_limit": "400"},
        {"daily_limit": True},
        {"daily_limit": 1.5},
        {"limit": 400},
        "400",
    ],
)
def test_invalid_daily_limit(email):
    with pytest.raises(ConfigFileError):
        parse_config(base(email=email))


def test_repo_config_explains_targets_are_unused_in_multi_user_mode():
    """多人版的目標價在網站上設定：config.yaml 的 target 只給退回單人版、migrate-me 用。"""
    text = (ROOT / "config.yaml").read_text(encoding="utf-8")
    note = text[: text.index("\ncurrencies:")]
    assert (
        "target、buffer 只在退回單人版、執行 migrate-me 時使用；使用者的目標價在網站上設定" in note
    )
    assert "MAIL_TO" not in text


def test_readme_explains_config_targets():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    section = readme[readme.index("### `config.yaml`") :]
    section = section[: section.index("\n## ")]
    assert "只在退回單人版、執行 `migrate-me` 時使用" in section
    assert "使用者的目標價在網站上設定" in section
