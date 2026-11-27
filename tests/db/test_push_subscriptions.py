"""手機推播訂閱（20261128021121_push_subscriptions）：白名單、flag、RLS、裝置上限。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

DENIED = "42501"
CHECK = "23514"

CASES = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "push_endpoints.json").read_text("utf-8")
)
APPLE = CASES["allowed"][0]
P256DH = "B" + "A" * 86  # 65 bytes 的 base64url（87 字）
AUTH = "C" * 22  # 16 bytes 的 base64url（22 字）

SAVE = "select public.save_push_subscription(%s, %s, %s, %s)"


def set_web_push(db, *, enabled: bool, rollout: int = 100) -> None:
    db.as_postgres()
    db.run(
        "update public.feature_flags set enabled = %s, rollout = %s where flag = 'web_push'",
        (enabled, rollout),
    )


def save(db, user, endpoint=APPLE, *, label=None, p256dh=P256DH, auth=AUTH) -> int:
    db.as_user(user)
    return db.one(SAVE, (endpoint, p256dh, auth, label))


def endpoint(n: int) -> str:
    return f"https://web.push.apple.com/device-{n}"


# ---------------------------------------------------------------------------
# 白名單
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("url", CASES["allowed"])
def test_allowed_push_services(db, url):
    db.as_postgres()
    assert db.one("select public.is_allowed_push_endpoint(%s)", (url,)) is True


@pytest.mark.parametrize("reason", sorted(CASES["denied"]))
def test_denied_endpoints(db, reason):
    db.as_postgres()
    url = CASES["denied"][reason]
    assert db.one("select public.is_allowed_push_endpoint(%s)", (url,)) is False, reason


def test_too_long_and_null_endpoints_are_denied(db):
    db.as_postgres()
    ok = "https://web.push.apple.com/" + "A" * (2048 - 27)
    assert db.one("select public.is_allowed_push_endpoint(%s)", (ok,)) is True
    assert db.one("select public.is_allowed_push_endpoint(%s)", (ok + "A",)) is False
    assert db.one("select public.is_allowed_push_endpoint(null)") is False


def test_denied_endpoint_cannot_be_saved_even_by_service_role(db, alice):
    """檢查限制在資料表上：繞過 save_push_subscription() 直接寫也擋得住"""
    db.as_service()
    for url in CASES["denied"].values():
        db.fails(
            "insert into public.push_subscriptions (user_id, endpoint, p256dh, auth) "
            "values (%s, %s, %s, %s)",
            (alice, url, P256DH, AUTH),
            sqlstate=CHECK,
        )


def test_attacker_lookalike_is_rejected_through_the_api(db, alice):
    set_web_push(db, enabled=True)
    db.as_user(alice)
    db.fails(
        SAVE,
        ("https://evil.push.apple.com.attacker.com/x", P256DH, AUTH, None),
        sqlstate=CHECK,
    )


# ---------------------------------------------------------------------------
# web_push flag
# ---------------------------------------------------------------------------


def test_web_push_is_off_for_everyone_by_default(db, alice):
    db.as_anon()
    assert db.rows(
        "select enabled, rollout, allowed_roles from public.feature_flags where flag = 'web_push'"
    ) == [(False, 0, ["admin", "beta"])]
    db.as_postgres()
    db.run("update public.profiles set role = 'admin' where user_id = %s", (alice,))
    db.as_user(alice)
    assert db.one("select public.is_feature_on('web_push')") is False
    db.fails(SAVE, (APPLE, P256DH, AUTH, None), sqlstate=DENIED)


def test_flag_on_user_can_subscribe(db, alice):
    set_web_push(db, enabled=True)
    sid = save(db, alice, label="我的 iPhone")
    assert db.rows("select id, endpoint, label, fail_count from public.push_subscriptions") == [
        (sid, APPLE, "我的 iPhone", 0)
    ]


def test_beta_only_rollout(db, alice, bob):
    set_web_push(db, enabled=True, rollout=0)
    db.as_postgres()
    db.run("update public.profiles set role = 'beta' where user_id = %s", (bob,))
    db.as_user(alice)
    db.fails(SAVE, (endpoint(1), P256DH, AUTH, None), sqlstate=DENIED)
    assert save(db, bob, endpoint(2)) > 0


def test_anon_cannot_subscribe(db):
    set_web_push(db, enabled=True)
    db.as_anon()
    db.fails(SAVE, (APPLE, P256DH, AUTH, None), sqlstate=DENIED)


def test_flag_off_keeps_existing_devices_and_allows_delete(db, alice):
    """關掉功能不刪使用者的資料；可以看、可以刪，但不能新增"""
    set_web_push(db, enabled=True)
    save(db, alice, endpoint(1))
    set_web_push(db, enabled=False)
    db.as_user(alice)
    assert db.one("select count(*) from public.push_subscriptions") == 1
    db.fails(SAVE, (endpoint(2), P256DH, AUTH, None), sqlstate=DENIED)
    assert db.run("delete from public.push_subscriptions") == 1


# ---------------------------------------------------------------------------
# RLS 與欄位權限
# ---------------------------------------------------------------------------


def test_users_cannot_insert_directly(db, alice):
    """新增只能透過 save_push_subscription()（它檢查 flag）"""
    set_web_push(db, enabled=True)
    db.as_user(alice)
    db.fails(
        "insert into public.push_subscriptions (user_id, endpoint, p256dh, auth) "
        "values (%s, %s, %s, %s)",
        (alice, APPLE, P256DH, AUTH),
        sqlstate=DENIED,
    )


def test_users_cannot_read_keys(db, alice):
    set_web_push(db, enabled=True)
    save(db, alice)
    db.as_user(alice)
    for col in ("p256dh", "auth", "user_id", "*"):
        db.fails(f"select {col} from public.push_subscriptions", sqlstate=DENIED)


def test_users_only_see_and_change_their_own_devices(db, alice, bob):
    set_web_push(db, enabled=True)
    a = save(db, alice, endpoint(1))
    b = save(db, bob, endpoint(2))
    db.as_user(alice)
    assert db.rows("select id from public.push_subscriptions") == [(a,)]
    assert db.run("update public.push_subscriptions set label = 'x' where id = %s", (b,)) == 0
    assert db.run("delete from public.push_subscriptions where id = %s", (b,)) == 0
    assert (
        db.run("update public.push_subscriptions set label = '客廳平板' where id = %s", (a,)) == 1
    )
    for col, value in (("fail_count", 0), ("last_success_at", None), ("endpoint", endpoint(3))):
        db.fails(
            f"update public.push_subscriptions set {col} = %s where id = %s",
            (value, a),
            sqlstate=DENIED,
        )
    db.as_user(bob)
    assert db.rows("select label from public.push_subscriptions") == [(None,)]


def test_anon_sees_nothing(db, alice):
    set_web_push(db, enabled=True)
    save(db, alice)
    db.as_anon()
    db.fails("select id from public.push_subscriptions", sqlstate=DENIED)


def test_service_role_reads_keys_and_records_results(db, alice):
    set_web_push(db, enabled=True)
    sid = save(db, alice)
    db.as_service()
    assert db.one(
        "select (p256dh = %s and auth = %s) from public.push_subscriptions where id = %s",
        (P256DH, AUTH, sid),
    )
    db.run(
        "update public.push_subscriptions set fail_count = 2, last_success_at = now() "
        "where id = %s",
        (sid,),
    )
    assert db.run("delete from public.push_subscriptions where id = %s", (sid,)) == 1


@pytest.mark.parametrize(
    ("p256dh", "auth", "label"),
    [
        ("short", AUTH, None),
        (P256DH, "C" * 21, None),
        (P256DH + "!", AUTH, None),
        (P256DH, AUTH, ""),
        (P256DH, AUTH, "x" * 31),
    ],
)
def test_key_and_label_formats(db, alice, p256dh, auth, label):
    set_web_push(db, enabled=True)
    db.as_user(alice)
    db.fails(SAVE, (APPLE, p256dh, auth, label), sqlstate=CHECK)


# ---------------------------------------------------------------------------
# 重新同步、換帳號、上限、刪除帳號
# ---------------------------------------------------------------------------


def test_resync_same_device_updates_keys_and_keeps_label(db, alice):
    set_web_push(db, enabled=True)
    first = save(db, alice, label="我的 iPhone")
    db.as_service()
    db.run(
        "update public.push_subscriptions "
        "set fail_count = 3, last_seen_at = now() - interval '9 days'"
    )
    new_auth = "D" * 22
    assert save(db, alice, auth=new_auth) == first  # 同一台裝置，不會多一列
    db.as_service()
    assert (
        db.one(
            "select (auth, label, fail_count, last_seen_at > now() - interval '1 minute')::text "
            "from public.push_subscriptions"
        )
        == f'({new_auth},"我的 iPhone",0,t)'
    )


def test_device_switching_account_moves_to_new_user(db, alice, bob):
    """同一台裝置換人登入：推播改送給新帳號，舊帳號的名稱和成功紀錄不帶過去"""
    set_web_push(db, enabled=True)
    sid = save(db, alice, label="Alice 的手機")
    db.as_service()
    db.run("update public.push_subscriptions set last_success_at = now()")
    assert save(db, bob) == sid
    db.as_service()
    assert db.rows(
        "select user_id = %s, label, last_success_at from public.push_subscriptions", (bob,)
    ) == [(True, None, None)]
    db.as_user(alice)
    assert db.one("select count(*) from public.push_subscriptions") == 0


def test_at_most_five_devices_per_user(db, alice, bob):
    set_web_push(db, enabled=True)
    for n in range(5):
        save(db, alice, endpoint(n))
    db.as_user(alice)
    db.fails(SAVE, (endpoint(5), P256DH, AUTH, None), sqlstate=CHECK)
    assert save(db, alice, endpoint(0)) > 0  # 重新同步已有的裝置不算新增

    # 換帳號也算：bob 已經有 5 台時，不能把 alice 的裝置搬過來
    for n in range(10, 15):
        save(db, bob, endpoint(n))
    db.as_user(bob)
    db.fails(SAVE, (endpoint(0), P256DH, AUTH, None), sqlstate=CHECK)


def test_deleting_account_deletes_devices(db, alice):
    set_web_push(db, enabled=True)
    save(db, alice)
    db.as_postgres()
    db.run("delete from auth.users where id = %s", (alice,))
    assert db.one("select count(*) from public.push_subscriptions") == 0
