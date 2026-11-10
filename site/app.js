// FX Alert 前端：讀 data.json，畫出頁面。
// - 匯率比較、距離 %、最高最低價等「資料」由 Python 的 build-site 算好；
//   這裡負責「怎麼用白話說給使用者聽」，以及會跟著輸入變動的換匯試算。
// - 一律用 textContent 放文字（不用 innerHTML），資料內容不會被當成 HTML 執行。
// - 上半部都是純函式（不碰畫面），在 site/tests/app.test.js 測試。
// - 登入功能（多人版）由 config.js 的 authEnabled 決定，見 bootPlan()：
//     關閉：和單人版一樣，目標價、通知紀錄來自 data.json；不載入 supabase-js。
//     開啟：沒登入只顯示公開行情；登入後由 account.js 從 Supabase 讀自己的目標價。
"use strict";

const STALE_AFTER_MINUTES = 120;
const DEFAULT_TWD = 10000;
const MAX_TWD = 100000000;
const STORAGE_KEY = "fx-alert.twd";
const WEEKDAYS = ["週日", "週一", "週二", "週三", "週四", "週五", "週六"];
const numberFmt = new Intl.NumberFormat("zh-TW");

// ====================================================================
// 純函式
// ====================================================================

/** 本機預覽可用 ?data=samples/rebound.json 換資料；只允許站內的 .json 檔 */
function dataUrl(search) {
  const name = new URLSearchParams(search).get("data");
  return name && /^[\w-]+(\/[\w-]+)*\.json$/.test(name) ? name : "data.json";
}

/** 本機預覽可用 ?now=2026-11-14T12:00 指定「現在」（台灣時間），看非檢查時段等畫面 */
function previewNow(search) {
  const s = new URLSearchParams(search).get("now");
  if (!s || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(s)) return null;
  const d = new Date(`${s}:00+08:00`);
  return Number.isNaN(d.getTime()) ? null : d;
}

/** 本機預覽可用 ?public=1 模擬「多人版、沒登入」的公開頁面（拿掉範例資料裡的目標價） */
function previewPublic(search) {
  return new URLSearchParams(search).get("public") === "1";
}

/** 台灣時間的星期、分鐘數（台灣是 UTC+8、沒有日光節約，直接加 8 小時） */
function taipeiParts(date) {
  const t = new Date(date.getTime() + 8 * 3600 * 1000);
  return { day: t.getUTCDay(), minutes: t.getUTCHours() * 60 + t.getUTCMinutes() };
}

function pad2(n) {
  return String(n).padStart(2, "0");
}

/** "2026-11-09T16:10:12+08:00" → "11/09 16:10"（一律台灣時間） */
function formatTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  const t = new Date(d.getTime() + 8 * 3600 * 1000);
  return `${pad2(t.getUTCMonth() + 1)}/${pad2(t.getUTCDate())} ${pad2(t.getUTCHours())}:${pad2(t.getUTCMinutes())}`;
}

/** 只要日期："11/09" */
function formatDate(iso) {
  return formatTime(iso).split(" ")[0];
}

/** 多久以前：「剛剛」「5 分鐘前」「3 小時前」「2 天前」 */
function timeAgo(iso, now = new Date()) {
  const diff = (now - new Date(iso)) / 60000;
  if (!Number.isFinite(diff)) return "";
  if (diff < 1) return "剛剛";
  if (diff < 60) return `${Math.floor(diff)} 分鐘前`;
  if (diff < 60 * 24) return `${Math.floor(diff / 60)} 小時前`;
  return `${Math.floor(diff / 60 / 24)} 天前`;
}

/** 還要跌多少：小數 1 位（四捨五入）；大於 0 但不到 0.1% 時寫「不到 0.1%」 */
function formatPct(pct) {
  if (pct > 0 && pct < 0.1) return "不到 0.1%";
  return `${(Math.round(pct * 10 + 1e-9) / 10).toFixed(1)}%`;
}

/**
 * 匯率換算說明。匯率 = 1 單位外幣要付幾元台幣。
 * 數字夠大（日圓）：「1 台幣 ≈ 4.72 日圓」；
 * 數字很小（韓元）：「1,000 韓元 ≈ 26.3 台幣」比較好懂。
 */
