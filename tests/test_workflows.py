"""GitHub Actions 設定檢查：多人版切換後的排程、部署、保活。"""

import json
import re
from pathlib import Path

import yaml

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


def test_pages_deploy_retries_transient_github_errors():
    """GitHub 偶爾讀不到剛上傳的網站檔案：最多部署 3 次。
    前兩次失敗不讓 job 失敗，第 3 次失敗才算失敗。"""
    job = yaml.safe_load(read("deploy-pages.yml"))["jobs"]["deploy"]
    steps = job["steps"]
    deploys = [s for s in steps if str(s.get("uses", "")).startswith("actions/deploy-pages@")]
    assert [s["id"] for s in deploys] == ["deploy1", "deploy2", "deploy3"]
    assert deploys[0].get("continue-on-error") is True and "if" not in deploys[0]
    assert deploys[1].get("continue-on-error") is True
    assert deploys[1]["if"] == "steps.deploy1.outcome == 'failure'"
    assert "continue-on-error" not in deploys[2]  # 最後一次失敗 → job 失敗（不會默默沒部署）
    assert deploys[2]["if"] == (
        "steps.deploy1.outcome == 'failure' && steps.deploy2.outcome == 'failure'"
    )
    # 重試之間有等待；每次最多 2 分鐘，加起來不超過 job 的時間上限
    waits = [s for s in steps if "sleep" in s.get("run", "")]
    assert len(waits) == 2
    total = sum(d["with"]["timeout"] for d in deploys) / 60000 + (30 + 90) / 60
    assert total < job["timeout-minutes"] - 2  # 留時間給前面的步驟
    # 只有一個上傳：三次都部署同一份檔案
    upload = "actions/upload-pages-artifact@"
    uploads = [s for s in steps if str(s.get("uses", "")).startswith(upload)]
    assert len(uploads) == 1
    url = job["environment"]["url"]
    assert all(f"steps.{i}.outputs.page_url" in url for i in ("deploy1", "deploy2", "deploy3"))


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


# ---------- deploy-migrations：staging → 正式專案 ----------


def _deploy_jobs(wf: str) -> dict:
    return yaml.safe_load(wf)["jobs"]


def test_deploy_migrations_staging_first_then_production_with_approval():
    wf = read("deploy-migrations.yml")
    jobs = _deploy_jobs(wf)
    assert list(jobs) == ["staging", "production"]
    assert jobs["staging"]["environment"] == "staging"  # 不需核准
    assert jobs["production"]["environment"] == "production"  # 必要審核者在 GitHub Environment 設定
    # 正式專案：staging 成功才會執行（if 沒有 always()/failure() → 預設 success()）
    assert jobs["production"]["needs"] == "staging"
    cond = jobs["production"]["if"]
    assert "github.ref == 'refs/heads/main'" in cond
    for fn in ("always()", "failure()", "cancelled()", "!cancelled()"):
        assert fn not in cond
    assert "cancel-in-progress: false" in wf  # 不能中途取消一半的部署
    assert "permissions:\n  contents: read" in wf


def test_deploy_migrations_manual_target_choice():
    on = yaml.safe_load(read("deploy-migrations.yml"))[True]  # YAML 的 on 會被讀成 True
    target = on["workflow_dispatch"]["inputs"]["target"]
    assert target["type"] == "choice" and target["options"] == ["staging", "production"]
    assert target["default"] == "staging"  # 預設不會碰正式專案


def test_deploy_migrations_checks_before_writing():
    """每個 job：比對 → 事前檢查 → 補紀錄＋套用 → 確認；寫入的 apply 一定在事前檢查之後。"""
    for name, job in _deploy_jobs(read("deploy-migrations.yml")).items():
        runs = [s.get("run", "") for s in job["steps"]]
        order = [next(i for i, r in enumerate(runs) if f"migrations_deploy {cmd}" in r)
                 for cmd in ("plan", "precheck", "apply", "verify")]  # fmt: skip
        assert order == sorted(order), name
        # 要補紀錄（只有 repair）時也要先跑檢查，確認物件存在
        cond = "steps.plan.outputs.repair != '' || steps.plan.outputs.pending != ''"
        assert sum(s.get("if") == cond for s in job["steps"]) == 2, name


