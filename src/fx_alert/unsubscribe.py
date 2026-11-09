"""一鍵退訂：用 HMAC 簽章產生退訂連結（沒有 token 表）。

    簽章 = base64url( HMAC-SHA256(UNSUBSCRIBE_SECRET, "fx-alert:unsubscribe:v1:" + user_id) )

  * 同一個人的簽章永遠一樣 → 舊信裡的連結一直有效。
  * 換 UNSUBSCRIBE_SECRET 會讓所有舊連結失效。
  * 驗證在 Edge Function（supabase/functions/unsubscribe/handler.ts），演算法必須和這裡完全一致；
    兩邊的測試用同一組測試向量（tests/test_unsubscribe.py、handler.test.ts）。

信裡有兩種連結：
  * 內文：GitHub Pages 的確認頁 unsubscribe.html?u=…&s=…&p=…（打開只顯示按鈕，按下才退訂）
  * List-Unsubscribe 標頭：Edge Function 的網址，Gmail 的「取消訂閱」按鈕會直接 POST 過去
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlencode, urlsplit

PREFIX = "fx-alert:unsubscribe:v1:"
MIN_SECRET_LENGTH = 32
DEFAULT_SITE_URL = "https://xysmr.github.io/fx-alert/"

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_PROJECT_URL_RE = re.compile(r"^https://([a-z0-9]{20})\.supabase\.co$")


class UnsubscribeError(ValueError):
    pass


def _check_user_id(user_id: str) -> str:
    uid = user_id.strip().lower()
    if not _UUID_RE.match(uid):
        raise UnsubscribeError("user_id 必須是 UUID")
    return uid


def sign(secret: str, user_id: str) -> str:
    uid = _check_user_id(user_id)
    mac = hmac.new(secret.encode(), (PREFIX + uid).encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).rstrip(b"=").decode()


def verify(secret: str, user_id: str, signature: str) -> bool:
    try:
        expected = sign(secret, user_id)
    except UnsubscribeError:
        return False
    return hmac.compare_digest(expected, signature)  # 固定時間比對


@dataclass(frozen=True)
class UnsubscribeLinks:
    page_url: str  # 給人點的：確認頁
    one_click_url: str  # 給信箱用的：List-Unsubscribe（POST）


@dataclass(frozen=True)
class UnsubscribeConfig:
    secret: str = field(repr=False)
    supabase_url: str
    site_url: str = DEFAULT_SITE_URL

    @property
    def project_ref(self) -> str:
        m = _PROJECT_URL_RE.match(self.supabase_url)
        if not m:
            raise UnsubscribeError("SUPABASE_URL 必須是 https://<20 碼專案代號>.supabase.co")
        return m.group(1)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> UnsubscribeConfig:
        env = os.environ if env is None else env
        secret = env.get("UNSUBSCRIBE_SECRET", "").strip()
        if not secret:
            raise UnsubscribeError("缺少環境變數：UNSUBSCRIBE_SECRET")
        if len(secret) < MIN_SECRET_LENGTH:
            raise UnsubscribeError(f"UNSUBSCRIBE_SECRET 至少要 {MIN_SECRET_LENGTH} 個字元")
        site = env.get("FX_SITE_URL", "").strip() or DEFAULT_SITE_URL
        if urlsplit(site).scheme != "https":
            raise UnsubscribeError("FX_SITE_URL 必須是 https:// 開頭")
        cfg = cls(
            secret=secret,
            supabase_url=env.get("SUPABASE_URL", "").strip().rstrip("/"),
            site_url=site if site.endswith("/") else site + "/",
        )
        cfg.project_ref  # 先檢查格式  # noqa: B018
        return cfg

    def links(self, user_id: str) -> UnsubscribeLinks:
        uid = _check_user_id(user_id)
        query = {"u": uid, "s": sign(self.secret, uid)}
        page = f"{self.site_url}unsubscribe.html?" + urlencode({**query, "p": self.project_ref})
        one_click = f"{self.supabase_url}/functions/v1/unsubscribe?" + urlencode(query)
        return UnsubscribeLinks(page_url=page, one_click_url=one_click)
