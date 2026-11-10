"""GitHub Actions 設定檢查：多人版切換後的排程、部署、保活。"""

from pathlib import Path

WF = Path(__file__).resolve().parents[1] / ".github/workflows"


def read(name: str) -> str:
    return (WF / name).read_text(encoding="utf-8")


def test_check_rates_runs_multi_user_mode_without_mail_to():
    wf = read("check-rates.yml")
    assert "python -m fx_alert check --mode multi $FORCE $DRY_RUN" in wf
    code = "\n".join(line.split("#")[0] for line in wf.splitlines())
    assert "MAIL_TO" not in code  # 收件人來自 Supabase
    for secret in ("SUPABASE_URL", "SUPABASE_SECRET_KEY", "UNSUBSCRIBE_SECRET", "SMTP_PASSWORD"):
        assert f"${{{{ secrets.{secret} }}}}" in wf, secret
    assert "STAGING_" not in wf


def test_check_rates_checks_for_emails_before_commit():
    wf = read("check-rates.yml")
    assert wf.index("Check data for emails") < wf.index("Commit data to state branch")


def test_deploy_builds_public_data_when_auth_enabled():
    wf = read("deploy-pages.yml")
    assert "build-site --out site/data.json --public" in wf
    assert 'if [ "${AUTH_ENABLED,,}" = "true" ]; then' in wf
    assert "public_problems" in wf  # 部署前再檢查一次
    assert wf.index("Check data.json is public-only") < wf.index("Deploy to GitHub Pages")
    assert "site/privacy.html" in wf


def test_keepalive_uses_only_public_values():
    wf = read("keepalive.yml")
    assert "secrets." not in wf
    assert "${{ vars.SUPABASE_URL }}" in wf and "${{ vars.SUPABASE_PUBLISHABLE_KEY }}" in wf
    assert "/rest/v1/currencies?select=code&limit=1" in wf
    assert "schedule:" in wf and "workflow_dispatch:" in wf


def test_no_workflow_prints_secrets():
    for path in WF.glob("*.yml"):
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            if "echo" in line:
                assert "secrets." not in line, f"{path.name}: {line.strip()}"
                assert "SECRET_KEY" not in line.split("#")[0] or "::error::" in line, (
                    f"{path.name}: {line.strip()}"
                )


def test_check_rates_pings_monitor_from_secret_only():
    wf = read("check-rates.yml")
    assert wf.count("${{ secrets.HEALTHCHECK_PING_URL }}") == 2
    assert "if: ${{ success() && !inputs.dry_run }}" in wf
    assert "if: ${{ failure() && !inputs.dry_run }}" in wf
    assert '"$HC_URL/fail"' in wf
    # ping 放在最後：要等前面的檢查、commit、錯誤判斷都結束
    assert wf.index("Fail if check reported errors") < wf.index("Ping monitor (success)")
    for line in wf.splitlines():
        if "echo" in line:
            assert "HC_URL" not in line, line


def test_no_monitoring_url_committed_anywhere():
    """ping 網址等同密碼，repo 是公開的：任何檔案都不能有 hc-ping.com/<uuid>。"""
    import re

    root = WF.parents[1]
    pattern = re.compile(r"hc-ping\.com/[0-9a-fA-F-]{20,}")
    for path in root.rglob("*"):
        if ".git" in path.parts or "node_modules" in path.parts or not path.is_file():
            continue
        if path.suffix not in {
            ".py",
            ".yml",
            ".yaml",
            ".md",
            ".js",
            ".html",
            ".json",
            ".toml",
            ".ts",
        }:
            continue
        assert not pattern.search(path.read_text(encoding="utf-8", errors="ignore")), path