function rateHint(rate, name) {
  const r = Number(rate);
  if (!rate || !(r > 0)) return "";
  if (r >= 0.1) return `1 台幣 ≈ ${(1 / r).toFixed(2)} ${name}`;
  return `1,000 ${name} ≈ ${(1000 * r).toFixed(1)} 台幣`;
}

/**
 * 卡片要怎麼說：徽章、主要訊息（tone：neutral / good / info）、詳細資訊。
 * 已通知的情況分三種：還在目標以下、回升但沒超過門檻、已超過門檻（等下次檢查）。
 */
function cardView(c) {
  if (c.target === null || c.target === undefined) {
    // 沒有目標價（沒登入、或還沒設定）：只顯示行情
    const details = c.rate_at ? [`匯率時間：${formatTime(c.rate_at)}`] : [];
    return { badge: null, tone: "neutral", message: null, details };
  }
  if (c.paused) {
    // 多人版：提醒停用、或整個 Email 通知關閉時，排程不檢查也不寄信
    return {
      badge: c.paused === "email" ? "通知已關閉" : "已停用",
      tone: "neutral",
      message: c.paused === "email"
        ? "Email 到價通知已關閉，不會檢查也不會寄信"
        : "這個提醒已停用，不會寄 Email",
      details: c.rate_at ? [`匯率時間：${formatTime(c.rate_at)}`] : [],
    };
  }
  const rate = c.rate === null || c.rate === undefined ? null : Number(c.rate);
  const target = Number(c.target);
  const rearm = Number(c.rearm_above);
  const triggered = c.status === "triggered";
  const details = [];

  if (c.rate_at) details.push(`匯率時間：${formatTime(c.rate_at)}`);
  if (triggered) {
    if (c.status_since) details.push(`${formatTime(c.status_since)} 寄出通知`);
    details.push(`為了避免一直寄信，匯率要先回升超過 ${c.rearm_above}、再跌回 ${c.target}，才會再提醒你`);
  } else {
    if (c.status_since) details.push(`從 ${formatTime(c.status_since)} 開始等待`);
    details.push(`匯率跌到 ${c.target} 或更低時，會寄一封 Email 給你`);
  }

  let view;
  if (rate === null) {
    view = { badge: triggered ? "已通知" : "等待中", tone: "neutral", message: "還沒有匯率資料" };
  } else if (!triggered) {
    view = c.distance_pct > 0
      ? { badge: "等待中", tone: "neutral", message: `還要再跌 ${formatPct(c.distance_pct)} 才會通知你` }
      : { badge: "已到價", tone: "good", message: "已達到目標，下次檢查時會寄 Email 給你" };
  } else if (rate <= target) {
    view = { badge: "已到價", tone: "good", message: "✓ 已低於目標，Email 已寄出" };
  } else if (rate <= rearm) {
    view = {
      badge: "已通知",
      tone: "info",
      message: `已通知過・目前 ${c.rate} 高於目標，回升超過 ${c.rearm_above} 才會重新開始等待`,
    };
  } else {
    view = { badge: "已通知", tone: "info", message: "已通知過・匯率已回升，下次檢查時會重新開始等待" };
  }
  return { ...view, details };
}

function toMinutes(hhmm) {
  const [h, m] = String(hhmm).split(":").map(Number);
  return h * 60 + m;
}

function isCheckDay(hours, day) {
  return !hours.weekdays_only || (day !== 0 && day !== 6);
}

/** 現在是不是在檢查時段內（從第一次排程檢查到結束時間） */
function inCheckHours(hours, now = new Date()) {
  if (!hours) return true;
  const { day, minutes } = taipeiParts(now);
  const first = toMinutes(hours.first_check || hours.start);
  return isCheckDay(hours, day) && minutes >= first && minutes <= toMinutes(hours.end);
}

/** 下次更新的時間：「今天 09:05」「明天 09:05」「週一 09:05」（pg_cron 每天第一次觸發） */
function nextCheck(hours, now = new Date()) {
  const firstText = hours.first_check || hours.start;
  const first = toMinutes(firstText);
  const { day, minutes } = taipeiParts(now);
  for (let offset = 0; offset <= 7; offset++) {
    const d = (day + offset) % 7;
    if (!isCheckDay(hours, d)) continue;
    if (offset === 0 && minutes >= first) continue;
    const when = offset === 0 ? "今天" : offset === 1 ? "明天" : WEEKDAYS[d];
    return `${when} ${firstText}`;
  }
  return firstText;
}

