"""從兆豐銀行下載牌告匯率 JSON（只負責 I/O，不做解析）。

本機手動測試：
    python -m fx_alert.fetcher
會實際抓一次，印出 JPY、KRW 的報價。
"""

from __future__ import annotations

import requests

API_URL = "https://www.megabank.com.tw/api/client/ExchangeRate/GetRateData"
API_PARAMS = {"sc_lang": "zh-TW", "sc_site": "bank-zh-tw", "dic_lang": "zh-TW"}
USER_AGENT = "fx-alert/0.1 (personal exchange-rate reminder; runs a few times per weekday)"
TIMEOUT_SECONDS = 15


class FetchError(RuntimeError):
    """下載失敗（網路錯誤、HTTP 非 200、回應是空的或是 HTML）。"""


def fetch_rates_json(*, session: requests.Session | None = None) -> str:
    """下載一次匯率 JSON，回傳原始文字。只發一個 request，失敗就拋 FetchError，不重試。"""
    http = session or requests.Session()
    try:
        resp = http.get(
            API_URL,
            params=API_PARAMS,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise FetchError(f"下載兆豐匯率失敗：{exc}") from exc

    text = resp.content.decode("utf-8-sig", errors="replace")
    if not text.strip():
        raise FetchError("兆豐匯率 API 回應是空的")
    if text.lstrip().startswith("<"):
        # 可能是維護頁或機器人驗證頁
        raise FetchError("兆豐回傳的是 HTML 網頁而不是 JSON（可能在維護或被擋）")
    return text


def _main() -> None:
    from fx_alert.parser import parse_rates

    board = parse_rates(fetch_rates_json())
    print(f"牌告時間：{board.quoted_at or '（未提供）'}")
    for code in ("JPY", "KRW"):
        q = board.quotes.get(code)
        if q is None:
            print(f"{code}: 找不到")
            continue
        print(f"{code}: 現金賣出={q.cash_sell}  現金買入={q.cash_buy}  即期賣出={q.spot_sell}")


if __name__ == "__main__":
    _main()
