"""一次性的探測腳本：測試各銀行匯率 API 能不能用程式抓到，以及有沒有 JPY / KRW 現金賣出價。

用法：python scripts/probe_banks.py
每家銀行只發 1 個 request。
"""

from __future__ import annotations

import json
import time

import requests

UA = "fx-alert-probe/0.1 (personal exchange-rate reminder)"
TIMEOUT = 20
WANT = ("JPY", "KRW")


def mega(s: requests.Session) -> dict[str, tuple]:
    r = s.get(
        "https://www.megabank.com.tw/api/client/ExchangeRate/GetRateData",
        params={"sc_lang": "zh-TW", "sc_site": "bank-zh-tw", "dic_lang": "zh-TW"},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    out = {}
    for item in r.json().get("rates", []):
        code = str(item.get("currKey", "")).split("|")[0].strip().upper()
        cash = item.get("cash") or {}
        out[code] = (cash.get("bid"), cash.get("ask"))
    return out


def esun(s: requests.Session) -> dict[str, tuple]:
    r = s.post("https://www.esunbank.com/api/client/ExchangeRate/LastRateInfo", timeout=TIMEOUT)
    r.raise_for_status()
    out = {}
    for item in r.json().get("Rates", []):
        code = str(item.get("CCY", "")).split("/")[0].strip().upper()
        out[code] = (item.get("CashBBoardRate"), item.get("CashSBoardRate"))
    return out


def sinopac(s: requests.Session) -> dict[str, tuple]:
    ts = int(time.time() * 1000)
    r = s.post(
        f"https://m.sinopac.com/ws/share/rate/ws_exchange.ashx?{ts}",
        data={"exchangeType": "CASH"},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    out = {}
    for item in r.json()[0].get("SubInfo", []):
        code = str(item.get("DataValue4", "")).strip().upper()
        out[code] = (item.get("DataValue2"), item.get("DataValue3"))
    return out


def fubon(s: requests.Session) -> dict[str, tuple]:
    r = s.get(
        "https://www.fubon.com/Fubon_Portal/banking/Personal/deposit/exchange_rate/"
        "exchange_rate1_newVersion.jsp",
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    out = {}
    for item in r.json().get("FE_data", []):
        code = str(item.get("currencyEname", "")).strip().upper()
        out[code] = (item.get("cash_BUY"), item.get("cash_SELL"))
    return out


BANKS = {"兆豐 Mega": mega, "玉山 E.SUN": esun, "永豐 SinoPac": sinopac, "富邦 Fubon": fubon}


def main() -> None:
    s = requests.Session()
    s.headers["User-Agent"] = UA
    for name, fn in BANKS.items():
        try:
            rates = fn(s)
            found = {c: rates.get(c) for c in WANT}
            print(f"[OK ] {name:12s} 共 {len(rates)} 幣別  ", json.dumps(found, ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001 — 探測腳本，所有錯誤都印出來
            print(f"[ERR] {name:12s} {type(exc).__name__}: {str(exc)[:150]}")


if __name__ == "__main__":
    main()
