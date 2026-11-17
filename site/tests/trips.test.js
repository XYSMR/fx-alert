// node --test site/tests/*.test.js
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const t = require("../trips.js");

const FIXTURE = JSON.parse(fs.readFileSync(
  path.join(__dirname, "../../tests/fixtures/trip_schedules.json"), "utf8"));
const CAL = t.makeCalendar(FIXTURE.calendar);

// ---------- 提醒日期：和 Python 的 trip_rules.schedule 逐日一致 ----------

test("schedule 和 Python 算的完全一樣（2026-11-17～2027-12-31 每天出發）", () => {
  assert.ok(FIXTURE.cases.length > 400);
  for (const [dep, near, fin, last] of FIXTURE.cases) {
    const s = t.schedule(dep, CAL);
    assert.deepEqual([s.nearOn, s.finalOn, s.lastCall], [near, fin, last], dep);
  }
});

test("週末、假日往前移；春節長假時最後提醒提早", () => {
  const s = t.schedule("2027-01-17", CAL); // 週日出發
  assert.deepEqual([s.nearOn, s.finalOn, s.lastCall, s.spare],
    ["2026-12-24", "2027-01-08", "2027-01-14", "2027-01-15"]);
  const ny = t.schedule("2027-02-11", CAL);
  assert.deepEqual([ny.finalOn, ny.lastCall, ny.spare], ["2027-02-01", "2027-02-02", "2027-02-03"]);
});

test("營業日：颱風停班、補假不算", () => {
  assert.equal(CAL.isBusinessDay("2026-10-23"), false);
  assert.equal(CAL.isBusinessDay("2026-10-22"), true);
  assert.equal(CAL.isBusinessDay("2027-02-09"), false);
  assert.equal(t.makeCalendar({ workday: ["2027-03-06"] }).isBusinessDay("2027-03-06"), true);
});

test("phase 和 phaseText", () => {
  const s = t.schedule("2027-01-17", CAL);
  assert.equal(t.phase(s, "2026-11-20"), "far");
  assert.equal(t.phase(s, "2026-12-24"), "near");
  assert.equal(t.phase(s, "2027-01-08"), "final");
  assert.equal(t.phase(s, "2027-01-17"), "departed");
  assert.match(t.phaseText(s, "2026-11-20"), /^距離出發還有 58 天。.*前 20% 低.*12\/24（四） 起/);
  assert.match(t.phaseText(s, "2027-01-04"), /前 40% 低.*1\/8（五） 寄最後換匯提醒/);
  assert.match(t.phaseText(s, "2027-01-10"), /最後階段.*1\/14（四）/);
});

test("milestones：已過、今天、建立時已過的不寄", () => {
  const s = t.schedule("2027-01-17", CAL);
  const m = t.milestones(s, "2027-01-08", "2026-11-16");
  assert.deepEqual(m.map((x) => x.state), ["done", "today", "next"]);
  const late = t.milestones(s, "2027-01-12", "2027-01-11");
  assert.deepEqual(late.map((x) => x.state), ["skipped", "skipped", "next"]);
  assert.match(m[2].text, /1\/15（五） 是出發前最後一個營業日/);
});

test("日期工具", () => {
  assert.equal(t.addDays("2026-12-31", 1), "2027-01-01");
  assert.equal(t.daysBetween("2026-11-16", "2027-01-17"), 62);
  assert.equal(t.taipeiToday(new Date("2026-11-16T16:30:00Z")), "2026-11-17"); // 台灣 00:30
  assert.equal(t.taipeiToday(new Date("2026-11-16T15:59:00Z")), "2026-11-16");
  assert.equal(t.shortDate("2027-01-17"), "1/17（日）");
});

// ---------- 金額 ----------

test("parseAmount / formatAmount", () => {
  assert.equal(t.parseAmount("50000"), 5000000n);
  assert.equal(t.parseAmount("50,000.5"), 5000050n);
  for (const bad of ["", "abc", "1.234", "-1", "1e5"]) assert.equal(t.parseAmount(bad), null, bad);
  assert.equal(t.formatAmount(5000050n), "50,000.5");
  assert.equal(t.formatAmount(5000000000n), "50,000,000");
});

