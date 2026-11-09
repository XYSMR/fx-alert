"""規則與狀態的存放處：run_check 只透過這裡讀寫，不知道資料在檔案還是資料庫。

    load_watches()                 讀出這次要檢查的所有規則（含上一次的狀態、收件人）
    record_alert(w, rate, now)     寄信成功後：狀態改成「已通知」＋寫一筆通知紀錄；
                                   回傳 False 表示規則已不存在（例如使用者剛好刪除帳號）
    record_transition(w, s, ...)   沒寄信但狀態可能改變（例如回升後重新等待）
    flush()                        把累積的變更寫出去；回傳「狀態是否有變」
    count_sent_since(t)            t 之後寄出了幾封通知（每日寄信上限用）

兩種實作：
  * FileStore：單人版。規則來自 config.yaml，狀態在 state.json，通知紀錄在 alerts.csv，
    收件人來自 MAIL_TO。
  * SupabaseStore：多人版。用 secret key 呼叫 Supabase 的 REST API。
    規則從 view public.active_rules 讀，只有「規則啟用、而且 Email 通知開著」的規則；
    Email 關閉的人不判斷、不更新狀態。
  * ReadOnlyStore：包住另一個 store，只讀不寫（多人版的 --dry-run）。

注意：GitHub Actions 的 log 在 public repo 是公開的。Watch.label 會出現在 log，
所以只能放幣別和規則編號，不能放 email。
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Protocol

import requests

from fx_alert.config import AppConfig
from fx_alert.history import append_alert, read_alerts
from fx_alert.rules import AlertState, Rule
from fx_alert.state_store import (
    StateError,
    States,
    current,
    load_states,
    save_states,
    target_at_transition,
    with_transition,
)


class StoreError(RuntimeError):
    """讀寫規則或狀態失敗。"""


@dataclass(frozen=True)
class Watch:
    """一條要檢查的規則。"""

    key: str  # 存放處用來找回這條規則的識別碼
    label: str  # 給 log 看（不能含 email）
    rule: Rule
    state: AlertState | None  # None = 沒有紀錄（第一次），視為等待中
    state_target: Decimal | None  # 進入目前狀態時的目標價；給 rules.decide 判斷「改過目標價」
    recipients: tuple[str, ...] = field(repr=False)
    user_id: str | None = None  # 多人版才有；用來產生退訂連結


class RuleStore(Protocol):
    def load_watches(self) -> list[Watch]: ...

    def record_alert(self, watch: Watch, rate: Decimal, now: datetime) -> bool: ...

    def record_transition(
        self, watch: Watch, new_state: AlertState, rate: Decimal, now: datetime
    ) -> bool:
        """回傳 False 表示狀態在這段期間被別人改過，這次沒有寫入。"""
        ...

    def flush(self) -> bool: ...

    def count_sent_since(self, since: datetime) -> int: ...


# ---------------------------------------------------------------------------
# FileStore：單人版（現在的做法）
# ---------------------------------------------------------------------------


class FileStore:
    def __init__(self, cfg: AppConfig, recipients: Sequence[str]):
        self._cfg = cfg
        self._recipients = tuple(recipients)
        self._states: States | None = None

    def load_watches(self) -> list[Watch]:
        try:
            states = load_states(self._cfg.state_file)
        except StateError as exc:
            raise StoreError(str(exc)) from exc
        self._states = states
        return [
            Watch(
                key=r.currency,
                label=r.currency,
                rule=r,
                state=current(states, r.currency),
                state_target=target_at_transition(states, r.currency),
                recipients=self._recipients,
            )
            for r in self._cfg.rules
        ]

    def record_alert(self, watch: Watch, rate: Decimal, now: datetime) -> bool:
        append_alert(self._cfg.data_dir, now, watch.rule.currency, rate, watch.rule.target)
        self._transition(watch, AlertState.TRIGGERED, rate, now)
        return True

    def record_transition(
        self, watch: Watch, new_state: AlertState, rate: Decimal, now: datetime
    ) -> bool:
        self._transition(watch, new_state, rate, now)
        return True

    def _transition(self, watch, new_state, rate, now) -> None:
        if self._states is None:
            raise StoreError("load_watches() 之前不能更新狀態")
        self._states = with_transition(
            self._states, watch.key, new_state, rate, now, target=watch.rule.target
        )

    def flush(self) -> bool:
        if self._states is None:
            return False
        return save_states(self._cfg.state_file, self._states)

    def count_sent_since(self, since: datetime) -> int:
        return sum(1 for a in read_alerts(self._cfg.data_dir) if a.sent_at >= since)


# ---------------------------------------------------------------------------
# SupabaseStore：多人版
# ---------------------------------------------------------------------------

TIMEOUT_SECONDS = 15

# 數字一律轉成文字取回，避免 JSON 浮點數造成精度問題
_RULE_COLUMNS = ",".join(
    [
        "id",
        "user_id",
        "currency",
        "target::text",
        "effective_buffer::text",
        "state",
        "state_target::text",
        "email",
    ]
)


@dataclass(frozen=True)
class SupabaseConfig:
    url: str
    secret_key: str = field(repr=False)  # 不出現在 repr / log 裡

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> SupabaseConfig:
        env = os.environ if env is None else env
        missing = [k for k in ("SUPABASE_URL", "SUPABASE_SECRET_KEY") if not env.get(k, "").strip()]
        if missing:
            raise StoreError(f"缺少環境變數：{', '.join(missing)}")
        url = env["SUPABASE_URL"].strip().rstrip("/")
        if not url.startswith("https://") and not url.startswith("http://127.0.0.1"):
            raise StoreError("SUPABASE_URL 必須是 https:// 開頭")
        return cls(url=url, secret_key=env["SUPABASE_SECRET_KEY"].strip())


class SupabaseStore:
    def __init__(self, cfg: SupabaseConfig, *, session: requests.Session | None = None):
        self._cfg = cfg
        self._session = session or requests.Session()
        self._changed = False

    # ---- HTTP ----

    def _request(self, method: str, path: str, **kw) -> requests.Response:
        headers = {
            # 新版 secret key（sb_secret_…）只放在 apikey，不放 Authorization
            "apikey": self._cfg.secret_key,
            "Content-Type": "application/json",
            **kw.pop("headers", {}),
        }
        url = f"{self._cfg.url}/rest/v1/{path}"
        try:
            resp = self._session.request(
                method, url, headers=headers, timeout=TIMEOUT_SECONDS, **kw
            )
        except requests.RequestException as exc:
            # 只回報錯誤類型，不把網址或標頭帶進 log
            raise StoreError(f"連不到 Supabase：{type(exc).__name__}") from exc
        if not resp.ok:
            raise StoreError(f"Supabase 回應 {resp.status_code}：{_error_message(resp)}")
        return resp

    # ---- RuleStore ----

    def load_watches(self) -> list[Watch]:
        # active_rules 已經排除停用的規則和關閉 Email 的人（見 supabase/migrations）
        resp = self._request("GET", "active_rules", params={"select": _RULE_COLUMNS, "order": "id"})
        try:
            rows = resp.json()
            return [_row_to_watch(row) for row in rows]
        except (ValueError, KeyError, TypeError, InvalidOperation) as exc:
            raise StoreError(f"Supabase 回傳的規則格式不對：{type(exc).__name__}") from exc

    def count_sent_since(self, since: datetime) -> int:
        resp = self._request(
            "GET",
            "notifications",
            params={
                "select": "id",
                "sent_at": f"gte.{since.isoformat(timespec='seconds')}",
                "limit": "1",
            },
            headers={"Prefer": "count=exact"},
        )
        # Content-Range 長得像 "0-0/12" 或 "*/0"
        total = resp.headers.get("Content-Range", "").rpartition("/")[2]
        if not total.isdigit():
            raise StoreError("Supabase 沒有回傳通知數量")
        return int(total)

    def record_alert(self, watch: Watch, rate: Decimal, now: datetime) -> bool:
        # 改狀態＋寫通知紀錄在資料庫的同一個交易裡完成（public.record_alert）。
        # 回傳 null = 規則或帳號在這段期間被刪除，什麼都沒寫
        resp = self._request(
            "POST",
            "rpc/record_alert",
            json={
                "p_rule_id": int(watch.key),
                "p_rate": str(rate),
                "p_target": str(watch.rule.target),
                "p_sent_at": now.isoformat(timespec="seconds"),
            },
        )
        try:
            recorded = resp.json() is not None
        except ValueError:
            recorded = True  # 沒有內容但成功：保守當成已寫入，避免重複寄信
        self._changed = self._changed or recorded
        return recorded

    def record_transition(
        self, watch: Watch, new_state: AlertState, rate: Decimal, now: datetime
    ) -> bool:
        if new_state is (watch.state or AlertState.ARMED):
            return True  # 沒變就不寫
        # 只有在狀態還是我們讀到的那個時才更新；使用者剛好在這段期間重新開啟 Email
        # （資料庫把規則重設為等待中）的話，就不要用舊資料蓋掉
        resp = self._request(
            "PATCH",
            "alert_rules",
            params={"id": f"eq.{watch.key}", "state": f"eq.{watch.state or 'armed'}"},
            headers={"Prefer": "return=representation"},
            json={
                "state": new_state.value,
                "state_changed_at": now.isoformat(timespec="seconds"),
                "state_rate": str(rate),
                "state_target": str(watch.rule.target),
            },
        )
        applied = bool(resp.json())
        self._changed = self._changed or applied
        return applied

    def flush(self) -> bool:
        return self._changed  # 每次都已直接寫入，這裡只回報有沒有變更


class ReadOnlyStore:
    """只讀不寫：判斷照常，但狀態和通知紀錄都不寫入（多人版 --dry-run 用）。"""

    def __init__(self, inner: RuleStore, log=print):
        self._inner = inner
        self._log = log

    def load_watches(self) -> list[Watch]:
        return self._inner.load_watches()

    def count_sent_since(self, since: datetime) -> int:
        return self._inner.count_sent_since(since)

    def record_alert(self, watch: Watch, rate: Decimal, now: datetime) -> bool:
        self._log(f"[dry-run] {watch.label}: 不寫入「已通知」")
        return True

    def record_transition(
        self, watch: Watch, new_state: AlertState, rate: Decimal, now: datetime
    ) -> bool:
        if new_state is not (watch.state or AlertState.ARMED):
            self._log(f"[dry-run] {watch.label}: 不寫入 {new_state.value}")
        return True

    def flush(self) -> bool:
        return False


def _row_to_watch(row: dict) -> Watch:
    currency = row["currency"]
    rule = Rule(currency, target=_dec(row["target"]), buffer=_dec(row["effective_buffer"]))
    state_target = row.get("state_target")
    return Watch(
        key=str(row["id"]),
        label=f"{currency} 規則#{row['id']}",
        rule=rule,
        state=AlertState(row["state"]),
        state_target=None if state_target is None else _dec(state_target),
        recipients=(row["email"],),
        user_id=row["user_id"],
    )


def _dec(text: str) -> Decimal:
    """資料庫的 numeric(12,6) 會帶多餘的 0（0.300000）；去掉，信件和 log 比較好讀。"""
    d = Decimal(text)
    if not d.is_finite():
        raise InvalidOperation(text)
    return Decimal(format(d.normalize(), "f"))


def _error_message(resp: requests.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.reason or "（沒有說明）"
    if isinstance(body, dict):
        return str(body.get("message") or body.get("msg") or body.get("error") or body)[:200]
    return str(body)[:200]
