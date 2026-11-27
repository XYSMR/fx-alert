"""多人版 → 單人版的反向匯出（python -m fx_alert export-state），退回方案用。

把資料庫裡「自己的」規則狀態和通知紀錄寫回 state 分支：

  * <data>/state.json  每個 config.yaml 裡的幣別：狀態、轉換時間、匯率、觸發時的目標價
  * <data>/alerts.csv  補上資料庫有、檔案裡還沒有的通知（依時間排序附加在後面）

為什麼不直接用切換前的備份覆蓋：多人版期間如果寄過通知，備份裡那個幣別還是「待命」，
切回單人版後同一次到價會再寄一封。用資料庫的狀態寫回就不會。

規則：
  * 只處理 config.yaml 裡的幣別（單人版只看 config.yaml）。資料庫多出來的幣別只提醒、不寫入。
  * 資料庫裡沒有這個幣別、或從來沒轉換過狀態（state_changed_at 是空的）→ 保留檔案原本的內容。
  * 目標價、buffer 一律以 config.yaml 為準；網頁上改過的話只提醒（config.yaml 要自己決定要不要改）。
  * 通知去重用「時間點（到秒）、幣別、匯率、目標價」：alerts.csv 只記到秒，資料庫有微秒，
    所以比對前把時間截到秒，跑幾次結果都一樣。
  * --dry-run：照樣讀資料庫，但不寫任何檔案。

安全：secret key 只從環境變數讀；會用到 email，所以拒絕在 GitHub Actions（公開 log）裡執行。
寫出的檔案和原本一樣不含 email。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from fx_alert.config import TAIPEI, AppConfig
from fx_alert.history import append_alert, read_alerts
from fx_alert.migrate import MigrationError, Plan, PlannedNotification, PlannedRule
from fx_alert.rules import AlertState
from fx_alert.state_store import (
    CurrencyState,
    StateError,
    States,
    load_states,
    save_states,
)


@dataclass(frozen=True)
class ExportPlan:
    states: States  # 要寫進 state.json 的完整內容
    state_changes: tuple[str, ...]  # 給人看的變更說明
    new_alerts: tuple[PlannedNotification, ...]  # 要附加到 alerts.csv 的通知（依時間排序）
    warnings: tuple[str, ...] = field(default=())

    @property
    def has_changes(self) -> bool:
        return bool(self.state_changes or self.new_alerts)


def _changed_at(dt: datetime) -> str:
    # 和單人版 with_transition 寫的格式一樣：台灣時間、到秒
    return dt.astimezone(TAIPEI).isoformat(timespec="seconds")


def _to_state(rule: PlannedRule) -> CurrencyState:
    try:
        state = AlertState(rule.state)
    except ValueError as exc:
        raise MigrationError(f"{rule.currency} 的狀態無法辨識：{rule.state}") from exc
    if rule.state_changed_at is None or rule.state_rate is None:
        raise MigrationError(f"{rule.currency} 的狀態缺少時間或匯率")
    return CurrencyState(
        state=state,
        changed_at=_changed_at(rule.state_changed_at),
        rate=str(rule.state_rate),
        target=None if rule.state_target is None else str(rule.state_target),
    )


def _alert_key(sent_at: datetime, currency: str, rate: Decimal, target: Decimal) -> tuple:
    # 時間點截到秒（alerts.csv 只記到秒）；Decimal 比數值（0.0250 == 0.025）
    return (int(sent_at.timestamp()), currency, rate.normalize(), target.normalize())


def plan_export(db: Plan, cfg: AppConfig, data_dir: Path) -> ExportPlan:
    """比較資料庫（db）和 data_dir 的檔案，算出要寫什麼。不寫任何檔案。"""
    try:
        current = load_states(data_dir / "state.json")
    except StateError as exc:
        raise MigrationError(str(exc)) from exc

    codes = [r.currency for r in cfg.rules]
    db_rules = {r.currency: r for r in db.rules}
    warnings: list[str] = []
    changes: list[str] = []
    states: States = dict(current)

    for extra in sorted(set(db_rules) - set(codes)):
        warnings.append(f"{extra}：只在網頁上設定過，config.yaml 沒有，單人版不會處理（略過）")

    for cfg_rule in cfg.rules:
        code = cfg_rule.currency
        r = db_rules.get(code)
        if r is None:
            warnings.append(f"{code}：資料庫沒有這條提醒，保留 state.json 原本的內容")
            continue
        if r.target != cfg_rule.target or r.buffer != cfg_rule.buffer:
            warnings.append(
                f"{code}：網頁上的設定（目標 {r.target}、buffer {r.buffer}）和 config.yaml"
                f"（目標 {cfg_rule.target}、buffer {cfg_rule.buffer}）不同；"
                "退回後以 config.yaml 為準，需要的話請自己改 config.yaml"
            )
        if r.state_changed_at is None:
            continue  # 資料庫裡從來沒轉換過：沿用檔案原本的內容
        new = _to_state(r)
        old = current.get(code)
        if old != new:
            before = old.state.value if old else "（沒有紀錄）"
            changes.append(f"{code}：{before} → {new.state.value}（{new.changed_at}）")
            states[code] = new

    existing = {_alert_key(a.sent_at, a.currency, a.rate, a.target) for a in read_alerts(data_dir)}
    seen: set[tuple] = set()
    new_alerts: list[PlannedNotification] = []
    for n in sorted(db.notifications, key=lambda n: (n.sent_at.timestamp(), n.currency)):
        if n.currency not in codes:
            continue
        key = _alert_key(n.sent_at, n.currency, n.rate, n.target)
        if key in existing or key in seen:
            continue
        seen.add(key)
        new_alerts.append(n)

    return ExportPlan(
        states=states,
        state_changes=tuple(changes),
        new_alerts=tuple(new_alerts),
        warnings=tuple(warnings),
    )


def write_export(plan: ExportPlan, data_dir: Path) -> None:
    """把 plan_export 的結果寫進檔案（state.json 先寫暫存檔再取代；alerts.csv 只附加）。"""
    save_states(data_dir / "state.json", plan.states)
    for n in plan.new_alerts:
        append_alert(data_dir, n.sent_at, n.currency, n.rate, n.target)


def describe_export(plan: ExportPlan) -> Iterable[str]:
    if plan.state_changes:
        yield "state.json 會更新："
        yield from (f"  {c}" for c in plan.state_changes)
    else:
        yield "state.json 不用改"
    if plan.new_alerts:
        yield f"alerts.csv 會補上 {len(plan.new_alerts)} 筆通知："
        for n in plan.new_alerts:
            yield f"  {_changed_at(n.sent_at)} {n.currency} 匯率 {n.rate}（目標 {n.target}）"
    else:
        yield "alerts.csv 不用改"
    for w in plan.warnings:
        yield f"⚠ {w}"


__all__ = [
    "ExportPlan",
    "describe_export",
    "plan_export",
    "write_export",
]
