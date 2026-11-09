// deno test supabase/functions/unsubscribe/
// 不依賴外部套件（CI 和本機都不用下載任何東西）
import { handle, sign, verify } from "./handler.ts";

function assert(cond: unknown, msg = "assertion failed"): asserts cond {
  if (!cond) throw new Error(msg);
}

function assertEquals(actual: unknown, expected: unknown): void {
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  if (a !== e) throw new Error(`expected ${e}, got ${a}`);
}

// 和 tests/test_unsubscribe.py 共用的測試向量：Python 和這裡必須算出一樣的簽章
const SECRET = "fx-alert-test-vector-secret-0123456789";
const USER = "00000000-0000-4000-8000-000000000001";
const SIG = "AjoUefqQcmpwV0VAbKi-BoBMszBZ6bicHXfBpHuWxDQ";
const FN = "https://abcdefghijklmnopqrst.supabase.co/functions/v1/unsubscribe";
const SITE = "https://xysmr.github.io";

function setup(fail = false) {
  const disabled: string[] = [];
  const deps = {
    secret: SECRET,
    allowedOrigins: [SITE],
    disable: (u: string): Promise<void> => {
      if (fail) return Promise.reject(new Error("db down"));
      disabled.push(u);
      return Promise.resolve();
    },
  };
  return { deps, disabled };
}

function oneClick(u = USER, s = SIG): Request {
  // RFC 8058：信箱把 List-Unsubscribe 的網址原封不動拿來 POST
  return new Request(`${FN}?u=${u}&s=${s}`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: "List-Unsubscribe=One-Click",
  });
}

function fromPage(body: unknown): Request {
  return new Request(FN, {
    method: "POST",
    headers: { "Content-Type": "application/json", Origin: SITE },
    body: JSON.stringify(body),
  });
}

// ---------- 簽章 ----------

Deno.test("signature matches the vector shared with Python", async () => {
  assertEquals(await sign(SECRET, USER), SIG);
  assert(await verify(SECRET, USER, SIG));
  assert(await verify(SECRET, USER.toUpperCase(), SIG));
});

Deno.test("verify rejects tampering, other users and other secrets", async () => {
  assert(!(await verify(SECRET, "00000000-0000-4000-8000-000000000002", SIG)));
  assert(!(await verify(SECRET, USER, SIG.slice(0, -1) + "A")));
  assert(!(await verify(SECRET, USER, "")));
  assert(!(await verify(SECRET, USER, "!!!not-base64!!!")));
  assert(!(await verify(SECRET, "not-a-uuid", SIG)));
  assert(!(await verify("another-secret-of-sufficient-length-000", USER, SIG)));
});

// ---------- HTTP ----------

Deno.test("one-click POST from a mailbox disables that user", async () => {
  const { deps, disabled } = setup();
  const res = await handle(oneClick(), deps);
  assertEquals(res.status, 200);
  assertEquals(await res.json(), { ok: true });
  assertEquals(disabled, [USER]);
});

Deno.test("old link still works: the same link can be used again and again", async () => {
  const { deps, disabled } = setup();
  for (let i = 0; i < 3; i++) {
    assertEquals((await handle(oneClick(), deps)).status, 200);
  }
  assertEquals(disabled, [USER, USER, USER]); // 資料庫更新本身是冪等的
});

Deno.test("JSON POST from the confirm page, with CORS for the site", async () => {
  const { deps, disabled } = setup();
  const res = await handle(fromPage({ u: USER, s: SIG }), deps);
  assertEquals(res.status, 200);
  assertEquals(res.headers.get("Access-Control-Allow-Origin"), SITE);
  assertEquals(disabled, [USER]);
});

Deno.test("GET never unsubscribes (link scanners open links automatically)", async () => {
  const { deps, disabled } = setup();
  const res = await handle(new Request(`${FN}?u=${USER}&s=${SIG}`), deps);
  assertEquals(res.status, 405);
  assertEquals(res.headers.get("Allow"), "POST, OPTIONS");
  await res.body?.cancel();
  assertEquals(disabled, []);
});

Deno.test("bad or missing signature is rejected without touching the database", async () => {
  const { deps, disabled } = setup();
  for (
    const req of [
      oneClick(USER, SIG.slice(0, -1) + "A"),
      oneClick("00000000-0000-4000-8000-000000000002", SIG),
      fromPage({ u: USER }),
      fromPage("nonsense"),
      new Request(FN, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{",
      }),
    ]
  ) {
    const res = await handle(req, deps);
    assertEquals(res.status, 403);
    await res.body?.cancel();
  }
  assertEquals(disabled, []);
});

Deno.test("CORS preflight only for allowed origins", async () => {
  const { deps } = setup();
  const ok = await handle(new Request(FN, { method: "OPTIONS", headers: { Origin: SITE } }), deps);
  assertEquals(ok.status, 204);
  assertEquals(ok.headers.get("Access-Control-Allow-Origin"), SITE);
  const evil = await handle(
    new Request(FN, { method: "OPTIONS", headers: { Origin: "https://evil.example" } }),
    deps,
  );
  assertEquals(evil.headers.get("Access-Control-Allow-Origin"), null);
});

Deno.test("database failure returns 500 without details", async () => {
  const { deps } = setup(true);
  const res = await handle(oneClick(), deps);
  assertEquals(res.status, 500);
  assertEquals(await res.json(), { ok: false, error: "update_failed" });
});

Deno.test("missing secret means not configured, never 'valid'", async () => {
  const { deps, disabled } = setup();
  const res = await handle(oneClick(), { ...deps, secret: "" });
  assertEquals(res.status, 500);
  await res.body?.cancel();
  assertEquals(disabled, []);
});
