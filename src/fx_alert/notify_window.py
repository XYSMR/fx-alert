"""通知時段：每個人可以設定只在這段時間內收到到價通知（台灣時間）。

* 開始 < 結束：當天的時段（預設 08:00–22:00，不含 22:00）
* 開始 > 結束：跨午夜（例如 22:00–07:00）
* 開始 = 結束：全天

檢查時已到價、但不在時段內：不寄、狀態不變；時段開始後的第一次檢查會重新判斷
（那時還在目標以下才寄，不補寄夜裡曾經到價的通知）。見 main._check_rates。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time

from fx_alert.config import TAIPEI


@dataclass(frozen=True)
class NotifyWindow:
    start: time
    end: time

    @property
    def all_day(self) -> bool:
        return self.start == self.end

    def contains(self, at: datetime) -> bool:
        """at 是不是在時段內（一律換成台灣時間判斷）。"""
        if self.all_day:
            return True
        t = at.astimezone(TAIPEI).time() if at.tzinfo else at.time()
        if self.start < self.end:
            return self.start <= t < self.end
        return t >= self.start or t < self.end  # 跨午夜

    def label(self) -> str:
        if self.all_day:
            return "全天"
        return f"{self.start:%H:%M}–{self.end:%H:%M}"


def parse_window(start: str | None, end: str | None) -> NotifyWindow | None:
    """資料庫的 time（"08:00:00"）→ NotifyWindow；沒有欄位（migration 還沒套用）→ None = 全天。"""
    if not start or not end:
        return None
    return NotifyWindow(time.fromisoformat(start), time.fromisoformat(end))
