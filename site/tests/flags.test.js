// node --test site/tests/*.test.js
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const f = require("../flags.js");

/** 假的 supabase client：rpc('is_feature_on') 依 answers 回應 */
function fakeClient(answers) {
  const calls = [];
  return {
    calls,
    async rpc(name, args) {
      calls.push([name, args]);
      const a = answers[args.p_flag];
      if (a instanceof Error) throw a;
      return a;
    },
  };
}

test("兩個 flag 都問資料庫的 is_feature_on（前端不自己算分桶）", async () => {
  const client = fakeClient({
    trip_plan: { data: true, error: null },
    quick_target: { data: false, error: null },
  });
  assert.deepEqual(await f.fetchFlags(client), { trip_plan: true, quick_target: false });
  const byFlag = (a, b) => (a[1].p_flag < b[1].p_flag ? -1 : 1);
  assert.deepEqual([...client.calls].sort(byFlag), [
    ["is_feature_on", { p_flag: "quick_target" }],
    ["is_feature_on", { p_flag: "trip_plan" }],
  ]);
});

test("只有 data === true 才算開啟", async () => {
  for (const data of [null, undefined, "true", 1, false]) {
    const client = fakeClient({ trip_plan: { data, error: null }, quick_target: { data, error: null } });
    assert.deepEqual(await f.fetchFlags(client), { trip_plan: false, quick_target: false }, String(data));
  }
});

test("問不到：已上線的 trip_plan 維持開啟，新功能 quick_target 關閉", async () => {
  const missing = { data: null, error: { code: "PGRST202", message: "function not found" } };
  assert.deepEqual(await f.fetchFlags(fakeClient({ trip_plan: missing, quick_target: missing })),
    { trip_plan: true, quick_target: false });
  const offline = new Error("Failed to fetch");
  assert.deepEqual(await f.fetchFlags(fakeClient({ trip_plan: offline, quick_target: offline })),
    { trip_plan: true, quick_target: false });
  assert.deepEqual(f.FALLBACK, { trip_plan: true, quick_target: false });
});

test("沒登入／登出：全部關閉", () => {
  assert.deepEqual(f.allOff(), { trip_plan: false, quick_target: false });
});

test("結果寫在 <html data-flag-*>", () => {
  const attrs = {};
  const root = { setAttribute: (k, v) => { attrs[k] = v; } };
  f.applyTo(root, { trip_plan: true, quick_target: false });
  assert.deepEqual(attrs, { "data-flag-trip_plan": "on", "data-flag-quick_target": "off" });
});

test("style.css 對每個 flag 都有隱藏規則，而且預設（不是 on）就隱藏", () => {
  const css = fs.readFileSync(path.join(__dirname, "../style.css"), "utf8");
  for (const flag of f.FLAGS) {
    assert.ok(css.includes(`html:not([data-flag-${flag}="on"]) [data-flag="${flag}"]`), flag);
  }
});

test("瀏覽器裡只往全域放 FxFlags", () => {
  const vm = require("node:vm");
  const src = fs.readFileSync(path.join(__dirname, "../flags.js"), "utf8");
  const sandbox = { document: {} };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  const before = new Set(Object.keys(sandbox));
  vm.runInContext(src, sandbox);
  const added = Object.keys(sandbox).filter((k) => !before.has(k));
  assert.deepEqual(added, ["FxFlags"]);
  assert.deepEqual(Object.keys(sandbox.FxFlags).sort(), ["clear", "isOn", "load"]);
});
