"""支援的幣別：兆豐牌告上「有現金賣出價」的 18 種幣別。

2026/11/11 實際抓兆豐 API 確認的結果（共 20 種）：
  * 現鈔＋即期（12）：USD HKD GBP JPY AUD CAD SGD CHF THB NZD EUR CNY
  * 只有現鈔（6）：KRW MYR IDR PHP MOP VND
  * 只有即期（2）：ZAR SEK → 不支援（這個工具看的是現金賣出價）

這份清單是唯一的來源：
  * 資料庫 public.currencies 由 migration 寫入同樣的內容（tests/db 會比對）
  * 多人版每次檢查都記錄這 18 種幣別的走勢
  * 單人版 config.yaml 只能設定這裡列出的幣別

兆豐牌告每個幣別都是小數 4 位，所以「一個報價單位」是 0.0001：
目標價、回升門檻都要是 0.0001 的整數倍，畫面和信件上的價格一律顯示 4 位小數。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

PRICE_DECIMALS = 4
TICK = Decimal("0.0001")  # 一個報價單位


@dataclass(frozen=True)
class Currency:
    code: str
    name_zh: str
    sort_order: int  # 兆豐牌告上的順序
    popular_rank: int | None = None  # 下拉選單「常用」分組的順序；None = 不在常用


# 依兆豐牌告的順序（ZAR、SEK 只有即期，不列入）
CATALOG: tuple[Currency, ...] = (
    Currency("USD", "美元", 1, 3),
    Currency("HKD", "港幣", 2, 6),
    Currency("GBP", "英鎊", 3),
    Currency("JPY", "日圓", 4, 1),
    Currency("AUD", "澳幣", 5),
    Currency("CAD", "加拿大幣", 6),
    Currency("SGD", "新加坡幣", 7, 8),
    Currency("CHF", "瑞士法郎", 8),
    Currency("THB", "泰銖", 9, 7),
    Currency("NZD", "紐西蘭幣", 10),
    Currency("EUR", "歐元", 11, 4),
    Currency("KRW", "韓元", 12, 2),
    Currency("MYR", "馬來西亞幣", 13),
    Currency("IDR", "印尼盾", 14),
    Currency("PHP", "菲律賓比索", 15),
    Currency("MOP", "澳門幣", 16),
    Currency("VND", "越南盾", 17),
    Currency("CNY", "人民幣", 18, 5),
)

CODES: tuple[str, ...] = tuple(c.code for c in CATALOG)
BY_CODE: dict[str, Currency] = {c.code: c for c in CATALOG}

# 兆豐有、但只有即期匯率的幣別：不支援，網頁下拉選單下方會註明
SPOT_ONLY: dict[str, str] = {"ZAR": "南非幣", "SEK": "瑞典幣"}


def is_on_tick(value: Decimal) -> bool:
    """是不是 0.0001 的整數倍（兆豐牌告的位數）。"""
    return value == value.quantize(TICK)


def format_price(value: Decimal) -> str:
    """價格一律四捨五入到牌告的位數（小數 4 位）：0.205 → 0.2050、0.0015075 → 0.0015。"""
    return str(value.quantize(TICK, rounding=ROUND_HALF_UP))


def auto_buffer(target: Decimal) -> Decimal:
    """沒填回升門檻時用的值：目標價的 0.5%，四捨五入到 0.0001，至少一個報價單位。

    和資料庫 alert_rules.effective_buffer、網頁 account.js 的 autoBuffer() 必須一致。
    """
    return max((target * Decimal("0.005")).quantize(TICK, rounding=ROUND_HALF_UP), TICK)


def max_buffer(target: Decimal) -> Decimal:
    """回升門檻上限：目標價的 10%，但至少允許一個報價單位（越南盾目標 0.0010 時仍可設 0.0001）。"""
    return max(target * Decimal("0.1"), TICK)
