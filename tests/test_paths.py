"""workflow 用 pip install .（非 editable）時，也要找得到 repo 裡的 seed/。"""

import pytest

from fx_alert import paths


def test_prefers_env_then_cwd(tmp_path, monkeypatch):
    rel = "seed/x.csv"
    (tmp_path / "seed").mkdir()
    (tmp_path / rel).write_text("a", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert paths.repo_file(rel) == tmp_path / rel
    other = tmp_path / "other"
    (other / "seed").mkdir(parents=True)
    (other / rel).write_text("b", encoding="utf-8")
    monkeypatch.setenv("FX_ALERT_ROOT", str(other))
    assert paths.repo_file(rel) == other / rel


def test_missing_falls_back_to_package_root(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FX_ALERT_ROOT", raising=False)
    assert paths.repo_file("seed/nope.csv") == paths.PACKAGE_ROOT / "seed/nope.csv"


def test_read_seed_fails_loudly_when_missing(tmp_path):
    from fx_alert.daily import read_seed

    with pytest.raises(FileNotFoundError):
        read_seed(tmp_path / "missing.csv")
