// deno test supabase/functions/
// 不依賴外部套件
import {
  handle,
  isConfirmed,
  lastSignInAt,
  MAX_AUTH_AGE_SECONDS,
  normalizeConfirm,
} from "./handler.ts";
import CASES from "./confirm_cases.json" with { type: "json" };

function assert(cond: unknown, msg = "assertion failed"): asserts cond {
  if (!cond) throw new Error(msg);
}

function assertEquals(actual: unknown, expected: unknown): void {
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  if (a !== e) throw new Error(`expected ${e}, got ${a}`);
}

const FN = "https://abcdefghijklmnopqrst.supabase.co/functions/v1/delete-account";
const SITE = "https://xysmr.github.io";
const ALICE = "00000000-0000-4000-8000-00000000000a";
const BOB = "00000000-0000-4000-8000-00000000000b";
const NOW = 1_800_000_000;

/** 假的 JWT：只有 payload 有意義（真正的驗證交給 getUser） */
function token(signedInSecondsAgo: number | null, sub = ALICE): string {
  const amr = signedInSecondsAgo === null
    ? []
    : [{ method: "oauth", timestamp: NOW - signedInSecondsAgo }];
  const payload = btoa(JSON.stringify({ sub, amr, iat: NOW - 60 }))
    .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  return `eyJhbGciOiJIUzI1NiJ9.${payload}.sig`;
}

function setup(opts: { failDelete?: boolean } = {}) {
  const deleted: string[] = [];
  const valid = new Map<string, string>(); // token → user id
  const deps = {
    configured: true,
    allowedOrigins: [SITE],
    getUser: (t: string) => Promise.resolve(valid.has(t) ? { id: valid.get(t)! } : null),
    deleteUser: (id: string): Promise<void> => {
      if (opts.failDelete) return Promise.reject(new Error("auth down"));
      deleted.push(id);
      return Promise.resolve();
    },
    now: () => NOW,
  };
  const login = (secondsAgo: number | null, sub = ALICE) => {
    const t = token(secondsAgo, sub);
    valid.set(t, sub);
    return t;
  };
  return { deps, deleted, login };
}

function post(t: string | null, body: unknown = { confirm: "刪除我的帳號" }): Request {
  const headers: Record<string, string> = { "Content-Type": "application/json", Origin: SITE };
  if (t) headers.Authorization = `Bearer ${t}`;
  return new Request(FN, { method: "POST", headers, body: JSON.stringify(body) });
}

async function call(req: Request, deps: Parameters<typeof handle>[1]) {
  const res = await handle(req, deps);
  return { status: res.status, body: await res.json().catch(() => null), res };
}

// ---------- 確認文字：和前端讀同一份案例 ----------

Deno.test("confirm text: shared cases with the frontend", () => {
  assertEquals(CASES.text, "刪除我的帳號");
  for (const c of CASES.cases) {
    assertEquals([c.input, isConfirmed(c.input)], [c.input, c.ok]);
  }
  assertEquals(normalizeConfirm(42), "");
  assertEquals(isConfirmed(null), false);
});

// ---------- 成功 ----------

Deno.test("deletes only the token owner after recent sign-in", async () => {
  const { deps, deleted, login } = setup();
  const r = await call(post(login(60)), deps);
  assertEquals(r.status, 200);
  assertEquals(r.body, { ok: true });
  assertEquals(deleted, [ALICE]);
  assertEquals(r.res.headers.get("Access-Control-Allow-Origin"), SITE);
});

Deno.test("user_id in the body is ignored: cannot delete someone else", async () => {
  const { deps, deleted, login } = setup();
  const r = await call(
    post(login(60), { confirm: "刪除我的帳號", user_id: BOB, id: BOB, sub: BOB }),
    deps,
  );
  assertEquals(r.status, 200);
  assertEquals(deleted, [ALICE]);
});

// ---------- 拒絕 ----------

Deno.test("no token or invalid token → 401 not_logged_in", async () => {
  const { deps, deleted } = setup();
  assertEquals((await call(post(null), deps)).body, { ok: false, error: "not_logged_in" });
  const forged = token(60); // 格式正確但 Supabase Auth 不認得
  const r = await call(post(forged), deps);
  assertEquals([r.status, r.body], [401, { ok: false, error: "not_logged_in" }]);
  assertEquals(deleted, []);
});

Deno.test("signed in too long ago → 401 reauth_required", async () => {
  const { deps, deleted, login } = setup();
  const r = await call(post(login(MAX_AUTH_AGE_SECONDS + 1)), deps);
  assertEquals([r.status, r.body], [401, { ok: false, error: "reauth_required" }]);
  // 剛好 10 分鐘還可以
  assertEquals((await call(post(login(MAX_AUTH_AGE_SECONDS)), deps)).status, 200);
  assertEquals(deleted, [ALICE]);
});

Deno.test("token without sign-in time → reauth_required", async () => {
  const { deps, deleted, login } = setup();
  const r = await call(post(login(null)), deps);
  assertEquals(r.body, { ok: false, error: "reauth_required" });
  assertEquals(deleted, []);
});

Deno.test("wrong or missing confirm text → 400", async () => {
  const { deps, deleted, login } = setup();
  const t = login(60);
  for (const body of [{ confirm: "刪除我的帳號。" }, {}, { confirm: 1 }, "x", null]) {
    const r = await call(post(t, body), deps);
    assertEquals([r.status, r.body], [400, { ok: false, error: "confirm_mismatch" }]);
  }
  assertEquals(deleted, []);
});

Deno.test("GET never deletes", async () => {
  const { deps, deleted, login } = setup();
  const t = login(60);
  const res = await handle(new Request(FN, { headers: { Authorization: `Bearer ${t}` } }), deps);
  assertEquals(res.status, 405);
  await res.body?.cancel();
  assertEquals(deleted, []);
});

Deno.test("delete failure → 500 without details", async () => {
  const { deps, login } = setup({ failDelete: true });
  const r = await call(post(login(60)), deps);
  assertEquals([r.status, r.body], [500, { ok: false, error: "delete_failed" }]);
});

Deno.test("CORS only for allowed origins", async () => {
  const { deps } = setup();
  const ok = await handle(new Request(FN, { method: "OPTIONS", headers: { Origin: SITE } }), deps);
  assertEquals(ok.headers.get("Access-Control-Allow-Origin"), SITE);
  assert((ok.headers.get("Access-Control-Allow-Headers") ?? "").includes("authorization"));
  const evil = await handle(
    new Request(FN, { method: "OPTIONS", headers: { Origin: "https://evil.example" } }),
    deps,
  );
  assertEquals(evil.headers.get("Access-Control-Allow-Origin"), null);
});

Deno.test("lastSignInAt picks the latest amr timestamp", () => {
  assertEquals(
    lastSignInAt({ amr: [{ method: "oauth", timestamp: 10 }, { method: "x", timestamp: 20 }] }),
    20,
  );
  assertEquals(lastSignInAt({ amr: [{ method: "oauth" }] }), null);
  assertEquals(lastSignInAt(null), null);
});

Deno.test("keys missing (not configured) → 500 not_configured, nobody deleted", async () => {
  const { deps, deleted, login } = setup();
  const r = await call(post(login(60)), { ...deps, configured: false });
  assertEquals([r.status, r.body], [500, { ok: false, error: "not_configured" }]);
  assertEquals(deleted, []);
});

Deno.test("not configured still answers GET with 405 (smoke test order)", async () => {
  const { deps } = setup();
  const r = await call(new Request(FN), { ...deps, configured: false });
  assertEquals(r.status, 405);
});
