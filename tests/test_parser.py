import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from fx_alert.parser import ParseError, get_cash_sell, parse_rates

FIXTURE = Path(__file__).parent / "fixtures" / "mega_rates_sample.json"


def item(code, cash_bid, cash_ask, spot_bid="", spot_ask="", num="00"):
    """組一筆仿兆豐格式的幣別資料。"""
    return {
        "currKey": f"{code}|{num}",
        "spot": {"bid": spot_bid, "ask": spot_ask, "bidOld": "", "askOld": ""},
        "cash": {"bid": cash_bid, "ask": cash_ask, "bidOld": "", "askOld": ""},
        "update": "20261109022607",
    }


def make_json(*items, update_time="2026/11/09 02:26:07", bom=False) -> str:
    text = json.dumps({"updateTime": update_time, "rates": list(items)}, ensure_ascii=False)
    return ("﻿" + text) if bom else text


JPY = item("JPY", "0.1978", "0.2105", "0.2050", "0.2091", "08")


# ---------- 基本解析 ----------


def test_parses_jpy_cash_sell_as_exact_decimal():
    jpy = parse_rates(make_json(JPY)).quotes["JPY"]
    assert isinstance(jpy.cash_sell, Decimal)
    assert jpy.cash_sell == Decimal("0.2105")
    assert jpy.cash_buy == Decimal("0.1978")
    assert jpy.spot_buy == Decimal("0.2050")
    assert jpy.spot_sell == Decimal("0.2091")


def test_get_cash_sell_returns_value():
    assert get_cash_sell(parse_rates(make_json(JPY)), "JPY") == Decimal("0.2105")


def test_parses_update_time():
    assert parse_rates(make_json(JPY)).quoted_at == datetime(2026, 11, 9, 2, 26, 7)


@pytest.mark.parametrize("bad", [None, "", "昨天", 12345])
def test_bad_update_time_becomes_none_but_rates_still_parse(bad):
    board = parse_rates(make_json(JPY, update_time=bad))
    assert board.quoted_at is None
    assert board.quotes["JPY"].cash_sell == Decimal("0.2105")


def test_handles_bom_and_padded_numbers():
    text = make_json(item("JPY", " 0.1978", "0.2105  "), bom=True)
    assert parse_rates(text).quotes["JPY"].cash_sell == Decimal("0.2105")


def test_numeric_json_values_are_accepted():
    data = {"rates": [{"currKey": "JPY|08", "cash": {"bid": 0.1978, "ask": "0.2105"}}]}
    assert parse_rates(json.dumps(data)).quotes["JPY"].cash_buy == Decimal("0.1978")


def test_lowercase_currency_key_is_normalised():
    assert "JPY" in parse_rates(make_json(item("jpy", "0.1978", "0.2105"))).quotes


# ---------- 韓元沒有即期匯率 ----------


@pytest.mark.parametrize("missing", ["", "-", "0", "0.0000", " - ", None])
def test_krw_without_spot_rate_does_not_break(missing):
    krw = item("KRW", "0.0224", "0.0260", missing, missing, "13")
    board = parse_rates(make_json(JPY, krw))
    q = board.quotes["KRW"]
    assert q.cash_sell == Decimal("0.0260")
    assert q.cash_buy == Decimal("0.0224")
    assert q.spot_buy is None
    assert q.spot_sell is None
    assert board.quotes["JPY"].cash_sell == Decimal("0.2105")  # 其他幣別不受影響


def test_krw_with_spot_object_missing_entirely():
    data = {"rates": [{"currKey": "KRW|13", "cash": {"bid": "0.0224", "ask": "0.0260"}}]}
    q = parse_rates(json.dumps(data)).quotes["KRW"]
    assert q.cash_sell == Decimal("0.0260")
    assert q.spot_sell is None


# ---------- 錯誤情況：明確拋錯，不悄悄回傳錯的數字 ----------


@pytest.mark.parametrize("text", ["", "not json", "<!DOCTYPE html><html></html>"])
def test_invalid_json_raises(text):
    with pytest.raises(ParseError, match="JSON"):
        parse_rates(text)


@pytest.mark.parametrize("payload", [[], {"foo": 1}, {"rates": "x"}])
def test_wrong_structure_raises(payload):
    with pytest.raises(ParseError, match="rates"):
        parse_rates(json.dumps(payload))


def test_empty_rates_raises():
    with pytest.raises(ParseError, match="空"):
        parse_rates(make_json())


def test_item_without_currkey_raises():
    with pytest.raises(ParseError, match="currKey"):
        parse_rates(json.dumps({"rates": [{"cash": {"ask": "1"}}]}))


def test_cash_not_an_object_raises():
    with pytest.raises(ParseError, match="cash"):
        parse_rates(json.dumps({"rates": [{"currKey": "JPY|08", "cash": "0.2105"}]}))


@pytest.mark.parametrize("bad", ["abc", True])
def test_non_numeric_value_raises(bad):
    with pytest.raises(ParseError, match="不是數字"):
        parse_rates(make_json(item("JPY", "0.1978", bad)))


def test_get_cash_sell_missing_currency_raises():
    with pytest.raises(ParseError, match="KRW"):
        get_cash_sell(parse_rates(make_json(JPY)), "KRW")


def test_get_cash_sell_missing_cash_quote_raises():
    board = parse_rates(make_json(item("KRW", "", "")))
    with pytest.raises(ParseError, match="現金賣出"):
        get_cash_sell(board, "KRW")


# ---------- 真實兆豐回應（fixture） ----------


@pytest.mark.skipif(not FIXTURE.exists(), reason="尚未放入 tests/fixtures/mega_rates_sample.json")
def test_real_fixture_has_sane_jpy_and_krw():
    board = parse_rates(FIXTURE.read_bytes().decode("utf-8-sig"))

    jpy = get_cash_sell(board, "JPY")
    krw = get_cash_sell(board, "KRW")
    # 合理範圍檢查：抓到的是「現金賣出」而不是別的欄位
    assert Decimal("0.1") < jpy < Decimal("0.4")
    assert Decimal("0.01") < krw < Decimal("0.05")
    # 現金賣出一定比現金買入貴
    assert board.quotes["JPY"].cash_buy < jpy
    assert board.quotes["KRW"].cash_buy < krw
    # 兆豐韓元沒有即期報價
    assert board.quotes["KRW"].spot_sell is None
    assert board.quoted_at is not None
