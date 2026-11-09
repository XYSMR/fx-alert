// 前端純函式的單元測試。執行：node --test site/tests/
// 只用 Node 內建的 node:test，不需要 npm install。
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const app = require("../app.js");

const HOURS = { weekdays_only: true, start: "09:00", end: "16:30" };
// 台灣時間 → Date（用 +08:00 建立，跟電腦時區無關）
const tw = (s) => new Date(`${s}+08:00`);

test("dataUrl 只接受站內 .json 檔名", () => {
  assert.equal(app.dataUrl(""), "data.json");
  assert.equal(app.dataUrl("?data=samples/triggered.json"), "samples/triggered.json");
  assert.equal(app.dataUrl("?data=https://evil.example/x.json"), "data.json");
  assert.equal(app.dataUrl("?data=../secret.json"), "data.json");
  assert.equal(app.dataUrl("?data=x.js"), "data.json");
});

test("formatTime 一律顯示台灣時間", () => {
  assert.equal(app.formatTime("2026-11-09T16:10:12+08:00"), "11/09 16:10");
  assert.equal(app.formatTime("2026-11-09T01:07:00Z"), "11/09 09:07"); // UTC → 台灣
  assert.equal(app.formatTime(null), "—");
  assert.equal(app.formatTime("not a date"), "—");
});

test("timeAgo", () => {
  const now = tw("2026-11-09T16:30:00");
  assert.equal(app.timeAgo("2026-11-09T16:29:40+08:00", now), "剛剛");
  assert.equal(app.timeAgo("2026-11-09T16:25:00+08:00", now), "5 分鐘前");
  assert.equal(app.timeAgo("2026-11-09T13:30:00+08:00", now), "3 小時前");
  assert.equal(app.timeAgo("2026-11-07T16:30:00+08:00", now), "2 天前");
});

test("distanceText：正數還要跌、0 剛好、負數已低於", () => {
  assert.equal(app.distanceText(3.41), "還要跌 3.41%");
  assert.equal(app.distanceText(5.2), "還要跌 5.20%");
  assert.equal(app.distanceText(0), "剛好等於目標");
  assert.equal(app.distanceText(-0.05), "已低於目標 0.05%");
  assert.equal(app.distanceText(null), "尚無匯率資料");
});

test("isStale：時段內超過 2 小時沒檢查才算過期", () => {
  const lc = (at) => ({ at, ok: true, errors: 0 });
  // 週一 14:00，最後檢查 13:37 → 正常
  assert.equal(app.isStale(lc("2026-11-09T13:37:00+08:00"), HOURS, tw("2026-11-09T14:00:00")), false);
  // 週一 14:00，最後檢查 11:37 → 過期
  assert.equal(app.isStale(lc("2026-11-09T11:37:00+08:00"), HOURS, tw("2026-11-09T14:00:00")), true);
  // 週一 09:30，最後檢查是上週五 → 剛開盤 30 分鐘，不誤報
  assert.equal(app.isStale(lc("2026-11-06T16:07:00+08:00"), HOURS, tw("2026-11-09T09:30:00")), false);
  // 週一 11:30，最後檢查還是上週五 → 開盤 2.5 小時都沒跑，過期
  assert.equal(app.isStale(lc("2026-11-06T16:07:00+08:00"), HOURS, tw("2026-11-09T11:30:00")), true);
  // 週六、晚上：沒有新資料是正常的
  assert.equal(app.isStale(lc("2026-11-06T16:07:00+08:00"), HOURS, tw("2026-11-07T12:00:00")), false);
  assert.equal(app.isStale(lc("2026-11-09T16:07:00+08:00"), HOURS, tw("2026-11-09T22:00:00")), false);
  // 從來沒跑過，而且已經開盤很久 → 過期
  assert.equal(app.isStale(null, HOURS, tw("2026-11-09T12:00:00")), true);
  // 沒有時段資訊（舊的 data.json）→ 不判斷
  assert.equal(app.isStale(lc("2026-11-01T00:00:00+08:00"), undefined, tw("2026-11-09T12:00:00")), false);
});

test("bannerText：錯誤優先，其次過期，否則不顯示", () => {
  const now = tw("2026-11-09T14:00:00");
  const base = { business_hours: HOURS };
  assert.equal(
    app.bannerText({ ...base, last_check: { at: "2026-11-09T13:37:00+08:00", ok: true, errors: 0 } }, now),
    null,
  );
  assert.match(
    app.bannerText({ ...base, last_check: { at: "2026-11-09T13:37:00+08:00", ok: false, errors: 2 } }, now),
    /2 個錯誤/,
  );
  assert.match(
    app.bannerText({ ...base, last_check: { at: "2026-11-09T10:07:00+08:00", ok: true, errors: 0 } }, now),
    /資料可能過期：最後一次檢查是 3 小時前/,
  );
});

test("chartSummary", () => {
  const c = {
    code: "JPY", rate: "0.2049", target: "0.205",
    history: [["2026-11-01T09:05:00+08:00", "0.2150"], ["2026-11-09T16:10:12+08:00", "0.2049"]],
  };
  assert.equal(
    app.chartSummary(c),
    "JPY 近 30 天：最高 0.2150、最低 0.2049、最新 0.2049，目標 ≤ 0.205（共 2 份牌告）。",
  );
  assert.equal(app.chartSummary({ code: "KRW", history: [] }), "KRW 近 30 天還沒有走勢資料。");
});
