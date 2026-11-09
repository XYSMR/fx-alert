"""讀取並嚴格驗證 config.yaml。

CI 會跑 `python -m fx_alert validate-config`，驗證失敗就不能合併、也不會部署。
"""

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

# 兆豐銀行牌告匯率 API 有提供的幣別
SUPPORTED_CURRENCIES = frozenset(
    "USD HKD GBP JPY AUD CAD SGD ZAR SEK CHF THB NZD EUR KRW MYR IDR PHP MOP VND CNY".split()
)
CURRENCY_NAMES = {"JPY": "日圓", "KRW": "韓元", "USD": "美元", "EUR": "歐元", "CNY": "人民幣"}
MAX_BUFFER_RATIO = Decimal("0.1")  # buffer 不應超過目標價的 10%

TOP_LEVEL_KEYS = {"data_dir", "business_hours", "currencies", "email"}
EMAIL_KEYS = {"daily_limit"}
DEFAULT_DAILY_LIMIT = 400  # Gmail 個人帳號每天約 500 位收件人，留一點餘裕
MAX_DAILY_LIMIT = 2000
HOURS_KEYS = {"weekdays_only", "start", "end"}
CURRENCY_KEYS = {"target", "buffer"}


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
    data_dir: Path  # state.json、history/、alerts.csv、last_check.json 都放這裡
    hours: BusinessHours
    rules: tuple[Rule, ...]
    daily_limit: int = DEFAULT_DAILY_LIMIT  # 每天（台灣時間）最多寄幾封到價通知

    @property
    def state_file(self) -> Path:
        return self.data_dir / "state.json"


def _decimal(value: Any, where: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ConfigFileError(f"{where} 必須是數字")
    try:
        number = Decimal(str(value).strip())
    except InvalidOperation as exc:
        raise ConfigFileError(f"{where} 不是合法的數字：{value!r}") from exc
    if not number.is_finite():
        raise ConfigFileError(f"{where} 不是合法的數字：{value!r}")
    return number


def _time(value: Any, where: str) -> time:
    try:
        return datetime.strptime(str(value).strip(), "%H:%M").time()
    except ValueError as exc:
        raise ConfigFileError(f"{where} 必須是 HH:MM 格式：{value!r}") from exc


def _check_keys(obj: dict, allowed: set[str], where: str) -> None:
    unknown = sorted(str(k) for k in obj if k not in allowed)
    if unknown:
        raise ConfigFileError(f"{where} 有不認得的欄位：{', '.join(unknown)}（可能是打錯字）")


def parse_config(data: Any, base_dir: Path = Path(".")) -> AppConfig:
    if not isinstance(data, dict):
        raise ConfigFileError("設定檔最外層必須是物件")
    _check_keys(data, TOP_LEVEL_KEYS, "設定檔")

    hours_raw = data.get("business_hours") or {}
    if not isinstance(hours_raw, dict):
        raise ConfigFileError("business_hours 格式不對")
    _check_keys(hours_raw, HOURS_KEYS, "business_hours")
    weekdays_only = hours_raw.get("weekdays_only", True)
    if not isinstance(weekdays_only, bool):
        raise ConfigFileError("business_hours.weekdays_only 必須是 true 或 false")
    hours = BusinessHours(
        weekdays_only=weekdays_only,
        start=_time(hours_raw.get("start", "09:00"), "business_hours.start"),
        end=_time(hours_raw.get("end", "16:30"), "business_hours.end"),
    )
    if hours.start >= hours.end:
        raise ConfigFileError("business_hours.start 必須早於 end")

    currencies = data.get("currencies")
    if not isinstance(currencies, dict) or not currencies:
        raise ConfigFileError("currencies 至少要設定一個幣別")

    rules = []
    for raw_code, spec in currencies.items():
        code = str(raw_code).strip()
        if code not in SUPPORTED_CURRENCIES:
            hint = "（幣別代碼要大寫）" if code.upper() in SUPPORTED_CURRENCIES else ""
            raise ConfigFileError(f"不支援的幣別代碼：{code}{hint}")
        if not isinstance(spec, dict) or "target" not in spec:
            raise ConfigFileError(f"{code} 缺少 target")
        _check_keys(spec, CURRENCY_KEYS, code)
        target = _decimal(spec["target"], f"{code}.target")
        buffer = _decimal(spec.get("buffer", "0"), f"{code}.buffer")
        try:
            rule = Rule(code, target=target, buffer=buffer)
        except ValueError as exc:
            raise ConfigFileError(str(exc)) from exc
        if buffer > target * MAX_BUFFER_RATIO:
            limit = target * MAX_BUFFER_RATIO
            raise ConfigFileError(f"{code}.buffer = {buffer} 太大：不應超過目標價的 10%（{limit}）")
        rules.append(rule)

    email_raw = data.get("email") or {}
    if not isinstance(email_raw, dict):
        raise ConfigFileError("email 格式不對")
    _check_keys(email_raw, EMAIL_KEYS, "email")
    daily_limit = email_raw.get("daily_limit", DEFAULT_DAILY_LIMIT)
    if isinstance(daily_limit, bool) or not isinstance(daily_limit, int):
        raise ConfigFileError("email.daily_limit 必須是整數")
    if not 1 <= daily_limit <= MAX_DAILY_LIMIT:
        raise ConfigFileError(f"email.daily_limit 必須介於 1 到 {MAX_DAILY_LIMIT}")

    data_dir = Path(str(data.get("data_dir", "data")))
    if not data_dir.is_absolute():
        data_dir = base_dir / data_dir
    return AppConfig(data_dir=data_dir, hours=hours, rules=tuple(rules), daily_limit=daily_limit)


class _UniqueKeyLoader(yaml.SafeLoader):
    """跟 SafeLoader 一樣，但重複的 key 會報錯（預設是後面的悄悄蓋掉前面的）。"""


def _construct_mapping(loader: yaml.SafeLoader, node: yaml.MappingNode, deep: bool = False):
    seen = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise ConfigFileError(f"設定檔第 {key_node.start_mark.line + 1} 行：重複的欄位 {key}")
        seen.add(key)
    return loader.construct_mapping(node, deep=deep)


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


def load_config(path: Path) -> AppConfig:
    try:
        data = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)  # noqa: S506
    except OSError as exc:
        raise ConfigFileError(f"讀不了設定檔 {path}：{exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigFileError(f"{path} 不是合法的 YAML：{exc}") from exc
    return parse_config(data, base_dir=path.parent)
