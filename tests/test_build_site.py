import json
from datetime import datetime, time, timedelta
from decimal import Decimal as D

import pytest

from fx_alert import main as main_mod
from fx_alert.build_site import build_data, distance_pct, write_data_json
from fx_alert.config import TAIPEI, AppConfig, BusinessHours
from fx_alert.history import append_alert, append_quotes, write_last_check
from fx_alert.rules import AlertState, Rule
from fx_alert.state_store import CurrencyState, save_states

NOW = datetime(2026, 11, 9, 16, 12, tzinfo=TAIPEI)


@pytest.fixture
def cfg(tmp_path):
    return AppConfig(
        data_dir=tmp_path / "data",
        hours=BusinessHours(True, time(9, 0), time(16, 30)),
        rules=(
            Rule("JPY", target=D("0.205"), buffer=D("0.001")),
            Rule("KRW", target=D("0.0250"), buffer=D("0.0002")),
        ),
    )


def seed(cfg):
    d = cfg.data_dir
    append_quotes(
        d, datetime(2026, 10, 1, 9, 5), {"JPY": D("0.2200"), "KRW": D("0.0270")}
    )  # >30 天
    append_quotes(d, datetime(2026, 10, 20, 9, 5), {"JPY": D("0.2150"), "KRW": D("0.0266")})
    append_quotes(d, datetime(2026, 11, 9, 16, 10, 12), {"JPY": D("0.2049"), "KRW": D("0.0263")})
    save_states(
        d / "state.json",
        {
            "JPY": CurrencyState(
                AlertState.TRIGGERED, "2026-11-09T16:12:10+08:00", "0.2049", "0.205"
            ),
            "KRW": CurrencyState(AlertState.ARMED, "2026-11-09T15:46:16+08:00", "0.0264"),
        },
    )
    append_alert(
        d, datetime(2026, 11, 9, 16, 12, 10, tzinfo=TAIPEI), "JPY", D("0.2049"), D("0.205")
    )
    write_last_check(d, datetime(2026, 11, 9, 16, 12, 10, tzinfo=TAIPEI), ok=True, errors=0)


# ---------- 距離目標 ----------


@pytest.mark.parametrize(
    ("rate", "target", "expected"),
    [
        ("0.2120", "0.205", 3.41),  # 還要跌 3.41%
        ("0.2050", "0.205", 0.0),
        ("0.2040", "0.205", -0.49),  # 已低於目標
        ("0.0263", "0.0250", 5.2),
        ("0.02503", "0.0250", 0.12),  # 0.12 四捨五入
    ],
)
def test_distance_pct(rate, target, expected):
    assert distance_pct(D(rate), D(target)) == expected


# ---------- 整份 data.json ----------


def test_build_data_full(cfg):
    seed(cfg)
    data = build_data(cfg, cfg.data_dir, now=NOW, repo="XYSMR/fx-alert")

    assert data["schema"] == 1
    assert data["generated_at"] == "2026-11-09T16:12:00+08:00"
    assert data["quoted_at"] == "2026-11-09T16:10:12+08:00"
    assert data["last_check"] == {"at": "2026-11-09T16:12:10+08:00", "ok": True, "errors": 0}
    assert data["edit_config_url"] == "https://github.com/XYSMR/fx-alert/edit/main/config.yaml"

    jpy, krw = data["currencies"]
    assert jpy == {
        "code": "JPY",
        "name": "日圓",
        "rate": "0.2049",
        "rate_at": "2026-11-09T16:10:12+08:00",
        "target": "0.205",
        "buffer": "0.001",
        "rearm_above": "0.206",
        "distance_pct": -0.05,
        "status": "triggered",
        "status_since": "2026-11-09T16:12:10+08:00",
        "history": [
            ["2026-10-20T09:05:00+08:00", "0.2150"],
            ["2026-11-09T16:10:12+08:00", "0.2049"],
        ],
    }
    assert krw["status"] == "waiting" and krw["distance_pct"] == 5.2
    assert len(krw["history"]) == 2  # 10/01 超過 30 天，被排除

    assert data["alerts"] == [
        {
            "sent_at": "2026-11-09T16:12:10+08:00",
            "currency": "JPY",
            "rate": "0.2049",
            "target": "0.205",
        }
    ]