def test_deploy_migrations_each_job_uses_its_own_db_and_target():
    wf = read("deploy-migrations.yml")
    jobs = _deploy_jobs(wf)
    for name, secret in (("staging", "STAGING_DB_URL"), ("production", "PRODUCTION_DB_URL")):
        steps = [s for s in jobs[name]["steps"] if "DB_URL" in s.get("env", {})]
        assert len(steps) == 4, name
        for step in steps:
            assert step["env"]["DB_URL"] == f"${{{{ secrets.{secret} }}}}", name
            assert step["env"]["FX_DEPLOY_TARGET"] == name
        assert "env" not in jobs[name]  # 不放在 job 層級：安裝套件的步驟拿不到
    assert "SUPABASE_DB_URL" not in wf  # 連線字串不能叫這個名字
    for line in wf.splitlines():
        assert not re.search(r"echo .*DB_URL", line), line


def test_deploy_migrations_writes_with_python_not_cli():
    """寫入用 psycopg（CLI 3.0.0 解析不了 pooler 連線字串）；apply 要拿到第 1 步的計畫。"""
    for name, job in _deploy_jobs(read("deploy-migrations.yml")).items():
        runs = " ".join(s.get("run", "") for s in job["steps"])
        assert "npx" not in runs and "supabase@" not in runs
        (apply,) = [s for s in job["steps"] if "migrations_deploy apply" in s.get("run", "")]
        assert apply["env"]["EXPECT_REPAIR"] == "${{ steps.plan.outputs.repair }}", name
        assert apply["env"]["EXPECT_PENDING"] == "${{ steps.plan.outputs.pending }}", name


def test_deploy_migrations_triggers_only_on_migration_changes():
    """只有 supabase/migrations/** 或 supabase/prechecks/** 有變動、或手動執行才觸發；
    改前端、文件、其他程式、物件檢查（baseline_checks）都不會。"""
    wf = read("deploy-migrations.yml")
    on = wf.split("\non:\n", 1)[1].split("\npermissions:", 1)[0]
    triggers = re.findall(r"^  (\w+):", on, re.M)
    assert triggers == ["push", "workflow_dispatch"]
    paths = re.findall(r'^      - "(.+)"$', on, re.M)
    assert paths == ["supabase/migrations/**", "supabase/prechecks/**"]
    assert "paths-ignore" not in on and "branches: [main]" in on


# ---------- 只有資料有變才 commit／部署；gate 先讀 Supabase ----------


def test_commit_and_deploy_only_when_data_changed():
    wf = read("check-rates.yml")
    commit = wf[wf.index("- name: Commit data to state branch") :]
    commit = commit[: commit.index("- name: Fail if check reported errors")]
    assert "python3 -m fx_alert.publish" in commit
    assert 'echo "published=true"' in commit and 'echo "published=false"' in commit
    # 判斷失敗時照常 commit（寧可多部署一次）
    assert "|| echo true" in commit
    assert "published: ${{ steps.commit.outputs.published }}" in wf
    deploy = wf[wf.index("\n  deploy:") :]
    assert "needs.check.outputs.published == 'true'" in deploy


def test_gate_reads_check_status_with_public_values_only():
    wf = read("check-rates.yml")
    gate = wf[wf.index("\n  gate:") : wf.index("\n  check:")]
    assert "SUPABASE_URL: ${{ vars.SUPABASE_URL }}" in gate
    assert "SUPABASE_PUBLISHABLE_KEY: ${{ vars.SUPABASE_PUBLISHABLE_KEY }}" in gate
    assert "secrets." not in gate  # gate 不需要任何 secret


# 這些 action 的舊版用 Node.js 20（GitHub 已停用，會被強制改用 Node 24 並出現警告）。
# 下面是改用 Node 24 的最低版本；新版也可以，舊版不行。
NODE24_MIN = {
    "actions/checkout": 5,
    "actions/setup-python": 6,
    "actions/setup-node": 5,
    "actions/configure-pages": 6,
    "actions/deploy-pages": 5,
    "actions/upload-pages-artifact": 4,
}


