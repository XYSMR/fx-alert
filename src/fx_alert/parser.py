"""把台灣銀行牌告匯率 CSV 解析成結構化資料。

台銀 CSV 的每一列大致長這樣（欄位前後可能有空白）：

    JPY,本行買入,<現金>,<即期>,<遠期...>,本行賣出,<現金>,<即期>,<遠期...>

標題列的「本行買入／本行賣出」各出現兩次，用欄位名稱對應很容易出錯，
所以這裡改成在「每一列」裡找這兩個標記，取標記後的第 1 格（現金）、第 2 格（即期）。
這樣就算台銀增減遠期欄位，也不會影響現金價的位置。

缺值（例如韓元沒有即期匯率）可能是空字串、"-" 或 0，一律解析成 None。
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

BUY_MARKER = "本行買入"
SELL_MARKER = "本行賣出"
_MISSING = {"", "-", "--", "N/A"}
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


class ParseError(ValueError):
    """CSV 格式不如預期（可能是台銀改版），呼叫端應該視為錯誤並告警。"""


@dataclass(frozen=True)
class Quote:
    currency: str
    cash_buy: Decimal | None
    cash_sell: Decimal | None
    spot_buy: Decimal | None
    spot_sell: Decimal | None


def _to_decimal(raw: str, *, currency: str, field: str) -> Decimal | None:
    value = raw.strip()
    if value in _MISSING:
        return None
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise ParseError(f"{currency} 的 {field} 不是數字：{raw!r}") from exc
    if number <= 0:
        # 台銀可能用 0 表示「無此報價」
        return None
    return number


def _block(cells: list[str], start: int, end: int, idx: int) -> str:
    """取 cells[start:end] 的第 idx 格，不存在就回傳空字串（視為缺值）。"""
    pos = start + idx
    return cells[pos] if pos < end else ""


def _parse_row(cells: list[str]) -> Quote:
    currency = cells[0]
    try:
        buy_idx = cells.index(BUY_MARKER)
        sell_idx = cells.index(SELL_MARKER)
    except ValueError as exc:
        raise ParseError(f"{currency} 這一列找不到「{BUY_MARKER}／{SELL_MARKER}」標記") from exc
    if not buy_idx < sell_idx:
        raise ParseError(f"{currency} 這一列的買入／賣出標記順序不對")

    buy_start, buy_end = buy_idx + 1, sell_idx
    sell_start, sell_end = sell_idx + 1, len(cells)

    def dec(start: int, end: int, idx: int, field: str) -> Decimal | None:
        return _to_decimal(_block(cells, start, end, idx), currency=currency, field=field)

    return Quote(
        currency=currency,
        cash_buy=dec(buy_start, buy_end, 0, "現金買入"),
        cash_sell=dec(sell_start, sell_end, 0, "現金賣出"),
        spot_buy=dec(buy_start, buy_end, 1, "即期買入"),
        spot_sell=dec(sell_start, sell_end, 1, "即期賣出"),
    )


def parse_rates(text: str) -> dict[str, Quote]:
    """解析整份 CSV，回傳 {幣別代碼: Quote}。"""
    text = text.lstrip("﻿")  # 去掉可能的 UTF-8 BOM
    quotes: dict[str, Quote] = {}
    for row in csv.reader(io.StringIO(text)):
        cells = [c.strip() for c in row]
        if not cells or not cells[0]:
            continue
        if not _CURRENCY_RE.match(cells[0]):
            continue  # 標題列或其他非資料列
        quote = _parse_row(cells)
        quotes[quote.currency] = quote

    if not quotes:
        raise ParseError("CSV 裡沒有任何幣別資料，可能是空檔或格式已變更")
    return quotes


def get_cash_sell(quotes: dict[str, Quote], currency: str) -> Decimal:
    """取得指定幣別的現金賣出價；缺幣別或缺報價時拋出 ParseError。"""
    quote = quotes.get(currency)
    if quote is None:
        raise ParseError(f"CSV 裡找不到幣別 {currency}")
    if quote.cash_sell is None:
        raise ParseError(f"{currency} 沒有現金賣出報價")
    return quote.cash_sell
