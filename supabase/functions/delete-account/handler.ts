// 刪除帳號的邏輯（不依賴 Deno 專屬 API，方便測試）。
//
// 安全規則：
//   * 只接受 POST（GET 405）；CORS 只允許自己的網站。
//   * 必須帶使用者自己的登入 token（Authorization: Bearer …），由 Supabase Auth 驗證是誰。
//   * 只刪除 token 本人：不接受請求裡的任何 user_id。
//   * 最近 10 分鐘內登入過才可以刪（token 被偷、或電腦借別人用時多一層保護）。
//   * body 必須是 {"confirm": "刪除我的帳號"}；比對規則和前端一樣（normalizeConfirm）。
// 刪除 auth.users 之後，profiles、alert_rules、notifications 由資料庫連帶刪除。

export const CONFIRM_TEXT = "刪除我的帳號";
export const MAX_AUTH_AGE_SECONDS = 10 * 60;

export interface Deps {
  /** 金鑰都讀得到（見 keys.ts）；false 時回 500 not_configured */
  configured: boolean;
  allowedOrigins: string[];
  /** 用使用者的 token 問 Supabase Auth 這是誰；無效或過期回傳 null */
  getUser: (token: string) => Promise<{ id: string } | null>;
  /** 用 secret key 刪除這個使用者 */
  deleteUser: (userId: string) => Promise<void>;
  /** 現在時間（秒）；測試可以換掉 */
  now: () => number;
}

/** 確認文字的比對規則（前端 site/account.js 用同一套）：NFC 正規化、去掉前後空白 */
export function normalizeConfirm(text: unknown): string {
  return typeof text === "string" ? text.normalize("NFC").trim() : "";
}

export function isConfirmed(text: unknown): boolean {
  return normalizeConfirm(text) === CONFIRM_TEXT;
}

/** 讀出 JWT 的內容（只在 getUser 驗證過 token 之後使用） */
export function jwtClaims(token: string): Record<string, unknown> | null {
  const part = token.split(".")[1];
  if (!part) return null;
  try {
    const b64 = part.replace(/-/g, "+").replace(/_/g, "/");
    const padded = b64 + "=".repeat((4 - (b64.length % 4)) % 4);
    const text = new TextDecoder().decode(Uint8Array.from(atob(padded), (c) => c.charCodeAt(0)));
    const claims = JSON.parse(text);
    return claims && typeof claims === "object" ? claims : null;
  } catch {
    return null;
  }
}

/** 最近一次登入的時間（秒）：Supabase 的 token 在 amr 裡記錄每種登入方式的時間 */
export function lastSignInAt(claims: Record<string, unknown> | null): number | null {
  const amr = claims && Array.isArray(claims.amr) ? claims.amr : [];
  const times = amr
    .map((m) => (m && typeof m === "object" ? (m as { timestamp?: unknown }).timestamp : null))
    .filter((t): t is number => typeof t === "number" && Number.isFinite(t));
  return times.length ? Math.max(...times) : null;
}

function corsHeaders(req: Request, allowed: string[]): Record<string, string> {
  const origin = req.headers.get("Origin");
  if (!origin || !allowed.includes(origin)) return {};
  return {
    "Access-Control-Allow-Origin": origin,
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "authorization, apikey, content-type, x-client-info",
    "Vary": "Origin",
  };
}

function json(status: number, body: unknown, extra: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8", ...extra },
  });
}

export async function handle(req: Request, deps: Deps): Promise<Response> {
  const cors = corsHeaders(req, deps.allowedOrigins);

  if (req.method === "OPTIONS") {
    return new Response(null, { status: 204, headers: cors });
  }
  if (req.method !== "POST") {
    return json(405, { ok: false, error: "method_not_allowed" }, {
      ...cors,
      Allow: "POST, OPTIONS",
    });
  }
  if (!deps.configured) {
    return json(500, { ok: false, error: "not_configured" }, cors);
  }

  const auth = req.headers.get("Authorization") ?? "";
  const token = auth.startsWith("Bearer ") ? auth.slice(7).trim() : "";
  if (!token) return json(401, { ok: false, error: "not_logged_in" }, cors);

  let user: { id: string } | null;
  try {
    user = await deps.getUser(token);
  } catch {
    user = null;
  }
  if (!user || typeof user.id !== "string") {
    return json(401, { ok: false, error: "not_logged_in" }, cors);
  }

  const signedInAt = lastSignInAt(jwtClaims(token));
  if (signedInAt === null || deps.now() - signedInAt > MAX_AUTH_AGE_SECONDS) {
    return json(401, { ok: false, error: "reauth_required" }, cors);
  }

  let body: unknown = null;
  try {
    body = await req.json();
  } catch {
    body = null;
  }
  const confirm = body && typeof body === "object" ? (body as { confirm?: unknown }).confirm : null;
  if (!isConfirmed(confirm)) {
    return json(400, { ok: false, error: "confirm_mismatch" }, cors);
  }

  try {
    await deps.deleteUser(user.id); // 只刪 token 本人
  } catch {
    return json(500, { ok: false, error: "delete_failed" }, cors);
  }
  return json(200, { ok: true }, cors);
}
