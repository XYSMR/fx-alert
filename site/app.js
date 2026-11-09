// FX Alert 前端：讀 data.json，畫出頁面。
// 所有計算（距離 %、狀態）都在 Python 的 build-site 做好了，這裡只負責「顯示」。
// 一律用 textContent 放文字（不用 innerHTML），避免資料裡的內容被當成 HTML 執行。
"use strict";

// ---------- 小工具（純函式，方便日後測試） ----------

/** 本機測試可用 ?data=samples/triggered.json 換資料；只允許站內的 .json 檔 */
function dataUrl(search) {
  const name = new URLSearchParams(search).get("data");
  return name && /^[\w-]+(\/[\w-]+)*\.json$/.test(name) ? name : "data.json";
}

/** "2026-11-09T16:10:12+08:00" → "11/09 16:10"（一律顯示台灣時間） */
function formatTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  const parts = new Intl.DateTimeFormat("zh-TW", {
    timeZone: "Asia/Taipei", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hour12: false,
  }).formatToParts(d);
  const get = (type) => parts.find((p) => p.type === type)?.value ?? "";
  return `${get("month")}/${get("day")} ${get("hour")}:${get("minute")}`;
}

/** 多久以前：「剛剛」「5 分鐘前」「3 小時前」「2 天前」 */
function timeAgo(iso, now = new Date()) {
  const diff = (now - new Date(iso)) / 60000; // 分鐘
  if (!Number.isFinite(diff)) return "";
  if (diff < 1) return "剛剛";
  if (diff < 60) return `${Math.floor(diff)} 分鐘前`;
  if (diff < 60 * 24) return `${Math.floor(diff / 60)} 小時前`;
  return `${Math.floor(diff / 60 / 24)} 天前`;
}

/** 距離目標的文字：正數 = 還要跌，0 = 剛好，負數 = 已低於 */
function distanceText(pct) {
  if (pct === null || pct === undefined) return "尚無匯率資料";
  if (pct > 0) return `還要跌 ${pct.toFixed(2)}%`;
  if (pct === 0) return "剛好等於目標";
  return `已低於目標 ${Math.abs(pct).toFixed(2)}%`;
}

const STATUS_LABEL = { waiting: "等待中", triggered: "已到價" };

// ---------- 畫面 ----------

function setText(id, text) {
  document.getElementById(id).textContent = text;
}

function renderHeader(data) {
  const src = data.source || {};
  setText("source", `${src.bank ?? "兆豐銀行"}・${src.rate_type ?? "現金賣出"}`);

  const lc = data.last_check;
  const el = document.getElementById("last-check");
  if (lc) {
    el.textContent = `${formatTime(lc.at)}（${timeAgo(lc.at)}）${lc.ok ? " ✓ 正常" : " ✕ 有錯誤"}`;
    el.className = lc.ok ? "ok" : "bad";
  } else {
    el.textContent = "尚未執行";
  }
  setText("quoted-at", formatTime(data.quoted_at));
  setText("generated-at", formatTime(data.generated_at));
  if (data.repo_url) document.getElementById("repo-link").href = data.repo_url;
}

function renderCard(c) {
  const node = document.getElementById("card-template").content.cloneNode(true);
  const card = node.querySelector(".card");
  const q = (sel) => node.querySelector(sel);
  const status = c.status === "triggered" ? "triggered" : "waiting";

  card.classList.add(`is-${status}`);
  q(".code").textContent = c.code;
  q(".name").textContent = c.name;
  q(".badge").textContent = STATUS_LABEL[status];
  q(".badge").classList.add(status);
  q(".rate").textContent = c.rate ?? "—";
  q(".target").textContent = `目標 ≤ ${c.target}`;
  q(".distance").textContent = distanceText(c.distance_pct);
  if (c.distance_pct !== null && c.distance_pct <= 0) q(".distance").classList.add("below");
  q(".since").textContent = c.status_since
    ? `${STATUS_LABEL[status]}，從 ${formatTime(c.status_since)} 開始`
    : "";
  q(".rearm").textContent = status === "triggered"
    ? `已通知過；回升到 > ${c.rearm_above} 後再跌回目標，才會再通知`
    : "";
  return node;
}

function renderCards(data) {
  const box = document.getElementById("cards");
  box.replaceChildren(...data.currencies.map(renderCard));
}

function showError(message) {
  const el = document.getElementById("load-error");
  el.textContent = message;
  el.hidden = false;
  document.getElementById("cards").replaceChildren();
}

async function main() {
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
    showError("data.json 格式不符（schema 版本不對），請重新執行 build-site。");
    return;
  }
  renderHeader(data);
  renderCards(data);
}

main();
