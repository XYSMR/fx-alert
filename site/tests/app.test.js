// 前端純函式的單元測試。執行：node --test site/tests/app.test.js
// 只用 Node 內建的 node:test，不需要 npm install。
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const app = require("../app.js");

const HOURS = { weekdays_only: true, start: "09:00", end: "16:30", first_check: "09:05" };
const tw = (s) => new Date(`${s}+08:00`); // 台灣時間 → Date，跟電腦時區無關
const card = (over) => ({
  code: "JPY", name: "日圓", rate: "0.2119", rate_at: "2026-11-09T16:10:12+08:00",
  target: "0.205", rearm_above: "0.206", distance_pct: 3.256,
  status: "waiting", status_since: "2026-11-09T15:46:16+08:00", ...over,
});

// ---------- 網址參數 ----------

test("dataUrl 只接受站內 .json 檔名", () => {
  assert.equal(app.dataUrl(""), "data.json");
  assert.equal(app.dataUrl("?data=samples/rebound.json"), "samples/rebound.json");
  assert.equal(app.dataUrl("?data=https://evil.example/x.json"), "data.json");
  assert.equal(app.dataUrl("?data=../secret.json"), "data.json");
});

test("previewPublic：?public=1 才模擬沒登入的公開頁面", () => {
  assert.equal(app.previewPublic("?data=samples/new.json&public=1"), true);
  assert.equal(app.previewPublic("?public=0"), false);
  assert.equal(app.previewPublic(""), false);
});

test("previewNow：?now= 指定台灣時間，格式不對就忽略", () => {
  assert.equal(app.previewNow("?now=2026-11-14T12:00").toISOString(), "2026-11-14T04:00:00.000Z");
  assert.equal(app.previewNow("?now=tomorrow"), null);
  assert.equal(app.previewNow(""), null);
});

// ---------- 時間 ----------

test("formatTime / formatDate 一律台灣時間", () => {
  assert.equal(app.formatTime("2026-11-09T16:10:12+08:00"), "11/09 16:10");
  assert.equal(app.formatTime("2026-11-09T01:07:00Z"), "11/09 09:07");
  assert.equal(app.formatTime(null), "—");
  assert.equal(app.formatDate("2026-10-14T09:05:00+08:00"), "10/14");
});

test("timeAgo", () => {
  const now = tw("2026-11-09T16:30:00");
  assert.equal(app.timeAgo("2026-11-09T16:29:40+08:00", now), "剛剛");
  assert.equal(app.timeAgo("2026-11-09T16:25:00+08:00", now), "5 分鐘前");
  assert.equal(app.timeAgo("2026-11-09T13:30:00+08:00", now), "3 小時前");
});

// ---------- 1. 還要跌多少 ----------

test("formatPct：小數 1 位四捨五入，不到 0.1% 另外寫", () => {
  assert.equal(app.formatPct(3.256), "3.3%");   // (0.2119−0.205)/0.2119
  assert.equal(app.formatPct(3.25), "3.3%");    // 剛好 .x5 也要進位
  assert.equal(app.formatPct(3.37), "3.4%");
  assert.equal(app.formatPct(4.943), "4.9%");
  assert.equal(app.formatPct(0.1), "0.1%");
  assert.equal(app.formatPct(0.099), "不到 0.1%");
  assert.equal(app.formatPct(0.001), "不到 0.1%");
});

// ---------- 2. 換算 ----------

test("rateHint：日圓寫 1 台幣換多少，韓元寫 1,000 韓元多少台幣", () => {
  assert.equal(app.rateHint("0.2119", "日圓"), "1 台幣 ≈ 4.72 日圓");
  assert.equal(app.rateHint("0.0263", "韓元"), "1,000 韓元 ≈ 26.3 台幣");
  assert.equal(app.rateHint(null, "日圓"), "");
});

// ---------- 3. 卡片用語（四種狀況） ----------

