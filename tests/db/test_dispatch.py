"""pg_cron 外部觸發（private.dispatch_check_rates）：送出的請求內容、權限、排程。

每個測試都在 rollback 的交易裡：pg_net 只會處理已 commit 的請求，所以測試不會真的呼叫 GitHub。
"""

from __future__ import annotations

import json

import pytest

TOKEN = "github_pat_TESTONLY_not_a_real_token"
API = "https://api.github.com/repos/XYSMR/fx-alert/actions/workflows/"


def configure(db, workflow: str | None = "check-rates.yml", token: str | None = TOKEN) -> None:
    db.as_postgres()
    if token is not None:
        db.run("select vault.create_secret(%s, 'github_dispatch_token')", (token,))
    if workflow is not None:
        db.run("select vault.create_secret(%s, 'github_dispatch_workflow')", (workflow,))


def queued(db, request_id: int) -> dict:
    url, headers, body = db.one(
        "select url, headers, convert_from(body, 'utf8') from net.http_request_queue where id = %s",
        (request_id,),
    )
    return {"url": url, "headers": headers, "body": json.loads(body)}


def dispatch(db):
    db.as_postgres()
    return db.one("select private.dispatch_check_rates()")


# ---------- 送出的請求 ----------


def test_check_rates_dispatch_sends_force_false(db):
    """check-rates 的 force 預設是 true，pg_cron 觸發一定要明確送 "false"，才會檢查時段。"""
    configure(db, "check-rates.yml")
    req = queued(db, dispatch(db))
    assert req["url"] == API + "check-rates.yml/dispatches"
    assert req["body"] == {"ref": "main", "inputs": {"force": "false", "dry_run": "false"}}
    assert req["body"]["inputs"]["force"] == "false"  # 字串，不是省略、也不是 true


def test_staging_dispatch_is_read_only(db):
    configure(db, "staging-check.yml")
    req = queued(db, dispatch(db))
    assert req["url"] == API + "staging-check.yml/dispatches"
    assert req["body"] == {"ref": "main", "inputs": {"send": "false"}}


def test_token_only_in_authorization_header(db):
    configure(db, " check-rates.yml\n", token=f"  {TOKEN}\n")  # 從後台貼上時多了空白也沒關係
    req = queued(db, dispatch(db))
    assert req["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert req["headers"]["Accept"] == "application/vnd.github+json"
    assert req["headers"]["X-GitHub-Api-Version"] == "2022-11-28"
    assert TOKEN not in req["url"] and TOKEN not in json.dumps(req["body"])


@pytest.mark.parametrize(
    ("workflow", "token"),
    [(None, None), ("check-rates.yml", None), (None, TOKEN), ("check-rates.yml", "")],
)
def test_not_configured_does_nothing(db, workflow, token):
    configure(db, workflow, token)
    before = db.one("select count(*) from net.http_request_queue")
    assert dispatch(db) is None
    assert db.one("select count(*) from net.http_request_queue") == before


@pytest.mark.parametrize(
    "workflow", ["deploy-pages.yml", "ci.yml", "../../evil", "check-rates.yml/../ci.yml"]
)
def test_only_whitelisted_workflows(db, workflow):
    configure(db, workflow)
    db.fails("select private.dispatch_check_rates()", sqlstate="22023")


# ---------- 權限 ----------


@pytest.mark.parametrize("who", ["anon", "user", "service"])
@pytest.mark.parametrize(
    "sql", ["select private.dispatch_check_rates()", "select private.dispatch_inputs('ci.yml')"]
)
def test_api_roles_cannot_call_dispatch(db, alice, who, sql):
    configure(db)
    {"anon": db.as_anon, "user": lambda: db.as_user(alice), "service": db.as_service}[who]()
    db.fails(sql, sqlstate="42501")  # permission denied


# ---------- 排程 ----------


def test_cron_jobs_use_utc_schedule_and_no_secret(db):
    db.as_postgres()
    jobs = dict(
        db.rows(
            "select schedule, command from cron.job where jobname like 'fx-alert-dispatch%%' "
            "order by jobname"
        )
    )
    # UTC：台灣平日 09:05–15:35 每 30 分鐘，以及 16:05
    assert jobs == {
        "5,35 1-7 * * 1-5": "select private.dispatch_check_rates()",
        "5 8 * * 1-5": "select private.dispatch_check_rates()",
    }
    for command in jobs.values():
        assert "Bearer" not in command and "github_pat" not in command
