// 一鍵退訂的邏輯（不依賴 Deno 專屬 API，方便測試）。
//
// 簽章演算法必須和 src/fx_alert/unsubscribe.py 完全一致：
//   base64url( HMAC-SHA256(UNSUBSCRIBE_SECRET, "fx-alert:unsubscribe:v1:" + user_id) )
//
// 只接受 POST：
//   * Gmail 等信箱的「取消訂閱」按鈕（RFC 8058）：
//       POST <List-Unsubscribe 網址>?u=…&s=…，body 是 List-Unsubscribe=One-Click
//   * GitHub Pages 的確認頁：POST，body 是 JSON {"u": "...", "s": "..."}
// GET 一律 405：信箱的連結掃描器會自動打開信裡的連結，不能因此就退訂。

export const PREFIX = "fx-alert:unsubscribe:v1:";

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const SIG_RE = /^[A-Za-z0-9_-]{43}$/; // 32 bytes 的 base64url，沒有補 =

export interface Deps {
  secret: string;
  allowedOrigins: string[];
  /**
   * 把這個使用者的 email_enabled 設成 false；重複呼叫也沒關係。
   * 回傳 false = 找不到這個帳號（已經刪除）：本來就不會再寄信，對使用者來說一樣是成功。
   */
  disable: (userId: string) => Promise<boolean>;
}

const encoder = new TextEncoder();

async function hmacKey(secret: string, usage: "sign" | "verify"): Promise<CryptoKey> {
  return await crypto.subtle.importKey(
    "raw",
    encoder.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    [usage],
  );
}

function toBase64Url(bytes: Uint8Array): string {
  let bin = "";
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function fromBase64Url(text: string): Uint8Array<ArrayBuffer> {
  const b64 = text.replace(/-/g, "+").replace(/_/g, "/") + "=";
  const bin = atob(b64);
  return Uint8Array.from(bin, (c) => c.charCodeAt(0));
}

export async function sign(secret: string, userId: string): Promise<string> {
  const key = await hmacKey(secret, "sign");
  const mac = await crypto.subtle.sign("HMAC", key, encoder.encode(PREFIX + userId.toLowerCase()));
  return toBase64Url(new Uint8Array(mac));
}

/** crypto.subtle.verify 是固定時間比對 */
export async function verify(secret: string, userId: string, sig: string): Promise<boolean> {
  const uid = userId.toLowerCase();
  if (!UUID_RE.test(uid) || !SIG_RE.test(sig)) return false;
  const key = await hmacKey(secret, "verify");
  return await crypto.subtle.verify(
    "HMAC",
    key,
    fromBase64Url(sig),
    encoder.encode(PREFIX + uid),
  );
}

function corsHeaders(req: Request, allowed: string[]): Record<string, string> {
  const origin = req.headers.get("Origin");
  if (!origin || !allowed.includes(origin)) return {};
  return {
    "Access-Control-Allow-Origin": origin,
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "content-type",
    "Vary": "Origin",
  };
}

function json(status: number, body: unknown, extra: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8", ...extra },
  });
}

async function readParams(req: Request): Promise<{ u: string; s: string } | null> {
  const url = new URL(req.url);
  let u = url.searchParams.get("u");
  let s = url.searchParams.get("s");
  if ((!u || !s) && (req.headers.get("Content-Type") ?? "").includes("application/json")) {
    try {
      const body = await req.json();
      if (body && typeof body === "object") {
        u = typeof body.u === "string" ? body.u : u;
        s = typeof body.s === "string" ? body.s : s;
      }
    } catch {
      return null;
    }
  }
  return u && s ? { u, s } : null;
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
  if (deps.secret.length < 32) {
    return json(500, { ok: false, error: "not_configured" }, cors);
  }

  const params = await readParams(req);
  if (!params || !(await verify(deps.secret, params.u, params.s))) {
    return json(403, { ok: false, error: "invalid_link" }, cors);
  }

  let found: boolean;
  try {
    found = await deps.disable(params.u.toLowerCase());
  } catch {
    // 不把資料庫錯誤細節回給呼叫者
    return json(500, { ok: false, error: "update_failed" }, cors);
  }
  // 帳號已刪除的人點舊信的連結：不是錯誤，告訴他已經不會再收到通知
  return json(200, { ok: true, status: found ? "unsubscribed" : "no_account" }, cors);
}
