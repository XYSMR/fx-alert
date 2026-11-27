"""把兆豐銀行牌告匯率 API 的 JSON 解析成結構化資料。

API 回應格式（節錄）：

    {
      "updateTime": "2026/11/09 02:26:07",
      "rates": [
        {"currKey": "JPY|08",
         "spot": {"bid": "0.2050", "ask": "0.2091", ...},
         "cash": {"bid": "0.1978", "ask": "0.2105", ...},
         "update": "20261109022607"},
        {"currKey": "KRW|13",
         "spot": {"bid": "", "ask": "", ...},          ← 韓元沒有即期報價
         "cash": {"bid": "0.0224", "ask": "0.0260", ...}},
        ...
      ]
    }

bid = 銀行買入、ask = 銀行賣出。我們要的「現金賣出」就是 cash.ask。
缺值可能是空字串、"-"、0 或整個欄位不存在，一律解析成 None。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

_MISSING = {"", "-", "--", "N/A"}
_UPDATE_TIME_FORMAT = "%Y/%m/%d %H:%M:%S"


class ParseError(ValueError):
    """回應格式不如預期（可能是銀行改版），呼叫端應該視為錯誤並告警。"""


@dataclass(frozen=True)
class Quote:
    currency: str
    cash_buy: Decimal | None
    cash_sell: Decimal | None
    spot_buy: Decimal | None
    spot_sell: Decimal | None


@dataclass(frozen=True)
class RateBoard:
    quoted_at: datetime | None  # 牌告時間（台灣時間，naive）；格式不對就是 None
    quotes: dict[str, Quote]


def _to_decimal(raw: Any, *, currency: str, field: str) -> Decimal | None:
    if raw is None:
        return None
    if isinstance(raw, bool):
        raise ParseError(f"{currency} 的 {field} 不是數字：{raw!r}")
    value = str(raw).strip()
    if value in _MISSING:
        return None
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise ParseError(f"{currency} 的 {field} 不是數字：{raw!r}") from exc
    if number <= 0:
        return None  # 0 視為「無此報價」
    return number


def _side(item: dict, key: str) -> dict:
    side = item.get(key)
    if side is None:
        return {}
    if not isinstance(side, dict):
        raise ParseError(f"{item.get('currKey')} 的 {key} 欄位格式不對")
    return side


def _parse_item(item: Any) -> Quote:
    if not isinstance(item, dict):
        raise ParseError(f"rates 裡有不是物件的項目：{item!r:.80}")
    curr_key = item.get("currKey")
    if not isinstance(curr_key, str) or not curr_key.strip():
        raise ParseError(f"缺少 currKey：{item!r:.80}")
    currency = curr_key.split("|")[0].strip().upper()

    cash = _side(item, "cash")
    spot = _side(item, "spot")

    def dec(side: dict, k: str, field: str) -> Decimal | None:
        return _to_decimal(side.get(k), currency=currency, field=field)

    return Quote(
        currency=currency,
        cash_buy=dec(cash, "bid", "現金買入"),
        cash_sell=dec(cash, "ask", "現金賣出"),
        spot_buy=dec(spot, "bid", "即期買入"),
        spot_sell=dec(spot, "ask", "即期賣出"),
    )


def _parse_update_time(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        return datetime.strptime(raw.strip(), _UPDATE_TIME_FORMAT)
    except ValueError:
        return None


def parse_rates(text: str) -> RateBoard:
    """解析整份 JSON 回應。"""
    try:
        data = json.loads(text.lstrip("﻿"))
    except json.JSONDecodeError as exc:
        raise ParseError(f"回應不是合法的 JSON：{exc}") from exc

    if not isinstance(data, dict) or not isinstance(data.get("rates"), list):
        raise ParseError("JSON 結構不對：找不到 rates 陣列")

    quotes: dict[str, Quote] = {}
    for item in data["rates"]:
        quote = _parse_item(item)
        quotes[quote.currency] = quote

    if not quotes:
        raise ParseError("rates 是空的，沒有任何幣別資料")
    return RateBoard(quoted_at=_parse_update_time(data.get("updateTime")), quotes=quotes)


def get_cash_sell(board: RateBoard, currency: str) -> Decimal:
    """取得指定幣別的現金賣出價；缺幣別或缺報價時拋出 ParseError。"""
    quote = board.quotes.get(currency)
    if quote is None:
        raise ParseError(f"資料裡找不到幣別 {currency}")
    if quote.cash_sell is None:
        raise ParseError(f"{currency} 沒有現金賣出報價")
    return quote.cash_sell
