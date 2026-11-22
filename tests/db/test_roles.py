"""角色（RBAC）：預設 user；使用者讀得到、改不了自己的角色；只有 service_role 能改。"""

from __future__ import annotations

import uuid

DENIED = "42501"
CHECK = "23514"


def test_new_user_is_plain_user(db, alice):
    db.as_user(alice)
    assert db.one("select role from public.profiles") == "user"


def test_user_reads_own_role_via_function(db, alice):
    db.as_user(alice)
    assert db.one("select public.current_user_role()") == "user"


def test_user_cannot_promote_self(db, alice):
    """就算繞過前端直接呼叫 API，也不能把自己升級成 admin。"""
    db.as_user(alice)
    db.fails("update public.profiles set role = 'admin'", sqlstate=DENIED)
    db.as_service()
    assert db.one("select role from public.profiles where user_id = %s", (alice,)) == "user"


def test_user_cannot_change_other_role(db, alice, bob):
    db.as_user(alice)
    db.fails("update public.profiles set role = 'user' where user_id = %s", (bob,), sqlstate=DENIED)


def test_service_role_can_assign_roles(db, alice):
    db.as_service()
    assert db.run("update public.profiles set role = 'beta' where user_id = %s", (alice,)) == 1
    db.as_user(alice)
    assert db.one("select public.current_user_role()") == "beta"


def test_unknown_role_rejected(db, alice):
    db.as_service()
    db.fails(
        "update public.profiles set role = 'superuser' where user_id = %s", (alice,), sqlstate=CHECK
    )


def test_anon_cannot_call_role_function(db):
    db.as_anon()
    db.fails("select public.current_user_role()", sqlstate=DENIED)


def test_role_function_is_null_without_profile(db):
    """登入了但沒有 profile（理論上不會發生）→ null，不會被當成任何角色。"""
    db.as_user(uuid.uuid4())
    assert db.one("select public.current_user_role()") is None
