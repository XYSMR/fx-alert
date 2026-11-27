"""出國換匯計畫：決定今天要不要寄提醒、寄哪一種（純函式，不碰資料庫和網路）。

階段（D = 出發日；日期都是營業日，見 schedule()）：

  遠期   今天 < near_on           價格在近 30 天的第 20 百分位以內才提醒，同一趟 7 天最多 1 封
  接近   near_on ≤ 今天 < final_on 進入時寄 1 封「可以考慮分批」；
                                  之後第 40 百分位以內才提醒，5 天最多 1 封
  最後   final_on                 不管價格寄 1 封「最後換匯提醒」
  出發前 last_call                不管價格再寄 1 封（出發前最後一個營業日的前一個營業日）

  near_on   = D-21 當天或之前最近的營業日
  final_on  = D-7 當天或之前最近的營業日，但最晚是「出發前倒數第 3 個營業日」
              （遇到春節這種長假時，最後提醒和出發前提醒之間至少隔 1 個營業日）
  last_call = 出發前倒數第 2 個營業日；留最後 1 個營業日當備用

其他規則：
  * 只在營業日寄（週末、假日銀行不能換現鈔）
  * 剩餘金額 ≤ 0（已經換完）就不再寄
  * 固定日期的提醒（接近、最後、出發前）錯過了（例如那天沒檢查到）會在下一個營業日補寄，
    但不會在下一個階段開始後才補
  * 建立日：分批、最後兩封是「提醒日早於建立日才不寄」（提醒日當天建立會寄）；
    出發前那封要在提醒日之前建立才寄。價格提醒沒有建立日的限制（建立當天就可能寄）
  * 近 30 天少於 15 個營業日的收盤價：不寄「相對便宜」（daily.MIN_CLOSES）
  * 冷卻：價格提醒距離這趟旅程上一封信（任何種類）未滿 N 天就不寄

用語：只描述事實（第幾低、還有幾天），不預測、不建議買賣。信件和網頁都附「僅供參考，不保證未來匯率走勢」。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from fx_alert.bizdays import Calendar
from fx_alert.daily import Recent

FAR_PCT, FAR_COOLDOWN = 20, 7
NEAR_PCT, NEAR_COOLDOWN = 40, 5
NEAR_DAYS, FINAL_DAYS = 21, 7

CHEAP_FAR = "cheap_far"
NEAR_START = "near_start"
CHEAP_NEAR = "cheap_near"
FINAL = "final"
LAST_CALL = "last_call"
KINDS = (CHEAP_FAR, NEAR_START, CHEAP_NEAR, FINAL, LAST_CALL)
DISCLAIMER = "僅供參考，不保證未來匯率走勢"


@dataclass(frozen=True)
class Schedule:
    departure: date
    near_on: date
    final_on: date
    last_call: date
    spare: date  # 出發前最後一個營業日（last_call 之後的備用日）

    def phase(self, today: date) -> str:
        if today >= self.departure:
            return "departed"
        if today >= self.final_on:
            return "final"
        if today >= self.near_on:
            return "near"
        return "far"

    def as_dict(self) -> dict:
        keys = ("departure", "near_on", "final_on", "last_call")
        return {k: getattr(self, k).isoformat() for k in keys}


def schedule(departure: date, cal: Calendar) -> Schedule:
    last_call = cal.before(departure, 2)
    final_on = min(
        cal.on_or_before(departure - timedelta(days=FINAL_DAYS)), cal.before(departure, 3)
    )
    near_on = min(cal.on_or_before(departure - timedelta(days=NEAR_DAYS)), cal.before(final_on, 1))
    return Schedule(departure, near_on, final_on, last_call, cal.before(departure, 1))


@dataclass(frozen=True)
class Trip:
    departure: date
    created_on: date
    remaining: Decimal  # 還沒換的外幣金額


@dataclass(frozen=True)
class Sent:
    kind: str
    on: date


@dataclass(frozen=True)
class Decision:
    kind: str
    days_left: int  # 距離出發的日曆天
    rank: int | None = None  # 近 30 天第幾低（1 = 最低）；價格提醒才有
    window: int | None = None  # 近 30 天有幾個營業日
    remaining: Decimal | None = None

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "days_left": self.days_left,
            "rank": self.rank,
            "window": self.window,
            "remaining": str(self.remaining) if self.remaining is not None else None,
        }


def percentile(recent: Recent) -> float:
    """比現在便宜的天數占近 30 天的百分比（0 = 比近 30 天都便宜或一樣）。"""
    return recent.lower / recent.days * 100 if recent.days else 100.0


def decide(
    trip: Trip,
    today: date,
    recent: Recent | None,
    sent: list[Sent],
    cal: Calendar,
) -> Decision | None:
    """today 要寄哪一種提醒（一天最多一封）；不用寄回傳 None。

    recent：現在的價格在近 30 天收盤價的統計（daily.recent_stats）；沒有報價時給 None。
    sent：這趟旅程已經寄過的提醒。
    """
    if trip.remaining <= 0 or not cal.is_business_day(today):
        return None
    sch = schedule(trip.departure, cal)
    if today >= sch.departure:
        return None
    if any(s.on == today for s in sent):
        return None
    kinds = {s.kind for s in sent}
    days_left = (sch.departure - today).days

    def fixed(kind: str) -> Decision:
        return Decision(kind, days_left, remaining=trip.remaining)

    # 固定日期的提醒，越接近出發的越優先
    if today >= sch.last_call and LAST_CALL not in kinds and trip.created_on < sch.last_call:
        return fixed(LAST_CALL)
    in_final = sch.final_on <= today < sch.last_call
    if in_final and FINAL not in kinds and trip.created_on <= sch.final_on:
        return fixed(FINAL)
    in_near = sch.near_on <= today < sch.final_on
    if in_near and NEAR_START not in kinds and trip.created_on <= sch.near_on:
        return fixed(NEAR_START)

    # 價格提醒：只在遠期、接近兩個階段
    phase = sch.phase(today)
    if phase not in ("far", "near") or recent is None or not recent.enough:
        return None
    if phase == "far":
        limit, cooldown, kind = FAR_PCT, FAR_COOLDOWN, CHEAP_FAR
    else:
        limit, cooldown, kind = NEAR_PCT, NEAR_COOLDOWN, CHEAP_NEAR
    if percentile(recent) > limit:
        return None
    last = max((s.on for s in sent), default=None)
    if last is not None and (today - last).days < cooldown:
        return None
    return Decision(
        kind, days_left, rank=recent.lower + 1, window=recent.days, remaining=trip.remaining
    )
