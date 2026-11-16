// 多人版：Google 登入、我的提醒、我的通知紀錄。
// - 只有 config.js 的 authEnabled 為 true 時，app.js 才會呼叫 FxAccount.start()；
//   這時才動態載入 vendor/supabase.umd.js。關閉時這個檔案什麼都不做。
// - 前端只有 publishable key；能讀寫什麼由資料庫的 RLS 決定（只能碰自己的資料）。
// - 金額一律用「百萬分之一」的整數（BigInt）計算，避免浮點誤差；資料庫是 numeric(12,6)。
// - 一律用 textContent 放文字。上半部是純函式，在 site/tests/account.test.js 測試。
"use strict";

const SCALE = 1000000n; // 小數 6 位
const TICK = 100n; // 兆豐牌告的報價單位 0.0001（每個幣別都是小數 4 位）
const PRICE_DECIMALS = 4;
const FAR_FROM_RATE = 0.3; // 目標價和現價差超過 30% 就再確認一次
const MAX_TARGET = 1000n * SCALE;
const CONFIRM_TEXT = "刪除我的帳號";

// ====================================================================
// 純函式
// ====================================================================

/** "0.205" → 205000n（百萬分之一）；格式不對回傳 null。最多 6 位小數 */
function parseDec(text) {
  const s = String(text ?? "").trim();
  const m = /^(\d+)(?:\.(\d{1,6}))?$/.exec(s);
  if (!m) return null;
  return BigInt(m[1]) * SCALE + BigInt((m[2] ?? "").padEnd(6, "0"));
}

/** 205000n → "0.205"（去掉多餘的 0）；fixed 有給就補到固定位數 */
function formatDec(micros, fixed = null) {
  const neg = micros < 0n;
  const v = neg ? -micros : micros;
  const int = (v / SCALE).toString();
  let frac = (v % SCALE).toString().padStart(6, "0");
  frac = fixed === null ? frac.replace(/0+$/, "") : frac.slice(0, fixed).padEnd(fixed, "0");
  return (neg ? "-" : "") + (frac ? `${int}.${frac}` : int);
}

/** 銀行報價有幾位小數："0.2120" → 4；沒有就當 4 位 */
function decimalsOf(rate) {
  const m = /\.(\d+)$/.exec(String(rate ?? ""));
  return m ? Math.min(m[1].length, 6) : 4;
}

/**
 * 沒填回升門檻時用的值：目標價的 0.5%，四捨五入到報價單位 0.0001，至少 0.0001。
 * 和資料庫 alert_rules.effective_buffer、Python 的 currencies.auto_buffer() 一致。
 * 例：日圓 0.205 → 0.0010；越南盾 0.0015 → 0.0001（不會變成 0.0000075）
 */
function autoBuffer(targetMicros) {
  const ticks = (targetMicros * 5n + 50000n) / 100000n; // 0.5% 換成報價單位，四捨五入
  const v = ticks * TICK;
  return v < TICK ? TICK : v;
}

/** 回升門檻上限：目標價的 10%，但至少允許一個報價單位 */
function maxBuffer(targetMicros) {
  const max = targetMicros / 10n;
  return max < TICK ? TICK : max;
}

/** 價格一律顯示到牌告的位數（小數 4 位），四捨五入 */
function formatPrice(micros) {
  return formatDec(((micros + TICK / 2n) / TICK) * TICK, PRICE_DECIMALS);
}

function effectiveBuffer(targetMicros, bufferMicros) {
  return bufferMicros === null || bufferMicros === undefined ? autoBuffer(targetMicros) : bufferMicros;
}

/**
 * 匯率要回到多少才會重新開始等待：排程的規則是「匯率 > 目標 + 回升門檻」，
 * 換成銀行報價的位數，就是比它大的最小報價。例：目標 0.205、門檻 0.001、報價 4 位 → 0.2061
 */
function rearmQuote(targetMicros, bufferMicros, decimals) {
  const step = 10n ** BigInt(6 - decimals);
  const rearm = targetMicros + effectiveBuffer(targetMicros, bufferMicros);
  return formatDec((rearm / step + 1n) * step, decimals);
}

function rearmText(targetMicros, bufferMicros, decimals) {
  return `匯率回升到 ${rearmQuote(targetMicros, bufferMicros, decimals)} 以上，才會再提醒你`;
}

