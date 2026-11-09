// Supabase Edge Function：一鍵退訂。邏輯在 handler.ts，金鑰在 keys.ts。
//
// 需要的 secrets（Supabase 後台 → Edge Functions → Secrets）：
//   UNSUBSCRIBE_SECRET   和 GitHub Secrets 裡的同一個值（至少 32 個字元）
//   FX_SECRET_KEY        給 Edge Function 用的 secret key（sb_secret_…）
// Supabase 自動提供：SUPABASE_URL
// 不使用舊版的 SUPABASE_SERVICE_ROLE_KEY。
//
// 這個 function 不檢查 JWT（Gmail 和沒登入的人也要能呼叫），身分由簽章驗證：
// supabase/config.toml 的 [functions.unsubscribe] verify_jwt = false，
// 在後台部署時也要把「Verify JWT」關掉。

import { handle } from "./handler.ts";
import { secretKey, supabaseUrl } from "./keys.ts";

const env = (name: string) => Deno.env.get(name);
const SUPABASE_URL = supabaseUrl(env);
const SECRET_KEY = secretKey(env);
const SECRET = Deno.env.get("UNSUBSCRIBE_SECRET") ?? "";

const ALLOWED_ORIGINS = ["https://xysmr.github.io", "http://localhost:8000"];

/** 回傳 false = 沒有這個帳號（已經刪除） */
async function disable(userId: string): Promise<boolean> {
  const res = await fetch(
    `${SUPABASE_URL}/rest/v1/profiles?user_id=eq.${encodeURIComponent(userId)}&select=user_id`,
    {
      method: "PATCH",
      headers: {
        apikey: SECRET_KEY ?? "", // 新版 secret key 只放 apikey
        "Content-Type": "application/json",
        Prefer: "return=representation",
      },
      body: JSON.stringify({ email_enabled: false }),
    },
  );
  if (!res.ok) {
    await res.body?.cancel();
    console.error(`profiles update failed: ${res.status}`); // 不記錄任何個人資料
    throw new Error("update failed");
  }
  const rows = await res.json();
  return Array.isArray(rows) && rows.length > 0;
}

Deno.serve((req) =>
  handle(req, {
    configured: SUPABASE_URL !== null && SECRET_KEY !== null,
    secret: SECRET,
    allowedOrigins: ALLOWED_ORIGINS,
    disable,
  })
);