/**
 * 資料是不是「太久沒更新」？只在檢查時段內判斷：
 * 從「今天開始檢查」和「最後一次檢查」中較晚的那個算起，超過 2 小時才算。
 */
function isStale(lastCheck, hours, now = new Date()) {
  if (!hours || !inCheckHours(hours, now)) return false;
  const sinceOpen = taipeiParts(now).minutes - toMinutes(hours.start);
  const sinceCheck = lastCheck ? (now - new Date(lastCheck.at)) / 60000 : Infinity;
  return Math.min(sinceOpen, sinceCheck) > STALE_AFTER_MINUTES;
}

/** 頂端的一行狀態。tone：ok / warn / bad / idle；link = 要不要附「查看原因」 */
function statusLine(data, now = new Date()) {
  const lc = data.last_check;
  const hours = data.business_hours;
  if (!lc) return { tone: "idle", text: "還沒有資料，第一次檢查之後就會顯示", link: false };
  const when = formatTime(lc.at);
  if (lc.ok === false) {
    return { tone: "bad", text: `✕ 最近一次更新失敗・${when}`, link: true };
  }
  if (hours && !inCheckHours(hours, now)) {
    return { tone: "idle", text: `目前非檢查時段，下次更新：${nextCheck(hours, now)}・最後更新 ${when}`, link: false };
  }
  if (isStale(lc, hours, now)) {
    return { tone: "warn", text: `⚠ 資料可能過期・最後更新 ${when}（${timeAgo(lc.at, now)}）`, link: true };
  }
  return { tone: "ok", text: `✓ 資料正常・${when} 更新（${timeAgo(lc.at, now)}）`, link: false };
}

/** 使用者輸入的台幣金額 → 數字；不合法回傳 null。允許逗號和空白 */
function parseAmount(text) {
  const s = String(text ?? "").replace(/[,\s，]/g, "");
  if (!/^\d+(\.\d+)?$/.test(s)) return null;
  const n = Number(s);
  return n > 0 && n <= MAX_TWD ? n : null;
}

/**
 * 換匯試算：現在換、在目標價換，各能拿到多少外幣（四捨五入到整數）。
 * 沒有目標價時 targetLine 是 null；hint 有值時（多人版沒登入）「等到目標價」那一行改顯示提示文字，
 * 沒有 hint 就整行隱藏，不會出現只有標題沒有數字的一行。
 */
function converterView(twd, rate, target, name, hint = null) {
  if (rate === null || rate === undefined) {
    return { error: "還沒有匯率資料，暫時無法試算" };
  }
  if (twd === null) return { error: "請輸入台幣金額（例如 10000）" };
  const nowAmount = Math.round(twd / Number(rate));
  if (target === null || target === undefined) {
    return { nowLine: `約 ${numberFmt.format(nowAmount)} ${name}`, nowAmount, targetLine: null, targetHint: hint };
  }
  const targetAmount = Math.round(twd / Number(target));
  const diff = targetAmount - nowAmount;
  let verdict;
  if (diff > 0) verdict = `等到目標價再換，可以多拿約 ${numberFmt.format(diff)} ${name}`;
  else if (diff < 0) verdict = `現在的匯率已經比目標價划算，現在換可以多拿約 ${numberFmt.format(-diff)} ${name}`;
  else verdict = "現在換和在目標價換，拿到的差不多";
  return {
    nowLine: `約 ${numberFmt.format(nowAmount)} ${name}`,
    targetLine: `約 ${numberFmt.format(targetAmount)} ${name}`,
    targetHint: null,
    nowAmount,
    targetAmount,
    diff,
    verdict,
  };
}

/**
 * 走勢圖 Y 軸範圍：一定包含所有匯率和目標價，而且至少涵蓋「現價上下各 1%」，
 * 避免 0.3% 的小波動被放大成像暴跌；上下留一點空間，
 * 並對齊到整齊的刻度間距（1、2、5 × 10ⁿ），避免出現 0.2032 這種刻度。
 * current 省略時用最後一個匯率當現價。
 */
