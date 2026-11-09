"""到價規則：每個幣別一個兩狀態的狀態機（純函式，不做任何 I/O）。

         rate ≤ target（寄信）
 ARMED ───────────────────────► TRIGGERED
   ▲                                │
   └────────────────────────────────┘
      rate > target + buffer（重新上膛，不寄信）

- ARMED（待命）：還沒通知過，或已經回升、可以再通知。
- TRIGGERED（已通知）：這次到價已經寄過信，不再重複寄。
- 沒有前一個狀態（第一次執行）視為 ARMED，所以一開始就低於目標也會通知。
- 介於 (target, target + buffer] 的匯率不改變狀態，避免在目標附近來回跳時一直重寄。
- 修改目標價：已通知（TRIGGERED）時若目標價改了、而且現價高於「新」目標，
  直接回到待命（不寄信），下次跌到新目標才再通知。觸發時的目標價由呼叫端傳入 prev_target。

「寄信成功才更新狀態」由呼叫端負責：decide() 只回傳「如果這次通知成功，下一個狀態是什麼」，
呼叫端寄信失敗時就不要把 next_state 存起來，下次執行會得到同樣的結果、再試一次。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum


class AlertState(StrEnum):
    ARMED = "armed"
    TRIGGERED = "triggered"


@dataclass(frozen=True)
class Rule:
    currency: str
    target: Decimal  # 現金賣出價 ≤ target 就通知
    buffer: Decimal  # 回升到 > target + buffer 才重新上膛

    def __post_init__(self) -> None:
        if not isinstance(self.target, Decimal) or not isinstance(self.buffer, Decimal):
            raise TypeError("target 和 buffer 必須是 Decimal")
        if self.target <= 0:
            raise ValueError(f"{self.currency} 的 target 必須大於 0")
        if self.buffer < 0:
            raise ValueError(f"{self.currency} 的 buffer 不能是負數")

    @property
    def rearm_above(self) -> Decimal:
        return self.target + self.buffer


@dataclass(frozen=True)
class Decision:
    next_state: AlertState
    notify: bool
    reason: str = field(default="", compare=False)  # 給 log 看的說明，不影響相等比較


def decide(
    rule: Rule,
    rate: Decimal,
    prev: AlertState | None,
    prev_target: Decimal | None = None,
) -> Decision:
    """根據這次的匯率和上一個狀態，決定要不要通知、下一個狀態是什麼。

    prev_target：進入目前狀態時的目標價（沒有紀錄就是 None，維持原本行為）。
    """
    if not isinstance(rate, Decimal):
        raise TypeError("rate 必須是 Decimal")
    if rate <= 0:
        raise ValueError(f"{rule.currency} 的匯率不合理：{rate}")

    state = prev or AlertState.ARMED

    if state is AlertState.ARMED:
        if rate <= rule.target:
            return Decision(AlertState.TRIGGERED, notify=True, reason="到價")
        return Decision(AlertState.ARMED, notify=False, reason="尚未到價")

    # TRIGGERED
    target_changed = prev_target is not None and prev_target != rule.target
    if target_changed and rate > rule.target:
        return Decision(
            AlertState.ARMED, notify=False, reason="目標已調整且現價高於新目標，重新上膛"
        )
    if rate > rule.rearm_above:
        return Decision(AlertState.ARMED, notify=False, reason="回升超過 buffer，重新上膛")
    return Decision(AlertState.TRIGGERED, notify=False, reason="已通知過，不重複寄")
