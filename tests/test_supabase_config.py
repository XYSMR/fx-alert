"""supabase/ 底下的設定檢查。"""

import json
import tomllib
from pathlib import Path

SUPABASE = Path(__file__).resolve().parents[1] / "supabase"


def test_every_function_disables_platform_jwt_and_verifies_in_code():
    """每個 Edge Function 都要明確設定 verify_jwt = false，並在程式裡自己驗證身分。"""
    cfg = tomllib.loads((SUPABASE / "config.toml").read_text(encoding="utf-8"))
    functions = sorted(p.name for p in (SUPABASE / "functions").iterdir() if p.is_dir())
    assert functions == ["delete-account", "unsubscribe"]
    for name in functions:
        assert cfg["functions"][name]["verify_jwt"] is False, name
    handler = {n: (SUPABASE / "functions" / n / "handler.ts").read_text("utf-8") for n in functions}
    assert "verify(deps.secret" in handler["unsubscribe"]  # HMAC 簽章
    assert "deps.getUser(token)" in handler["delete-account"]  # Supabase Auth
    assert "MAX_AUTH_AGE_SECONDS" in handler["delete-account"]  # 最近登入


def test_confirm_cases_file_is_valid():
    data = json.loads((SUPABASE / "functions/delete-account/confirm_cases.json").read_text("utf-8"))
    assert data["text"] == "刪除我的帳號"
    assert {c["ok"] for c in data["cases"]} == {True, False}
