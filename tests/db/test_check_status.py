"""最後一次檢查的結果（migration 20261116040000）：任何人可讀、只有 secret key 可寫、只有一列。"""

CHECK = "23514"
NO_PRIVILEGE = "42501"
UPSERT = """
    insert into public.check_status (id, checked_at, ok, errors)
    values (1, %s, %s, %s)
    on conflict (id) do update
      set checked_at = excluded.checked_at, ok = excluded.ok, errors = excluded.errors,
          updated_at = now()
"""


def test_service_role_upserts_single_row(db):
    db.as_service()
    db.run(UPSERT, ("2026-11-16T09:05:00+08:00", True, 0))
    db.run(UPSERT, ("2026-11-16T09:35:00+08:00", False, 2))
    assert db.rows("select id, ok, errors from public.check_status") == [(1, False, 2)]


def test_only_one_row(db):
    db.as_service()
    db.fails(
        "insert into public.check_status (id, checked_at, ok) values (2, now(), true)",
        sqlstate=CHECK,
    )
    db.fails(
        "insert into public.check_status (checked_at, ok, errors) values (now(), true, -1)",
        sqlstate=CHECK,
    )


def test_anyone_can_read(db, alice):
    db.as_service()
    db.run(UPSERT, ("2026-11-16T09:05:00+08:00", True, 0))
    db.as_anon()
    assert db.one("select ok from public.check_status") is True
    db.as_user(alice)
    assert db.one("select errors from public.check_status") == 0


def test_anon_and_users_cannot_write(db, alice):
    db.as_service()
    db.run(UPSERT, ("2026-11-16T09:05:00+08:00", True, 0))
    for who in ("anon", "user"):
        db.as_anon() if who == "anon" else db.as_user(alice)
        db.fails(UPSERT, ("2026-11-16T10:05:00+08:00", True, 0), sqlstate=NO_PRIVILEGE)
        db.fails("update public.check_status set ok = false", sqlstate=NO_PRIVILEGE)
        db.fails("delete from public.check_status", sqlstate=NO_PRIVILEGE)


def test_columns_are_public_safe(db):
    """公開可讀：只有時間、成功與否、錯誤數量，沒有錯誤訊息之類的文字欄位。"""
    db.as_postgres()
    cols = db.rows(
        "select column_name, data_type from information_schema.columns "
        "where table_schema = 'public' and table_name = 'check_status' order by ordinal_position"
    )
    assert cols == [
        ("id", "smallint"),
        ("checked_at", "timestamp with time zone"),
        ("ok", "boolean"),
        ("errors", "integer"),
        ("updated_at", "timestamp with time zone"),
    ]
