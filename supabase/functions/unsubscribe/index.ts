// Supabase Edge Function：一鍵退訂。邏輯在 handler.ts。
//
// 需要的 secrets（Supabase 後台 → Edge Functions → Secrets）：
//   UNSUBSCRIBE_SECRET   和 GitHub Secrets 裡的同一個值（至少 32 個字元）
// Supabase 自動提供：SUPABASE_URL、SUPABASE_SERVICE_ROLE_KEY
//
// 這個 function 不檢查 JWT（Gmail 和沒登入的人也要能呼叫），身分由簽章驗證：
// supabase/config.toml 的 [functions.unsubscribe] verify_jwt = false，
// 在後台部署時也要把「Verify JWT」關掉。

import { handle } from "./handler.ts";

const SUPABASE_URL = Deno.env.get("SUPABASE_URL") ?? "";
const SERVICE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? "";
const SECRET = Deno.env.get("UNSUBSCRIBE_SECRET") ?? "";

const ALLOWED_ORIGINS = ["https://xysmr.github.io", "http://localhost:8000"];

async function disable(userId: string): Promise<void> {
  const res = await fetch(
    `${SUPABASE_URL}/rest/v1/profiles?user_id=eq.${encodeURIComponent(userId)}`,
    {
      method: "PATCH",
      headers: {
        apikey: SERVICE_KEY,
        Authorization: `Bearer ${SERVICE_KEY}`,
        "Content-Type": "application/json",
        Prefer: "return=minimal",
      },
      body: JSON.stringify({ email_enabled: false }),
    },
  );
  if (!res.ok) {
    console.error(`profiles update failed: ${res.status}`); // 不記錄 user_id 以外的資料
    throw new Error("update failed");
  }
}

Deno.serve((req) => handle(req, { secret: SECRET, allowedOrigins: ALLOWED_ORIGINS, disable }));
