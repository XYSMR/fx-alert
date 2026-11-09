"""讀寫 state.json：記錄每個幣別目前是「待命」還是「已通知」。

檔案格式（排序固定、縮排 2、結尾換行，方便在 git diff 裡閱讀）：

    {
      "currencies": {
        "JPY": {"changed_at": "2026-11-09T10:07:00+08:00", "rate": "0.2049",
                "state": "triggered", "target": "0.205"}
      },
      "version": 1
    }

設計重點：
- 檔案不存在 = 第一次執行，回傳空字典（每個幣別都會被視為待命）。
- 檔案壞掉時「拋錯」而不是悄悄重置：重置會讓已通知的幣別又寄一次信，寧可讓排程失敗、被看見。
- 只在狀態「轉換」時記錄時間、匯率和當時的目標價（target 為選填，舊檔案沒有也能讀）。
- 先寫暫存檔再 os.replace，寫到一半中斷也不會留下半個檔案。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from fx_alert.rules import AlertState

FORMAT_VERSION = 1


class StateError(RuntimeError):
    """state.json 內容無法辨識。"""


@dataclass(frozen=True)
class CurrencyState:
    state: AlertState
    changed_at: str  # ISO 8601，狀態最後一次轉換的時間
    rate: str  # 轉換當下的匯率（字串保存，避免精度問題）
    target: str | None = None  # 轉換當下的目標價；用來偵測「目標價被改過」


States = dict[str, CurrencyState]


def load_states(path: Path) -> States:
    """讀取狀態檔。不存在回傳 {}；內容不合法拋 StateError。"""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StateError(f"讀不了 {path}：{exc}") from exc

    if not isinstance(data, dict) or data.get("version") != FORMAT_VERSION:
        raise StateError(f"{path} 的版本不是 {FORMAT_VERSION}，或格式不對")
    currencies = data.get("currencies")
    if not isinstance(currencies, dict):
        raise StateError(f"{path} 缺少 currencies 物件")

    states: States = {}
    for code, entry in currencies.items():
        if not isinstance(entry, dict):
            raise StateError(f"{code} 的狀態格式不對：{entry!r}")
        try:
            states[code] = CurrencyState(
                state=AlertState(entry["state"]),
                changed_at=str(entry["changed_at"]),
                rate=str(entry["rate"]),
                target=None if entry.get("target") is None else str(entry["target"]),
            )
        except (KeyError, ValueError) as exc:
            raise StateError(f"{code} 的狀態內容不合法：{entry!r}") from exc
    return states


def dumps_states(states: States) -> str:
    """序列化成固定格式的文字（同樣內容一定產生同樣文字）。"""
    payload = {
        "version": FORMAT_VERSION,
        "currencies": {code: _entry(s) for code, s in states.items()},
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _entry(s: CurrencyState) -> dict[str, str]:
    d = {"state": s.state.value, "changed_at": s.changed_at, "rate": s.rate}
    if s.target is not None:
        d["target"] = s.target
    return d


def save_states(path: Path, states: States) -> bool:
    """寫入狀態檔；內容跟現有檔案一樣就不寫。回傳「是否有變更」。"""
    text = dumps_states(states)
    if path.exists() and path.read_bytes() == text.encode("utf-8"):
        return False

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)
    return True


def current(states: States, currency: str) -> AlertState | None:
    """取得某幣別目前的狀態；沒有紀錄回傳 None（= 第一次，視為待命）。"""
    entry = states.get(currency)
    return entry.state if entry else None


def target_at_transition(states: States, currency: str) -> Decimal | None:
    """進入目前狀態時的目標價；沒有紀錄（或舊格式）回傳 None。"""
    entry = states.get(currency)
    return Decimal(entry.target) if entry and entry.target is not None else None


def with_transition(
    states: States,
    currency: str,
    new_state: AlertState,
    rate: Decimal,
    now: datetime,
    target: Decimal | None = None,
) -> States:
    """回傳套用新狀態後的「新字典」；狀態沒變就原樣回傳（不更新時間）。"""
    if current(states, currency) is new_state:
        return states
    entry = CurrencyState(
        state=new_state,
        changed_at=now.isoformat(timespec="seconds"),
        rate=str(rate),
        target=None if target is None else str(target),
    )
    return {**states, currency: entry}