const MIN_HALF_RANGE = 0.01;
function yRange(rates, target, current) {
  const values = rates.map(Number);
  const now = Number(current ?? values[values.length - 1]);
  if (target !== null && target !== undefined) values.push(Number(target));
  if (now > 0) values.push(now * (1 - MIN_HALF_RANGE), now * (1 + MIN_HALF_RANGE));
  const lo = Math.min(...values);
  const hi = Math.max(...values);
  const span = hi - lo;
  const raw = span / 5;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 5, 10].map((k) => k * mag)
    .reduce((best, s) => (Math.abs(s - raw) < Math.abs(best - raw) ? s : best));
  const round = (n) => Number(n.toFixed(10)); // 去掉浮點誤差
  return {
    min: round(Math.floor((lo - span * 0.05) / step) * step),
    max: round(Math.ceil((hi + span * 0.05) / step) * step),
    step: round(step),
  };
}

/** 變動百分比：+0.3%、−1.9%；不到 0.1% 時多留一位小數（+0.05%） */
function formatChange(pct) {
  if (!Number.isFinite(pct)) return "—";
  const digits = Math.abs(pct) < 0.1 ? 2 : 1;
  const text = Math.abs(pct).toFixed(digits);
  if (Number(text) === 0) return `${(0).toFixed(digits)}%`;
  return `${pct > 0 ? "+" : "−"}${text}%`;
}

/** 這段期間的變動：第一個點 → 最後一個點 */
function periodChange(history, label = "近 30 天") {
  if (!history || history.length < 2) return null;
  const from = Number(history[0][1]);
  const to = Number(history[history.length - 1][1]);
  const pct = ((to - from) / from) * 100;
  return { pct, text: `${label}變動 ${formatChange(pct)}（${history[0][1]} → ${history[history.length - 1][1]}）` };
}

/**
 * 走勢資料涵蓋的期間。資料不滿 30 天（最早一筆牌告比「30 天前」還晚）時，
 * 標示「資料從 11/09 開始累積」，最高／最低也改說「11/09 以來」。
 * 舊版 data.json 沒有 history_since 時當作滿 30 天。
 */
function historyWindow(data) {
  const days = Number(data && data.history_days) || 30;
  const full = { partial: false, label: `近 ${days} 天`, note: null };
  const since = data && data.history_since ? new Date(data.history_since) : null;
  const generated = data && data.generated_at ? new Date(data.generated_at) : null;
  if (!since || !generated || Number.isNaN(since.getTime()) || Number.isNaN(generated.getTime())) return full;
  if (generated - since >= days * 24 * 3600 * 1000) return full;
  const day = formatDate(data.history_since);
  return { partial: true, label: `${day} 以來`, note: `資料從 ${day} 開始累積，還不滿 ${days} 天` };
}

const HOUR = 3600 * 1000;
const TICK_HOURS = [1, 2, 3, 6, 12, 24, 48, 72, 168];

/**
 * 時間軸刻度（台灣時間對齊整點／午夜）。點依實際時間排列，所以 8 小時和 8 分鐘的間隔看得出差別；
 * 刻度間距挑最小、但刻度數不超過 maxCount 的那一個。回傳毫秒時間戳和刻度間距（小時）。
 */
function timeTicks(minT, maxT, maxCount) {
  const span = maxT - minT;
  const hours = TICK_HOURS.find((h) => span / (h * HOUR) <= Math.max(1, maxCount - 1)) ?? 168;
  const step = hours * HOUR;
  const offset = 8 * HOUR; // 台灣時間午夜 = UTC 16:00
  let t = Math.ceil((minT + offset) / step) * step - offset;
  const ticks = [];
  for (; t <= maxT; t += step) ticks.push(t);
  return { ticks, hours };
}

/** 刻度文字：一天以上（或剛好午夜）寫日期「11/09」，一天內寫時間「13:00」 */
function tickLabel(t, hours) {
  const [date, time] = formatTime(new Date(t).toISOString()).split(" ");
  return hours >= 24 || time === "00:00" ? date : time;
}

