"""Feature flags（Assignment 10：trip_plan、quick_target）。

flag 存在 Supabase 的 public.feature_flags，網頁、RLS 用 SQL 函式 public.is_feature_on() 判斷；
這裡是同一套規則的 Python 版，給排程和測試用。兩邊的分桶算法必須一樣，
否則同一個使用者在網頁看到功能、排程卻當成關閉（tests/test_flags.py 會用 Postgres 算出的值比對）。

規則（順序有意義）：
  1. 沒有這個 flag、enabled = false（kill switch）、沒有使用者 → 關閉
  2. 使用者的 role 在 allowed_roles 裡（admin、beta）→ 開啟，不看 rollout
  3. 其他人：bucket(flag, user_id) < rollout → 開啟

bucket = md5("<flag>:<user_id>") 的前 8 個 16 進位字元當成無號整數，再 % 100。
SQL：('x' || substr(md5(p_flag || ':' || auth.uid()::text), 1, 8))::bit(32)::bigint % 100
同一個使用者每次都落在同一桶；不同 flag 的分桶互相獨立。
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

BUCKETS = 100


def bucket(flag: str, user_id: str) -> int:
    """回傳 0～99。必須和 public.is_feature_on() 的算法相同。"""
    digest = hashlib.md5(f"{flag}:{user_id}".encode(), usedforsecurity=False).hexdigest()
    return int(digest[:8], 16) % BUCKETS


def is_on(
    flag: str,
    user_id: str | None,
    flags: Mapping[str, Mapping[str, Any]],
    role: str | None = None,
) -> bool:
    """flags 的格式和 feature_flags 表相同：{flag: {enabled, rollout, allowed_roles}}。"""
    f = flags.get(flag)
    if not f or not f.get("enabled", False) or not user_id:
        return False
    if role is not None and role in f.get("allowed_roles", ()):
        return True
    # 表的預設是 0（沒設定 rollout 就不開給一般使用者）
    return bucket(flag, user_id) < f.get("rollout", 0)
