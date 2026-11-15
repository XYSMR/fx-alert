"""幣別清單、報價單位、價格格式。"""

import json
import re
from decimal import Decimal as D
from pathlib import Path

import pytest

from fx_alert.currencies import (
    BY_CODE,
    CATALOG,
    CODES,
    SPOT_ONLY,
    TICK,
    auto_buffer,
    format_price,
    is_on_tick,
    max_buffer,
)
from fx_alert.notifier import EmailConfig, build_alert_message
from fx_alert.parser import parse_rates
from fx_alert.rules import Rule

FIXTURE = Path(__file__).parent / "fixtures" / "mega_rates_sample.json"


def test_catalog_has_18_cash_currencies_in_board_order():
    assert len(CODES) == len(set(CODES)) == 18
    assert [c.sort_order for c in CATALOG] == list(range(1, 19))
    ranks = sorted(c.popular_rank for c in CATALOG if c.popular_rank)
    assert ranks == list(range(1, 9))  # 「常用」8 種，順序不重複
    assert [BY_CODE[c].popular_rank for c in ("JPY", "KRW", "USD")] == [1, 2, 3]
    assert not set(SPOT_ONLY) & set(CODES)


def test_catalog_matches_real_mega_board():
    """以實際抓到的兆豐牌告為準：有現金賣出價的正好是這 18 種，ZAR、SEK 只有即期。"""
    board = parse_rates(FIXTURE.read_text(encoding="utf-8"))
    with_cash = {code for code, q in board.quotes.items() if q.cash_sell is not None}
    spot_only = {
        code for code, q in board.quotes.items() if q.cash_sell is None and q.spot_sell is not None
    }
    assert with_cash == set(CODES)
    assert spot_only == set(SPOT_ONLY)
    # 牌告每個價格都是小數 4 位 → 報價單位 0.0001
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    prices = [
        v
        for item in raw["rates"]
        for side in ("cash", "spot")
        for k, v in item[side].items()
        if k in ("bid", "ask") and v
    ]
    assert prices and all(re.fullmatch(r"\d+\.\d{4}", p) for p in prices)


@pytest.mark.parametrize(
    ("value", "shown"),
    [
        (D("0.0015075"), "0.0015"),  # 越南盾：不會出現 0.0015075
        (D("0.00215"), "0.0022"),  # 印尼盾：四捨五入
        (D("0.205"), "0.2050"),
        (D("0.206025"), "0.2060"),
        (D("33.14"), "33.1400"),
        (D("0.0021"), "0.0021"),
    ],
)
def test_format_price_rounds_to_board_decimals(value, shown):
    assert format_price(value) == shown


@pytest.mark.parametrize(
    ("target", "buffer"),
    [
        (D("0.0015"), D("0.0001")),  # 越南盾：0.5% 只有 0.0000075 → 至少一個報價單位
        (D("0.0021"), D("0.0001")),  # 印尼盾
        (D("0.0245"), D("0.0001")),  # 韓元：0.0001225 → 0.0001
        (D("0.205"), D("0.0010")),  # 日圓：0.001025 → 0.0010
        (D("32.5"), D("0.1625")),  # 美元
    ],
)
def test_auto_buffer_is_on_tick_and_at_least_one_tick(target, buffer):
    assert auto_buffer(target) == buffer
    assert is_on_tick(auto_buffer(target)) and auto_buffer(target) >= TICK
    assert is_on_tick(target + auto_buffer(target))


def test_max_buffer_allows_one_tick_for_tiny_targets():
    assert max_buffer(D("0.0010")) == TICK
    assert max_buffer(D("0.205")) == D("0.0205")


@pytest.mark.parametrize(
    ("code", "target", "rate"), [("VND", "0.0015", "0.0015"), ("IDR", "0.0021", "0.002")]
)
def test_alert_email_prices_have_board_decimals_for_vnd_idr(code, target, rate):
    rule = Rule(code, target=D(target), buffer=auto_buffer(D(target)))
    cfg = EmailConfig(user="me@gmail.com", password="x", to=("me@gmail.com",))
    msg = build_alert_message(cfg, rule, D(rate), None)
    text = msg["Subject"] + "\n" + msg.get_content()
    prices = re.findall(r"\d+\.\d+", text)
    assert prices and all(re.fullmatch(r"\d+\.\d{4}", p) for p in prices), prices
    assert "0.0015075" not in text
    assert f"> {format_price(D(target) + TICK)}" in text  # 回升一個報價單位