/** 目標價輸入檢查：兆豐牌告只到小數第 4 位，目標價也最多 4 位小數 */
function validateTarget(text) {
  const micros = parseDec(text);
  if (micros === null) return { error: "請輸入數字，例如 0.205（最多 4 位小數）" };
  if (micros % TICK !== 0n) return { error: "兆豐牌告只到小數第 4 位，目標價最多 4 位小數" };
  if (micros <= 0n) return { error: "目標價必須大於 0" };
  if (micros >= MAX_TARGET) return { error: "目標價太大了，請確認是「1 外幣要付幾元台幣」" };
  return { micros, value: formatDec(micros) };
}

/**
 * 回升門檻輸入檢查：空白 = 自動（目標價的 0.5%）。
 * 至少一個報價單位 0.0001、最多 4 位小數；最多是目標價的 10%（但至少允許 0.0001）。
 */
function validateBuffer(text, targetMicros) {
  if (String(text ?? "").trim() === "") return { micros: null, value: null };
  const micros = parseDec(text);
  if (micros === null) return { error: "回升門檻請輸入數字，或留空使用預設值" };
  if (micros < TICK || micros % TICK !== 0n) {
    return { error: "回升門檻至少 0.0001（一個報價單位），最多 4 位小數" };
  }
  const max = maxBuffer(targetMicros);
  if (micros > max) return { error: `回升門檻最多是目標價的 10%（${formatPrice(max)}）` };
  return { micros, value: formatDec(micros) };
}

/** 目標價和現價差太多（> 30%）時要再確認的訊息；不用確認回傳 null */
function farFromRateMessage(targetText, rate) {
  const t = Number(targetText);
  const r = Number(rate);
  if (!(t > 0) || !(r > 0)) return null;
  const pct = Math.abs(t - r) / r;
  if (pct <= FAR_FROM_RATE) return null;
  return `目標價 ${targetText} 和現在的匯率 ${rate} 差了 ${Math.round(pct * 100)}%。` +
    "確定沒有打錯小數點嗎？";
}

/** 新增提醒的幣別選單：只列出還沒設定過的幣別 */
function availableCurrencies(currencies, rules) {
  const used = new Set(rules.map((r) => r.currency));
  return currencies.filter((c) => !used.has(c.code)).map((c) => ({ code: c.code, name: c.name }));
}

/**
 * 新增提醒的幣別選單（分組）：常用（popular_rank）→ 其他（兆豐牌告順序），已經設定過的不列。
 * 選項文字和上方下拉選單一樣：「日圓 JPY」。
 */
function addOptions(currencies, rules) {
  const used = new Set(rules.map((r) => r.currency));
  const left = currencies.filter((c) => !used.has(c.code));
  const opt = (c) => ({ code: c.code, label: `${c.name} ${c.code}` });
  const popular = left.filter((c) => c.popular_rank).sort((a, b) => a.popular_rank - b.popular_rank);
  const others = left.filter((c) => !c.popular_rank).sort((a, b) => (a.sort_order ?? 99) - (b.sort_order ?? 99));
  return [["常用", popular], ["其他", others]]
    .filter(([, items]) => items.length)
    .map(([label, items]) => ({ label, options: items.map(opt) }));
}

/** 新增提醒預設選哪個幣別：正在看的幣別（還沒設定過的話），否則第一個 */
function defaultAddCode(groups, viewing) {
  const codes = groups.flatMap((g) => g.options.map((o) => o.code));
  return codes.includes(viewing) ? viewing : codes[0] ?? null;
}

/** 規則依兆豐牌告順序排（和下拉選單、全部幣別一覽一致） */
function sortRules(rules, currencies) {
  const order = Object.fromEntries(currencies.map((c) => [c.code, c.sort_order ?? 99]));
  return [...rules].sort((a, b) => (order[a.currency] ?? 99) - (order[b.currency] ?? 99) ||
    String(a.currency).localeCompare(String(b.currency)));
}

/**
 * 目標價的建議值（輸入框的 placeholder）：比現價低約 2%，對齊報價單位，至少比現價低一格。
 * 匯率 ≥ 1 時取到小數 2 位，比較好讀（美元 33.3673 → 32.69）。
 * 越南盾 0.0015 → 0.0014；日圓 0.2119 → 0.2076。
 */
function suggestTarget(rate) {
  const r = parseDec(rate);
  if (r === null || r <= 0n) return null;
  const step = r >= SCALE ? SCALE / 100n : TICK;
  let t = ((r * 98n) / 100n / step) * step;
  if (t >= r) t = r - TICK;
  return t > 0n ? formatPrice(t) : null;
}

