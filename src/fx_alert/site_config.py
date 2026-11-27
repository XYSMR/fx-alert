"""產生前端的 config.js（部署時執行，不進 git）。

    AUTH_ENABLED=false（或沒設定）→ window.FX_CONFIG = {"authEnabled": false};
        頁面維持單人版的顯示（目標價來自 data.json），不出現登入按鈕、
        不載入 supabase-js、也不包含任何 Supabase 網址或金鑰。
    AUTH_ENABLED=true → 另外放 Supabase 網址和 publishable key（本來就是公開的）。

    WEB_PUSH_PUBLIC_KEY（選填，登入功能開啟時才放）→ 手機推播用的 VAPID「公鑰」（本來就是公開的）。
        只接受 87 字、B 開頭的 base64url（P-256 未壓縮公鑰）。私鑰是 43 字，設錯會直接失敗。
        私鑰只放在 Environment production-runtime／staging-runtime 的 WEB_PUSH_PRIVATE_KEY。

只接受 sb_publishable_ 開頭的金鑰：不小心把 secret key（sb_secret_…）或舊版 JWT 金鑰
（可能是 service_role）設進來時直接失敗，不會被部署到公開網站。
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from pathlib import Path

_PROJECT_URL_RE = re.compile(r"^https://[a-z0-9]{20}\.supabase\.co$")
_LOCAL_URL_RE = re.compile(r"^http://(127\.0\.0\.1|localhost):\d+$")  # supabase start（本機）
_PUBLISHABLE_RE = re.compile(r"^sb_publishable_[A-Za-z0-9_-]{10,}$")
_VAPID_PUBLIC_RE = re.compile(r"^B[A-Za-z0-9_-]{86}$")  # 65 bytes：0x04 + x + y


class SiteConfigError(ValueError):
    pass


def build_site_config(env: Mapping[str, str] | None = None) -> dict:
    env = os.environ if env is None else env
    if env.get("AUTH_ENABLED", "").strip().lower() != "true":
        return {"authEnabled": False}

    url = env.get("SUPABASE_URL", "").strip().rstrip("/")
    key = env.get("SUPABASE_PUBLISHABLE_KEY", "").strip()
    if not (_PROJECT_URL_RE.match(url) or _LOCAL_URL_RE.match(url)):
        raise SiteConfigError("SUPABASE_URL 必須是 https://<20 碼專案代號>.supabase.co")
    if key.startswith("sb_secret_"):
        raise SiteConfigError("SUPABASE_PUBLISHABLE_KEY 是 secret key！不能放進公開網站")
    if not _PUBLISHABLE_RE.match(key):
        raise SiteConfigError("SUPABASE_PUBLISHABLE_KEY 必須是 sb_publishable_ 開頭的金鑰")
    cfg = {"authEnabled": True, "supabaseUrl": url, "supabaseKey": key}
    vapid = env.get("WEB_PUSH_PUBLIC_KEY", "").strip().rstrip("=")
    if vapid:
        if re.fullmatch(r"[A-Za-z0-9_-]{43}", vapid):
            raise SiteConfigError("WEB_PUSH_PUBLIC_KEY 看起來是私鑰（43 字）！不能放進公開網站")
        if not _VAPID_PUBLIC_RE.match(vapid):
            raise SiteConfigError("WEB_PUSH_PUBLIC_KEY 必須是 87 字、B 開頭的 VAPID 公鑰")
        cfg["webPushPublicKey"] = vapid
    return cfg


def render_site_config(cfg: dict) -> str:
    body = json.dumps(cfg, ensure_ascii=False, sort_keys=True)
    return (
        "// 由 python -m fx_alert site-config 產生（部署時），請勿手動修改。\n"
        f"window.FX_CONFIG = Object.freeze({body});\n"
    )


def write_site_config(out: Path, env: Mapping[str, str] | None = None) -> dict:
    cfg = build_site_config(env)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_site_config(cfg), encoding="utf-8")
    return cfg