test("cardView：等待中", () => {
  const v = app.cardView(card());
  assert.equal(v.badge, "等待中");
  assert.equal(v.message, "還要再跌 3.3% 才會通知你");
  assert.ok(v.details.includes("匯率跌到 0.205 或更低時，會寄一封 Email 給你"));
  assert.ok(v.details.includes("從 11/09 15:46 開始等待"));
});

test("cardView：等待中但距離不到 0.1%", () => {
  assert.equal(app.cardView(card({ rate: "0.2051", distance_pct: 0.049 })).message, "還要再跌 不到 0.1% 才會通知你");
});

test("cardView：已低於目標、已寄信", () => {
  const v = app.cardView(card({ rate: "0.2049", distance_pct: -0.049, status: "triggered" }));
  assert.equal(v.badge, "已到價");
  assert.equal(v.tone, "good");
  assert.equal(v.message, "✓ 已低於目標，Email 已寄出");
});

test("cardView：已通知過，但回升到目標和門檻之間 → 不能說「已低於目標」", () => {
  const v = app.cardView(card({ rate: "0.2055", distance_pct: 0.243, status: "triggered" }));
  assert.equal(v.badge, "已通知");
  assert.equal(v.tone, "info");
  assert.equal(v.message, "已通知過・目前 0.2055 高於目標，回升超過 0.206 才會重新開始等待");
  assert.doesNotMatch(v.message, /已低於目標/);
});

test("cardView：剛好等於回升門檻，還是「之間」那一種", () => {
  assert.match(app.cardView(card({ rate: "0.2060", status: "triggered" })).message, /回升超過 0\.206/);
});

test("cardView：已通知過，匯率已超過門檻（等下次檢查）", () => {
  const v = app.cardView(card({ rate: "0.2070", status: "triggered" }));
  assert.equal(v.message, "已通知過・匯率已回升，下次檢查時會重新開始等待");
});

test("cardView：等待中但已達目標（還沒輪到檢查）", () => {
  const v = app.cardView(card({ rate: "0.2050", distance_pct: 0 }));
  assert.equal(v.message, "已達到目標，下次檢查時會寄 Email 給你");
});

test("卡片和詳細資訊不出現專有名詞", () => {
  const all = [
    card(), card({ rate: "0.2049", status: "triggered" }), card({ rate: "0.2055", status: "triggered" }),
    card({ rate: "0.2070", status: "triggered" }), card({ rate: null, distance_pct: null }),
  ].flatMap((c) => { const v = app.cardView(c); return [v.badge, v.message, ...v.details]; }).join(" ");
  for (const word of ["buffer", "上膛", "rearm", "armed", "triggered", "現金賣出"]) {
    assert.ok(!all.includes(word), `出現了「${word}」`);
  }
});

// ---------- 4. 頂端狀態列 ----------

test("inCheckHours：從第一次檢查 09:05 到 16:30，平日", () => {
  assert.equal(app.inCheckHours(HOURS, tw("2026-11-09T09:04:00")), false);
  assert.equal(app.inCheckHours(HOURS, tw("2026-11-09T09:05:00")), true);
  assert.equal(app.inCheckHours(HOURS, tw("2026-11-09T16:30:00")), true);
  assert.equal(app.inCheckHours(HOURS, tw("2026-11-09T16:31:00")), false);
  assert.equal(app.inCheckHours(HOURS, tw("2026-11-14T12:00:00")), false); // 週六
});

test("nextCheck：今天、明天、週一", () => {
  assert.equal(app.nextCheck(HOURS, tw("2026-11-09T08:30:00")), "今天 09:05");   // 週一早上
  assert.equal(app.nextCheck(HOURS, tw("2026-11-09T20:00:00")), "明天 09:05");   // 週一晚上
  assert.equal(app.nextCheck(HOURS, tw("2026-11-13T20:00:00")), "週一 09:05");   // 週五晚上
  assert.equal(app.nextCheck(HOURS, tw("2026-11-14T12:00:00")), "週一 09:05");   // 週六
  assert.equal(app.nextCheck(HOURS, tw("2026-11-15T12:00:00")), "明天 09:05");   // 週日 → 明天就是週一
});

