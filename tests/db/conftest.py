"""資料庫（Supabase）測試的共用工具。

這些測試需要一個已經套用 supabase/migrations 的 Postgres：
  * CI：`supabase db start` 啟動本機 Supabase，連線字串放在 SUPABASE_DB_URL。
  * 本機：裝了 Supabase CLI + Docker 的話，同樣先 `supabase db start`，再設定
    SUPABASE_DB_URL=postgresql://postgres:postgres@127.0.0.1:54322/postgres

沒有設定 SUPABASE_DB_URL 時，整個資料夾自動略過，不影響一般的 pytest。

每個測試都在一個「最後一定 rollback」的交易裡跑，所以不會留下任何資料。
用 `SET LOCAL ROLE` + `request.jwt.claims` 模擬 Supabase 的三種身分：
  * anon：沒登入、只帶 publishable key
  * authenticated：用 Google 登入的使用者（auth.uid() = sub）
  * service_role：排程用的 secret key（略過 RLS）
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import pytest

try:
    import psycopg
except ImportError:  # 沒裝 .[db] 的時候
    psycopg = None

DB_URL = os.environ.get("SUPABASE_DB_URL")

if psycopg is None or not DB_URL:
    if os.environ.get("FX_REQUIRE_DB"):
        # CI 的 db job 會設定這個，避免「其實沒跑卻顯示通過」
        raise RuntimeError("FX_REQUIRE_DB 已設定，但沒有 psycopg 或 SUPABASE_DB_URL")
    # 一般的 pytest：整個 tests/db 不收集
    collect_ignore_glob = ["test_*.py"]


@dataclass
class Db:
    """包一層 cursor，提供切換身分和常用查詢。"""

    cur: psycopg.Cursor

    # ---- 身分 ----

    def as_postgres(self) -> None:
        self.cur.execute("reset role")
        self.cur.execute("select set_config('request.jwt.claims', '', true)")

    def as_anon(self) -> None:
        self._as("anon", {"role": "anon"})

    def as_user(self, user_id: uuid.UUID) -> None:
        self._as("authenticated", {"sub": str(user_id), "role": "authenticated"})

    def as_service(self) -> None:
        self._as("service_role", {"role": "service_role"})

    def _as(self, role: str, claims: dict) -> None:
        self.as_postgres()
        self.cur.execute("select set_config('request.jwt.claims', %s, true)", (json.dumps(claims),))
        self.cur.execute(f"set local role {role}")

    # ---- 查詢 ----

    def rows(self, sql: str, params: tuple = ()) -> list[tuple]:
        self.cur.execute(sql, params)
        return self.cur.fetchall()

    def one(self, sql: str, params: tuple = ()):
        row = self.rows(sql, params)
        assert len(row) == 1, f"預期 1 列，實際 {len(row)} 列"
        return row[0][0] if len(row[0]) == 1 else row[0]

    def run(self, sql: str, params: tuple = ()) -> int:
        """執行並回傳受影響的列數。"""
        self.cur.execute(sql, params)
        return self.cur.rowcount

    def fails(self, sql: str, params: tuple = (), *, sqlstate: str) -> None:
        """預期失敗；用 savepoint 包起來，失敗後交易還能繼續用。"""
        conn = self.cur.connection
        with pytest.raises(psycopg.Error) as exc_info:
            with conn.transaction():
                self.cur.execute(sql, params)
        assert exc_info.value.sqlstate == sqlstate, exc_info.value

    # ---- 測試資料（以 postgres 身分建立，模擬 Supabase Auth 的行為）----

    def signup(self, email: str) -> uuid.UUID:
        """模擬 Google 第一次登入：Supabase Auth 會在 auth.users 新增一列。"""
        user_id = uuid.uuid4()
        self.as_postgres()
        self.cur.execute(
            "insert into auth.users (id, email, aud, role) "
            "values (%s, %s, 'authenticated', 'authenticated')",
            (user_id, email),
        )
        return user_id

    def add_rule(self, user_id: uuid.UUID, currency: str, target: str, **state) -> int:
        """以 service_role 建立規則（可直接指定 state_* 欄位）。"""
        self.as_service()
        cols = ["user_id", "currency", "target", *state]
        vals = [user_id, currency, target, *state.values()]
        placeholders = ", ".join(["%s"] * len(vals))
        self.cur.execute(
            f"insert into public.alert_rules ({', '.join(cols)}) "
            f"values ({placeholders}) returning id",
            vals,
        )
        return self.cur.fetchone()[0]

    def add_notification(self, user_id: uuid.UUID, rule_id: int | None, currency: str) -> int:
        self.as_service()
        self.cur.execute(
            "insert into public.notifications (user_id, rule_id, currency, rate, target) "
            "values (%s, %s, %s, 0.2, 0.205) returning id",
            (user_id, rule_id, currency),
        )
        return self.cur.fetchone()[0]


@pytest.fixture(scope="session")
def conn() -> Iterator[psycopg.Connection]:
    with psycopg.connect(DB_URL) as c:
        yield c


@pytest.fixture
def db(conn: psycopg.Connection) -> Iterator[Db]:
    with conn.transaction(force_rollback=True):
        with conn.cursor() as cur:
            yield Db(cur)


@pytest.fixture
def alice(db: Db) -> uuid.UUID:
    return db.signup("alice@example.com")


@pytest.fixture
def bob(db: Db) -> uuid.UUID:
    return db.signup("bob@example.com")