/**
 * 越南盾、印尼盾這種很小的匯率：一個報價單位 0.0001 就佔現價好幾 %，目標價只能一格一格設。
 * 一格 ≥ 現價的 1% 時回傳說明，其他幣別回傳 null。
 */
function tickHint(rate, name) {
  const r = parseDec(rate);
  if (r === null || r <= 0n) return null;
  const pct = (Number(TICK) / Number(r)) * 100;
  if (pct < 1) return null;
  const steps = [r - 2n * TICK, r - TICK, r].filter((v) => v > 0n).map(formatPrice);
  return `${name}的牌告只到小數第 4 位，每跳一格（0.0001）約 ${pct.toFixed(1)}%，` +
    `目標價只能設成 ${steps.join("、")} 這樣的數字。`;
}

/** 距離目標還要跌幾 %（除以現價），四捨五入到小數 3 位；和 Python 的 build-site 一樣 */
function distancePct(rate, targetMicros) {
  const r = parseDec(rate);
  if (r === null || r === 0n) return null;
  const pct = (Number(r - targetMicros) / Number(r)) * 100;
  return Math.round(pct * 1000) / 1000;
}

// ---------- 通知時段（台灣時間） ----------

const DEFAULT_WINDOW = { start: "08:00", end: "22:00" };

/** 下拉選單的選項：00:00、00:30 … 23:30（檢查每 30 分鐘一次，只能選整點或半點） */
function windowOptions() {
  const out = [];
  for (let m = 0; m < 24 * 60; m += 30) {
    out.push(`${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`);
  }
  return out;
}

/** profiles 的 notify_start / notify_end（"08:00:00"）→ { start: "08:00", end: "22:00" }；沒有欄位就是預設值 */
function notifyWindow(profile) {
  const cut = (t) => (typeof t === "string" && /^\d{2}:\d{2}/.test(t) ? t.slice(0, 5) : null);
  const start = cut(profile && profile.notify_start);
  const end = cut(profile && profile.notify_end);
  return start && end ? { start, end } : { ...DEFAULT_WINDOW };
}

/** 「08:00–22:00」、「22:00–隔天 07:00」或「全天」 */
function windowLabel(w) {
  if (w.start === w.end) return "全天";
  return w.start < w.end ? `${w.start}–${w.end}` : `${w.start}–隔天 ${w.end}`;
}

/** 台灣時間的「HH:MM」 */
function taipeiHHMM(date) {
  const d = new Date(date.getTime() + 8 * 3600 * 1000);
  return `${String(d.getUTCHours()).padStart(2, "0")}:${String(d.getUTCMinutes()).padStart(2, "0")}`;
}

/** now 在不在通知時段內（和 Python 的 notify_window.py 一樣：含開始、不含結束；開始=結束是全天） */
function inWindow(w, now) {
  if (w.start === w.end) return true;
  const t = taipeiHHMM(now);
  return w.start < w.end ? w.start <= t && t < w.end : t >= w.start || t < w.end;
}

/**
 * 已到價、但現在不在通知時段：卡片上的說明。在時段內或全天回傳 null。
 * 規則照常是「等待中」；時段開始後的第一次檢查會重新判斷（那時還在目標以下才寄）。
 */
function windowNote(w, now) {
  if (inWindow(w, now)) return null;
  return `已達到目標，但現在不在你的通知時段（台灣時間 ${windowLabel(w)}）。` +
    `${w.start} 之後的第一次檢查會再判斷一次，那時還在目標以下才寄 Email`;
}

/**
 * 公開行情 + 我的規則 → 卡片要的資料（和單人版 data.json 的欄位一樣，cardView 直接用）。
 * profile.email_enabled 為 false 時，所有卡片都顯示「通知已關閉」。
 */
