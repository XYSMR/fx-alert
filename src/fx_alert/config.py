"""讀取並驗證 config.yaml。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml

from fx_alert.rules import Rule

# 台灣沒有日光節約時間，用固定 +8 時區即可（也避免 Windows 上缺少 tzdata 的問題）
TAIPEI = timezone(timedelta(hours=8), name="Asia/Taipei")


class ConfigFileError(ValueError):
    """config.yaml 內容不合法。"""


@dataclass(frozen=True)
class BusinessHours:
    weekdays_only: bool
    start: time
    end: time

    def contains(self, now: datetime) -> bool:
        """now 換算成台灣時間後，是否在檢查時段內（start、end 都包含）。"""
        local = now.astimezone(TAIPEI)
        if self.weekdays_only and local.weekday() >= 5:
            return False
        t = local.time().replace(second=0, microsecond=0)
        return self.start <= t <= self.end


@dataclass(frozen=True)
class AppConfig:
    state_file: Path
    hours: BusinessHours
    rules: tuple[Rule, ...]


def _decimal(value: Any, where: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ConfigFileError(f"{where} 必須是數字")
    try:
        return Decimal(str(value).strip())
    except InvalidOperation as exc:
        raise ConfigFileError(f"{where} 不是合法的數字：{value!r}") from exc


def _time(value: Any, where: str) -> time:
    try:
        return datetime.strptime(str(value).strip(), "%H:%M").time()
    except ValueError as exc:
        raise ConfigFileError(f"{where} 必須是 HH:MM 格式：{value!r}") from exc


def parse_config(data: Any, base_dir: Path = Path(".")) -> AppConfig:
    if not isinstance(data, dict):
        raise ConfigFileError("設定檔最外層必須是物件")

    hours_raw = data.get("business_hours") or {}
    if not isinstance(hours_raw, dict):
        raise ConfigFileError("business_hours 格式不對")
    hours = BusinessHours(
        weekdays_only=bool(hours_raw.get("weekdays_only", True)),
        start=_time(hours_raw.get("start", "09:00"), "business_hours.start"),
        end=_time(hours_raw.get("end", "16:30"), "business_hours.end"),
    )
    if hours.start >= hours.end:
        raise ConfigFileError("business_hours.start 必須早於 end")

    currencies = data.get("currencies")
    if not isinstance(currencies, dict) or not currencies:
        raise ConfigFileError("currencies 至少要設定一個幣別")

    rules = []
    for code, spec in currencies.items():
        code = str(code).strip().upper()
        if not isinstance(spec, dict) or "target" not in spec:
            raise ConfigFileError(f"{code} 缺少 target")
        target = _decimal(spec["target"], f"{code}.target")
        buffer = _decimal(spec.get("buffer", "0"), f"{code}.buffer")
        try:
            rules.append(Rule(code, target=target, buffer=buffer))
        except ValueError as exc:
            raise ConfigFileError(str(exc)) from exc

    state_file = Path(str(data.get("state_file", "state.json")))
    if not state_file.is_absolute():
        state_file = base_dir / state_file
    return AppConfig(state_file=state_file, hours=hours, rules=tuple(rules))


def load_config(path: Path) -> AppConfig:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigFileError(f"讀不了設定檔 {path}：{exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigFileError(f"{path} 不是合法的 YAML：{exc}") from exc
    return parse_config(data, base_dir=path.parent)
