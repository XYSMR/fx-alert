"""出國換匯計畫的提醒信（規則在 trip_rules.py）。

用語只描述事實：現在的價格在近 30 天是第幾低、距離出發還有幾天、之後哪幾天會再寄信。
不談匯率之後的走向，也不叫人買賣；每封信都附「僅供參考，不保證未來匯率走勢」。
tests/test_trip_mail.py 會檢查信件不出現投資建議的字眼。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from email.message import EmailMessage

from fx_alert.currencies import CATALOG, format_price
from fx_alert.notifier import EmailConfig
from fx_alert.trip_rules import (
    CHEAP_FAR,
    CHEAP_NEAR,
    DISCLAIMER,
    FAR_PCT,
    FINAL,
    NEAR_PCT,
    NEAR_START,
    Decision,
    Schedule,
)
from fx_alert.unsubscribe import DEFAULT_SITE_URL, UnsubscribeLinks

NAMES = {c.code: c.name_zh for c in CATALOG}
WEEKDAYS = "一二三四五六日"
PRICE_LABEL = "兆豐銀行現金賣出價"
NEXT_TITLE = "接下來會在這些日子寄信（都避開週末和國定假日）："
UNSUBSCRIBE_TITLE = "不想再收到 fx-alert 的 Email（到價通知和換匯計畫）？"


def short_date(d: date) -> str:
    """2027-01-17 → 1/17（日）"""
    return f"{d.month}/{d.day}（{WEEKDAYS[d.weekday()]}）"


def format_amount(value: Decimal) -> str:
    """50000.00 → 50,000；1234.50 → 1,234.5"""
    q = value.quantize(Decimal("0.01"))
    text = f"{q:,.2f}".rstrip("0").rstrip(".")
    return text


def estimate_twd(remaining: Decimal, rate: Decimal | None) -> str | None:
    if rate is None or rate <= 0 or remaining <= 0:
        return None
    twd = (remaining * rate).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return f"{twd:,}"


def build_subject(currency: str, decision: Decision, sch: Schedule, rate: Decimal | None) -> str:
    name = NAMES.get(currency, currency)
    days = decision.days_left
    if decision.kind in (CHEAP_FAR, CHEAP_NEAR) and rate is not None:
        return (
            f"[FX Alert] 換匯計畫：{name} {format_price(rate)}，"
            f"近 30 天第 {decision.rank} 低（距離出發 {days} 天）"
        )
    if decision.kind == NEAR_START:
        return f"[FX Alert] 換匯計畫：距離出發 {days} 天，可以考慮分批換{name}"
    if decision.kind == FINAL:
        return f"[FX Alert] 換匯計畫：{name}最後換匯提醒（距離出發 {days} 天）"
    return f"[FX Alert] 換匯計畫：{name}出發前提醒（{short_date(sch.departure)} 出發）"


def _reason(decision: Decision, sch: Schedule, rate: Decimal | None, name: str) -> list[str]:
    days = decision.days_left
    price = format_price(rate) if rate is not None else None
    if decision.kind in (CHEAP_FAR, CHEAP_NEAR):
        pct = FAR_PCT if decision.kind == CHEAP_FAR else NEAR_PCT
        return [
            f"{name}現在的{PRICE_LABEL}是 {price}，"
            f"在近 30 天（{decision.window} 個營業日的收盤價）中是第 {decision.rank} 低。",
            f"距離出發還有 {days} 天。這個階段的價格在近 30 天的前 {pct}% 低時會寄這封提醒。",
        ]
    lines = []
    if decision.kind == NEAR_START:
        lines.append(f"距離出發還有 {days} 天，進入接近出發的階段。")
        lines.append("還沒換的部分可以考慮分批換，不用一次換完。")
    elif decision.kind == FINAL:
        lines.append(f"距離出發還有 {days} 天，這是最後換匯提醒（不看價格）。")
        lines.append("現鈔只能在銀行營業日換，有些分行的外幣現鈔需要預約。")
    else:
        lines.append(
            f"{short_date(sch.departure)} 出發；出發前最後一個營業日是 "
            f"{short_date(sch.spare)}。這是出發前提醒（不看價格）。"
        )
    if price is not None:
        lines.append(f"{name}現在的{PRICE_LABEL}是 {price}。")
    return lines


def _next_steps(sch: Schedule, today: date) -> list[str]:
    items = [
        (sch.near_on, "可以考慮分批換的提醒"),
        (sch.final_on, "最後換匯提醒"),
        (sch.last_call, "出發前提醒"),
    ]
    return [f"  {short_date(d)} {text}" for d, text in items if d > today]


def build_trip_message(
    cfg: EmailConfig,
    *,
    currency: str,
    decision: Decision,
    sch: Schedule,
    today: date,
    rate: Decimal | None,
    quoted_at: datetime | None,
    remaining: Decimal,
    to: Sequence[str],
    unsubscribe: UnsubscribeLinks | None = None,
    site_url: str = DEFAULT_SITE_URL,
) -> EmailMessage:
    name = NAMES.get(currency, currency)
    msg = EmailMessage()
    msg["Subject"] = build_subject(currency, decision, sch, rate)
    msg["From"] = cfg.user
    msg["To"] = ", ".join(to)
    twd = estimate_twd(remaining, rate)
    when = quoted_at.strftime("%Y-%m-%d %H:%M") if quoted_at else None
    next_steps = _next_steps(sch, today)
    lines = [
        f"出國換匯計畫：{short_date(sch.departure)} 出發，{name}（{currency}）",
        "",
        *_reason(decision, sch, rate, name),
        "",
        f"還沒換：{format_amount(remaining)} {name}"
        + (f"（以現在的價格約 {twd} 元台幣，未含手續費、面額限制）" if twd else ""),
        *([f"牌告時間：{when}"] if when and rate is not None else []),
        "",
        *([NEXT_TITLE, *next_steps, ""] if next_steps else []),
        "換了之後到網站記錄已換的金額，剩下的會繼續追蹤；全部換完就不再寄提醒。",
        f"網站：{site_url}",
        "",
        f"※ {DISCLAIMER}。實際匯率以兆豐銀行公告為準。",
        "",
        "— fx-alert（GitHub Actions 自動寄出）",
        *(["", f"{UNSUBSCRIBE_TITLE}取消訂閱：{unsubscribe.page_url}"] if unsubscribe else []),
    ]
    msg.set_content("\n".join(lines))
    if unsubscribe:
        msg["List-Unsubscribe"] = f"<{unsubscribe.one_click_url}>"
        msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    return msg