function personalCurrencies(publicList, rules, profile, now = new Date()) {
  const byCode = Object.fromEntries(rules.map((r) => [r.currency, r]));
  const emailOff = profile && profile.email_enabled === false;
  const note = windowNote(notifyWindow(profile), now);
  return publicList.map((c) => {
    const r = byCode[c.code];
    if (!r) return { ...c };
    const target = parseDec(r.target);
    const buffer = r.buffer === null ? null : parseDec(r.buffer);
    const rearm = target + effectiveBuffer(target, buffer);
    const rate = parseDec(c.rate);
    const reached = r.state !== "triggered" && rate !== null && rate <= target;
    return {
      ...c,
      target: formatPrice(target),
      rearm_above: formatPrice(rearm),
      distance_pct: distancePct(c.rate, target),
      status: r.state === "triggered" ? "triggered" : "waiting",
      status_since: r.state_changed_at ?? null,
      paused: emailOff ? "email" : r.enabled === false ? "rule" : null,
      notify_note: reached && note ? note : null,
    };
  });
}

/** 規則目前的狀態（設定頁用） */
function ruleStatusText(rule, profile) {
  if (profile && profile.email_enabled === false) return "Email 通知已關閉";
  if (rule.enabled === false) return "已停用";
  return rule.state === "triggered" ? "已通知，等匯率回升後重新開始" : "等待中";
}

/** Supabase / PostgREST 的錯誤 → 給使用者看的話（不顯示技術細節） */
function errorMessage(err) {
  const code = err && err.code;
  if (code === "23503") return "帳號已不存在，請重新登入";
  if (code === "23505") return "這個幣別已經有提醒了";
  if (code === "23514") return "數值不符合規則（目標價要大於 0，回升門檻最多是目標價的 10%）";
  if (code === "42501" || code === "PGRST301") return "沒有權限，請重新登入";
  return "儲存失敗，請稍後再試";
}

/** 確認文字的比對規則（和 supabase/functions/delete-account/handler.ts 一樣）：NFC、去掉前後空白 */
function normalizeConfirm(text) {
  return typeof text === "string" ? text.normalize("NFC").trim() : "";
}

function isConfirmed(text) {
  return normalizeConfirm(text) === CONFIRM_TEXT;
}

/**
 * 帳號是不是已經不存在（例如在另一台裝置刪除了帳號）：
 *   讀 profile 成功但沒有資料（RLS 下只看得到自己的那一列），或寫入時外鍵錯誤（23503）。
 */
function isAccountGone(result) {
  if (!result) return false;
  if (result.error) return result.error.code === "23503";
  return result.data === null || result.data === undefined;
}

/** 刪除帳號 Edge Function 的錯誤 → 給使用者看的話 */
function deleteErrorMessage(error) {
  if (error === "reauth_required") return "為了安全，請重新登入後再刪除";
  if (error === "confirm_mismatch") return `請輸入「${CONFIRM_TEXT}」`;
  if (error === "not_logged_in") return "登入已失效，請重新登入";
  return "刪除失敗，請稍後再試";
}

// ====================================================================
// 畫面
// ====================================================================

const NAMES = {};
let client = null;
let config = null;
let state = { session: null, profile: null, rules: [], notifications: [] };
let publicList = [];
let notify = () => {};
let viewing = null;
let windowSupported = true; // 資料庫有沒有通知時段欄位（migration 還沒套用時是 false） // 上方下拉選單正在看的幣別（app.js 的 fx:selected）

const byId = (id) => document.getElementById(id);

function h(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === false || v === null || v === undefined) continue;
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : v);
  }
  node.append(...children.filter((c) => c !== null && c !== undefined));
  return node;
}

function say(text, tone = "ok") {
  const box = byId("my-rules-status");
  box.className = `form-status ${tone}`;
  box.textContent = text;
}

function notice(text) {
  const box = byId("account-notice");
  box.textContent = text;
  box.hidden = !text;
}

let loggingOut = false;

/** 帳號已不存在（另一台裝置刪除了帳號）：提示並登出 */
async function accountGone() {
  notice("帳號已不存在，請重新登入");
  loggingOut = true;
  await client.auth.signOut({ scope: "local" });
  loggingOut = false;
  state.session = null;
  await refresh();
}

/** 寫入失敗的共同處理 */
async function writeFailed(error) {
  if (isAccountGone({ error })) return accountGone();
  say(errorMessage(error), "error");
}

function loadScript(src) {
  return new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = src;
    s.onload = resolve;
    s.onerror = () => reject(new Error(`無法載入 ${src}`));
    document.head.append(s);
  });
}

function rateOf(code) {
  const c = publicList.find((x) => x.code === code);
  return c ? c.rate : null;
}

// ---------- 帳號區（右上角） ----------

