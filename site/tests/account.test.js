// node --test site/tests/*.test.js
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const a = require("../account.js");
const app = require("../app.js");

const M = (s) => a.parseDec(s);

// ---------- 小數 ----------

test("parseDec / formatDec 用整數算，沒有浮點誤差", () => {
  assert.equal(M("0.205"), 205000n);
  assert.equal(M("0.000001"), 1n);
  assert.equal(M("12"), 12000000n);
  for (const bad of ["", "abc", "0.1234567", "-1", "1e-3", "0,205", ".5"]) assert.equal(M(bad), null, bad);
  assert.equal(a.formatDec(205000n), "0.205");
  assert.equal(a.formatDec(2000000n), "2");
  assert.equal(a.formatDec(206000n, 4), "0.2060");
  assert.equal(a.formatDec(M("0.1") + M("0.2")), "0.3");
});

test("decimalsOf 依銀行報價的位數，沒有就當 4 位", () => {
  assert.equal(a.decimalsOf("0.2120"), 4);
  assert.equal(a.decimalsOf("0.0263"), 4);
  assert.equal(a.decimalsOf("0.21"), 2);
  assert.equal(a.decimalsOf(null), 4);
});

// ---------- 回升門檻 ----------

test("沒填回升門檻 = 目標價的 0.5%（四捨五入到 6 位，和資料庫一樣）", () => {
  assert.equal(a.autoBuffer(M("0.205")), M("0.001025"));
  assert.equal(a.autoBuffer(M("0.025")), M("0.000125"));
  assert.equal(a.autoBuffer(M("0.0001")), 1n); // 0.0000005 → 0.000001
});

test("進階設定的白話說明：報價位數 + 比門檻大的最小報價", () => {
  // 排程的規則是「匯率 > 目標 + 門檻」，0.2060 剛好等於門檻不算，所以是 0.2061
  assert.equal(a.rearmText(M("0.205"), M("0.001"), 4), "匯率回升到 0.2061 以上，才會再提醒你");
  assert.equal(a.rearmText(M("0.205"), null, 4), "匯率回升到 0.2061 以上，才會再提醒你"); // 0.206025
  assert.equal(a.rearmText(M("0.025"), M("0.0002"), 4), "匯率回升到 0.0253 以上，才會再提醒你");
  assert.equal(a.rearmQuote(M("0.2"), M("0"), 4), "0.2001");
});

// ---------- 輸入檢查 ----------

test("validateTarget", () => {
  assert.deepEqual(a.validateTarget(" 0.2050 "), { micros: 205000n, value: "0.205" });
  assert.match(a.validateTarget("0").error, /大於 0/);
  assert.match(a.validateTarget("abc").error, /請輸入數字/);
  assert.match(a.validateTarget("0.1234567").error, /最多 6 位小數/);
  assert.match(a.validateTarget("1000").error, /太大/);
});

test("validateBuffer：空白 = 自動；最多目標價的 10%", () => {
  assert.deepEqual(a.validateBuffer("", M("0.205")), { micros: null, value: null });
  assert.deepEqual(a.validateBuffer("0.002", M("0.205")), { micros: 2000n, value: "0.002" });
  assert.deepEqual(a.validateBuffer("0.0205", M("0.205")), { micros: 20500n, value: "0.0205" });
  assert.match(a.validateBuffer("0.0206", M("0.205")).error, /10%（0.0205）/);
  assert.match(a.validateBuffer("x", M("0.205")).error, /請輸入數字/);
});

test("目標價和現價差超過 30% 要再確認（防止打錯小數點）", () => {
  assert.equal(a.farFromRateMessage("0.205", "0.2120"), null);
  assert.equal(a.farFromRateMessage("0.15", "0.2120"), null); // 差 29%
  assert.match(a.farFromRateMessage("0.0205", "0.2120"), /差了 90%。確定沒有打錯小數點嗎？/);
  assert.match(a.farFromRateMessage("2.05", "0.2120"), /差了 867%/);
  assert.equal(a.farFromRateMessage("0.0205", null), null); // 沒有現價就不擋
});

test("新增提醒的幣別選單只列出還沒設定過的", () => {
  const all = [{ code: "JPY", name: "日圓" }, { code: "KRW", name: "韓元" }];
  assert.deepEqual(a.availableCurrencies(all, []).map((c) => c.code), ["JPY", "KRW"]);
  assert.deepEqual(a.availableCurrencies(all, [{ currency: "JPY" }]), [{ code: "KRW", name: "韓元" }]);
  assert.deepEqual(a.availableCurrencies(all, [{ currency: "JPY" }, { currency: "KRW" }]), []);
});

