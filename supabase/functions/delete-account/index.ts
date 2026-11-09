// Supabase Edge Function：刪除自己的帳號。邏輯在 handler.ts。
//
// 不需要額外的 secrets。Supabase 自動提供：
//   SUPABASE_URL、SUPABASE_ANON_KEY（驗證使用者 token 用）、SUPABASE_SERVICE_ROLE_KEY（刪除用）
//
// 平台層的「Verify JWT」關閉（supabase/config.toml 的 verify_jwt = false）：
// 身分改在 handler.ts 裡用 Supabase Auth 驗證，並額外檢查最近 10 分鐘內登入過。

import { handle } from "./handler.ts";

const SUPABASE_URL = Deno.env.get("SUPABASE_URL") ?? "";
const ANON_KEY = Deno.env.get("SUPABASE_ANON_KEY") ?? "";
const SERVICE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? "";

const ALLOWED_ORIGINS = ["https://xysmr.github.io", "http://localhost:8000"];

async function getUser(token: string): Promise<{ id: string } | null> {
  const res = await fetch(`${SUPABASE_URL}/auth/v1/user`, {
    headers: { apikey: ANON_KEY, Authorization: `Bearer ${token}` },
  });
  if (!res.ok) {
    await res.body?.cancel();
    return null;
  }
  const user = await res.json();
  return user && typeof user.id === "string" ? { id: user.id } : null;
}

async function deleteUser(userId: string): Promise<void> {
  const res = await fetch(`${SUPABASE_URL}/auth/v1/admin/users/${encodeURIComponent(userId)}`, {
    method: "DELETE",
    headers: { apikey: SERVICE_KEY, Authorization: `Bearer ${SERVICE_KEY}` },
  });
  await res.body?.cancel();
  if (!res.ok && res.status !== 404) { // 404 = 已經刪掉了
    console.error(`delete user failed: ${res.status}`); // 不記錄任何個人資料
    throw new Error("delete failed");
  }
}

Deno.serve((req) =>
  handle(req, {
    allowedOrigins: ALLOWED_ORIGINS,
    getUser,
    deleteUser,
    now: () => Math.floor(Date.now() / 1000),
  })
);
