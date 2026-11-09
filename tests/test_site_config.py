import pytest

from fx_alert import main as main_mod
from fx_alert.site_config import SiteConfigError, build_site_config, render_site_config

URL = "https://hvfgxplwjpnxizhlatjn.supabase.co"
KEY = "sb_publishable_AbCdEf0123456789"
ON = {"AUTH_ENABLED": "true", "SUPABASE_URL": URL, "SUPABASE_PUBLISHABLE_KEY": KEY}


@pytest.mark.parametrize("value", [None, "", "false", "False", "0", "yes", "1"])
def test_disabled_has_no_supabase_settings_even_if_env_has_them(value):
    env = {**ON}
    if value is None:
        del env["AUTH_ENABLED"]
    else:
        env["AUTH_ENABLED"] = value
    cfg = build_site_config(env)
    assert cfg == {"authEnabled": False}
    text = render_site_config(cfg)
    assert "supabase" not in text.lower()
    assert KEY not in text


def test_enabled():
    cfg = build_site_config({**ON, "AUTH_ENABLED": "TRUE", "SUPABASE_URL": URL + "/"})
    assert cfg == {"authEnabled": True, "supabaseUrl": URL, "supabaseKey": KEY}
    assert render_site_config(cfg).startswith("// 由 python -m fx_alert site-config 產生")


def test_local_supabase_url_allowed():
    cfg = build_site_config({**ON, "SUPABASE_URL": "http://127.0.0.1:54321"})
    assert cfg["supabaseUrl"] == "http://127.0.0.1:54321"


@pytest.mark.parametrize(
    ("over", "msg"),
    [
        ({"SUPABASE_PUBLISHABLE_KEY": "sb_secret_AbCdEf0123456789"}, "secret key"),
        ({"SUPABASE_PUBLISHABLE_KEY": "eyJhbGciOiJIUzI1NiJ9.e30.x"}, "sb_publishable_"),
        ({"SUPABASE_PUBLISHABLE_KEY": ""}, "sb_publishable_"),
        ({"SUPABASE_URL": "https://evil.example.com"}, "SUPABASE_URL"),
        ({"SUPABASE_URL": ""}, "SUPABASE_URL"),
    ],
)
def test_enabled_rejects_bad_values(over, msg):
    with pytest.raises(SiteConfigError, match=msg):
        build_site_config({**ON, **over})


def test_cli_writes_disabled_config(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("AUTH_ENABLED", raising=False)
    monkeypatch.setenv("SUPABASE_URL", URL)
    out = tmp_path / "config.js"
    assert main_mod.main(["site-config", "--out", str(out)]) == 0
    assert out.read_text(encoding="utf-8").endswith(
        'window.FX_CONFIG = Object.freeze({"authEnabled": false});\n'
    )
    assert "關閉" in capsys.readouterr().out


def test_cli_refuses_secret_key(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("SUPABASE_URL", URL)
    monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY", "sb_secret_AbCdEf0123456789")
    out = tmp_path / "config.js"
    assert main_mod.main(["site-config", "--out", str(out)]) == 1
    assert not out.exists()