/** 走勢圖上要特別標出的點：現在（最後一點）、30 天最高、30 天最低 */
function markerIndexes(history, high, low) {
  const find = (m) => {
    if (!m) return -1;
    const byTime = history.findIndex(([t]) => t === m.at);
    return byTime >= 0 ? byTime : history.findIndex(([, v]) => Number(v) === Number(m.rate));
  };
  return { current: history.length - 1, high: find(high), low: find(low) };
}

/** 走勢的文字摘要（顯示在圖下方，也是給螢幕閱讀器的替代文字） */
function chartSummary(c, win = historyWindow(null)) {
  const name = c.name ?? c.code;
  const label = win.partial ? ` ${win.label}` : win.label;
  if (!c.history || !c.history.length) return `${name}${label}還沒有走勢資料。`;
  const extremes = [
    c.high && `最高 ${c.high.rate}（${formatDate(c.high.at)}）`,
    c.low && `最低 ${c.low.rate}（${formatDate(c.low.at)}）`,
  ].filter(Boolean).join("、");
  const goal = c.target === null || c.target === undefined ? "" : `，你的目標 ${c.target}`;
  const change = periodChange(c.history);
  const moved = change ? `，期間變動 ${formatChange(change.pct)}` : "";
  return `${name}${label}：${extremes}，現在 ${c.rate}${goal}${moved}。`;
}

// ---------- 登入開關（多人版） ----------

/** config.js 的內容 → 要怎麼啟動頁面。關閉時不載入 supabase-js、不顯示登入、不連任何 Supabase */
function bootPlan(cfg) {
  const ok = Boolean(cfg && cfg.authEnabled === true &&
    typeof cfg.supabaseUrl === "string" && typeof cfg.supabaseKey === "string" &&
    /^(https:\/\/[a-z0-9]{20}\.supabase\.co|http:\/\/(127\.0\.0\.1|localhost):\d+)$/.test(cfg.supabaseUrl) &&
    cfg.supabaseKey.startsWith("sb_publishable_"));
  if (!ok) {
    return { auth: null, loadSupabase: false, showLogin: false, targetsFromDataJson: true };
  }
  return {
    auth: { url: cfg.supabaseUrl, key: cfg.supabaseKey },
    loadSupabase: true,
    showLogin: true,
    targetsFromDataJson: false,
  };
}

const PERSONAL_FIELDS = [
  "target", "buffer", "rearm_above", "distance_pct", "status", "status_since", "paused",
];

/** 只留公開行情（多人版沒登入時用）：拿掉 data.json 裡任何個人設定 */
function publicCurrencies(list) {
  return list.map((c) => {
    const copy = { ...c };
    for (const k of PERSONAL_FIELDS) delete copy[k];
    return copy;
  });
}

// ====================================================================
// 畫面
// ====================================================================

const $ = (id) => document.getElementById(id);

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else node.setAttribute(k, v);
  }
  node.append(...children);
  return node;
}

function renderStatus(data, now) {
  const s = statusLine(data, now);
  const box = $("status");
  box.className = `status ${s.tone}`;
  const parts = [s.text];
  if (s.link && data.actions_url) {
    parts.push(" ", el("a", { href: data.actions_url, target: "_blank", rel: "noopener" }, "查看原因 →"));
  }
  box.replaceChildren(...parts);
}

let cardHint = null; // 沒有目標價時卡片下方的提示（例如「登入後可以設定目標價」）

function renderCard(c) {
  const v = cardView(c);
  const card = el("article", { class: `card tone-${v.tone}` });
  const head = el("header", { class: "card-head" },
    el("h3", {}, c.name, " ", el("span", { class: "code" }, c.code)));
  if (v.badge) head.append(el("span", { class: `badge tone-${v.tone}` }, v.badge));
  card.append(head,
    el("p", { class: "rate" }, c.rate ?? "—"),
    el("p", { class: "hint" }, rateHint(c.rate, c.name)));
  const hasTarget = c.target !== null && c.target !== undefined;
  if (hasTarget) card.append(el("p", { class: "target" }, "你的目標 ", el("b", {}, c.target)));
  if (v.message) card.append(el("p", { class: `message tone-${v.tone}` }, v.message));
  if (!hasTarget && cardHint) card.append(el("p", { class: "message" }, cardHint));
  if (v.details.length) {
    card.append(el("details", {},
      el("summary", {}, "詳細資訊"),
      el("ul", {}, ...v.details.map((d) => el("li", {}, d)))));
  }
  return card;
}