// ---------- 卡片資料 ----------

const PUBLIC = [
  { code: "JPY", name: "日圓", rate: "0.2120", rate_at: "2026-11-09T10:05:00+08:00", history: [] },
  { code: "KRW", name: "韓元", rate: "0.0241", rate_at: "2026-11-09T10:05:00+08:00", history: [] },
];
const RULE = {
  id: 1, currency: "JPY", target: "0.205000", buffer: null, effective_buffer: "0.001025",
  enabled: true, state: "armed", state_changed_at: "2026-11-09T09:07:00+08:00",
};

test("personalCurrencies：只有設定過的幣別有目標，用語和介面改版一致", () => {
  const [jpy, krw] = a.personalCurrencies(PUBLIC, [RULE], { email_enabled: true });
  assert.equal(jpy.target, "0.205"); // 不顯示 0.205000
  assert.equal(jpy.rearm_above, "0.206025");
  assert.equal(jpy.distance_pct, 3.302);
  assert.equal(jpy.status, "waiting");
  assert.equal(krw.target, undefined);
  const v = app.cardView(jpy);
  assert.equal(v.message, "還要再跌 3.3% 才會通知你");
});

test("personalCurrencies：已通知、停用、Email 關閉", () => {
  const trig = a.personalCurrencies(PUBLIC, [{ ...RULE, state: "triggered" }], { email_enabled: true })[0];
  assert.equal(trig.status, "triggered");
  const off = a.personalCurrencies(PUBLIC, [{ ...RULE, enabled: false }], { email_enabled: true })[0];
  assert.equal(app.cardView(off).badge, "已停用");
  const mute = a.personalCurrencies(PUBLIC, [RULE], { email_enabled: false })[0];
  assert.equal(app.cardView(mute).badge, "通知已關閉");
  assert.equal(a.ruleStatusText(RULE, { email_enabled: false }), "Email 通知已關閉");
  assert.equal(a.ruleStatusText({ ...RULE, state: "triggered" }, { email_enabled: true }),
    "已通知，等匯率回升後重新開始");
});

test("錯誤訊息不顯示技術細節", () => {
  assert.equal(a.errorMessage({ code: "23505", message: "duplicate key ..." }), "這個幣別已經有提醒了");
  assert.match(a.errorMessage({ code: "23514" }), /數值不符合規則/);
  assert.equal(a.errorMessage({ code: "XX000", message: "internal" }), "儲存失敗，請稍後再試");
  assert.equal(a.errorMessage(null), "儲存失敗，請稍後再試");
});

// ---------- 刪除帳號 ----------

// 和 Edge Function 的測試讀同一份案例，保證前後端比對規則一致
const CASES = require("../../supabase/functions/delete-account/confirm_cases.json");

test("確認文字：和 Edge Function 共用同一份案例", () => {
  assert.equal(a.CONFIRM_TEXT, CASES.text);
  assert.ok(CASES.cases.length >= 10);
  for (const c of CASES.cases) {
    assert.equal(a.isConfirmed(c.input), c.ok, JSON.stringify(c.input));
  }
  assert.equal(a.normalizeConfirm(42), "");
  assert.equal(a.isConfirmed(undefined), false);
});

test("刪除失敗的提示：登入太久要重新登入", () => {
  assert.equal(a.deleteErrorMessage("reauth_required"), "為了安全，請重新登入後再刪除");
  assert.equal(a.deleteErrorMessage("confirm_mismatch"), "請輸入「刪除我的帳號」");
  assert.equal(a.deleteErrorMessage("not_logged_in"), "登入已失效，請重新登入");
  assert.equal(a.deleteErrorMessage(null), "刪除失敗，請稍後再試");
});

// ---------- 另一台裝置刪除了帳號 ----------

test("isAccountGone：讀不到自己的 profile、或寫入時外鍵錯誤，代表帳號已不存在", () => {
  assert.equal(a.isAccountGone({ data: null, error: null }), true); // RLS 下只看得到自己 → 空的
  assert.equal(a.isAccountGone({ data: { email_enabled: true }, error: null }), false);
  assert.equal(a.isAccountGone({ data: null, error: { code: "23503" } }), true); // 新增規則時
  assert.equal(a.isAccountGone({ data: null, error: { code: "08006" } }), false); // 網路問題不算
  assert.equal(a.isAccountGone(null), false);
  assert.equal(a.errorMessage({ code: "23503" }), "帳號已不存在，請重新登入");
});
