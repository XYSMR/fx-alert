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

const STALE_AFTER_MINUTES = 120;

/** "09:00" → 540 */
function toMinutes(hhmm) {
  const [h, m] = String(hhmm).split(":").map(Number);
  return h * 60 + m;
}

/**
 * 資料是不是「太久沒更新」？只有在檢查時段內才算：
 * 從「今天開始檢查的時間」和「最後一次檢查」中較晚的那個算起，超過 2 小時就算過期。
 * （週末、晚上沒有新資料是正常的；早上剛開始的前 2 小時也不會誤報。）
 */
function isStale(lastCheck, hours, now = new Date()) {
  if (!hours) return false;
  const tw = new Date(now.getTime() + 8 * 3600 * 1000); // 換成台灣時間（UTC+8，無日光節約）
  const day = tw.getUTCDay();
  if (hours.weekdays_only && (day === 0 || day === 6)) return false;
  const nowMin = tw.getUTCHours() * 60 + tw.getUTCMinutes();
  const start = toMinutes(hours.start);
  if (nowMin < start || nowMin > toMinutes(hours.end)) return false;
  const sinceOpen = nowMin - start;
  const sinceCheck = lastCheck ? (now - new Date(lastCheck.at)) / 60000 : Infinity;
  return Math.min(sinceOpen, sinceCheck) > STALE_AFTER_MINUTES;
}

/** 頁頂提示：最後檢查有錯誤 → 提示；太久沒更新 → 提示；都沒有 → null */
function bannerText(data, now = new Date()) {
  const lc = data.last_check;
  if (lc && lc.ok === false) {
    return `最後一次檢查（${formatTime(lc.at)}）發生 ${lc.errors} 個錯誤，資料可能不完整。`;
  }
  if (isStale(lc, data.business_hours, now)) {
    const when = lc ? `最後一次檢查是 ${timeAgo(lc.at, now)}` : "今天還沒有檢查紀錄";
    return `資料可能過期：${when}。`;
  }
  return null;
}

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
    const stale = lc.ok && isStale(lc, data.business_hours);
    const label = !lc.ok ? " ✕ 有錯誤" : stale ? " ⚠ 可能過期" : " ✓ 正常";
    el.textContent = `${formatTime(lc.at)}（${timeAgo(lc.at)}）${label}`;
    el.className = !lc.ok ? "bad" : stale ? "warn" : "ok";
  } else {
    el.textContent = "尚未執行";
  }
  setText("quoted-at", formatTime(data.quoted_at));

  const banner = document.getElementById("status-banner");
  const message = bannerText(data);
  banner.hidden = !message;
  if (message) {
    const link = document.createElement("a");
    link.href = data.actions_url || "#";
    link.target = "_blank";
    link.rel = "noopener";
    link.textContent = "查看 GitHub Actions";
    banner.replaceChildren(`${message} `, link);
  }
  if (data.edit_config_url) document.getElementById("edit-link").href = data.edit_config_url;
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

// ---------- 走勢圖 ----------

const COLORS = { rate: "#2f5fb3", target: "#a14a00", grid: "#e6e9ee", text: "#5b6676" };
let chart = null;

/** 走勢摘要文字（也給螢幕閱讀器用）：最高、最低、最新 */
function chartSummary(c) {
  if (!c.history.length) return `${c.code} 近 30 天還沒有走勢資料。`;
  const values = c.history.map(([, v]) => Number(v));
  const fmt = (n) => n.toFixed(4);
  return `${c.code} 近 30 天：最高 ${fmt(Math.max(...values))}、最低 ${fmt(Math.min(...values))}、` +
    `最新 ${c.rate}，目標 ≤ ${c.target}（共 ${values.length} 份牌告）。`;
}

function drawChart(c) {
  const labels = c.history.map(([t]) => formatTime(t));
  const rates = c.history.map(([, v]) => Number(v));
  const target = Number(c.target);
  const canvas = document.getElementById("chart");
  canvas.setAttribute("aria-label", chartSummary(c));
  setText("chart-summary", chartSummary(c));

  if (chart) chart.destroy();
  chart = null;
  // 沒有走勢資料：不畫空白座標，只留下說明文字
  document.querySelector(".chart-box").hidden = c.history.length === 0;
  if (c.history.length === 0) return;
  if (typeof Chart === "undefined") {
    setText("chart-summary", "圖表元件載入失敗。" + chartSummary(c));
    return;
  }
  chart = new Chart(canvas, {
    type: "line",
    data: {
      labels,
      datasets: [
        {
          label: `${c.code} 現金賣出`,
          data: rates,
          borderColor: COLORS.rate,
          backgroundColor: COLORS.rate,
          borderWidth: 2,
          pointRadius: 0,
          pointHitRadius: 8,
          tension: 0.2,
        },
        {
          label: `目標 ${c.target}`,
          data: labels.map(() => target),
          borderColor: COLORS.target,
          borderWidth: 2,
          borderDash: [6, 4],
          pointRadius: 0,
          pointHitRadius: 0,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { labels: { color: COLORS.text, boxHeight: 2 } },
        tooltip: { callbacks: { label: (ctx) => `${ctx.dataset.label}：${Number(ctx.parsed.y).toFixed(4)}` } },
      },
      scales: {
        x: {
          // 軸上只顯示日期（"11/09"），完整時間在提示框裡，手機上才不會擠在一起
          ticks: {
            color: COLORS.text,
            maxRotation: 0,
            maxTicksLimit: window.innerWidth < 640 ? 4 : 7,
            callback(value) { return this.getLabelForValue(value).split(" ")[0]; },
          },
          grid: { display: false },
        },
        y: {
          ticks: { color: COLORS.text, callback: (v) => Number(v).toFixed(4) },
          grid: { color: COLORS.grid },
          // 讓目標線一定在圖裡看得到，就算匯率離目標很遠
          suggestedMin: Math.min(target, ...rates),
          suggestedMax: Math.max(target, ...rates),
        },
      },
    },
  });
}

function renderChart(data) {
  const tabs = document.getElementById("chart-tabs");
  const select = (code) => {
    for (const b of tabs.children) b.setAttribute("aria-selected", String(b.dataset.code === code));
    drawChart(data.currencies.find((c) => c.code === code));
  };
  tabs.replaceChildren(...data.currencies.map((c) => {
    const b = document.createElement("button");
    b.type = "button";
    b.setAttribute("role", "tab");
    b.dataset.code = c.code;
    b.textContent = c.code;
    b.addEventListener("click", () => select(c.code));
    return b;
  }));
  if (data.currencies.length) select(data.currencies[0].code);
}

// ---------- 到價通知紀錄 ----------

function renderAlerts(data) {
  const alerts = Array.isArray(data.alerts) ? data.alerts : [];
  document.getElementById("alerts-table").hidden = alerts.length === 0;
  document.getElementById("alerts-empty").hidden = alerts.length > 0;
  document.getElementById("alerts-body").replaceChildren(...alerts.map((a) => {
    const tr = document.createElement("tr");
    for (const text of [formatTime(a.sent_at), a.currency, a.rate, `≤ ${a.target}`]) {
      const td = document.createElement("td");
      td.textContent = text;
      tr.appendChild(td);
    }
    return tr;
  }));
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
  renderChart(data);
  renderAlerts(data);
}

// 瀏覽器裡直接執行；在 Node（測試）裡則只匯出純函式
if (typeof module !== "undefined" && module.exports) {
  module.exports = { dataUrl, formatTime, timeAgo, distanceText, toMinutes, isStale, bannerText, chartSummary };
} else {
  main();
}