test("isStale：只有檢查時段內、超過 2 小時沒更新才算", () => {
  const lc = (at) => ({ at, ok: true, errors: 0 });
  assert.equal(app.isStale(lc("2026-11-09T13:37:00+08:00"), HOURS, tw("2026-11-09T14:00:00")), false);
  assert.equal(app.isStale(lc("2026-11-09T11:37:00+08:00"), HOURS, tw("2026-11-09T14:00:00")), true);
  assert.equal(app.isStale(lc("2026-11-06T16:07:00+08:00"), HOURS, tw("2026-11-09T09:30:00")), false);
  assert.equal(app.isStale(lc("2026-11-06T16:07:00+08:00"), HOURS, tw("2026-11-09T11:30:00")), true);
  assert.equal(app.isStale(lc("2026-11-06T16:07:00+08:00"), HOURS, tw("2026-11-07T12:00:00")), false);
  assert.equal(app.isStale(lc("2026-11-09T16:07:00+08:00"), HOURS, tw("2026-11-09T22:00:00")), false);
});

test("statusLine：正常 / 過期 / 失敗 / 非檢查時段 / 沒資料", () => {
  const data = (lc) => ({ last_check: lc, business_hours: HOURS });
  const ok = { at: "2026-11-09T13:37:00+08:00", ok: true, errors: 0 };

  let s = app.statusLine(data(ok), tw("2026-11-09T14:00:00"));
  assert.equal(s.tone, "ok");
  assert.equal(s.text, "✓ 資料正常・11/09 13:37 更新（23 分鐘前）");

  s = app.statusLine(data({ ...ok, at: "2026-11-09T10:07:00+08:00" }), tw("2026-11-09T14:00:00"));
  assert.equal(s.tone, "warn");
  assert.equal(s.text, "⚠ 資料可能過期・最後更新 11/09 10:07（3 小時前）");
  assert.equal(s.link, true);

  s = app.statusLine(data({ ...ok, ok: false, errors: 1 }), tw("2026-11-09T14:00:00"));
  assert.equal(s.tone, "bad");
  assert.equal(s.text, "✕ 最近一次更新失敗・11/09 13:37");

  // 週五晚上：最後更新是 16:07，不能說過期，要說下次什麼時候更新
  s = app.statusLine(data({ ...ok, at: "2026-11-13T16:07:00+08:00" }), tw("2026-11-13T21:00:00"));
  assert.equal(s.tone, "idle");
  assert.equal(s.text, "目前非檢查時段，下次更新：週一 09:05・最後更新 11/13 16:07");
  assert.doesNotMatch(s.text, /過期/);

  // 週一早上 09:00，還沒到第一次檢查
  s = app.statusLine(data({ ...ok, at: "2026-11-13T16:07:00+08:00" }), tw("2026-11-16T09:00:00"));
  assert.equal(s.text, "目前非檢查時段，下次更新：今天 09:05・最後更新 11/13 16:07");

  assert.equal(app.statusLine(data(null), tw("2026-11-09T14:00:00")).tone, "idle");
});

// ---------- 5. 走勢圖 ----------

test("yRange：一定包含目標價（就算匯率離目標很遠）", () => {
  const r = app.yRange(["0.2160", "0.2120", "0.2119"], "0.205");
  assert.ok(r.min < 0.205 && r.max > 0.216);
  const k = app.yRange(["0.0263"], "0.0250"); // 只有一個點
  assert.ok(k.min < 0.025 && k.max > 0.0263);
  const below = app.yRange(["0.2049", "0.2045"], "0.205"); // 目標在所有匯率之上
  assert.ok(below.max > 0.205 && below.min < 0.2045);
});

test("yRange：範圍對齊整齊的刻度間距", () => {
  const r = app.yRange(["0.2165", "0.2049", "0.2055"], "0.205");
  assert.deepEqual(r, { min: 0.202, max: 0.218, step: 0.002 }); // 下限含現價 0.2055 的 −1%
  const isMultiple = (n, step) => Math.abs(n / step - Math.round(n / step)) < 1e-6;
  for (const k of [app.yRange(["0.0263", "0.0268"], "0.0250"), app.yRange(["0.2119"], "0.205")]) {
    assert.ok(isMultiple(k.min, k.step) && isMultiple(k.max, k.step), JSON.stringify(k));
  }
});

