// Supabase Edge Function：刪除自己的帳號。邏輯在 handler.ts，金鑰在 keys.ts。
//
// 需要的 secrets（Supabase 後台 → Edge Functions → Secrets）：
//   FX_SECRET_KEY   給 Edge Function 用的 secret key（sb_secret_…），刪除使用者用
// Supabase 自動提供：SUPABASE_URL、SUPABASE_PUBLISHABLE_KEYS（驗證使用者 token 用）
// 不使用舊版的 SUPABASE_ANON_KEY / SUPABASE_SERVICE_ROLE_KEY。
//
// 平台層的「Verify JWT」關閉（supabase/config.toml 的 verify_jwt = false）：
// 身分改在 handler.ts 裡用 Supabase Auth 驗證，並額外檢查最近 10 分鐘內登入過。

import { handle } from "./handler.ts";
import { publishableKey, secretKey, supabaseUrl } from "./keys.ts";

const env = (name: string) => Deno.env.get(name);
const SUPABASE_URL = supabaseUrl(env);
const PUBLISHABLE_KEY = publishableKey(env);
const SECRET_KEY = secretKey(env);

const ALLOWED_ORIGINS = ["https://xysmr.github.io", "http://localhost:8000"];

async function getUser(token: string): Promise<{ id: string } | null> {
  const res = await fetch(`${SUPABASE_URL}/auth/v1/user`, {
    headers: { apikey: PUBLISHABLE_KEY ?? "", Authorization: `Bearer ${token}` },
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
    headers: { apikey: SECRET_KEY ?? "" }, // 新版 secret key 只放 apikey
  });
  await res.body?.cancel();
  if (!res.ok && res.status !== 404) { // 404 = 已經刪掉了
    console.error(`delete user failed: ${res.status}`); // 不記錄任何個人資料
    throw new Error("delete failed");
  }
}

Deno.serve((req) =>
  handle(req, {
    configured: SUPABASE_URL !== null && PUBLISHABLE_KEY !== null && SECRET_KEY !== null,
    allowedOrigins: ALLOWED_ORIGINS,
    getUser,
    deleteUser,
    now: () => Math.floor(Date.now() / 1000),
  })
);