def test_actions_use_node24_versions():
    found = []
    for path in WF.glob("*.yml"):
        for name, major in re.findall(r"uses:\s*([\w-]+/[\w-]+)@v(\d+)", path.read_text("utf-8")):
            if name in NODE24_MIN:
                found.append(name)
                assert int(major) >= NODE24_MIN[name], f"{path.name}: {name}@v{major} 還是 Node 20"
    assert set(found) == set(NODE24_MIN)  # 每個都有檢查到（避免改名後測試默默失效）


def test_jobs_with_secrets_are_bound_to_an_environment():
    """協作者有寫入權限（個人 repo 沒有 Triage）：推自己的 branch 就能跑改過的 workflow。
    用到 secrets 的 job 一律綁 Environment，金鑰只放在 Environment，不放 repository secrets。"""
    expected = {
        ("check-rates.yml", "check"): "production-runtime",
        ("staging-check.yml", "check"): "staging-runtime",
        ("deploy-migrations.yml", "staging"): "staging",
        ("deploy-migrations.yml", "production"): "production",
    }
    found = {}
    for path in WF.glob("*.yml"):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        for name, job in doc.get("jobs", {}).items():
            if not re.search(r"secrets\.(?!GITHUB_TOKEN)", json.dumps(job)):
                continue
            env = job.get("environment")
            found[(path.name, name)] = env["name"] if isinstance(env, dict) else env
    assert found == expected


def _assembled_paths() -> set[str]:
    """deploy-pages 的 Assemble site 會複製哪些 site/ 底下的檔案或資料夾"""
    steps = yaml.safe_load(read("deploy-pages.yml"))["jobs"]["deploy"]["steps"]
    run = next(s["run"] for s in steps if s.get("name") == "Assemble site")
    return set(re.findall(r"site/([\w./-]+)", run))


def _is_assembled(ref: str, assembled: set[str]) -> bool:
    return any(ref == a or ref.startswith(a.rstrip("/") + "/") for a in assembled)


def test_every_local_file_the_pages_use_is_deployed():
    """網頁引用的本站檔案都要在 Assemble site 裡，否則正式網站會 404
    （之前 trips.js 就漏過）。config.js 由部署另外產生。"""
    site = WF.parents[1] / "site"
    assembled = _assembled_paths()
    generated = {"config.js"}
    for page in ("index.html", "privacy.html", "unsubscribe.html"):
        assert page in assembled, page
        html = (site / page).read_text(encoding="utf-8")
        for ref in re.findall(r'(?:src|href)="([^"#:]+)"', html):
            if ref in generated or ref.endswith("/"):
                continue  # 連到首頁（./）之類的頁面連結
            assert (site / ref).exists(), f"{page} 引用了不存在的 {ref}"
            assert _is_assembled(ref, assembled), f"{page} 引用的 {ref} 沒有部署"


def test_pwa_files_are_deployed_at_the_site_root():
    """sw.js 必須放在網站根目錄，scope 才能涵蓋整個網站；manifest 的圖示也要部署"""
    assembled = _assembled_paths()
    for f in ("sw.js", "pwa.js", "manifest.webmanifest"):
        assert f in assembled, f
    site = WF.parents[1] / "site"
    manifest = json.loads((site / "manifest.webmanifest").read_text(encoding="utf-8"))
    for icon in manifest["icons"]:
        assert (site / icon["src"]).exists(), icon["src"]
        assert _is_assembled(icon["src"], assembled), icon["src"]


def test_push_private_key_comes_from_the_right_environment():
    """正式排程用 production-runtime 的 WEB_PUSH_PRIVATE_KEY；staging 用自己的
    STAGING_WEB_PUSH_PRIVATE_KEY（兩組金鑰不同，不能互用）。私鑰不能出現在網站部署。"""
    prod = read("check-rates.yml")
    assert "WEB_PUSH_PRIVATE_KEY: ${{ secrets.WEB_PUSH_PRIVATE_KEY }}" in prod
    staging = read("staging-check.yml")
    assert staging.count("WEB_PUSH_PRIVATE_KEY: ${{ secrets.STAGING_WEB_PUSH_PRIVATE_KEY }}") == 2
    assert "secrets.WEB_PUSH_PRIVATE_KEY" not in staging
    assert "WEB_PUSH_PRIVATE_KEY" not in read("deploy-pages.yml")
