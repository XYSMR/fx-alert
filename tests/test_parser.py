from decimal import Decimal
from pathlib import Path

import pytest

from fx_alert.parser import ParseError, get_cash_sell, parse_rates

FIXTURE = Path(__file__).parent / "fixtures" / "bot_rates_sample.csv"

HEADER = (
    "幣別,匯率,現金,即期,遠期10天,遠期30天,遠期60天,遠期90天,遠期120天,遠期150天,遠期180天,"
    "匯率,現金,即期,遠期10天,遠期30天,遠期60天,遠期90天,遠期120天,遠期150天,遠期180天"
)


def row(code: str, cash_buy: str, spot_buy: str, cash_sell: str, spot_sell: str) -> str:
    """組一列仿台銀格式的資料（遠期欄位填固定值）。"""
    fwd = ",".join(["0.00000"] * 7)
    return f"{code},本行買入,{cash_buy},{spot_buy},{fwd},本行賣出,{cash_sell},{spot_sell},{fwd}"


def make_csv(*rows: str, bom: bool = False) -> str:
    text = "\n".join([HEADER, *rows]) + "\n"
    return ("﻿" + text) if bom else text


# ---------- 合成資料：基本解析 ----------


def test_parses_jpy_cash_sell_as_exact_decimal():
    quotes = parse_rates(make_csv(row("JPY", "0.1958", "0.2035", "0.2086", "0.2085")))
    jpy = quotes["JPY"]
    assert isinstance(jpy.cash_sell, Decimal)
    assert jpy.cash_sell == Decimal("0.2086")
    assert jpy.cash_buy == Decimal("0.1958")
    assert jpy.spot_buy == Decimal("0.2035")
    assert jpy.spot_sell == Decimal("0.2085")


def test_get_cash_sell_returns_value():
    quotes = parse_rates(make_csv(row("JPY", "0.1958", "0.2035", "0.2086", "0.2085")))
    assert get_cash_sell(quotes, "JPY") == Decimal("0.2086")


def test_handles_bom_and_padded_cells():
    text = make_csv(row("JPY", "0.1958 ", " 0.2035", "  0.2086", "0.2085  "), bom=True)
    assert parse_rates(text)["JPY"].cash_sell == Decimal("0.2086")


def test_handles_crlf_line_endings():
    text = make_csv(row("JPY", "0.1958", "0.2035", "0.2086", "0.2085")).replace("\n", "\r\n")
    assert parse_rates(text)["JPY"].cash_sell == Decimal("0.2086")


# ---------- 韓元沒有即期匯率 ----------


@pytest.mark.parametrize("missing", ["", "-", "0", "0.00000", " - "])
def test_krw_without_spot_rate_does_not_break(missing):
    text = make_csv(
        row("JPY", "0.1958", "0.2035", "0.2086", "0.2085"),
        row("KRW", "0.02183", missing, "0.02573", missing),
    )
    quotes = parse_rates(text)
    krw = quotes["KRW"]
    assert krw.cash_sell == Decimal("0.02573")
    assert krw.cash_buy == Decimal("0.02183")
    assert krw.spot_buy is None
    assert krw.spot_sell is None
    # 其他幣別不受影響
    assert quotes["JPY"].cash_sell == Decimal("0.2086")


def test_krw_row_truncated_after_cash_still_parses():
    text = make_csv("KRW,本行買入,0.02183,本行賣出,0.02573")
    krw = parse_rates(text)["KRW"]
    assert krw.cash_sell == Decimal("0.02573")
    assert krw.spot_sell is None


# ---------- 錯誤情況：應該明確拋錯，而不是悄悄回傳錯的數字 ----------


def test_empty_text_raises():
    with pytest.raises(ParseError):
        parse_rates("")


def test_header_only_raises():
    with pytest.raises(ParseError):
        parse_rates(make_csv())


def test_row_without_markers_raises():
    with pytest.raises(ParseError, match="標記"):
        parse_rates(make_csv("JPY,0.1958,0.2035,0.2086,0.2085"))


def test_non_numeric_value_raises():
    with pytest.raises(ParseError, match="不是數字"):
        parse_rates(make_csv(row("JPY", "0.1958", "0.2035", "abc", "0.2085")))


def test_get_cash_sell_missing_currency_raises():
    quotes = parse_rates(make_csv(row("JPY", "0.1958", "0.2035", "0.2086", "0.2085")))
    with pytest.raises(ParseError, match="KRW"):
        get_cash_sell(quotes, "KRW")


def test_get_cash_sell_missing_cash_quote_raises():
    quotes = parse_rates(make_csv(row("KRW", "-", "-", "-", "-")))
    with pytest.raises(ParseError, match="現金賣出"):
        get_cash_sell(quotes, "KRW")


# ---------- 真實台銀檔案（你下載的 fixture） ----------


@pytest.mark.skipif(not FIXTURE.exists(), reason="尚未放入 tests/fixtures/bot_rates_sample.csv")
def test_real_fixture_has_sane_jpy_and_krw():
    text = FIXTURE.read_bytes().decode("utf-8-sig")
    quotes = parse_rates(text)

    jpy = get_cash_sell(quotes, "JPY")
    krw = get_cash_sell(quotes, "KRW")
    # 合理範圍檢查：抓到的是「現金賣出」而不是別的欄位
    assert Decimal("0.1") < jpy < Decimal("0.4")
    assert Decimal("0.01") < krw < Decimal("0.05")
    # 現金賣出一定比現金買入貴
    assert quotes["JPY"].cash_buy < jpy
    assert quotes["KRW"].cash_buy < krw
    # 台銀韓元沒有即期報價
    assert quotes["KRW"].spot_sell is None