test("markerIndexes：現在、最高、最低", () => {
  const h = [["t1", "0.2100"], ["t2", "0.2165"], ["t3", "0.2089"], ["t4", "0.2119"]];
  const m = app.markerIndexes(h, { rate: "0.2165", at: "t2" }, { rate: "0.2089", at: "t3" });
  assert.deepEqual(m, { current: 3, high: 1, low: 2 });
  assert.deepEqual(app.markerIndexes(h, null, null), { current: 3, high: -1, low: -1 });
});

test("chartSummary", () => {
  const c = card({
    history: [["2026-10-14T09:05:00+08:00", "0.2165"], ["2026-11-09T16:10:12+08:00", "0.2119"]],
    high: { rate: "0.2165", at: "2026-10-14T09:05:00+08:00" },
    low: { rate: "0.2119", at: "2026-11-09T16:10:12+08:00" },
  });
  assert.equal(app.chartSummary(c), "日圓近 30 天：最高 0.2165（10/14）、最低 0.2119（11/09），現在 0.2119，你的目標 0.205，期間變動 −2.1%。");
  assert.equal(app.chartSummary(card({ history: [] })), "日圓近 30 天還沒有走勢資料。");
  // 資料不滿 30 天
  const win = app.historyWindow(PARTIAL);
  assert.match(app.chartSummary(c, win), /^日圓 11\/09 以來：/);
  assert.equal(app.chartSummary(card({ history: [] }), win), "日圓 11/09 以來還沒有走勢資料。");
});

const PARTIAL = { history_days: 30, history_since: "2026-11-09T09:05:00+08:00", generated_at: "2026-11-10T18:12:00+08:00" };

test("yRange：小波動至少顯示現價上下 1%，0.3% 的變動不會看起來像暴跌", () => {
  const rates = ["0.2114", "0.2107", "0.2116", "0.2110"]; // 最高最低只差約 0.4%
  const r = app.yRange(rates, undefined);
  assert.ok(r.min <= 0.2110 * 0.99 && r.max >= 0.2110 * 1.01, JSON.stringify(r));
  // 變動佔整個 Y 軸的比例不到三分之一
  assert.ok((0.2116 - 0.2107) / (r.max - r.min) < 1 / 3);
  // 可以指定現價（例如卡片上的最新匯率）
  const k = app.yRange(["0.0263", "0.0264"], undefined, "0.0263");
  assert.ok(k.min <= 0.0263 * 0.99 && k.max >= 0.0263 * 1.01);
  // 大波動時照資料範圍，不受 1% 影響
  const big = app.yRange(["0.2300", "0.2050"], undefined);
  assert.ok(big.min <= 0.2050 * 0.99 && big.max >= 0.2300);
});

test("formatChange / periodChange：期間變動百分比", () => {
  assert.equal(app.formatChange(-1.94), "−1.9%");
  assert.equal(app.formatChange(0.3), "+0.3%");
  assert.equal(app.formatChange(0.047), "+0.05%");
  assert.equal(app.formatChange(0), "0.00%");
  assert.equal(app.formatChange(NaN), "—");
  const h = [["2026-11-09T09:05:00+08:00", "0.2114"], ["2026-11-10T18:07:00+08:00", "0.2107"]];
  const ch = app.periodChange(h, "11/09 以來");
  assert.equal(ch.text, "11/09 以來變動 −0.3%（0.2114 → 0.2107）");
  assert.equal(app.periodChange(h).text.startsWith("近 30 天變動"), true);
  assert.equal(app.periodChange([h[0]]), null); // 只有一個點不算變動
  assert.equal(app.periodChange([]), null);
});