function renderAccount() {
  const box = byId("account");
  box.hidden = false;
  if (!state.session) {
    box.replaceChildren(h("button", { type: "button", class: "primary", onclick: login }, "用 Google 登入"));
    return;
  }
  const email = state.session.user.email ?? "";
  box.replaceChildren(
    h("span", { class: "who", title: email }, email),
    h("button", { type: "button", class: "link", onclick: logout }, "登出"),
  );
}

async function login() {
  const redirectTo = location.origin + location.pathname;
  notice("");
  const { error } = await client.auth.signInWithOAuth({ provider: "google", options: { redirectTo } });
  if (error) alert("無法開始登入，請稍後再試");
}

async function logout() {
  loggingOut = true;
  await client.auth.signOut();
  loggingOut = false;
}

// ---------- 讀資料 ----------

async function loadMine() {
  const [p0, r, n] = await Promise.all([
    client.from("profiles").select("email_enabled,notify_start,notify_end").maybeSingle(),
    client.from("alert_rules")
      .select("id,currency,target,buffer,effective_buffer,enabled,state,state_changed_at")
      .order("currency"),
    client.from("notifications")
      .select("sent_at,currency,rate,target")
      .order("sent_at", { ascending: false })
      .limit(20),
  ]);
  // 通知時段的 migration 還沒套用（欄位不存在）：只讀 email_enabled，時段用預設值、不能修改
  let p = p0;
  windowSupported = !(p0.error && p0.error.code === "42703");
  if (!windowSupported) p = await client.from("profiles").select("email_enabled").maybeSingle();
  if (p.error || r.error || n.error) throw p.error || r.error || n.error;
  if (isAccountGone(p)) return false;
  state.profile = p.data;
  state.rules = r.data ?? [];
  state.notifications = n.data ?? [];
  return true;
}

async function refresh() {
  if (!state.session) {
    state = { session: null, profile: null, rules: [], notifications: [] };
    byId("my-rules").hidden = true;
    byId("my-alerts").hidden = true;
    renderAccount();
    notify(publicList, false);
    return;
  }
  let exists;
  try {
    exists = await loadMine();
  } catch {
    renderAccount();
    byId("my-rules").hidden = false;
    byId("my-rules-body").replaceChildren();
    say("讀不到你的提醒設定，請重新整理頁面", "error");
    return;
  }
  if (!exists) return accountGone();
  renderAccount();
  renderRules();
  renderNotifications();
  notify(personalCurrencies(publicList, state.rules, state.profile), true);
}

// ---------- 我的提醒 ----------

function renderEmailSwitch() {
  const on = state.profile.email_enabled !== false;
  const input = h("input", { type: "checkbox", id: "email-enabled", checked: on, role: "switch" });
  input.addEventListener("change", async () => {
    const next = input.checked;
    if (!next && !confirm("關閉後不會再寄任何到價通知，也不會檢查你的提醒。確定要關閉嗎？")) {
      input.checked = true;
      return;
    }
    input.disabled = true;
    const { error } = await client.from("profiles")
      .update({ email_enabled: next }).eq("user_id", state.session.user.id);
    if (error) {
      input.checked = !next;
      input.disabled = false;
      return writeFailed(error);
    }
    say(next
      ? "已開啟 Email 通知。所有提醒會從「等待中」重新開始，不會補寄關閉期間的通知。"
      : "已關閉 Email 通知。");
    await refresh();
  });
  return h("div", { class: "email-switch" },
    h("label", { for: "email-enabled" }, input, h("span", {}, "Email 到價通知")),
    h("span", { class: on ? "pill on" : "pill off" }, on ? "開啟" : "已關閉"),
    on ? null : h("p", { class: "hint" },
      "已關閉。重新開啟後，所有提醒會從「等待中」重新開始，不會補寄關閉期間的通知。"));
}

