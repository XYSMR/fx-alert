"""規則與狀態的存放處：run_check 只透過這裡讀寫，不知道資料在檔案還是資料庫。

    load_watches()                 讀出這次要檢查的所有規則（含上一次的狀態、收件人）
    record_alert(w, rate, now)     寄信成功後：狀態改成「已通知」＋寫一筆通知紀錄；
                                   回傳 False 表示規則已不存在（例如使用者剛好刪除帳號）
    record_transition(w, s, ...)   沒寄信但狀態可能改變（例如回升後重新等待）
    flush()                        把累積的變更寫出去；回傳「狀態是否有變」
    count_sent_since(t)            t 之後寄出了幾封到價通知（每日寄信上限用）

出國換匯計畫（只有多人版；run_check 用 getattr 判斷 store 有沒有這兩個方法）：
    load_trips()                   還沒出發、還沒換完、Email 開著的旅程（view public.active_trips）
    count_trip_sent_since(t)       t 之後寄出了幾封換匯提醒（和到價通知一起算每日上限）
    record_trip_notice(t, d, ...)  寄出換匯提醒後寫一筆 trip_notifications；
                                   回傳 False 表示已經寫過（同一天、同一種固定提醒）或旅程已刪除

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
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Protocol

import requests

from fx_alert.config import AppConfig
from fx_alert.history import append_alert, read_alerts
from fx_alert.notify_window import NotifyWindow, parse_window
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
from fx_alert.trip_rules import Decision, Sent, Trip


class StoreError(RuntimeError):
    """讀寫規則或狀態失敗。"""


@dataclass(frozen=True)
class Watch:
    """一條要檢查的規則。"""

    key: str  # 存放處用來找回這條規則的識別碼
    label: str  # 給公開 log 看：只能是編號（不能含 email、幣別、目標價）
    rule: Rule
    state: AlertState | None  # None = 沒有紀錄（第一次），視為等待中
    state_target: Decimal | None  # 進入目前狀態時的目標價；給 rules.decide 判斷「改過目標價」
    recipients: tuple[str, ...] = field(repr=False)
    user_id: str | None = None  # 多人版才有；用來產生退訂連結
    window: NotifyWindow | None = None  # 通知時段（多人版）；None = 全天


@dataclass(frozen=True)
class TripWatch:
    """一趟要檢查的旅程（出國換匯計畫）。"""

    key: str  # trips.id
    label: str  # 給公開 log 看：只能是編號（不能含 email、幣別、金額、日期）
    currency: str
    departure: date
    created_on: date
    remaining: Decimal = field(repr=False)
    sent: tuple[Sent, ...]
    recipients: tuple[str, ...] = field(repr=False)
    user_id: str | None = None
    window: NotifyWindow | None = None

    def as_trip(self) -> Trip:
        return Trip(self.departure, self.created_on, self.remaining)


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

    def record_check(self, at: datetime, ok: bool, errors: int) -> None:
        """記錄這次檢查的結果（多人版寫到 Supabase 的 check_status；單人版不做事）。"""
        ...


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

    def record_check(self, at: datetime, ok: bool, errors: int) -> None:
        return None  # 單人版：最後檢查時間只寫 last_check.json


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
# 通知時段（migration 20261116142000）。程式比 migration 早上線時（等核准），
# 資料庫還沒有這兩欄 → load_watches 改用上面的欄位、時段當作全天
_WINDOW_COLUMNS = "notify_start::text,notify_end::text"


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

    #: 這次讀規則時，資料庫有沒有通知時段欄位（main 用來提醒「migration 還沒套用」）
    window_supported: bool = True

    def load_watches(self) -> list[Watch]:
        # active_rules 已經排除停用的規則和關閉 Email 的人（見 supabase/migrations）
        try:
            resp = self._request(
                "GET",
                "active_rules",
                params={"select": f"{_RULE_COLUMNS},{_WINDOW_COLUMNS}", "order": "id"},
            )
            self.window_supported = True
        except StoreError as exc:
            if "notify_start" not in str(exc) and "notify_end" not in str(exc):
                raise
            resp = self._request(
                "GET", "active_rules", params={"select": _RULE_COLUMNS, "order": "id"}
            )
            self.window_supported = False
        try:
            rows = resp.json()
            return [_row_to_watch(row) for row in rows]
        except (ValueError, KeyError, TypeError, InvalidOperation) as exc:
            raise StoreError(f"Supabase 回傳的規則格式不對：{type(exc).__name__}") from exc

    def count_sent_since(self, since: datetime) -> int:
        return self._count("notifications", since)

    def count_trip_sent_since(self, since: datetime) -> int:
        """換匯計畫的提醒（每日上限是 Gmail 的寄信量，和到價通知一起算；有旅程要寄時才查）。"""
        return self._count("trip_notifications", since)

    def _count(self, table: str, since: datetime) -> int:
        resp = self._request(
            "GET",
            table,
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

    # ---- 出國換匯計畫 ----

    #: 資料庫有沒有換匯計畫的表（migration 20261116195500 還沒套用時是 False）
    trips_supported: bool = True

    def load_trips(self) -> list[TripWatch]:
        try:
            resp = self._request(
                "GET", "active_trips", params={"select": _TRIP_COLUMNS, "order": "id"}
            )
        except StoreError as exc:
            if not _table_missing(exc, "active_trips"):
                raise
            self.trips_supported = False
            return []
        try:
            return [_row_to_trip(row) for row in resp.json()]
        except (ValueError, KeyError, TypeError, InvalidOperation) as exc:
            raise StoreError(f"Supabase 回傳的旅程格式不對：{type(exc).__name__}") from exc

    def record_trip_notice(
        self, trip: TripWatch, decision: Decision, rate: Decimal | None, now: datetime
    ) -> bool:
        try:
            self._request(
                "POST",
                "trip_notifications",
                headers={"Prefer": "return=minimal"},
                json={
                    "trip_id": int(trip.key),
                    "user_id": trip.user_id,
                    "kind": decision.kind,
                    "sent_on": now.date().isoformat(),
                    "sent_at": now.isoformat(timespec="seconds"),
                    "rate": None if rate is None else str(rate),
                    "rank": decision.rank,
                    "window_days": decision.window,
                    "days_left": decision.days_left,
                    "remaining": str(trip.remaining),
                },
            )
        except StoreError as exc:
            # 409：同一天已經寄過、固定提醒已經寄過（unique），或旅程剛好被刪除（外鍵）
            if "回應 409" in str(exc):
                return False
            raise
        return True

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

    def record_check(self, at: datetime, ok: bool, errors: int) -> None:
        """upsert check_status 唯一的一列（id=1）。公開資訊：只有時間、成功與否、錯誤數量。"""
        self._request(
            "POST",
            "check_status",
            params={"on_conflict": "id"},
            headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
            json={
                "id": 1,
                "checked_at": at.isoformat(timespec="seconds"),
                "ok": ok,
                "errors": errors,
                "updated_at": at.isoformat(timespec="seconds"),
            },
        )


class ReadOnlyStore:
    """只讀不寫：判斷照常，但狀態和通知紀錄都不寫入（多人版 --dry-run 用）。"""

    def __init__(self, inner: RuleStore, log=print):
        self._inner = inner
        self._log = log

    def load_watches(self) -> list[Watch]:
        return self._inner.load_watches()

    @property
    def window_supported(self) -> bool:
        return getattr(self._inner, "window_supported", True)

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

    @property
    def trips_supported(self) -> bool:
        return getattr(self._inner, "trips_supported", True)

    def load_trips(self) -> list[TripWatch]:
        load = getattr(self._inner, "load_trips", None)
        return load() if load else []

    def count_trip_sent_since(self, since: datetime) -> int:
        return self._inner.count_trip_sent_since(since)

    def record_trip_notice(self, trip, decision, rate, now) -> bool:
        self._log(f"[dry-run] {trip.label}: 不寫入換匯提醒 {decision.kind}")
        return True

    def record_check(self, at: datetime, ok: bool, errors: int) -> None:
        self._log(f"[dry-run] 不寫入檢查狀態（ok={ok}、錯誤 {errors} 個）")


def _row_to_watch(row: dict) -> Watch:
    currency = row["currency"]
    rule = Rule(currency, target=_dec(row["target"]), buffer=_dec(row["effective_buffer"]))
    state_target = row.get("state_target")
    return Watch(
        key=str(row["id"]),
        # 公開 log 用：只有編號（不含幣別，避免和公開的匯率對照推出目標價）
        label=f"規則#{row['id']}",
        rule=rule,
        state=AlertState(row["state"]),
        state_target=None if state_target is None else _dec(state_target),
        recipients=(row["email"],),
        user_id=row["user_id"],
        window=parse_window(row.get("notify_start"), row.get("notify_end")),
    )


_TRIP_COLUMNS = ",".join(
    [
        "id",
        "user_id",
        "currency",
        "departure",
        "remaining::text",
        "created_on",
        "email",
        "notify_start::text",
        "notify_end::text",
        "sent",
    ]
)


def _row_to_trip(row: dict) -> TripWatch:
    sent = tuple(
        Sent(item["kind"], date.fromisoformat(item["on"])) for item in (row.get("sent") or [])
    )
    return TripWatch(
        key=str(row["id"]),
        label=f"旅程#{row['id']}",  # 公開 log 用：只有編號
        currency=row["currency"],
        departure=date.fromisoformat(row["departure"]),
        created_on=date.fromisoformat(row["created_on"]),
        remaining=_dec(row["remaining"]),
        sent=sent,
        recipients=(row["email"],),
        user_id=row["user_id"],
        window=parse_window(row.get("notify_start"), row.get("notify_end")),
    )


def _table_missing(exc: StoreError, table: str) -> bool:
    """migration 還沒套用：PostgREST 回 404（PGRST205）或訊息裡提到表名。"""
    text = str(exc)
    return "回應 404" in text or (table in text and "schema cache" in text)


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
