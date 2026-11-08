"""從台灣銀行下載牌告匯率 CSV（只負責 I/O，不做解析）。

本機手動測試：
    python -m fx_alert.fetcher
會實際抓一次台銀，印出 JPY、KRW 的報價。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

import requests

CSV_URL = "https://rate.bot.com.tw/xrt/flcsv/0/day"
USER_AGENT = "fx-alert/0.1 (personal exchange-rate reminder; runs a few times per weekday)"
TIMEOUT_SECONDS = 15

# 台銀回傳的檔名類似 ExchangeRate@202611091530.csv，可以從裡面取得牌告時間
_FILENAME_TS_RE = re.compile(r"@(\d{12})")


class FetchError(RuntimeError):
    """下載失敗（網路錯誤、HTTP 非 200、回應是空的）。"""


@dataclass(frozen=True)
class FetchResult:
    text: str
    quoted_at: datetime | None  # 牌告時間（台灣時間，naive）；拿不到就是 None


def _parse_quoted_at(content_disposition: str | None) -> datetime | None:
    if not content_disposition:
        return None
    match = _FILENAME_TS_RE.search(content_disposition)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y%m%d%H%M")
    except ValueError:
        return None


def fetch_csv(url: str = CSV_URL, *, session: requests.Session | None = None) -> FetchResult:
    """下載一次 CSV。只發一個 request，失敗就拋 FetchError，不在這裡重試。"""
    http = session or requests.Session()
    try:
        resp = http.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT_SECONDS)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise FetchError(f"下載台銀 CSV 失敗：{exc}") from exc

    text = resp.content.decode("utf-8-sig", errors="replace")
    if not text.strip():
        raise FetchError("台銀 CSV 回應是空的")
    if text.lstrip().startswith("<"):
        # 台銀有時會回傳 HTML（例如機器人驗證頁），不是 CSV
        raise FetchError("台銀回傳的是 HTML 網頁而不是 CSV（可能被機器人驗證擋下）")

    return FetchResult(
        text=text,
        quoted_at=_parse_quoted_at(resp.headers.get("Content-Disposition")),
    )


def _main() -> None:
    from fx_alert.parser import parse_rates

    result = fetch_csv()
    quotes = parse_rates(result.text)
    print(f"牌告時間：{result.quoted_at or '（未提供）'}")
    for code in ("JPY", "KRW"):
        q = quotes.get(code)
        if q is None:
            print(f"{code}: 找不到")
            continue
        print(f"{code}: 現金賣出={q.cash_sell}  現金買入={q.cash_buy}  即期賣出={q.spot_sell}")


if __name__ == "__main__":
    _main()
