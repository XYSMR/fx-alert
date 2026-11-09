"""前端靜態檔案的安全檢查（不需要瀏覽器）。"""

import re
from pathlib import Path

SITE = Path(__file__).resolve().parents[1] / "site"
INDEX = (SITE / "index.html").read_text(encoding="utf-8")


def _script_srcs(html: str) -> list[str]:
    return re.findall(r'<script[^>]*\ssrc="([^"]+)"', html)


def test_index_never_loads_supabase_js_statically():
    """supabase-js 只有登入功能開啟時才由 account.js 動態載入。"""
    srcs = _script_srcs(INDEX)
    assert not [s for s in srcs if "supabase" in s.lower()]
    assert srcs.index("config.js") < srcs.index("account.js") < srcs.index("app.js")


def test_account_js_loads_supabase_only_inside_start():
    js = (SITE / "account.js").read_text(encoding="utf-8")
    call = 'loadScript("vendor/supabase.umd.js")'
    assert js.count(call) == 1
    assert js.index(call) > js.index("async function start(")


def test_personal_panels_hidden_by_default():
    """不管開關如何，在 JavaScript 決定之前都不會先顯示登入或個人區塊。"""
    for el_id in ("account", "my-rules", "my-alerts", "alerts-panel", "edit-panel"):
        tag = re.search(rf'<[^>]*\sid="{el_id}"[^>]*>', INDEX)
        assert tag and " hidden" in tag.group(0), el_id


def test_csp_only_allows_self_scripts():
    csp = re.search(r'Content-Security-Policy"\s+content="([^"]+)"', INDEX).group(1)
    assert "script-src 'self';" in csp
    assert "unsafe-eval" not in csp


def test_no_secrets_or_config_committed():
    for path in SITE.rglob("*"):
        # vendor（第三方套件本身會檢查 sb_secret_ 字樣）和 tests 例外
        if "vendor" in path.parts or "tests" in path.parts:
            continue
        if path.is_file() and path.suffix in {".js", ".html", ".json", ".css"}:
            assert "sb_secret_" not in path.read_text(encoding="utf-8", errors="ignore"), path
    assert not (SITE / "config.js").exists() or "config.js" in (
        SITE.parent / ".gitignore"
    ).read_text(encoding="utf-8")
