// node --test site/tests/*.test.js
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { parseLink, resultView } = require("../unsubscribe.js");

const U = "00000000-0000-4000-8000-000000000001";
const S = "AjoUefqQcmpwV0VAbKi-BoBMszBZ6bicHXfBpHuWxDQ";
const P = "abcdefghijklmnopqrst";

test("parseLink reads a valid link from the email", () => {
  assert.deepEqual(parseLink(`?u=${U}&s=${S}&p=${P}`), {
    u: U,
    s: S,
    endpoint: `https://${P}.supabase.co/functions/v1/unsubscribe`,
  });
});

test("parseLink lower-cases the user id", () => {
  assert.equal(parseLink(`?u=${U.toUpperCase()}&s=${S}&p=${P}`).u, U);
});

test("parseLink rejects incomplete or tampered links", () => {
  for (const search of [
    "",
    `?u=${U}&s=${S}`, // 沒有專案代號
    `?u=${U}&p=${P}`, // 沒有簽章
    `?u=not-a-uuid&s=${S}&p=${P}`,
    `?u=${U}&s=${S.slice(1)}&p=${P}`,
    `?u=${U}&s=${S}&p=evil.example.com%2F`, // 不能把請求導到任意網址
    `?u=${U}&s=${S}&p=ABCDEFGHIJKLMNOPQRST`,
  ]) {
    assert.equal(parseLink(search), null, search);
  }
});

test("resultView", () => {
  assert.equal(resultView(200).tone, "ok");
  assert.match(resultView(200).text, /我的提醒/);
  assert.equal(resultView(403).title, "這個退訂連結無效");
  assert.equal(resultView(500).title, "暫時無法處理");
  assert.equal(resultView(0).title, "暫時無法處理"); // 網路錯誤
});