/** 幣別頁籤（試算和走勢圖各一組，互不影響） */
function renderTabs(box, currencies, onSelect, selected = null) {
  const select = (code) => {
    for (const b of box.children) b.setAttribute("aria-selected", String(b.dataset.code === code));
    onSelect(currencies.find((c) => c.code === code));
  };
  box.replaceChildren(...currencies.map((c) => {
    const b = el("button", { type: "button", role: "tab", "data-code": c.code }, c.name);
    b.addEventListener("click", () => select(c.code));
    return b;
  }));
  if (!currencies.length) return;
  select(currencies.some((c) => c.code === selected) ? selected : currencies[0].code);
}

// ---------- 換匯試算 ----------

function loadAmount() {
  try {
    const saved = parseAmount(localStorage.getItem(STORAGE_KEY));
    return saved ?? DEFAULT_TWD;
  } catch {
    return DEFAULT_TWD; // 無痕模式等情況讀不到 localStorage
  }
}

function saveAmount(n) {
  try {
    localStorage.setItem(STORAGE_KEY, String(n));
  } catch {
    // 存不了就算了，下次用預設值
  }
}

let converterCode = null;
let converterHint = null; // 多人版沒登入時：「等到目標價」那一行顯示的提示

function setupConverter(currencies) {
  const input = $("twd");
  let current = null;
  if (!input.value) input.value = numberFmt.format(loadAmount());

  const update = () => {
    if (!current) return;
    const twd = parseAmount(input.value);
    if (twd !== null) saveAmount(twd);
    const v = converterView(twd, current.rate, current.target, current.name, converterHint);
    $("conv-error").hidden = !v.error;
    $("conv-result").hidden = Boolean(v.error);
    if (v.error) {
      $("conv-error").textContent = v.error;
      return;
    }
    $("conv-now").textContent = v.nowLine;
    $("conv-now-rate").textContent = `（匯率 ${current.rate}）`;
    const hasTarget = v.targetLine !== null;
    $("conv-target-row").hidden = !hasTarget && !v.targetHint;
    $("conv-target-row").classList.toggle("is-hint", !hasTarget);
    $("conv-verdict").hidden = !hasTarget;
    if (!hasTarget) {
      $("conv-target").textContent = v.targetHint ?? "";
      $("conv-target-rate").textContent = "";
      return;
    }
    $("conv-target").textContent = v.targetLine;
    $("conv-target-rate").textContent = `（匯率 ${current.target}）`;
    $("conv-verdict").textContent = v.verdict;
  };
  input.oninput = update; // 重畫時覆蓋，不會重複綁定
  input.onblur = () => {
    const n = parseAmount(input.value);
    if (n !== null) input.value = numberFmt.format(n); // 離開輸入框時補上千分位
  };
  renderTabs($("conv-tabs"), currencies, (c) => {
    current = c;
    converterCode = c.code;
    update();
  }, converterCode);
}

// ---------- 走勢圖 ----------

const COLORS = { rate: "#2f5fb3", target: "#a14a00", mark: "#1c2430", grid: "#e6e9ee", text: "#5b6676" };
let chart = null;

let chartWindow = historyWindow(null); // 走勢資料涵蓋的期間（main() 依 data.json 設定）

function renderMarks(c) {
  const span = chartWindow.partial ? chartWindow.label : "30 天";
  const items = [];
  if (c.rate) items.push(["mark-now", `現在 ${c.rate}`, c.rate_at ? `（${formatTime(c.rate_at)}）` : ""]);
  if (c.high) items.push(["mark-high", `${span}最高 ${c.high.rate}`, `（${formatTime(c.high.at)}）`]);
  if (c.low) items.push(["mark-low", `${span}最低 ${c.low.rate}`, `（${formatTime(c.low.at)}）`]);
  $("chart-marks").replaceChildren(...items.map(([cls, main, sub]) =>
    el("li", {}, el("span", { class: `mark ${cls}`, "aria-hidden": "true" }), main, el("span", { class: "sub" }, sub))));
}