test("validateAmount / validateRate / validateName", () => {
  assert.deepEqual(t.validateAmount("50000"), { cents: 5000000n, value: "50000.00" });
  assert.match(t.validateAmount("0").error, /大於 0/);
  assert.match(t.validateAmount("1000000000000").error, /太大/);
  assert.match(t.validateAmount("abc", "換了多少").error, /^換了多少請輸入數字/);
  assert.deepEqual(t.validateRate(""), { value: null });
  assert.deepEqual(t.validateRate("0.2061"), { value: "0.2061" });
  for (const bad of ["0", "0.12345", "1000", "x"]) assert.ok(t.validateRate(bad).error, bad);
  assert.deepEqual(t.validateName("  "), { value: null });
  assert.ok(t.validateName("一".repeat(31)).error);
  assert.deepEqual(t.validateName("東京"), { value: "東京" });
});

test("validateDeparture：明天到一年內", () => {
  const today = "2026-11-16";
  assert.ok(t.validateDeparture("2026-11-16", today).error);
  assert.ok(t.validateDeparture("", today).error);
  assert.deepEqual(t.validateDeparture("2026-11-17", today), { value: "2026-11-17" });
  assert.deepEqual(t.validateDeparture("2027-11-16", today), { value: "2027-11-16" });
  assert.ok(t.validateDeparture("2027-11-17", today).error);
});

test("progress：已換、剩餘、超過也是 0", () => {
  assert.deepEqual(t.progress({ amount: "50000.00", exchanged: "20000" }),
    { amount: 5000000n, exchanged: 2000000n, remaining: 3000000n, pct: 40, done: false });
  const over = t.progress({ amount: "1000", exchanged: "1500" });
  assert.equal(over.remaining, 0n);
  assert.equal(over.pct, 100);
  assert.equal(over.done, true);
});

test("estimateTwd：剩下的外幣 × 現價，四捨五入到元", () => {
  assert.equal(t.estimateTwd(3000000n, "0.2099"), "6,297");
  assert.equal(t.estimateTwd(100000000n, "0.0235"), "23,500"); // 100 萬韓元
  assert.equal(t.estimateTwd(100n, null), null);
});

// ---------- 價格位置 ----------

test("rankText 只描述事實；資料不足時不比較", () => {
  assert.equal(t.rankText({ days: 19, lower: 7, enough: true }, "0.2099"),
    "現在 0.2099，在近 30 天（19 個營業日的收盤價）中是第 8 低。");
  assert.match(t.rankText({ days: 10, lower: 0, enough: false }, "0.2099"), /資料還不夠，先不比較高低/);
  assert.equal(t.rankText(null, null), "目前沒有報價。");
});

test("錯誤訊息", () => {
  assert.match(t.tripErrorMessage({ code: "23514", message: "每人最多 5 趟還沒出發的旅程" }), /最多 5 趟/);
  assert.match(t.tripErrorMessage({ code: "23514", message: "每趟旅程最多 50 筆換匯紀錄" }), /50 筆/);
  assert.match(t.tripErrorMessage({ code: "23514", message: "出發日要在明天到一年內" }), /一年內/);
  assert.equal(t.tripErrorMessage({ code: "42501" }), "沒有權限，請重新登入");
  assert.equal(t.tablesMissing({ code: "PGRST205" }), true);
  assert.equal(t.tablesMissing({ code: "42501" }), false);
});

test("旅程數和排序：已出發的不算上限、放在後面", () => {
  const trips = [{ departure: "2026-11-10" }, { departure: "2027-02-01" }, { departure: "2026-12-01" }];
  assert.equal(t.upcomingCount(trips, "2026-11-16"), 2);
  assert.deepEqual(t.sortTrips(trips, "2026-11-16").map((x) => x.departure),
    ["2026-12-01", "2027-02-01", "2026-11-10"]);
});

// ---------- 用語：不能讓人覺得是投資建議 ----------