/** 通知時段設定：兩個下拉選單（台灣時間），改了就存 */
function renderNotifyWindow() {
  const w = notifyWindow(state.profile);
  const opts = windowOptions();
  const select = (id, value, label) => h("select", { id, "aria-label": label, disabled: !windowSupported },
    ...opts.map((t) => h("option", { value: t, selected: t === value }, t)));
  const start = select("notify-start", w.start, "通知時段開始（台灣時間）");
  const end = select("notify-end", w.end, "通知時段結束（台灣時間）");
  const summary = h("span", { class: "pill on", id: "notify-window-label" }, windowLabel(w));
  const button = h("button", { type: "button", class: "primary", id: "notify-window-save", disabled: true }, "儲存時段");
  // 改下拉選單只更新預覽；按「儲存時段」才寫入（避免改到一半就存成別的時段）
  const preview = () => {
    const next = { start: start.value, end: end.value };
    summary.textContent = windowLabel(next);
    button.disabled = !windowSupported || (next.start === w.start && next.end === w.end);
  };
  const save = async () => {
    const next = { start: start.value, end: end.value };
    start.disabled = end.disabled = button.disabled = true;
    const { error } = await client.from("profiles")
      .update({ notify_start: next.start, notify_end: next.end }).eq("user_id", state.session.user.id);
    if (error) {
      start.disabled = end.disabled = false;
      start.value = w.start;
      end.value = w.end;
      preview();
      return writeFailed(error);
    }
    say(`已更新通知時段：台灣時間 ${windowLabel(next)}`);
    await refresh();
  };
  start.addEventListener("change", preview);
  end.addEventListener("change", preview);
  button.addEventListener("click", save);
  return h("fieldset", { class: "notify-window" },
    h("legend", {}, "通知時段（台灣時間）"),
    h("div", { class: "window-row" },
      h("label", { class: "field" }, h("span", {}, "從"), start),
      h("label", { class: "field" }, h("span", {}, "到"), end),
      summary,
      button),
    h("p", { class: "hint" },
      "只在這段台灣時間內寄到價通知（不含結束時間）。到價時如果不在時段內，不會寄信，" +
      "時段開始後的第一次檢查會再判斷一次：那時還在目標以下才寄，不會補寄。開始和結束選一樣 = 全天。"),
    windowSupported ? null : h("p", { class: "hint" }, "通知時段即將開放設定，目前是預設的 08:00–22:00。"));
}

function ruleRow(rule) {
  const name = NAMES[rule.currency] ?? rule.currency;
  const decimals = decimalsOf(rateOf(rule.currency));
  const target = h("input", {
    type: "text", inputmode: "decimal", value: formatPrice(parseDec(rule.target)),
    "aria-label": `${name}的目標價`, autocomplete: "off",
  });
  const enabled = h("input", { type: "checkbox", checked: rule.enabled !== false });
  const buffer = h("input", {
    type: "text", inputmode: "decimal", autocomplete: "off",
    value: rule.buffer === null ? "" : formatPrice(parseDec(rule.buffer)),
    placeholder: "留空 = 目標價的 0.5%", "aria-label": `${name}的回升門檻`,
  });
  const rearm = h("p", { class: "hint rearm" });
  const updateRearm = () => {
    const t = validateTarget(target.value);
    const b = t.error ? null : validateBuffer(buffer.value, t.micros);
    rearm.textContent = t.error || (b && b.error)
      ? (t.error || b.error)
      : rearmText(t.micros, b.micros, decimals);
  };
  target.addEventListener("input", updateRearm);
  buffer.addEventListener("input", updateRearm);
  updateRearm();

  const save = async (event) => {
    event.preventDefault();
    const t = validateTarget(target.value);
    if (t.error) return say(t.error, "error");
    const b = validateBuffer(buffer.value, t.micros);
    if (b.error) return say(b.error, "error");
    const far = farFromRateMessage(t.value, rateOf(rule.currency));
    if (far && !confirm(far)) return;
    const { error } = await client.from("alert_rules")
      .update({ target: t.value, buffer: b.value, enabled: enabled.checked }).eq("id", rule.id);
    if (error) return writeFailed(error);
    say(`已儲存${name}的提醒`);
    await refresh();
  };
  const remove = async () => {
    if (!confirm(`確定要刪除${name}的提醒嗎？通知紀錄會保留。`)) return;
    const { error } = await client.from("alert_rules").delete().eq("id", rule.id);
    if (error) return writeFailed(error);
    say(`已刪除${name}的提醒`);
    await refresh();
  };

  return h("form", { class: "rule", onsubmit: save, "data-currency": rule.currency },
    h("div", { class: "rule-main" },
      h("strong", { class: "rule-name" }, `${name} ${rule.currency}`),
      h("label", { class: "field" }, h("span", {}, "你的目標"), target),
      h("label", { class: "check" }, enabled, h("span", {}, "啟用"))),
    h("p", { class: "hint" }, "狀態：", ruleStatusText(rule, state.profile),
      rateOf(rule.currency) ? `・現在 ${rateOf(rule.currency)}` : ""),
    tickHint(rateOf(rule.currency), name) ? h("p", { class: "hint tick-hint" }, tickHint(rateOf(rule.currency), name)) : null,
    h("details", { class: "advanced" },
      h("summary", {}, "進階設定"),
      h("label", { class: "field" }, h("span", {}, "回升門檻"), buffer),
      rearm),
    h("div", { class: "rule-actions" },
      h("button", { type: "submit", class: "primary" }, "儲存"),
      h("button", { type: "button", class: "link danger", onclick: remove }, "刪除")));
}

