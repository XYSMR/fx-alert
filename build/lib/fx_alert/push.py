"""手機推播（Web Push）的寄送端（推播 PR 4／5，設計見 docs/web-push.md）。

寄 Email 成功、記錄完之後，同一則提醒也推播到這個人的裝置（推播是額外的，Email 照寄）：

    pusher.notify(user_id, payload, label)

規則：
  * web_push flag 對這個人沒開（含 kill switch）→ 不推。flag 的判斷和網頁、RLS 相同（flags.py）。
  * 每人每天最多 PER_USER_DAILY_LIMIT 則（Email 和換匯提醒的紀錄一起算），避免規則出錯時一直推。
  * 推播網址再檢查一次白名單（資料庫已經擋過，這裡是第二層），不在白名單的不送。
  * 不跟隨轉址：推播服務回 3xx 一律當成失敗（不會去連 Location 的網址）。
  * 404／410 → 這個訂閱已經失效，刪除。連續失敗 MAX_FAILURES 次也刪除。
  * 推播失敗不算這次檢查的錯誤（Email 已經寄出、狀態已經更新），只寫一行 log。

鎖定畫面看得到推播內容：只放幣別和價格（到價）、或提醒種類（換匯計畫），
不放目標價、金額、email。換匯計畫的價格提醒附「僅供參考，不保證未來匯率走勢」。

公開 log（LOG_PRIVACY）：只寫「規則#N：推播 2 台成功」，不寫推播網址、金鑰或推播服務的回應內容。
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol

import requests

from fx_alert.currencies import CATALOG, format_price
from fx_alert.flags import is_on
from fx_alert.trip_rules import (
    CHEAP_FAR,
    CHEAP_NEAR,
    DISCLAIMER,
    FINAL,
    LAST_CALL,
    NEAR_START,
    Decision,
)

FLAG = "web_push"
PER_USER_DAILY_LIMIT = 10
MAX_FAILURES = 10
TTL_SECONDS = 4 * 60 * 60  # 4 小時內沒送到就不送了（價格已經過時）
TIMEOUT_SECONDS = 10
DEFAULT_SUBJECT = "https://xysmr.github.io"  # VAPID 的聯絡方式（不用 email；只能是網站根網址）

NAMES = {c.code: c.name_zh for c in CATALOG}

_HOST_RE = re.compile(r"^https://([a-z0-9.-]+)/")
_BAD_CHARS_RE = re.compile(r"[\s\x00-\x1f\x7f\\]")
_SUFFIX_HOSTS = (
    re.compile(r"^([a-z0-9-]+\.)+push\.apple\.com$"),
    re.compile(r"^([a-z0-9-]+\.)+notify\.windows\.com$"),
)
_EXACT_HOSTS = frozenset({"fcm.googleapis.com", "updates.push.services.mozilla.com"})
_PRIVATE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_SUBJECT_RE = re.compile(
    r"^(https://[A-Za-z0-9.-]+\.[A-Za-z]{2,}|mailto:[^\s@]+@[^\s@]+\.[A-Za-z]{2,})$"
)


class PushConfigError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# 白名單（和資料庫的 is_allowed_push_endpoint()、網頁的 push.js 相同；
# 三邊都用 tests/fixtures/push_endpoints.json 測）
# ---------------------------------------------------------------------------


def is_allowed_endpoint(url: object) -> bool:
    if not isinstance(url, str) or len(url) > 2048 or _BAD_CHARS_RE.search(url):
        return False
    m = _HOST_RE.match(url)
    if not m:
        return False
    host = m.group(1)
    return host in _EXACT_HOSTS or any(p.match(host) for p in _SUFFIX_HOSTS)


# ---------------------------------------------------------------------------
# 推播內容（sw.js 顯示：title 是鎖定畫面的第一行）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Payload:
    title: str
    body: str
    url: str
    tag: str

    def to_json(self) -> str:
        return json.dumps(
            {"title": self.title, "body": self.body, "url": self.url, "tag": self.tag},
            ensure_ascii=False,
        )


def alert_payload(currency: str, rate: Decimal) -> Payload:
    """到價提醒：「日圓到價：0.2040」。不放目標價。"""
    name = NAMES.get(currency, currency)
    return Payload(
        title=f"{name}到價：{format_price(rate)}",
        body="兆豐銀行現金賣出價・點開查看",
        url=f"./#{currency}",
        tag=f"rate-{currency}",
    )


def trip_payload(currency: str, decision: Decision, rate: Decimal | None) -> Payload:
    """換匯計畫提醒。只描述事實；價格提醒附免責聲明。不放金額、出發日。"""
    name = NAMES.get(currency, currency)
    days = decision.days_left
    if decision.kind in (CHEAP_FAR, CHEAP_NEAR) and rate is not None:
        title = f"{name}換匯計畫：{format_price(rate)}，近 30 天第 {decision.rank} 低"
        body = f"距離出發 {days} 天・{DISCLAIMER}"
    elif decision.kind == NEAR_START:
        title = f"{name}換匯計畫：距離出發 {days} 天"
        body = "可以考慮分批換匯・點開查看進度"
    elif decision.kind == FINAL:
        title = f"{name}最後換匯提醒（距離出發 {days} 天）"
        body = "點開查看換匯進度"
    elif decision.kind == LAST_CALL:
        title = f"{name}出發前提醒"
        body = "出發前最後一個營業日・點開查看換匯進度"
    else:
        title = f"{name}換匯計畫提醒"
        body = "點開查看換匯進度"
    return Payload(title=title, body=body, url=f"./#{currency}", tag=f"trip-{currency}")


# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PushConfig:
    private_key: str = field(repr=False)  # 不出現在 repr / log 裡
    subject: str = DEFAULT_SUBJECT

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> PushConfig | None:
        """沒設定 WEB_PUSH_PRIVATE_KEY → None（不推播，Email 照常）。設了但格式不對 → 錯誤。"""
        env = os.environ if env is None else env
        key = env.get("WEB_PUSH_PRIVATE_KEY", "").strip().rstrip("=")
        if not key:
            return None
        if not _PRIVATE_KEY_RE.match(key):
            raise PushConfigError(
                "WEB_PUSH_PRIVATE_KEY 格式不對：要用 scripts/vapid_keygen.js 產生的私鑰（43 字）"
            )
        subject = env.get("WEB_PUSH_SUBJECT", "").strip() or DEFAULT_SUBJECT
        if not _SUBJECT_RE.match(subject):
            raise PushConfigError(
                "WEB_PUSH_SUBJECT 必須是 https:// 或 mailto: 開頭"
                "（https 只能是網站根網址，不能有路徑）"
            )
        return cls(private_key=key, subject=subject)


# ---------------------------------------------------------------------------
# 送出
# ---------------------------------------------------------------------------

OK, GONE, FAILED = "ok", "gone", "failed"


@dataclass(frozen=True)
class Device:
    id: int
    user_id: str
    endpoint: str = field(repr=False)
    p256dh: str = field(repr=False)
    auth: str = field(repr=False)
    fail_count: int = 0


class Sender(Protocol):
    def send(self, device: Device, payload: Payload) -> str: ...


class _NoRedirectSession(requests.Session):
    """推播服務回 3xx 時不跟隨（SSRF 防護：不會去連 Location 的網址）。"""

    def request(self, *args, **kwargs):
        kwargs["allow_redirects"] = False
        return super().request(*args, **kwargs)


def classify(status: int | None) -> str:
    """推播服務的 HTTP 狀態碼 → OK／GONE／FAILED"""
    if status is not None and 200 <= status <= 202:
        return OK
    if status in (404, 410):
        return GONE
    return FAILED  # 3xx（不跟隨）、401／403（金鑰不對）、413、429、5xx、連不上


class WebPushSender:
    """用 pywebpush 加密並送出。每次都用新的 claims dict：
    pywebpush 會把第一個推播網址的 aud 寫回傳入的 dict，重用會讓其他推播服務拒絕。"""

    def __init__(self, cfg: PushConfig, *, webpush: Callable[..., Any] | None = None):
        self._cfg = cfg
        if webpush is None:
            from pywebpush import webpush as _webpush

            webpush = _webpush
        self._webpush = webpush
        self._session = _NoRedirectSession()

    def send(self, device: Device, payload: Payload) -> str:
        if not is_allowed_endpoint(device.endpoint):
            return FAILED
        try:
            resp = self._webpush(
                subscription_info={
                    "endpoint": device.endpoint,
                    "keys": {"p256dh": device.p256dh, "auth": device.auth},
                },
                data=payload.to_json(),
                vapid_private_key=self._cfg.private_key,
                vapid_claims={"sub": self._cfg.subject},
                ttl=TTL_SECONDS,
                timeout=TIMEOUT_SECONDS,
                headers={"Urgency": "high"},
                requests_session=self._session,
            )
            return classify(getattr(resp, "status_code", None))
        except Exception as exc:  # noqa: BLE001 - 推播失敗不能讓整次檢查失敗
            resp = getattr(exc, "response", None)
            return classify(getattr(resp, "status_code", None))


class DryRunSender:
    """--dry-run：不送出，當作成功（不寫資料庫，ReadOnlyStore 會擋）。"""

    def send(self, device: Device, payload: Payload) -> str:
        return OK


# ---------------------------------------------------------------------------
# 推播給一個人
# ---------------------------------------------------------------------------


class PushStore(Protocol):
    def load_push_targets(self) -> tuple[dict, list[Device], dict[str, str]]: ...
    def count_user_sent_since(self, user_id: str, since: datetime) -> int: ...
    def record_push_result(self, device: Device, outcome: str, now: datetime) -> None: ...


class Pusher:
    """一次檢查共用一個。第一次要推播時才讀 flag 和裝置（沒有提醒要寄的時候不多查資料庫）。"""

    def __init__(
        self,
        store: PushStore,
        sender: Sender,
        *,
        now: datetime,
        log: Callable[[str], None] = print,
    ):
        self._store = store
        self._sender = sender
        self._now = now
        self._log = log
        self._loaded = False
        self._flags: dict = {}
        self._devices: dict[str, list[Device]] = {}
        self._roles: dict[str, str] = {}

    def _load(self) -> bool:
        if not self._loaded:
            self._loaded = True  # 讀失敗也只試一次，這次檢查就不推播
            try:
                flags, devices, roles = self._store.load_push_targets()
            except Exception as exc:  # noqa: BLE001
                self._log(f"注意：讀不到推播設定（{type(exc).__name__}），這次不推播，Email 照常")
                return False
            self._flags, self._roles = flags, roles
            for d in devices:
                self._devices.setdefault(d.user_id, []).append(d)
        return bool(self._flags.get(FLAG, {}).get("enabled"))

    def notify(self, user_id: str | None, payload: Payload, label: str) -> None:
        if not user_id:
            return
        try:
            if not self._load():
                return  # kill switch／還沒開放：完全不推
            if not is_on(FLAG, user_id, self._flags, self._roles.get(user_id)):
                return
            if not self._devices.get(user_id):
                return
            day_start = self._now.replace(hour=0, minute=0, second=0, microsecond=0)
            if self._store.count_user_sent_since(user_id, day_start) > PER_USER_DAILY_LIMIT:
                self._log(f"{label}: 今天的推播已達上限 {PER_USER_DAILY_LIMIT} 則，這次不推播")
                return
            self._log(f"{label}: {self._push(user_id, payload)}")
        except Exception as exc:  # noqa: BLE001 - 推播出錯不影響 Email 和狀態
            self._log(f"{label}: 推播失敗（{type(exc).__name__}），Email 已照常寄出")

    def _push(self, user_id: str, payload: Payload) -> str:
        counts = {OK: 0, GONE: 0, FAILED: 0}
        not_saved = 0
        kept: list[Device] = []
        for d in self._devices.get(user_id, []):
            outcome = self._sender.send(d, payload)
            counts[outcome] += 1
            fails = 0 if outcome == OK else d.fail_count + 1
            action = "delete" if outcome == GONE or fails >= MAX_FAILURES else outcome
            try:
                self._store.record_push_result(d, action, self._now)
            except Exception:  # noqa: BLE001 - 一台寫不進去不影響其他台
                not_saved += 1
            if action != "delete":
                kept.append(replace(d, fail_count=fails))
        self._devices[user_id] = kept
        parts = [f"推播 {counts[OK]} 台成功"]
        if counts[GONE]:
            parts.append(f"{counts[GONE]} 台已失效、已移除")
        if counts[FAILED]:
            parts.append(f"{counts[FAILED]} 台失敗")
        if not_saved:
            parts.append(f"{not_saved} 台的結果沒有寫進資料庫")
        return "、".join(parts)
