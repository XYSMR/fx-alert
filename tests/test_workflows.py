"""GitHub Actions 設定檢查：多人版切換後的排程、部署、保活。"""

import json
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
    assert "if: ${{ success() && !inputs.dry_run && steps.check.outputs.skipped == 'false' }}" in wf
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


def test_skipped_runs_do_not_ping_success():
    """時段外略過 → skipped=true → 不 ping 成功；沒有 skipped（設定錯誤）也不 ping 成功。"""
    wf = read("check-rates.yml")
    block = wf[
        wf.index("- name: Ping monitor (success)") : wf.index("- name: Ping monitor (failure)")
    ]
    cond = next(line for line in block.splitlines() if line.strip().startswith("if:"))
    assert "steps.check.outputs.skipped == 'false'" in cond  # 必須明確等於 false，空值不算
    assert "skipped != 'true'" not in cond


# ---------- 去重（gate job） ----------


def _jobs():
    import yaml

    return yaml.safe_load(read("check-rates.yml"))["jobs"]


def test_gate_runs_first_and_check_depends_on_it():
    jobs = _jobs()
    assert list(jobs) == ["gate", "check", "deploy"]
    assert jobs["check"]["needs"] == "gate"
    # fail-open：只有明確 run=false 才略過；gate 自己失敗時 run 是空的 → 照常檢查
    assert jobs["check"]["if"] == "${{ !cancelled() && needs.gate.outputs.run != 'false' }}"
    assert jobs["gate"]["outputs"]["run"] == "${{ steps.decide.outputs.run }}"


def test_skipped_check_means_no_deploy_and_no_ping():
    jobs = _jobs()
    # check 被略過 → config_ok 是空的 → deploy 也被略過（不部署）
    assert jobs["deploy"]["needs"] == "check"
    assert "needs.check.outputs.config_ok == 'true'" in jobs["deploy"]["if"]
    # ping 只在 check job 裡；gate job 沒有任何 ping、secret
    gate_text = json.dumps(jobs["gate"], ensure_ascii=False)
    check_names = [s.get("name", "") for s in jobs["check"]["steps"]]
    assert "Ping monitor (success)" in check_names and "Ping monitor (failure)" in check_names
    assert "HEALTHCHECK" not in gate_text and "secrets." not in gate_text


def test_gate_keeps_workflow_green():
    """gate 判斷失敗也不能讓 workflow 變紅：python 失敗時寫 run=true；只讀權限；不裝套件。"""
    jobs = _jobs()
    gate = jobs["gate"]
    assert gate["permissions"] == {"contents": "read"}
    decide = next(s for s in gate["steps"] if s.get("id") == "decide")
    assert "PYTHONPATH=src python3 -m fx_alert.gate" in decide["run"]
    assert '|| echo "run=true" >> "$GITHUB_OUTPUT"' in decide["run"]
    assert not any("pip install" in s.get("run", "") for s in gate["steps"])
    assert not any(s.get("continue-on-error") is False for s in gate["steps"])


def test_source_input_for_pg_cron():
    import yaml

    wf = yaml.safe_load(read("check-rates.yml"))
    inputs = wf.get("on", wf.get(True))["workflow_dispatch"]["inputs"]
    assert inputs["source"] == {
        "description": inputs["source"]["description"],
        "type": "choice",
        "options": ["manual", "pg_cron"],
        "default": "manual",
    }