function addForm() {
  const groups = addOptions(publicList, state.rules);
  if (!groups.length) return h("p", { class: "hint" }, "每個幣別都已經設定提醒了。");
  const select = h("select", { "aria-label": "幣別", id: "add-currency" },
    ...groups.map((g) => h("optgroup", { label: g.label },
      ...g.options.map((o) => h("option", { value: o.code }, o.label)))));
  select.value = defaultAddCode(groups, viewing);
  const target = h("input", {
    type: "text", inputmode: "decimal", autocomplete: "off", id: "add-target",
    "aria-label": "目標價", "aria-describedby": "add-hint",
  });
  const hint = h("p", { class: "hint", id: "add-hint" });
  const updateHint = () => {
    const code = select.value;
    const name = NAMES[code] ?? code;
    const rate = rateOf(code);
    const suggest = suggestTarget(rate);
    target.placeholder = suggest ? `例如 ${suggest}` : "";
    const tick = tickHint(rate, name);
    hint.replaceChildren(
      rate ? `${name}現在 ${rate}。匯率跌到你的目標或更低時，會寄一封 Email 給你。` : "",
      tick ? h("span", { class: "tick-hint" }, ` ${tick}`) : "");
  };
  select.addEventListener("change", updateHint);
  updateHint();
  const add = async (event) => {
    event.preventDefault();
    const t = validateTarget(target.value);
    if (t.error) return say(t.error, "error");
    const far = farFromRateMessage(t.value, rateOf(select.value));
    if (far && !confirm(far)) return;
    const { error } = await client.from("alert_rules").insert({ currency: select.value, target: t.value });
    if (error) return writeFailed(error);
    say(`已新增${NAMES[select.value] ?? select.value}的提醒`);
    await refresh();
  };
  return h("form", { class: "rule add", id: "add-form", onsubmit: add },
    h("strong", {}, "＋ 新增提醒"),
    h("label", { class: "field" }, h("span", {}, "幣別"), select),
    h("label", { class: "field" }, h("span", {}, "你的目標"), target),
    hint,
    h("button", { type: "submit", class: "primary" }, "新增"));
}

/** 卡片上的「為○○設定提醒」：已經有提醒就跳到那一列，沒有就選好幣別、跳到新增表單 */
function openAddRule(code) {
  const section = byId("my-rules");
  if (!section || section.hidden) return;
  const existing = section.querySelector(`form.rule[data-currency="${code}"] input[type="text"]`);
  const select = byId("add-currency");
  let focus = existing;
  if (!focus && select) {
    if ([...select.options].some((o) => o.value === code)) {
      select.value = code;
      select.dispatchEvent(new Event("change"));
    }
    focus = byId("add-target");
  }
  if (!focus) return;
  focus.scrollIntoView({ behavior: "smooth", block: "center" });
  focus.focus({ preventScroll: true });
}