function drawChart(c) {
  if (chart) chart.destroy();
  chart = null;
  const hasData = c.history.length > 0;
  $("chart-box").hidden = !hasData;
  $("chart-marks").hidden = !hasData;
  $("chart-note").hidden = !chartWindow.note;
  $("chart-note").textContent = chartWindow.note ?? "";
  const change = periodChange(c.history, chartWindow.label);
  $("chart-change").hidden = !change;
  $("chart-change").textContent = change ? change.text : "";
  $("chart-change").className = `change ${change && change.pct < 0 ? "down" : "up"}`;
  // 文字摘要只給螢幕閱讀器（畫面上已有圖和上面的標記，不重複顯示）
  $("chart-summary").textContent = chartSummary(c, chartWindow);
  if (!hasData) return;
  renderMarks(c);
  if (typeof Chart === "undefined") return;

  // X 軸依實際時間排列（毫秒時間戳），晚上、週末沒有牌告的空檔會照實際長度畫出來
  const points = c.history.map(([t, v]) => ({ x: new Date(t).getTime(), y: Number(v) }));
  const rates = points.map((pt) => pt.y);
  const minT = points[0].x;
  const maxT = points.length > 1 ? points[points.length - 1].x : minT + HOUR;
  const m = markerIndexes(c.history, c.high, c.low);
  const pointStyle = (i) => (i === m.current ? "circle" : "triangle");
  const isMark = (i) => i === m.current || i === m.high || i === m.low;
  const range = yRange(rates, c.target, c.rate);
  const maxTicks = window.innerWidth < 640 ? 4 : 7;
  let tickHours = 24;

  chart = new Chart($("chart"), {
    type: "line",
    data: {
      datasets: [
        {
          label: `${c.name}匯率`,
          data: points,
          borderColor: COLORS.rate,
          borderWidth: 2,
          tension: 0,
          pointRadius: rates.map((_, i) => (isMark(i) ? 6 : 0)),
          pointHoverRadius: 6,
          pointHitRadius: 8,
          pointStyle: rates.map((_, i) => pointStyle(i)),
          pointRotation: rates.map((_, i) => (i === m.low && i !== m.current ? 180 : 0)),
          pointBackgroundColor: rates.map((_, i) => (i === m.current ? COLORS.rate : COLORS.mark)),
          pointBorderColor: rates.map((_, i) => (i === m.current ? "#fff" : COLORS.mark)),
          pointBorderWidth: rates.map((_, i) => (i === m.current ? 2 : 1)),
        },
        ...(c.target === null || c.target === undefined ? [] : [{
          label: `你的目標 ${c.target}`,
          data: [{ x: minT, y: Number(c.target) }, { x: maxT, y: Number(c.target) }],
          borderColor: COLORS.target,
          borderWidth: 2,
          borderDash: [6, 4],
          pointRadius: 0,
          pointHitRadius: 0,
        }]),
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      parsing: false,
      layout: { padding: { right: 8 } }, // 最後一個點（現在）不要被切掉
      interaction: { mode: "nearest", axis: "x", intersect: false },
      plugins: {
        legend: { labels: { color: COLORS.text, boxHeight: 2 } },
        tooltip: {
          filter: (item) => item.datasetIndex === 0,
          callbacks: {
            title: (items) => (items.length ? formatTime(new Date(items[0].parsed.x).toISOString()) : ""),
            label: (ctx) => `${ctx.dataset.label.split(" ")[0]}：${Number(ctx.parsed.y).toFixed(4)}`,
          },
        },
      },
      scales: {
        x: {
          type: "linear",
          min: minT,
          max: maxT,
          afterBuildTicks(axis) {
            const t = timeTicks(axis.min, axis.max, maxTicks);
            tickHours = t.hours;
            axis.ticks = t.ticks.map((value) => ({ value }));
          },
          ticks: { color: COLORS.text, maxRotation: 0, autoSkip: false, callback: (v) => tickLabel(v, tickHours) },
          grid: { display: false },
        },
        y: {
          min: range.min, // 有目標價時一定包含它；至少涵蓋現價 ±1%
          max: range.max,
          ticks: { color: COLORS.text, stepSize: range.step, callback: (v) => Number(v).toFixed(4) },
          grid: { color: COLORS.grid },
        },
      },
    },
  });
}

// ---------- 通知紀錄 ----------

function renderAlerts(data) {
  const alerts = Array.isArray(data.alerts) ? data.alerts : [];
  const names = Object.fromEntries(data.currencies.map((c) => [c.code, c.name]));
  $("alerts-table").hidden = alerts.length === 0;
  $("alerts-empty").hidden = alerts.length > 0;
  $("alerts-body").replaceChildren(...alerts.map((a) =>
    el("tr", {},
      el("td", {}, formatTime(a.sent_at)),
      el("td", {}, names[a.currency] ?? a.currency),
      el("td", {}, a.rate),
      el("td", {}, a.target))));
}

// ---------- 主程式 ----------

function showError(message) {
  const box = $("status");
  box.className = "status bad";
  box.textContent = message;
  $("cards").replaceChildren();
}

let chartCode = null;

/** 依目前的幣別資料重畫卡片、試算、走勢（登入／登出、修改目標價後都會呼叫） */
function renderCurrencies(currencies) {
  $("cards").replaceChildren(...currencies.map(renderCard));
  setupConverter(currencies);
  renderTabs($("chart-tabs"), currencies, (c) => {
    chartCode = c.code;
    drawChart(c);
  }, chartCode);
}

async function main() {
  const now = previewNow(location.search) ?? new Date();
  if (previewNow(location.search)) $("preview-note").textContent = `（預覽時間：${formatTime(now.toISOString())}）`;
  const plan = bootPlan(window.FX_CONFIG);
  const forcePublic = previewPublic(location.search);
  const url = dataUrl(location.search);
  let data;
  try {
    const res = await fetch(url, { cache: "no-store" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    data = await res.json();
  } catch (err) {
    showError(`讀不到 ${url}（${err.message}）。本機預覽請先執行 python -m fx_alert build-site。`);
    return;
  }
  if (data.schema !== 1 || !Array.isArray(data.currencies)) {
    showError("data.json 格式不符，請重新執行 build-site。");
    return;
  }
  renderStatus(data, now);
  chartWindow = historyWindow(data);
  $("chart-title").textContent = `${chartWindow.label}走勢`;
  if (data.repo_url) $("repo-link").href = data.repo_url;
  $("generated-at").textContent = formatTime(data.generated_at);

  if (plan.targetsFromDataJson && !forcePublic) {
    // 登入功能關閉：和單人版完全一樣
    renderCurrencies(data.currencies);
    renderAlerts(data);
    $("alerts-panel").hidden = false;
    $("edit-panel").hidden = false;
    if (data.edit_config_url) $("edit-link").href = data.edit_config_url;
    return;
  }

  // 登入功能開啟：先畫公開行情，登入後由 account.js 加上自己的目標價
  $("privacy-note").hidden = false;
  const publicList = publicCurrencies(data.currencies);
  cardHint = "登入後可以設定目標價，到價時寄 Email 給你";
  converterHint = "登入後可以比較目標價";
  renderCurrencies(publicList);
  if (window.FxAccount && plan.auth) {
    window.FxAccount.start(plan.auth, publicList, (personal, loggedIn) => {
      cardHint = loggedIn ? "在下方「我的提醒」設定目標價" : "登入後可以設定目標價，到價時寄 Email 給你";
      converterHint = loggedIn ? null : "登入後可以比較目標價";
      renderCurrencies(personal);
    });
  }
}

// 瀏覽器裡直接執行；在 Node（測試）裡只匯出純函式
if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    dataUrl, previewNow, previewPublic, formatTime, formatDate, timeAgo, formatPct, rateHint, cardView,
    toMinutes, inCheckHours, nextCheck, isStale, statusLine, parseAmount, converterView,
    yRange, formatChange, periodChange, historyWindow, timeTicks, tickLabel,
    markerIndexes, chartSummary, bootPlan, publicCurrencies,
  };
} else {
  window.FxApp = { formatTime };
  main();
}