const FORBIDDEN = ["建議", "應該", "最佳", "推薦", "划算", "賺", "虧", "會漲", "會跌", "預測", "預估",
  "投資", "時機", "趁", "必", "保證會", "穩", "撿便宜", "抄底"];

test("trips.js 沒有投資建議的用語，並附上免責說明", () => {
  const src = fs.readFileSync(path.join(__dirname, "../trips.js"), "utf8");
  for (const w of FORBIDDEN) assert.ok(!src.includes(w), `出現「${w}」`);
  assert.equal(t.TRIP_DISCLAIMER, "僅供參考，不保證未來匯率走勢");
  assert.ok(src.includes("TRIP_DISCLAIMER}"), "畫面要顯示免責說明");
  for (const label of Object.values(t.KIND_LABELS)) {
    for (const w of FORBIDDEN) assert.ok(!label.includes(w), label);
  }
});

test("瀏覽器裡只往全域放 FxTrips（不會和 app.js 的 parseAmount 等互相覆蓋）", () => {
  const vm = require("node:vm");
  const src = fs.readFileSync(path.join(__dirname, "../trips.js"), "utf8");
  const sandbox = { document: {} };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  const before = new Set(Object.keys(sandbox));
  vm.runInContext(src, sandbox);
  const added = Object.keys(sandbox).filter((k) => !before.has(k));
  assert.deepEqual(added, ["FxTrips"]);
  assert.deepEqual(Object.keys(sandbox.FxTrips).sort(), ["hide", "show"]);
});

// ---------- 建立前的預覽：反映實際規則（今天建立會不會寄） ----------

test("previewSteps：提醒日當天建立 = 今天會寄；早於今天 = 已過，不會寄", () => {
  const s = t.schedule("2026-11-24", CAL); // 分批 11/03、最後 11/17、出發前 11/20
  assert.deepEqual(t.previewSteps(s, "2026-11-17").map((x) => [x.date, x.state]),
    [["2026-11-03", "past"], ["2026-11-17", "today"], ["2026-11-20", "next"]]);
  assert.deepEqual(t.previewSteps(s, "2026-11-18").map((x) => x.state), ["past", "past", "next"]);
  // 出發前提醒要在前一天以前建立：就是今天也不會寄
  assert.deepEqual(t.previewSteps(s, "2026-11-20").map((x) => x.state), ["past", "past", "past"]);
  assert.deepEqual(t.previewSteps(s, "2026-11-01").map((x) => x.state), ["next", "next", "next"]);
});

test("previewLines：畫面上的文字", () => {
  const s = t.schedule("2026-11-24", CAL);
  assert.deepEqual(t.previewLines(s, "2026-11-17"), [
    "建立後會在這些時候寄信（都避開週末和國定假日，只在銀行營業日寄）：",
    "11/3（二） 可以考慮分批換的提醒（已過，不會寄）",
    "11/17（二） 最後換匯提醒（今天）",
    "11/20（五） 出發前提醒",
    "已在最後階段：不寄價格提醒，只寄上面的固定提醒。",
  ]);
  const far = t.schedule("2027-01-17", CAL);
  assert.match(t.previewPriceNote(far, "2026-11-17"), /前 20% 低.*建立當天就可能寄；12\/24（四） 起改成前 40% 低/);
  assert.match(t.previewPriceNote(far, "2027-01-04"), /已在接近出發的階段.*前 40% 低.*建立當天就可能寄/);
});

test("預覽和 Python 的規則一致：fixture 裡每個出發日，今天建立時「今天」的那一封就是 decide 會寄的", () => {
  // Python 的規則：分批、最後是 created_on <= 提醒日；出發前是 created_on < 提醒日
  for (const [dep] of FIXTURE.cases.slice(0, 60)) {
    const s = t.schedule(dep, CAL);
    for (const today of [s.nearOn, s.finalOn, s.lastCall]) {
      const steps = t.previewSteps(s, today);
      assert.equal(steps[0].state === "today", today === s.nearOn && s.nearOn !== s.finalOn, dep);
      assert.equal(steps[2].state, today >= s.lastCall ? "past" : "next", dep);
    }
  }
});