function deleteSection() {
  const input = h("input", {
    type: "text", autocomplete: "off", "aria-label": "確認文字", placeholder: CONFIRM_TEXT,
  });
  const go = h("button", { type: "submit", class: "danger-button", disabled: true }, "永久刪除");
  const msg = h("p", { class: "form-status error", role: "status", "aria-live": "polite" });
  const relogin = h("button", { type: "button", class: "link", hidden: true, onclick: login },
    "重新登入");
  input.addEventListener("input", () => {
    go.disabled = !isConfirmed(input.value);
  });
  const submit = async (event) => {
    event.preventDefault();
    if (!isConfirmed(input.value)) return;
    go.disabled = true;
    msg.textContent = "刪除中…";
    relogin.hidden = true;
    let status = 0;
    let error = null;
    try {
      const res = await fetch(`${config.url}/functions/v1/delete-account`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          apikey: config.key,
          Authorization: `Bearer ${state.session.access_token}`,
        },
        body: JSON.stringify({ confirm: input.value }),
      });
      status = res.status;
      error = (await res.json().catch(() => ({}))).error ?? null;
    } catch {
      status = 0;
    }
    if (status === 200) {
      loggingOut = true;
      await client.auth.signOut({ scope: "local" });
      loggingOut = false;
      state.session = null;
      notice("帳號已刪除。你的提醒、通知紀錄和 Email 設定都已經刪除。");
      await refresh();
      return;
    }
    msg.textContent = deleteErrorMessage(error);
    relogin.hidden = error !== "reauth_required" && error !== "not_logged_in";
    go.disabled = !isConfirmed(input.value);
  };
  return h("details", { class: "danger-zone" },
    h("summary", {}, "刪除我的帳號"),
    h("form", { onsubmit: submit },
      h("p", {}, "會永久刪除：你的提醒、通知紀錄、Email 設定。已經寄出的信不會收回。刪除後無法復原。"),
      h("p", { class: "hint" }, "想再使用，用 Google 登入就會建立新帳號。為了安全，需要最近 10 分鐘內登入過。"),
      h("label", { class: "field" }, h("span", {}, `請輸入「${CONFIRM_TEXT}」確認`), input),
      h("div", { class: "rule-actions" }, go, relogin),
      msg));
}

function renderRules() {
  byId("my-rules").hidden = false;
  const schedule = window.FxApp && window.FxApp.scheduleHint ? window.FxApp.scheduleHint() : "";
  byId("my-rules-body").replaceChildren(
    renderEmailSwitch(),
    renderNotifyWindow(),
    schedule ? h("p", { class: "hint" }, schedule) : null,
    ...sortRules(state.rules, publicList).map(ruleRow),
    addForm(),
    h("h3", { class: "section-title" }, "帳號"),
    deleteSection(),
  );
}

function renderNotifications() {
  const rows = state.notifications;
  byId("my-alerts").hidden = false;
  byId("my-alerts-table").hidden = rows.length === 0;
  byId("my-alerts-empty").hidden = rows.length > 0;
  const fmt = (window.FxApp && window.FxApp.formatTime) || ((s) => s);
  byId("my-alerts-body").replaceChildren(...rows.map((a) =>
    h("tr", {},
      h("td", {}, fmt(a.sent_at)),
      h("td", {}, NAMES[a.currency] ?? a.currency),
      h("td", {}, formatDec(parseDec(String(a.rate)) ?? 0n, decimalsOf(rateOf(a.currency)))),
      h("td", {}, formatDec(parseDec(String(a.target)) ?? 0n)))));
}

// ---------- 啟動 ----------

async function start(auth, currencies, onChange) {
  publicList = currencies;
  notify = onChange;
  document.addEventListener("fx:selected", (e) => {
    viewing = e.detail.code;
  });
  document.addEventListener("fx:add-rule", (e) => openAddRule(e.detail.code));
  for (const c of currencies) NAMES[c.code] = c.name;
  try {
    await loadScript("vendor/supabase.umd.js");
  } catch {
    return; // 載不到就維持公開頁面
  }
  config = auth;
  client = window.supabase.createClient(auth.url, auth.key, {
    auth: { flowType: "pkce", persistSession: true, detectSessionInUrl: true },
  });
  const { data } = await client.auth.getSession();
  state.session = data.session;
  await refresh();
  client.auth.onAuthStateChange((event, session) => {
    const changed = (session && session.user.id) !== (state.session && state.session.user.id);
    // 不是自己按登出，卻被登出（例如帳號在另一台裝置刪除、登入過期）
    if (event === "SIGNED_OUT" && state.session && !loggingOut) {
      notice("登入已失效，請重新登入");
    }
    state.session = session;
    if (changed) setTimeout(refresh, 0); // 不在 callback 裡呼叫 Supabase（官方建議）
  });
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    parseDec, formatDec, formatPrice, decimalsOf, autoBuffer, maxBuffer, effectiveBuffer,
    rearmQuote, rearmText,
    validateTarget, validateBuffer, farFromRateMessage, availableCurrencies, distancePct,
    addOptions, defaultAddCode, sortRules, suggestTarget, tickHint,
    windowOptions, notifyWindow, windowLabel, inWindow, windowNote,
    personalCurrencies, ruleStatusText, errorMessage, normalizeConfirm, isConfirmed,
    isAccountGone, deleteErrorMessage, CONFIRM_TEXT,
  };
} else {
  window.FxAccount = { start };
}
