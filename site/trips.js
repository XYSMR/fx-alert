// 出國換匯計畫：建立旅程、旅程進度、記錄已經換了多少。
// - 登入後由 account.js 呼叫 FxTrips.show()，登出時 FxTrips.hide()。
// - 能讀寫什麼由資料庫的 RLS 決定（只能碰自己的旅程）；寄提醒是排程的工作，這裡只顯示。
// - 提醒日期的算法和 Python 的 src/fx_alert/trip_rules.py schedule() 一樣，
//   site/tests/trips.test.js 用 tests/fixtures/trip_schedules.json 逐日比對。
// - 用語只描述事實（第幾低、還有幾天、哪天寄信），不談匯率之後的走向、不叫人買賣。
//   site/tests/trips.test.js 會檢查這個檔案沒有出現不該用的字。
// - 一律用 textContent 放文字。上半部是純函式。
"use strict";

// 整個檔案包在自己的函式範圍裡：網頁上的 script 共用全域，app.js 也有 parseAmount 這種名字，
// 不包起來會互相覆蓋（site/tests/trips.test.js 檢查全域只多了 FxTrips）。
(function () {

  const TRIP_DISCLAIMER = "僅供參考，不保證未來匯率走勢";
  const MAX_TRIPS = 5;
  const MAX_EXCHANGES = 50;
  const CENTS = 100n;
  const MAX_AMOUNT = 1000000000000n * CENTS; // 1e12，和資料庫一樣
  const FAR_PCT = 20;
  const NEAR_PCT = 40;

  const KIND_LABELS = {
    cheap_far: "近 30 天相對低的價格",
    near_start: "距離出發約 3 週，可以考慮分批",
    cheap_near: "近 30 天相對低的價格",
    final: "最後換匯提醒",
    last_call: "出發前提醒",
  };

  // ====================================================================
  // 純函式：日期
  // ====================================================================

  /** "2027-01-17" → UTC 午夜的毫秒數（只用來算日期，不牽涉時區） */
  function dayMs(iso) {
    const [y, m, d] = iso.split("-").map(Number);
    return Date.UTC(y, m - 1, d);
  }

  function isoOf(ms) {
    return new Date(ms).toISOString().slice(0, 10);
  }

  function addDays(iso, n) {
    return isoOf(dayMs(iso) + n * 86400000);
  }

  function daysBetween(fromIso, toIso) {
    return Math.round((dayMs(toIso) - dayMs(fromIso)) / 86400000);
  }

  /** 台灣時間的今天（"2026-11-16"） */
  function taipeiToday(now = new Date()) {
    return new Date(now.getTime() + 8 * 3600 * 1000).toISOString().slice(0, 10);
  }

  /** "2027-01-17" → "1/17（日）" */
  function shortDate(iso) {
    const d = new Date(dayMs(iso));
    return `${d.getUTCMonth() + 1}/${d.getUTCDate()}（${"日一二三四五六"[d.getUTCDay()]}）`;
  }

  // ====================================================================
  // 純函式：營業日與提醒日期（和 trip_rules.schedule 一樣）
  // ====================================================================

  /** data.json 的 bank_calendar → 營業日判斷 */
  function makeCalendar(json) {
    const off = new Set((json && json.off) || []);
    const work = new Set((json && json.workday) || []);
    const isBusinessDay = (iso) => {
      if (work.has(iso)) return true;
      const wd = new Date(dayMs(iso)).getUTCDay();
      return wd !== 0 && wd !== 6 && !off.has(iso);
    };
    const onOrBefore = (iso) => {
      let d = iso;
      while (!isBusinessDay(d)) d = addDays(d, -1);
      return d;
    };
    /** iso 之前（不含）的第 n 個營業日 */
    const before = (iso, n = 1) => {
      let d = iso;
      for (let i = 0; i < n; i += 1) d = onOrBefore(addDays(d, -1));
      return d;
    };
    return { isBusinessDay, onOrBefore, before, until: (json && json.until) || null };
  }

  function minIso(a, b) {
    return a < b ? a : b;
  }

  function schedule(departure, cal) {
    const lastCall = cal.before(departure, 2);
    const finalOn = minIso(cal.onOrBefore(addDays(departure, -7)), cal.before(departure, 3));
    const nearOn = minIso(cal.onOrBefore(addDays(departure, -21)), cal.before(finalOn, 1));
    return { departure, nearOn, finalOn, lastCall, spare: cal.before(departure, 1) };
  }

  function phase(sch, today) {
    if (today >= sch.departure) return "departed";
    if (today >= sch.finalOn) return "final";
    if (today >= sch.nearOn) return "near";
    return "far";
  }

  /**
   * 進度頁的時程：每一項 { date, text, state }，state = done（已過）/ today / next。
   * 建立旅程時已經過了的固定提醒不會寄，標成 skipped。
   */
  function milestones(sch, today, createdOn) {
    const items = [
      { date: sch.nearOn, text: "寄一封「可以考慮分批換」的提醒；之後價格在近 30 天的前 40% 低時寄提醒（5 天最多 1 封）" },
      { date: sch.finalOn, text: "最後換匯提醒（不看價格）" },
      { date: sch.lastCall, text: `出發前提醒（不看價格）；${shortDate(sch.spare)} 是出發前最後一個營業日` },
    ];
    return items.map((it, i) => {
      const late = i === 2 ? createdOn >= it.date : createdOn > it.date;
      let state = it.date < today ? "done" : it.date === today ? "today" : "next";
      if (late) state = "skipped";
      return { ...it, state };
    });
  }

  /** 標題下的一句話：距離出發還有幾天、現在是哪個階段 */
  function phaseText(sch, today) {
    const left = daysBetween(today, sch.departure);
    switch (phase(sch, today)) {
      case "departed":
        return "已經出發，不再寄提醒。";
      case "final":
        return `距離出發還有 ${left} 天。已進入最後階段，${shortDate(sch.lastCall)} 會再寄一封出發前提醒。`;
      case "near":
        return `距離出發還有 ${left} 天。價格在近 30 天的前 ${NEAR_PCT}% 低時寄提醒；` +
          `${shortDate(sch.finalOn)} 寄最後換匯提醒。`;
      default:
        return `距離出發還有 ${left} 天。價格在近 30 天的前 ${FAR_PCT}% 低時寄提醒（7 天最多 1 封）；` +
          `${shortDate(sch.nearOn)} 起進入接近出發的階段。`;
    }
  }

  // ====================================================================
  // 純函式：金額
  // ====================================================================

  /** "50000" / "1234.5" → 分（BigInt）；格式不對回傳 null。最多 2 位小數，可以有千分位逗號 */
  function parseAmount(text) {
    const s = String(text ?? "").trim().replace(/,/g, "");
    const m = /^(\d+)(?:\.(\d{1,2}))?$/.exec(s);
    if (!m) return null;
    return BigInt(m[1]) * CENTS + BigInt((m[2] ?? "").padEnd(2, "0"));
  }

  /** 分 → "50,000" / "1,234.5" */
  function formatAmount(cents) {
    const neg = cents < 0n;
    const v = neg ? -cents : cents;
    const int = (v / CENTS).toString().replace(/\B(?=(\d{3})+(?!\d))/g, ",");
    const frac = (v % CENTS).toString().padStart(2, "0").replace(/0+$/, "");
    return (neg ? "-" : "") + (frac ? `${int}.${frac}` : int);
  }

  /** 資料庫的 numeric（字串或數字）→ 分 */
  function amountOf(value) {
    return parseAmount(String(value ?? "0")) ?? 0n;
  }

  function validateAmount(text, label = "金額") {
    const cents = parseAmount(text);
    if (cents === null) return { error: `${label}請輸入數字，例如 50000（最多 2 位小數）` };
    if (cents <= 0n) return { error: `${label}要大於 0` };
    if (cents >= MAX_AMOUNT) return { error: `${label}太大了` };
    return { cents, value: `${cents / CENTS}.${(cents % CENTS).toString().padStart(2, "0")}` };
  }

  /** 換匯的匯率（可以不填）：1 外幣 = 幾元台幣，最多 4 位小數 */
  function validateRate(text) {
    const s = String(text ?? "").trim();
    if (s === "") return { value: null };
    if (!/^\d+(\.\d{1,4})?$/.test(s) || !(Number(s) > 0) || Number(s) >= 1000) {
      return { error: "匯率請輸入數字（1 外幣 = 幾元台幣，最多 4 位小數），或留空" };
    }
    return { value: s };
  }

  /** 出發日：明天到一年內（和資料庫一樣，用台灣時間的今天） */
  function validateDeparture(iso, today) {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(String(iso ?? ""))) return { error: "請選擇出發日" };
    if (iso <= today) return { error: "出發日要在明天以後" };
    if (iso > addDays(today, 365)) return { error: "出發日要在一年內" };
    return { value: iso };
  }

  function validateName(text) {
    const s = String(text ?? "").trim();
    if (s === "") return { value: null };
    if ([...s].length > 30) return { error: "名稱最多 30 個字" };
    return { value: s };
  }

  /** trip_progress 的一列 → 進度 */
  function progress(trip) {
    const amount = amountOf(trip.amount);
    const exchanged = amountOf(trip.exchanged);
    const remaining = exchanged >= amount ? 0n : amount - exchanged;
    const pct = amount > 0n ? Number((exchanged * 1000n) / amount) / 10 : 0;
    return { amount, exchanged, remaining, pct: Math.min(pct, 100), done: remaining === 0n };
  }

  /** 剩下的外幣以現在的價格約需多少台幣（四捨五入到元） */
  function estimateTwd(remainingCents, rate) {
    if (!rate || !(Number(rate) > 0)) return null;
    const [i, f = ""] = String(rate).split(".");
    const rate4 = BigInt(i) * 10000n + BigInt(f.slice(0, 4).padEnd(4, "0"));
    const twd = (remainingCents * rate4 + 500000n) / 1000000n; // 分 × 萬分之一 → 元
    return twd.toString().replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  }

  // ====================================================================
  // 純函式：價格位置
  // ====================================================================

  /** data.json 的 recent（近 30 天收盤價統計）→ 一句事實描述 */
  function rankText(recent, rate) {
    if (!rate) return "目前沒有報價。";
    if (!recent || !recent.enough) {
      const n = recent ? recent.days : 0;
      return `現在 ${rate}。近 30 天的收盤價只有 ${n} 個營業日，資料還不夠，先不比較高低。`;
    }
    return `現在 ${rate}，在近 30 天（${recent.days} 個營業日的收盤價）中是第 ${recent.lower + 1} 低。`;
  }

  /** Supabase / PostgREST 的錯誤 → 給使用者看的話 */
  function tripErrorMessage(err) {
    const msg = String((err && err.message) || "");
    if (msg.includes("5 趟")) return `每人最多 ${MAX_TRIPS} 趟還沒出發的旅程`;
    if (msg.includes("50 筆")) return `每趟旅程最多 ${MAX_EXCHANGES} 筆換匯紀錄`;
    if (msg.includes("出發日")) return "出發日要在明天到一年內";
    const code = err && err.code;
    if (code === "23514") return "數值不符合規則，請檢查金額和日期";
    if (code === "23503") return "找不到這趟旅程，請重新整理頁面";
    if (code === "42501" || code === "PGRST301") return "沒有權限，請重新登入";
    return "儲存失敗，請稍後再試";
  }

  /** 資料庫還沒有換匯計畫的表（migration 還沒套用）：整個區塊不顯示 */
  function tablesMissing(err) {
    const code = err && err.code;
    return code === "42P01" || code === "PGRST205";
  }

  /** 還沒出發的旅程數（和資料庫的上限判斷一樣：出發日 > 今天） */
  function upcomingCount(trips, today) {
    return trips.filter((t) => t.departure > today).length;
  }

  /** 列表順序：還沒出發的依出發日，已出發的放後面（最近出發的在前） */
  function sortTrips(trips, today) {
    const up = trips.filter((t) => t.departure > today).sort((a, b) => (a.departure < b.departure ? -1 : 1));
    const past = trips.filter((t) => t.departure <= today).sort((a, b) => (a.departure > b.departure ? -1 : 1));
    return [...up, ...past];
  }

  // ====================================================================
  // 畫面
  // ====================================================================

  const trip$ = (id) => document.getElementById(id);
  let tClient = null;
  let tList = [];
  let tCal = makeCalendar(null);
  let tData = { trips: [], exchanges: [], notices: [] };

  function th(tag, attrs = {}, ...children) {
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

  function tSay(text, tone = "ok") {
    const box = trip$("my-trips-status");
    box.className = `form-status ${tone}`;
    box.textContent = text;
  }

  function nameOf(code) {
    const c = tList.find((x) => x.code === code);
    return c ? c.name : code;
  }

  function currencyOf(code) {
    return tList.find((x) => x.code === code) || null;
  }

  async function loadTrips() {
    const [t, e, n] = await Promise.all([
      tClient.from("trip_progress")
        .select("id,currency,departure,amount,name,created_at,exchanged,remaining,exchange_count")
        .order("departure"),
      tClient.from("trip_exchanges")
        .select("id,trip_id,amount,rate,exchanged_on")
        .order("exchanged_on", { ascending: false }),
      tClient.from("trip_notifications")
        .select("trip_id,kind,sent_on,rate,rank,window_days")
        .order("sent_at", { ascending: false })
        .limit(100),
    ]);
    const err = t.error || e.error || n.error;
    if (err) throw err;
    tData = { trips: t.data ?? [], exchanges: e.data ?? [], notices: n.data ?? [] };
  }

  async function reload(message) {
    try {
      await loadTrips();
    } catch (err) {
      if (tablesMissing(err)) {
        trip$("my-trips").hidden = true;
        return;
      }
      tSay("讀不到你的旅程，請重新整理頁面", "error");
      return;
    }
    render();
    if (message) tSay(message);
  }

  function currencySelect(id) {
    const popular = tList.filter((c) => c.popular_rank).sort((a, b) => a.popular_rank - b.popular_rank);
    const others = tList.filter((c) => !c.popular_rank).sort((a, b) => (a.sort_order ?? 99) - (b.sort_order ?? 99));
    const group = (label, items) => (items.length
      ? th("optgroup", { label }, ...items.map((c) => th("option", { value: c.code }, `${c.name} ${c.code}`)))
      : null);
    return th("select", { id, "aria-label": "幣別" }, group("常用", popular), group("其他", others));
  }

  function addTripForm(today) {
    const count = upcomingCount(tData.trips, today);
    if (count >= MAX_TRIPS) {
      return th("p", { class: "hint" }, `已經有 ${MAX_TRIPS} 趟還沒出發的旅程（上限）。出發後或刪除一趟才能再新增。`);
    }
    const select = currencySelect("trip-currency");
    const dep = th("input", {
      type: "date", id: "trip-departure", min: addDays(today, 1), max: addDays(today, 365),
      "aria-label": "出發日", required: true,
    });
    const amount = th("input", {
      type: "text", inputmode: "decimal", id: "trip-amount", autocomplete: "off",
      "aria-label": "需要的外幣金額", placeholder: "例如 50000",
    });
    const name = th("input", {
      type: "text", id: "trip-name", autocomplete: "off", maxlength: "30",
      "aria-label": "名稱（可不填）", placeholder: "例如 東京",
    });
    const preview = th("p", { class: "hint", id: "trip-preview" });
    const update = () => {
      const d = validateDeparture(dep.value, today);
      if (d.error) {
        preview.textContent = "";
        return;
      }
      const s = schedule(d.value, tCal);
      const parts = [`${shortDate(s.nearOn)} 起可以考慮分批`, `${shortDate(s.finalOn)} 最後換匯提醒`,
        `${shortDate(s.lastCall)} 出發前提醒`].filter((_, i) => [s.nearOn, s.finalOn, s.lastCall][i] > today);
      preview.textContent = parts.length ? `預計寄信的日子：${parts.join("、")}（都會避開週末和國定假日）。` : "";
    };
    dep.addEventListener("change", update);
    const submit = async (event) => {
      event.preventDefault();
      const d = validateDeparture(dep.value, today);
      if (d.error) return tSay(d.error, "error");
      const a = validateAmount(amount.value, "外幣金額");
      if (a.error) return tSay(a.error, "error");
      const n = validateName(name.value);
      if (n.error) return tSay(n.error, "error");
      const { error } = await tClient.from("trips")
        .insert({ currency: select.value, departure: d.value, amount: a.value, name: n.value });
      if (error) return tSay(tripErrorMessage(error), "error");
      await reload(`已建立${n.value ?? nameOf(select.value)}的旅程`);
    };
    return th("form", { class: "rule add", id: "trip-add-form", onsubmit: submit },
      th("strong", {}, "＋ 新增旅程"),
      th("label", { class: "field" }, th("span", {}, "幣別"), select),
      th("label", { class: "field" }, th("span", {}, "出發日"), dep),
      th("label", { class: "field" }, th("span", {}, "需要的外幣金額"), amount),
      th("label", { class: "field" }, th("span", {}, "名稱（可不填）"), name),
      th("button", { type: "submit", class: "primary" }, "建立"),
      preview);
  }

  function exchangeForm(trip, today) {
    const amount = th("input", {
      type: "text", inputmode: "decimal", autocomplete: "off", "aria-label": "這次換了多少外幣",
    });
    const rate = th("input", {
      type: "text", inputmode: "decimal", autocomplete: "off", "aria-label": "匯率（可不填）",
      placeholder: "可不填",
    });
    const on = th("input", { type: "date", value: today, max: today, "aria-label": "換匯日期" });
    const submit = async (event) => {
      event.preventDefault();
      const a = validateAmount(amount.value, "換了多少");
      if (a.error) return tSay(a.error, "error");
      const r = validateRate(rate.value);
      if (r.error) return tSay(r.error, "error");
      const { error } = await tClient.from("trip_exchanges")
        .insert({ trip_id: trip.id, amount: a.value, rate: r.value, exchanged_on: on.value || today });
      if (error) return tSay(tripErrorMessage(error), "error");
      await reload(`已記錄換了 ${formatAmount(a.cents)} ${nameOf(trip.currency)}`);
    };
    return th("form", { class: "rule-main trip-exchange", onsubmit: submit },
      th("label", { class: "field" }, th("span", {}, "這次換了多少"), amount),
      th("label", { class: "field" }, th("span", {}, "匯率"), rate),
      th("label", { class: "field" }, th("span", {}, "日期"), on),
      th("button", { type: "submit", class: "primary" }, "記錄"));
  }

  function exchangeList(trip) {
    const rows = tData.exchanges.filter((x) => x.trip_id === trip.id);
    if (!rows.length) return null;
    const remove = (x) => async () => {
      if (!confirm(`刪除 ${x.exchanged_on} 換的 ${formatAmount(amountOf(x.amount))}？`)) return;
      const { error } = await tClient.from("trip_exchanges").delete().eq("id", x.id);
      if (error) return tSay(tripErrorMessage(error), "error");
      await reload("已刪除換匯紀錄");
    };
    return th("details", { class: "trip-records" },
      th("summary", {}, `換匯紀錄（${rows.length} 筆）`),
      th("table", { class: "alerts" },
        th("thead", {}, th("tr", {}, th("th", { scope: "col" }, "日期"), th("th", { scope: "col" }, "金額"),
          th("th", { scope: "col" }, "匯率"), th("th", { scope: "col" }, ""))),
        th("tbody", {}, ...rows.map((x) => th("tr", {},
          th("td", {}, x.exchanged_on),
          th("td", {}, formatAmount(amountOf(x.amount))),
          th("td", {}, x.rate === null ? "—" : String(x.rate)),
          th("td", {}, th("button", { type: "button", class: "link danger", onclick: remove(x) }, "刪除")))))));
  }

  function editForm(trip) {
    const amount = th("input", {
      type: "text", inputmode: "decimal", autocomplete: "off", value: formatAmount(amountOf(trip.amount)),
      "aria-label": "需要的外幣金額",
    });
    const name = th("input", { type: "text", autocomplete: "off", maxlength: "30", value: trip.name ?? "",
      "aria-label": "名稱" });
    const save = async (event) => {
      event.preventDefault();
      const a = validateAmount(amount.value, "外幣金額");
      if (a.error) return tSay(a.error, "error");
      const n = validateName(name.value);
      if (n.error) return tSay(n.error, "error");
      const { error } = await tClient.from("trips").update({ amount: a.value, name: n.value }).eq("id", trip.id);
      if (error) return tSay(tripErrorMessage(error), "error");
      await reload("已更新旅程");
    };
    const remove = async () => {
      if (!confirm("確定要刪除這趟旅程嗎？換匯紀錄和提醒紀錄會一起刪除。")) return;
      const { error } = await tClient.from("trips").delete().eq("id", trip.id);
      if (error) return tSay(tripErrorMessage(error), "error");
      await reload("已刪除旅程");
    };
    return th("details", { class: "advanced" },
      th("summary", {}, "修改或刪除"),
      th("form", { class: "rule-main", onsubmit: save },
        th("label", { class: "field" }, th("span", {}, "需要的外幣金額"), amount),
        th("label", { class: "field" }, th("span", {}, "名稱"), name),
        th("button", { type: "submit", class: "primary" }, "儲存")),
      th("p", { class: "hint" }, "出發日和幣別不能修改（提醒日期是從出發日算的）；要改請刪除後重新建立。"),
      th("button", { type: "button", class: "link danger", onclick: remove }, "刪除這趟旅程"));
  }

  function tripCard(trip, today) {
    const sch = schedule(trip.departure, tCal);
    const p = progress(trip);
    const cur = currencyOf(trip.currency);
    const rate = cur ? cur.rate : null;
    const name = nameOf(trip.currency);
    const title = trip.name ? `${trip.name}・${name}` : `${name}旅程`;
    const createdOn = taipeiToday(new Date(trip.created_at));
    const departed = phase(sch, today) === "departed";
    const twd = p.done ? null : estimateTwd(p.remaining, rate);
    const notices = tData.notices.filter((x) => x.trip_id === trip.id).slice(0, 3);
    const steps = milestones(sch, today, createdOn);
    const stepLabel = { done: "已過", today: "今天", next: "", skipped: "建立時已過，不寄" };

    return th("article", { class: "trip", "data-trip": String(trip.id) },
      th("div", { class: "card-head" },
        th("h3", {}, title, " ", th("span", { class: "code" }, trip.currency)),
        th("span", { class: "badge tone-neutral" }, `${shortDate(trip.departure)} 出發`)),
      th("p", { class: "hint" }, p.done && !departed
        ? `距離出發還有 ${daysBetween(today, trip.departure)} 天。`
        : phaseText(sch, today)),
      th("div", { class: "trip-progress" },
        th("progress", { max: "100", value: String(p.pct), "aria-label": "已換的比例" }),
        th("p", {}, `已換 ${formatAmount(p.exchanged)} / ${formatAmount(p.amount)} ${name}，`,
          p.done ? "已經換完，不再寄提醒。" : `還剩 ${formatAmount(p.remaining)}。`)),
      departed || p.done ? null : th("p", {}, rankText(cur && cur.recent, rate),
        twd ? ` 剩下的金額以現在的現金賣出價約需 ${twd} 元台幣（未含手續費、面額限制）。` : ""),
      departed || p.done ? null : th("ul", { class: "trip-steps" },
        ...steps.map((s) => th("li", { class: `step-${s.state}` },
          th("b", {}, shortDate(s.date)), ` ${s.text}`,
          stepLabel[s.state] ? th("span", { class: "sub" }, `（${stepLabel[s.state]}）`) : null))),
      notices.length ? th("p", { class: "hint" }, "最近的提醒：",
        notices.map((x) => `${shortDate(x.sent_on)} ${KIND_LABELS[x.kind] ?? x.kind}`).join("、")) : null,
      departed ? null : exchangeForm(trip, today),
      exchangeList(trip),
      editForm(trip));
  }

  function render() {
    const today = taipeiToday();
    const trips = sortTrips(tData.trips, today);
    trip$("my-trips").hidden = false;
    const parts = [
      th("p", { class: "hint" },
        "輸入出發日和需要的外幣金額，系統依「近 30 天的價格」和「距離出發的天數」寄 Email 提醒，" +
        "可以記錄已經換了多少，剩下的繼續追蹤。只在銀行營業日寄信。"),
      th("p", { class: "disclaimer" }, `※ ${TRIP_DISCLAIMER}。實際匯率以兆豐銀行公告為準。`),
      ...trips.map((t) => tripCard(t, today)),
      trips.length ? null : th("p", { class: "empty" }, "還沒有旅程。"),
      addTripForm(today),
    ];
    // replaceChildren 會把 null 當成文字「null」，先拿掉
    trip$("my-trips-body").replaceChildren(...parts.filter((x) => x !== null));
  }

  async function show(client, currencies, calendarJson) {
    tClient = client;
    tList = currencies;
    tCal = makeCalendar(calendarJson);
    await reload();
  }

  function hide() {
    tClient = null;
    tData = { trips: [], exchanges: [], notices: [] };
    const box = trip$("my-trips");
    if (box) box.hidden = true;
  }

  const exported = {
    TRIP_DISCLAIMER, KIND_LABELS, MAX_TRIPS,
    dayMs, addDays, daysBetween, taipeiToday, shortDate,
    makeCalendar, schedule, phase, milestones, phaseText,
    parseAmount, formatAmount, amountOf, validateAmount, validateRate, validateDeparture, validateName,
    progress, estimateTwd, rankText, tripErrorMessage, tablesMissing,
    upcomingCount, sortTrips,
  };
  if (typeof module !== "undefined" && module.exports) {
    module.exports = exported;
  } else {
    window.FxTrips = { show, hide };
  }
})();
