"""推播健康報表 supabase/reports/web_push_health.sql：數字正確、只讀、不輸出個人資料。"""

from __future__ import annotations

from pathlib import Path

REPORT = Path(__file__).resolve().parents[2] / "supabase" / "reports" / "web_push_health.sql"
P256DH, AUTH = "B" + "A" * 86, "C" * 22


def report(db) -> dict[str, tuple[str, str]]:
    db.as_postgres()
    db.cur.execute("savepoint report")
    db.cur.execute(REPORT.read_text("utf-8"))
    while db.cur.nextset():
        pass
    rows = db.cur.fetchall()
    db.cur.execute("rollback to savepoint report")
    return {metric: (value, result) for metric, value, _target, result in rows}


def add(db, user, endpoint, *, fail=0, ok_ago=None, created_ago="0", seen_ago="0"):
    db.as_service()
    db.run(
        "insert into public.push_subscriptions "
        "(user_id, endpoint, p256dh, auth, fail_count, last_success_at, created_at, last_seen_at) "
        "values (%s, %s, %s, %s, %s, "
        " case when %s::text is null then null else now() - %s::interval end, "
        " now() - %s::interval, now() - %s::interval)",
        (user, endpoint, P256DH, AUTH, fail, ok_ago, ok_ago, created_ago, seen_ago),
    )


def test_empty(db):
    r = report(db)
    assert r["web_push flag（enabled / rollout）"] == ("off / 0", "INFO")
    assert r["接收推播的裝置"] == ("0", "INFO")
    assert r["失敗中的裝置比例"] == ("—", "NO DATA")
    assert r["每人最多幾台裝置"] == ("0", "PASS")
    assert r["不在白名單的推播網址"] == ("0", "PASS")


def test_numbers_and_threshold(db, alice, bob):
    eps = [
        "https://web.push.apple.com/a1",
        "https://web.push.apple.com/a2",
        "https://fcm.googleapis.com/fcm/send/g1",
        "https://updates.push.services.mozilla.com/wpush/v2/m1",
        "https://wns2-x.notify.windows.com/w/?token=w1",
    ]
    add(db, alice, eps[0], ok_ago="1 hour")
    add(db, alice, eps[1], fail=2, created_ago="5 days", seen_ago="40 days")
    add(db, bob, eps[2], ok_ago="2 days")
    add(db, bob, eps[3])
    add(db, bob, eps[4])
    r = report(db)
    assert r["接收推播的裝置"] == ("5", "INFO")
    assert r["有裝置的使用者"] == ("2", "INFO")
    assert r["裝置（Apple / Google / Mozilla / Windows）"] == ("2 / 1 / 1 / 1", "INFO")
    assert r["最近 24 小時送達過的裝置"] == ("1", "INFO")
    assert r["失敗中的裝置比例"] == ("20.0%（1 台）", "PASS")
    assert r["建立超過 3 天、從沒送達過的裝置"] == ("1", "INFO")
    assert r["超過 30 天沒打開網站的裝置"] == ("1", "INFO")
    assert r["每人最多幾台裝置"] == ("3", "PASS")

    db.as_service()
    db.run("update public.push_subscriptions set fail_count = 1 where endpoint = %s", (eps[2],))
    assert report(db)["失敗中的裝置比例"] == ("40.0%（2 台）", "FAIL")


def test_report_is_read_only_and_has_no_personal_data(db, alice):
    add(db, alice, "https://web.push.apple.com/SECRET-ENDPOINT")
    text = repr(report(db))
    for secret in ("SECRET-ENDPOINT", str(alice), P256DH, AUTH, "@"):
        assert secret not in text
    sql = REPORT.read_text("utf-8")
    assert "set transaction read only;" in sql
    for word in ("insert ", "update ", "delete ", "drop ", "alter "):
        body = "\n".join(line.split("--")[0] for line in sql.splitlines())
        assert word not in body.lower(), word