def test_currency_order_follows_config(cfg):
    seed(cfg)
    assert [c["code"] for c in build_data(cfg, cfg.data_dir, now=NOW)["currencies"]] == [
        "JPY",
        "KRW",
    ]


def test_target_comes_from_current_config_not_state(cfg):
    # 使用者改了目標價：頁面要立刻顯示新目標（狀態則等下次排程才重新判斷）
    seed(cfg)
    from dataclasses import replace

    lowered = replace(cfg, rules=(Rule("JPY", D("0.203"), D("0.001")), cfg.rules[1]))
    jpy = build_data(lowered, cfg.data_dir, now=NOW)["currencies"][0]
    assert jpy["target"] == "0.203" and jpy["distance_pct"] == 0.94


def test_empty_data_dir_first_deploy(cfg):
    data = build_data(cfg, cfg.data_dir, now=NOW)
    assert data["last_check"] is None and data["quoted_at"] is None and data["alerts"] == []
    for c in data["currencies"]:
        assert c["rate"] is None and c["distance_pct"] is None
        assert c["status"] == "waiting" and c["history"] == []


def test_alerts_limited_and_newest_first(cfg):
    for i in range(25):
        at = NOW - timedelta(days=i)
        append_alert(cfg.data_dir, at, "JPY", D("0.2049"), D("0.205"))
    alerts = build_data(cfg, cfg.data_dir, now=NOW)["alerts"]
    assert len(alerts) == 20
    assert alerts[0]["sent_at"] > alerts[-1]["sent_at"]


# ---------- 不能外洩 ----------


def test_output_never_contains_secrets(cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("SMTP_USER", "secret.user@gmail.com")
    monkeypatch.setenv("SMTP_PASSWORD", "abcdefghijklmnop")
    monkeypatch.setenv("MAIL_TO", "someone@example.com")
    seed(cfg)
    out = tmp_path / "site" / "data.json"
    write_data_json(build_data(cfg, cfg.data_dir, now=NOW), out)
    text = out.read_text(encoding="utf-8")
    for bad in ("@", "SMTP", "abcdefghijklmnop", "secret.user", "someone"):
        assert bad not in text


def test_repo_from_github_env(cfg, monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "someone/fork")
    assert build_data(cfg, cfg.data_dir, now=NOW)["repo_url"] == "https://github.com/someone/fork"
    monkeypatch.setenv("GITHUB_REPOSITORY", "not a repo")
    assert build_data(cfg, cfg.data_dir, now=NOW)["repo_url"] == "https://github.com/XYSMR/fx-alert"


def test_written_file_is_valid_utf8_json_with_lf(cfg, tmp_path):
    seed(cfg)
    out = tmp_path / "site" / "data.json"
    write_data_json(build_data(cfg, cfg.data_dir, now=NOW), out)
    raw = out.read_bytes()
    assert b"\r\n" not in raw
    assert json.loads(raw.decode("utf-8"))["currencies"][0]["name"] == "日圓"


# ---------- CLI ----------


def test_cli_build_site(cfg, tmp_path, monkeypatch, capsys):
    seed(cfg)
    conf = tmp_path / "config.yaml"
    conf.write_text(
        "data_dir: data\ncurrencies:\n  JPY:\n    target: '0.205'\n    buffer: '0.001'\n"
        "  KRW:\n    target: '0.0250'\n    buffer: '0.0002'\n",
        encoding="utf-8",
    )
    out = tmp_path / "site" / "data.json"
    assert main_mod.main(["build-site", "--config", str(conf), "--out", str(out)]) == 0
    assert "JPY 0.2049" in capsys.readouterr().out
    assert json.loads(out.read_text(encoding="utf-8"))["currencies"][1]["code"] == "KRW"


def test_cli_build_site_with_corrupt_state(cfg, tmp_path, capsys):
    cfg.data_dir.mkdir(parents=True)
    (cfg.data_dir / "state.json").write_text("{bad", encoding="utf-8")
    conf = tmp_path / "config.yaml"
    conf.write_text("currencies:\n  JPY:\n    target: '0.205'\n", encoding="utf-8")
    out = tmp_path / "site" / "data.json"
    assert main_mod.main(["build-site", "--config", str(conf), "--out", str(out)]) == 1
    assert not out.exists()
    assert "失敗" in capsys.readouterr().err
