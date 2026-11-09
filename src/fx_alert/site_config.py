"""產生前端的 config.js（部署時執行，不進 git）。

    AUTH_ENABLED=false（或沒設定）→ window.FX_CONFIG = {"authEnabled": false};
        頁面維持單人版的顯示（目標價來自 data.json），不出現登入按鈕、
        不載入 supabase-js、也不包含任何 Supabase 網址或金鑰。
    AUTH_ENABLED=true → 另外放 Supabase 網址和 publishable key（本來就是公開的）。

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
    return {"authEnabled": True, "supabaseUrl": url, "supabaseKey": key}


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
