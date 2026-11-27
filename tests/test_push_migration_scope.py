"""推播的 migration 只能「新增」，不能動到 Demo 用的東西（12/15 A10 Demo 前合併的條件）。

逐句檢查 20261128021121_push_subscriptions.sql：每一句都必須是新增推播自己的物件，
或是新增 web_push 這一筆 flag。不能有 drop、create or replace、alter 別的表、
update／delete 任何資料（函式內容 $$…$$ 不算，那是函式被呼叫時才執行的）。
"""

import re
from pathlib import Path

MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "supabase"
    / "migrations"
    / "20261128021121_push_subscriptions.sql"
)

OWN_OBJECTS = (
    "public.push_subscriptions",
    "public.is_allowed_push_endpoint",
    "public.check_push_subscription_limit",
    "public.save_push_subscription",
    "push_subscriptions_user_idx",
    "on_push_subscription_insert",
)

ALLOWED = [
    r"create function public\.(is_allowed_push_endpoint|check_push_subscription_limit|"
    r"save_push_subscription)\(",
    r"create table public\.push_subscriptions \(",
    r"create index push_subscriptions_user_idx on public\.push_subscriptions ",
    r"create trigger on_push_subscription_insert .* on public\.push_subscriptions ",
    r"create policy \"[^\"]+\" on public\.push_subscriptions ",
    r"comment on table public\.push_subscriptions is ",
    r"alter table public\.push_subscriptions enable row level security$",
    r"(grant|revoke) .* on (function )?public\.(push_subscriptions|is_allowed_push_endpoint|"
    r"check_push_subscription_limit|save_push_subscription)\b",
    r"insert into public\.feature_flags \(flag, enabled, rollout\) "
    r"values \('web_push', false, 0\)$",
]


def statements() -> list[str]:
    sql = MIGRATION.read_text("utf-8")
    sql = re.sub(r"\$\$.*?\$\$", "$$", sql, flags=re.S)  # 函式內容
    sql = re.sub(r"--[^\n]*", "", sql)  # 註解
    return [" ".join(s.split()) for s in sql.split(";") if s.strip()]


def test_every_statement_only_adds_push_objects():
    stmts = statements()
    assert len(stmts) >= 15
    for stmt in stmts:
        assert any(re.match(p, stmt, re.I) for p in ALLOWED), f"不允許的語句：{stmt[:120]}"


def test_no_destructive_or_replacing_statements():
    for stmt in statements():
        low = stmt.lower()
        assert not low.startswith(("drop", "update", "delete", "truncate", "alter function")), stmt
        assert "create or replace" not in low, stmt
        if low.startswith("alter table"):
            assert "public.push_subscriptions" in low, stmt


def test_web_push_flag_is_added_off():
    """flag 預設關閉：enabled = false 時連 admin、beta 都看不到"""
    inserts = [s for s in statements() if s.lower().startswith("insert")]
    assert inserts == [
        "insert into public.feature_flags (flag, enabled, rollout) values ('web_push', false, 0)"
    ]


def test_grants_only_touch_push_objects():
    for stmt in statements():
        if stmt.lower().startswith(("grant", "revoke")):
            assert any(obj in stmt for obj in OWN_OBJECTS), stmt