test("historyWindow：資料不滿 30 天時標示從哪天開始累積", () => {
  assert.deepEqual(app.historyWindow(PARTIAL), {
    partial: true, label: "11/09 以來", note: "資料從 11/09 開始累積，還不滿 30 天",
  });
  const full = app.historyWindow({ ...PARTIAL, history_since: "2026-10-05T09:05:00+08:00" });
  assert.deepEqual(full, { partial: false, label: "近 30 天", note: null });
  // 舊版 data.json 沒有這兩個欄位、或還沒有資料 → 照舊「近 30 天」
  assert.equal(app.historyWindow({ generated_at: PARTIAL.generated_at }).partial, false);
  assert.equal(app.historyWindow({ ...PARTIAL, history_since: null }).partial, false);
  assert.equal(app.historyWindow(null).label, "近 30 天");
});

test("timeTicks：依實際時間排列，刻度對齊台灣時間，不重複日期", () => {
  const t = (s) => Date.parse(`${s}+08:00`);
  // 兩天的資料：每 6 小時一個刻度，午夜寫日期、其他寫時間
  const short = app.timeTicks(t("2026-11-09T09:05:00"), t("2026-11-10T18:07:00"), 7);
  assert.equal(short.hours, 6);
  assert.deepEqual(short.ticks.map((x) => app.tickLabel(x, short.hours)),
    ["12:00", "18:00", "11/10", "06:00", "12:00", "18:00"]);
  // 30 天的資料：每個刻度是不同的日期
  const long = app.timeTicks(t("2026-10-12T09:05:00"), t("2026-11-09T16:10:00"), 7);
  const labels = long.ticks.map((x) => app.tickLabel(x, long.hours));
  assert.ok(long.ticks.length <= 7 && long.ticks.length >= 2);
  assert.equal(new Set(labels).size, labels.length);
  for (const x of long.ticks) assert.equal(app.formatTime(new Date(x).toISOString()).endsWith("00:00"), true);
  // 手機上刻度比較少
  assert.ok(app.timeTicks(t("2026-10-12T09:05:00"), t("2026-11-09T16:10:00"), 4).ticks.length <= 4);
});

test("走勢點的位置跟實際時間成正比：8 分鐘和 8 小時的間隔不一樣寬", () => {
  const data = require("../samples/new.json");
  const xs = data.currencies[0].history.map(([at]) => Date.parse(at));
  const gaps = xs.slice(1).map((x, i) => x - xs[i]);
  assert.ok(Math.min(...gaps) <= 8 * 60 * 1000);       // 範例裡有手動執行（相隔幾分鐘）
  assert.ok(Math.max(...gaps) >= 8 * 3600 * 1000);     // 也有隔一整晚
  assert.equal(app.historyWindow(data).partial, true);  // 範例資料從 11/09 開始
});

// ---------- 6. 換匯試算 ----------

test("parseAmount：接受千分位，拒絕不合理的輸入", () => {
  assert.equal(app.parseAmount("10,000"), 10000);
  assert.equal(app.parseAmount(" 5000 "), 5000);
  assert.equal(app.parseAmount("1234.5"), 1234.5);
  for (const bad of ["", "abc", "0", "-100", "1e5", "200000000", null]) {
    assert.equal(app.parseAmount(bad), null, `「${bad}」應該不合法`);
  }
});

test("converterView：還沒到目標 → 等到目標價可以多拿", () => {
  const v = app.converterView(10000, "0.2119", "0.205", "日圓");
  assert.equal(v.nowAmount, 47192);      // 10000 / 0.2119
  assert.equal(v.targetAmount, 48780);   // 10000 / 0.205
  assert.equal(v.nowLine, "約 47,192 日圓");
  assert.equal(v.verdict, "等到目標價再換，可以多拿約 1,588 日圓");
});

test("converterView：已經比目標划算 → 現在換比較多", () => {
  const v = app.converterView(10000, "0.2049", "0.205", "日圓");
  assert.ok(v.diff < 0);
  assert.equal(v.verdict, "現在的匯率已經比目標價划算，現在換可以多拿約 24 日圓"); // 48,804 − 48,780
});

