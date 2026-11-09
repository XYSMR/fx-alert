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


def test_both_functions_share_the_same_keys_module():
    """後台部署只帶同一個資料夾的檔案，所以 keys.ts 各放一份；兩份必須完全相同。"""
    a = (SUPABASE / "functions/unsubscribe/keys.ts").read_bytes()
    b = (SUPABASE / "functions/delete-account/keys.ts").read_bytes()
    assert a == b


def test_functions_do_not_use_legacy_jwt_keys():
    """舊版 SUPABASE_ANON_KEY / SUPABASE_SERVICE_ROLE_KEY 會淘汰：只能在註解裡提到。"""
    for path in sorted((SUPABASE / "functions").rglob("*.ts")):
        if path.name.endswith(".test.ts"):
            continue
        for lineno, line in enumerate(path.read_text("utf-8").splitlines(), 1):
            code = line.split("//", 1)[0]
            for legacy in ("SUPABASE_ANON_KEY", "SUPABASE_SERVICE_ROLE_KEY", "Bearer ${SECRET"):
                assert legacy not in code, f"{path.name}:{lineno} 還在用 {legacy}"


def test_secret_key_only_goes_in_apikey_header():
    """新版 secret key 不是 JWT：不能放進 Authorization。"""
    for name in ("unsubscribe", "delete-account"):
        src = (SUPABASE / "functions" / name / "index.ts").read_text("utf-8")
        assert "secretKey(env)" in src
        assert "Bearer ${SECRET_KEY" not in src
        assert "Authorization: `Bearer ${SECRET" not in src
