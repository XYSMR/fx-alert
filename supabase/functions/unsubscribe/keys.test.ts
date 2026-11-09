// deno test supabase/functions/（delete-account/keys.ts 是同一份副本）
import { publishableKey, secretKey, supabaseUrl } from "./keys.ts";

function assertEquals(actual: unknown, expected: unknown): void {
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  if (a !== e) throw new Error(`expected ${e}, got ${a}`);
}

const env = (vars: Record<string, string>) => (name: string) => vars[name];

Deno.test("secret key 從 FX_SECRET_KEY 讀，去掉前後空白", () => {
  assertEquals(secretKey(env({ FX_SECRET_KEY: " sb_secret_abc \n" })), "sb_secret_abc");
});

Deno.test("secret key 缺少、空的、或不是 sb_secret_ 開頭 → null", () => {
  assertEquals(secretKey(env({})), null);
  assertEquals(secretKey(env({ FX_SECRET_KEY: "" })), null);
  assertEquals(secretKey(env({ FX_SECRET_KEY: "sb_secret_" })), null);
  assertEquals(secretKey(env({ FX_SECRET_KEY: "sb_publishable_abc" })), null);
  assertEquals(secretKey(env({ FX_SECRET_KEY: "eyJhbGciOiJIUzI1NiJ9.x.y" })), null); // 舊版 JWT
});

Deno.test("不會退回去讀舊版的 SUPABASE_SERVICE_ROLE_KEY", () => {
  assertEquals(secretKey(env({ SUPABASE_SERVICE_ROLE_KEY: "eyJ.x.y" })), null);
  assertEquals(publishableKey(env({ SUPABASE_ANON_KEY: "eyJ.x.y" })), null);
});

Deno.test("publishable key 取 SUPABASE_PUBLISHABLE_KEYS 的 default", () => {
  const raw = JSON.stringify({ default: "sb_publishable_abc", other: "sb_publishable_xyz" });
  assertEquals(publishableKey(env({ SUPABASE_PUBLISHABLE_KEYS: raw })), "sb_publishable_abc");
});

Deno.test("publishable key：JSON 壞掉、沒有 default、格式不對 → null", () => {
  assertEquals(publishableKey(env({})), null);
  assertEquals(publishableKey(env({ SUPABASE_PUBLISHABLE_KEYS: "{not json" })), null);
  assertEquals(publishableKey(env({ SUPABASE_PUBLISHABLE_KEYS: "null" })), null);
  assertEquals(publishableKey(env({ SUPABASE_PUBLISHABLE_KEYS: '"sb_publishable_abc"' })), null);
  assertEquals(
    publishableKey(
      env({ SUPABASE_PUBLISHABLE_KEYS: JSON.stringify({ other: "sb_publishable_a" }) }),
    ),
    null,
  );
  assertEquals(
    publishableKey(env({ SUPABASE_PUBLISHABLE_KEYS: JSON.stringify({ default: "sb_secret_a" }) })),
    null,
  );
});

Deno.test("SUPABASE_URL 去掉結尾斜線；缺少就是 null", () => {
  assertEquals(
    supabaseUrl(env({ SUPABASE_URL: "https://abcdefghijklmnopqrst.supabase.co/" })),
    "https://abcdefghijklmnopqrst.supabase.co",
  );
  assertEquals(supabaseUrl(env({})), null);
});