test("converterView：韓元、沒有匯率、沒輸入金額", () => {
  assert.equal(app.converterView(10000, "0.0263", "0.0250", "韓元").nowLine, "約 380,228 韓元");
  assert.equal(app.converterView(10000, null, "0.205", "日圓").error, "還沒有匯率資料，暫時無法試算");
  assert.equal(app.converterView(null, "0.2119", "0.205", "日圓").error, "請輸入台幣金額（例如 10000）");
});

// ---------- 登入開關（多人版） ----------

const URL_ = "https://hvfgxplwjpnxizhlatjn.supabase.co";
const KEY_ = "sb_publishable_AbCdEf0123456789";

test("開關關閉（或沒有 config.js）時：不載入 supabase-js、不顯示登入、目標價照舊來自 data.json", () => {
  const off = { auth: null, loadSupabase: false, showLogin: false, targetsFromDataJson: true };
  assert.deepEqual(app.bootPlan(undefined), off); // config.js 不存在
  assert.deepEqual(app.bootPlan({ authEnabled: false }), off);
  // 就算有人在關閉的設定裡塞了網址和金鑰，也不會用
  assert.deepEqual(app.bootPlan({ authEnabled: false, supabaseUrl: URL_, supabaseKey: KEY_ }), off);
  assert.deepEqual(app.bootPlan({ authEnabled: "true", supabaseUrl: URL_, supabaseKey: KEY_ }), off);
});

test("開關開啟但設定不合法時也維持關閉（例如誤放 secret key）", () => {
  for (const cfg of [
    { authEnabled: true, supabaseUrl: URL_ },
    { authEnabled: true, supabaseUrl: URL_, supabaseKey: "sb_secret_AbCdEf0123456789" },
    { authEnabled: true, supabaseUrl: "https://evil.example.com", supabaseKey: KEY_ },
  ]) {
    assert.equal(app.bootPlan(cfg).loadSupabase, false, JSON.stringify(cfg));
  }
});

test("開關開啟：載入 supabase-js、顯示登入，目標價改從 Supabase 讀", () => {
  assert.deepEqual(app.bootPlan({ authEnabled: true, supabaseUrl: URL_, supabaseKey: KEY_ }), {
    auth: { url: URL_, key: KEY_ },
    loadSupabase: true,
    showLogin: true,
    targetsFromDataJson: false,
  });
});

test("publicCurrencies：沒登入時拿掉所有個人設定，只留行情", () => {
  const [c] = app.publicCurrencies([card({ history: [], high: null, low: null })]);
  for (const k of ["target", "buffer", "rearm_above", "distance_pct", "status", "status_since"]) {
    assert.equal(k in c, false, k);
  }
  assert.equal(c.rate, "0.2119");
});

test("沒有目標價：卡片只顯示行情，試算只有「現在換」，走勢摘要不提目標", () => {
  const [c] = app.publicCurrencies([card({ history: [] })]);
  const v = app.cardView(c);
  assert.equal(v.badge, null);
  assert.equal(v.message, null);
  const conv = app.converterView(10000, "0.2119", undefined, "日圓");
  assert.equal(conv.nowLine, "約 47,192 日圓");
  assert.equal(conv.targetLine, null);
  assert.equal(conv.targetHint, null); // 沒有提示 → 「等到目標價」整行隱藏
  // 多人版沒登入：那一行改顯示提示，不會只有標題沒有數字
  const pub = app.converterView(10000, "0.2119", undefined, "日圓", "登入後可以比較目標價");
  assert.equal(pub.targetLine, null);
  assert.equal(pub.targetHint, "登入後可以比較目標價");
  // 有目標價時不顯示提示
  assert.equal(app.converterView(10000, "0.2119", "0.205", "日圓", "登入後可以比較目標價").targetHint, null);
  assert.doesNotMatch(app.chartSummary({ ...c, history: [["t", "0.2119"]] }), /目標/);
  const r = app.yRange([0.21, 0.212], undefined);
  assert.ok(r.min <= 0.21 && r.max >= 0.212);
});
