// Edge Function 用的金鑰（不使用舊版的 SUPABASE_ANON_KEY / SUPABASE_SERVICE_ROLE_KEY）。
//
// 兩支 function 各放一份完全相同的副本（後台部署只會帶同一個資料夾的檔案），
// tests/test_supabase_config.py 會檢查兩份一致。
//
//   * secret key：自訂的 Edge Function Secret `FX_SECRET_KEY`（sb_secret_…）。
//     不用 SUPABASE_SECRET_KEYS 的 default：輪替時要能換成另一把 key 再刪舊的。
//   * publishable key：Supabase 自動提供的 SUPABASE_PUBLISHABLE_KEYS（JSON 字典）裡的 default。
//
// 新版金鑰不是 JWT，只能放在 apikey header，不能放 Authorization。
// 讀不到或格式不對就回傳 null，handler 會回 500 not_configured。

export type Env = (name: string) => string | undefined;

export function secretKey(env: Env): string | null {
  const key = (env("FX_SECRET_KEY") ?? "").trim();
  return key.startsWith("sb_secret_") && key.length > "sb_secret_".length ? key : null;
}

export function publishableKey(env: Env): string | null {
  let keys: unknown;
  try {
    keys = JSON.parse(env("SUPABASE_PUBLISHABLE_KEYS") ?? "");
  } catch {
    return null;
  }
  if (!keys || typeof keys !== "object") return null;
  const key = (keys as Record<string, unknown>)["default"];
  return typeof key === "string" && key.startsWith("sb_publishable_") ? key : null;
}

export function supabaseUrl(env: Env): string | null {
  const url = (env("SUPABASE_URL") ?? "").replace(/\/+$/, "");
  return /^https?:\/\/\S+$/.test(url) ? url : null;
}
