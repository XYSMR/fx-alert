"""單人版 → 多人版的搬家工具（python -m fx_alert migrate-me）。

來源（都在自己的電腦上讀）：
  * config.yaml        目標價、buffer（照原值搬，不改成自動 0.5%）
  * <data>/state.json  狀態、轉換時間、匯率、觸發時的目標價（舊格式沒有目標價 → 留空）
  * <data>/alerts.csv  通知紀錄
目的地：Supabase 的 public.import_rules_for_user（同一個交易；已有規則就拒絕，--replace 才覆蓋）。

搬完會重新讀回來逐項比對：
  * 數字用 Decimal 比較（0.0250 和 0.025 相同）
  * 時間一律轉成時間點比較（+08:00 和 UTC 寫法不同但同一刻 → 相同）

安全：
  * secret key 只從環境變數讀（SUPABASE_URL、SUPABASE_SECRET_KEY），不讀也不寫任何 .env 檔。
  * 會印出 email，所以拒絕在 GitHub Actions（公開 log）裡執行。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import requests

from fx_alert.config import AppConfig
from fx_alert.history import read_alerts
from fx_alert.state_store import StateError, load_states
from fx_alert.store import TIMEOUT_SECONDS, SupabaseConfig


class MigrationError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# 要搬的資料
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlannedRule:
    currency: str
    target: Decimal
    buffer: Decimal
    state: str  # armed / triggered
    state_changed_at: datetime | None
    state_rate: Decimal | None
    state_target: Decimal | None

    def as_json(self) -> dict:
        return {
            "currency": self.currency,
            "target": str(self.target),
            "buffer": str(self.buffer),
            "state": self.state,
            "state_changed_at": _iso(self.state_changed_at),
            "state_rate": _str(self.state_rate),
            "state_target": _str(self.state_target),
        }


@dataclass(frozen=True)
class PlannedNotification:
    currency: str
    rate: Decimal
    target: Decimal
    sent_at: datetime

    def as_json(self) -> dict:
        return {
            "currency": self.currency,
            "rate": str(self.rate),
            "target": str(self.target),
            "sent_at": _iso(self.sent_at),
        }


@dataclass(frozen=True)
class Plan:
    rules: tuple[PlannedRule, ...]
    notifications: tuple[PlannedNotification, ...]


def _iso(dt: datetime | None) -> str | None:
    return None if dt is None else dt.isoformat()


def _str(d: Decimal | None) -> str | None:
    return None if d is None else str(d)


def _parse_time(text: str | None) -> datetime | None:
    if not text:
        return None
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise MigrationError(f"時間缺少時區：{text}")
    return dt


def _parse_dec(text) -> Decimal | None:
    if text is None or text == "":
        return None
    try:
        d = Decimal(str(text))
    except InvalidOperation as exc:
        raise MigrationError(f"不是數字：{text}") from exc
    if not d.is_finite():
        raise MigrationError(f"不是數字：{text}")
    return d


def collect(cfg: AppConfig, data_dir: Path) -> Plan:
    """從 config.yaml、state.json、alerts.csv 整理出要搬的資料。"""
    try:
        states = load_states(data_dir / "state.json")
    except StateError as exc:
        raise MigrationError(str(exc)) from exc
    rules = []
    for r in cfg.rules:
        s = states.get(r.currency)
        rules.append(
            PlannedRule(
                currency=r.currency,
                target=r.target,
                buffer=r.buffer,
                state=s.state.value if s else "armed",
                state_changed_at=_parse_time(s.changed_at) if s else None,
                state_rate=_parse_dec(s.rate) if s else None,
                state_target=_parse_dec(s.target) if s else None,
            )
        )
    codes = {r.currency for r in cfg.rules}
    notes = tuple(
        PlannedNotification(a.currency, a.rate, a.target, a.sent_at)
        for a in read_alerts(data_dir)
        if a.currency in codes
    )
    return Plan(rules=tuple(rules), notifications=notes)


# ---------------------------------------------------------------------------
# 比對：Decimal 比數字、時間點比時間
# ---------------------------------------------------------------------------


def _rule_key(r: PlannedRule) -> tuple:
    return (
        r.currency,
        r.target,
        r.buffer,
        r.state,
        r.state_changed_at,
        r.state_rate,
        r.state_target,
    )


def _note_key(n: PlannedNotification) -> tuple:
    return (n.sent_at, n.currency, n.rate, n.target)


def compare(expected: Plan, actual: Plan) -> list[str]:
    """回傳不一致的地方（空的 = 完全一致）。

    Decimal 的 == 會把 0.0250 和 0.025 視為相同；aware datetime 的 == 比的是時間點。
    """
    problems = []
    exp_rules = {r.currency: r for r in expected.rules}
    act_rules = {r.currency: r for r in actual.rules}
    if set(exp_rules) != set(act_rules):
        problems.append(f"幣別不同：預期 {sorted(exp_rules)}，實際 {sorted(act_rules)}")
    for code in sorted(set(exp_rules) & set(act_rules)):
        e, a = _rule_key(exp_rules[code]), _rule_key(act_rules[code])
        names = ("currency", "target", "buffer", "state", "state_changed_at", "state_rate",
                 "state_target")  # fmt: skip
        for name, ev, av in zip(names, e, a, strict=True):
            if ev != av:
                problems.append(f"{code} {name}：預期 {ev}，實際 {av}")

    exp_notes = sorted(map(_note_key, expected.notifications), key=_sort_key)
    act_notes = sorted(map(_note_key, actual.notifications), key=_sort_key)
    if len(exp_notes) != len(act_notes):
        problems.append(f"通知紀錄筆數：預期 {len(exp_notes)}，實際 {len(act_notes)}")
    elif exp_notes != act_notes:
        problems.append("通知紀錄內容不一致")
    return problems


def _sort_key(k: tuple) -> tuple:
    sent_at, currency, rate, target = k
    return (sent_at.timestamp(), currency, rate, target)


# ---------------------------------------------------------------------------
# Supabase（secret key 只從環境變數來，見 SupabaseConfig.from_env）
# ---------------------------------------------------------------------------


class MigrationClient:
    def __init__(self, cfg: SupabaseConfig, *, session: requests.Session | None = None):
        self._cfg = cfg
        self._session = session or requests.Session()

    def _request(self, method: str, path: str, **kw) -> requests.Response:
        headers = {"apikey": self._cfg.secret_key, "Content-Type": "application/json"}
        try:
            resp = self._session.request(
                method,
                f"{self._cfg.url}/rest/v1/{path}",
                headers=headers,
                timeout=TIMEOUT_SECONDS,
                **kw,
            )
        except requests.RequestException as exc:
            raise MigrationError(f"連不到 Supabase：{type(exc).__name__}") from exc
        if not resp.ok:
            raise MigrationError(_explain(resp))
        return resp

    def find_user(self, email: str) -> str:
        resp = self._request(
            "GET", "profiles", params={"select": "user_id", "email": f"eq.{email.strip().lower()}"}
        )
        rows = resp.json()
        if not rows:
            raise MigrationError(
                "找不到這個 email 的帳號。"
                "請先用 Google 登入一次（正式網站，或本機開啟登入功能的頁面）"
            )
        return rows[0]["user_id"]

    def import_plan(self, user_id: str, plan: Plan, *, replace: bool) -> dict:
        resp = self._request(
            "POST",
            "rpc/import_rules_for_user",
            json={
                "p_user_id": user_id,
                "p_rules": [r.as_json() for r in plan.rules],
                "p_notifications": [n.as_json() for n in plan.notifications],
                "p_replace": replace,
            },
        )
        return resp.json()

    def fetch(self, user_id: str) -> Plan:
        rules = self._request(
            "GET",
            "alert_rules",
            params={
                "select": "currency,target::text,buffer::text,state,state_changed_at,"
                "state_rate::text,state_target::text",
                "user_id": f"eq.{user_id}",
            },
        ).json()
        notes = self._request(
            "GET",
            "notifications",
            params={
                "select": "currency,rate::text,target::text,sent_at",
                "user_id": f"eq.{user_id}",
                "order": "sent_at",
            },
        ).json()
        return Plan(
            rules=tuple(
                PlannedRule(
                    currency=r["currency"],
                    target=_parse_dec(r["target"]),
                    buffer=_parse_dec(r["buffer"]),
                    state=r["state"],
                    state_changed_at=_parse_time(r["state_changed_at"]),
                    state_rate=_parse_dec(r["state_rate"]),
                    state_target=_parse_dec(r["state_target"]),
                )
                for r in rules
            ),
            notifications=tuple(
                PlannedNotification(
                    n["currency"],
                    _parse_dec(n["rate"]),
                    _parse_dec(n["target"]),
                    _parse_time(n["sent_at"]),
                )  # fmt: skip
                for n in notes
            ),
        )


def _explain(resp: requests.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        body = {}
    message = str(body.get("message", "")) if isinstance(body, dict) else ""
    if "already has rules" in message:
        return (
            "這個帳號已經有提醒了。確定要用 config.yaml 的設定覆蓋（含通知紀錄）時，請加 --replace"
        )
    if "profile not found" in message:
        return "找不到這個帳號的 profile"
    return f"Supabase 回應 {resp.status_code}：{message[:200] or resp.reason}"


def mask_email(email: str) -> str:
    name, _, domain = email.partition("@")
    return f"{name[:3]}***@{domain}" if domain else "***"


def describe(plan: Plan) -> Sequence[str]:
    lines = []
    for r in plan.rules:
        state = "已通知" if r.state == "triggered" else "等待中"
        since = f"（{r.state_changed_at:%Y-%m-%d %H:%M}）" if r.state_changed_at else ""
        lines.append(f"  {r.currency}: 目標 {r.target}，buffer {r.buffer}，{state}{since}")
    lines.append(f"  通知紀錄 {len(plan.notifications)} 筆")
    return lines


__all__ = [
    "MigrationClient",
    "MigrationError",
    "Plan",
    "PlannedNotification",
    "PlannedRule",
    "collect",
    "compare",
    "describe",
    "mask_email",
]
