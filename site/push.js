// 手機推播：設定頁的「推播通知」區塊（推播 PR 3／5，設計見 docs/web-push.md）。
//
// - 只有 web_push flag 對這個人開啟時才顯示；新增訂閱一律呼叫資料庫的 save_push_subscription()，
//   它會再檢查一次 flag（隱藏按鈕不算權限控管）。
// - iPhone 的系統詢問只能問一次：按「開啟推播」先顯示我們自己的說明，按「好，開啟」才跳系統詢問。
// - Email 和推播互相獨立：退訂 Email 不影響推播，關掉推播也不影響 Email（設定頁上有說明表格）。
// - 每次打開網站都重新同步這台裝置的訂閱：Apple 的推播網址可能會換，而且失效時不一定會告訴我們。
// - 登出時停止這台裝置的推播（account.js 呼叫 forgetThisDevice）。
"use strict";

(function () {
  const STORAGE_KEY = "fx-push-device"; // 這台裝置為哪個帳號訂閱了哪個推播網址
  const P256DH_RE = /^[A-Za-z0-9_-]{87}=?$/;
  const AUTH_RE = /^[A-Za-z0-9_-]{22}(==)?$/;

  // ---------- 純函式（site/tests/push.test.js） ----------

  /**
   * 推播網址白名單：和資料庫的 is_allowed_push_endpoint() 規則相同
   * （tests/fixtures/push_endpoints.json 三邊共用）。資料庫才是真正的防線，這裡只是先給清楚的錯誤訊息。
   */
  function isAllowedEndpoint(url) {
    if (typeof url !== "string" || url.length > 2048) return false;
    if (/[\s\u0000-\u001f\u007f\\]/.test(url)) return false;
    const m = /^https:\/\/([a-z0-9.-]+)\//.exec(url);
    if (!m) return false;
    const host = m[1];
    return host === "fcm.googleapis.com" ||
      host === "updates.push.services.mozilla.com" ||
      /^([a-z0-9-]+\.)+push\.apple\.com$/.test(host) ||
      /^([a-z0-9-]+\.)+notify\.windows\.com$/.test(host);
  }

  /**
   * 這台裝置能不能開推播：
   *   "unsupported"  瀏覽器不支援
   *   "ios-install"  iPhone／iPad 還沒從主畫面打開（Safari 分頁裡不能訂閱）
   *   "denied"       之前按過「不允許」，網頁沒辦法再問
   *   "ready"        可以按「開啟推播」
   */
  function pushStatus(env) {
    if (env.iosNotInstalled) return "ios-install";
    if (!env.secure || !env.hasServiceWorker || !env.hasPushManager || !env.hasNotification) {
      return "unsupported";
    }
    if (env.permission === "denied") return "denied";
    return "ready";
  }

  /** VAPID 公鑰（base64url）→ pushManager.subscribe 要的 Uint8Array */
  function base64UrlToBytes(text) {
    const b64 = text.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((text.length + 3) % 4);
    const bin = atob(b64);
    return Uint8Array.from(bin, (c) => c.charCodeAt(0));
  }

  /** PushSubscription → 要存進資料庫的三個值；格式不對或網址不在白名單就回傳 null */
  function subscriptionKeys(sub) {
    const json = sub && typeof sub.toJSON === "function" ? sub.toJSON() : null;
    const keys = (json && json.keys) || {};
    if (!json || !isAllowedEndpoint(json.endpoint)) return null;
    if (!P256DH_RE.test(keys.p256dh || "") || !AUTH_RE.test(keys.auth || "")) return null;
    return { endpoint: json.endpoint, p256dh: keys.p256dh, auth: keys.auth };
  }

  /** 預設的裝置名稱（使用者在裝置列表上看得懂就好，之後可以改） */
  function deviceLabel(userAgent, maxTouchPoints) {
    const ua = String(userAgent || "");
    if (/iPhone|iPod/.test(ua)) return "iPhone";
    if (/iPad/.test(ua) || (/Macintosh/.test(ua) && Number(maxTouchPoints) > 1)) return "iPad";
    if (/Android/.test(ua)) return /Mobile/.test(ua) ? "Android 手機" : "Android 平板";
    if (/Macintosh/.test(ua)) return "Mac";
    if (/Windows/.test(ua)) return "Windows 電腦";
    return "這台裝置";
  }

  /** 按「開啟推播」後先顯示的說明（使用者確認後才跳系統詢問） */
  function explainerLines({ emailOn, windowText }) {
    return [
      "會推播到這台裝置：到價提醒、換匯計畫提醒。",
      `只在你的通知時段（台灣時間 ${windowText}）內送，和 Email 相同。`,
      "鎖定畫面只會顯示幣別和價格，例如「日圓到價：0.2040」，不會顯示你的目標價。",
      emailOn
        ? "Email 照常寄，推播是額外的通知。"
        : "你的 Email 通知目前是關閉的；開啟推播後，提醒只會推播到這台裝置。",
      "按「好，開啟」後，瀏覽器會問你是否允許通知。iPhone 只會問這一次：" +
        "如果按了「不允許」，之後要到 iPhone 的「設定」裡才能打開。",
    ];
  }

  /** 設定頁上的「Email 通知和推播的關係」 */
  const RELATION = Object.freeze({
    head: ["", "Email 通知", "推播通知"],
    rows: [
      ["管什麼", "寄到你信箱的到價提醒、換匯計畫提醒", "送到「這台裝置」的到價提醒、換匯計畫提醒"],
      ["怎麼關", "上面的 Email 開關，或信裡的退訂連結", "這裡的開關（每台裝置分開），或手機系統設定"],
      ["互相影響", "不會：退訂 Email，推播照送", "不會：關掉推播，Email 照寄"],
    ],
    shared: "兩者共用：通知時段、提醒規則（同一則提醒會同時送 Email 和推播）。刪除帳號時一起刪除。",
  });

  /** 拒絕過通知權限時，教使用者到系統設定打開 */
  function deniedHelp(kind) {
    if (kind === "ios") {
      return "你之前沒有允許通知。請到 iPhone 的「設定」→「通知」→「FX Alert」，打開「允許通知」，再回來重新整理。";
    }
    return "你之前沒有允許通知。請在瀏覽器網址列旁的網站設定（鎖頭圖示）把「通知」改成允許，" +
      "Android 也可以長按 FX Alert 圖示 →「應用程式資訊」→「通知」，再回來重新整理。";
  }

  /** 資料庫錯誤 → 給使用者看的訊息 */
  function saveErrorMessage(error) {
    const code = error && error.code;
    if (code === "42501") return "推播功能目前還沒開放給你的帳號。";
    if (code === "23514") {
      return /5 台/.test(String(error.message || ""))
        ? "每人最多 5 台裝置接收推播，請先移除不用的裝置。"
        : "這個瀏覽器的推播服務不在支援清單內，請改用 Safari、Chrome、Edge 或 Firefox。";
    }
    return "無法開啟推播，請稍後再試。";
  }

  /** localStorage 裡記的「這台裝置」：只認同一個帳號的紀錄 */
  function readDevice(storage, userId) {
    try {
      const v = JSON.parse(storage.getItem(STORAGE_KEY) || "null");
      return v && v.userId === userId && typeof v.endpoint === "string" ? v : null;
    } catch {
      return null; // 無痕模式等情況讀不到
    }
  }

  function writeDevice(storage, value) {
    try {
      if (value) storage.setItem(STORAGE_KEY, JSON.stringify(value));
      else storage.removeItem(STORAGE_KEY);
    } catch {
      // 寫不進去沒關係：只是少了自動重新訂閱
    }
  }

  /**
   * 每次打開網站要怎麼同步這台裝置（純判斷，實際動作在 resync）：
   *   "save"        瀏覽器有訂閱、資料庫也有這台：更新金鑰和 last_seen_at
   *   "resubscribe" 瀏覽器的訂閱不見了（Apple 換網址等），但資料庫還有這台：重新訂閱
   *   "forget"      資料庫已經沒有這台（在別台裝置移除了）：清掉紀錄，不自動加回去
   *   "none"        什麼都不做
   */
  function resyncAction({ permission, currentEndpoint, stored, rows }) {
    if (permission !== "granted") return "none";
    const has = (endpoint) => rows.some((r) => r.endpoint === endpoint);
    if (currentEndpoint && has(currentEndpoint)) return "save";
    if (!stored) return "none";
    if (!has(stored.endpoint)) return "forget";
    return currentEndpoint ? "none" : "resubscribe";
  }

  // ---------- 瀏覽器 ----------

  let ctx = null; // { client, userId, emailOn, windowText, say, isCurrent }
  let box = null;

  const h = (tag, attrs = {}, ...children) => {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (v === false || v === null || v === undefined) continue;
      if (k === "class") node.className = v;
      else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v === true ? "" : v);
    }
    node.append(...children.filter((c) => c !== null && c !== undefined));
    return node;
  };

  function browserEnv() {
    const hint = window.FxPwa && window.FxPwa.installHint ? window.FxPwa.installHint() : null;
    return {
      secure: window.isSecureContext === true,
      hasServiceWorker: "serviceWorker" in navigator,
      hasPushManager: "PushManager" in window,
      hasNotification: "Notification" in window,
      permission: "Notification" in window ? Notification.permission : "default",
      iosNotInstalled: hint === "ios",
    };
  }

  const vapidKey = () => (window.FX_CONFIG && window.FX_CONFIG.webPushPublicKey) || null;

  async function currentSubscription() {
    if (!("serviceWorker" in navigator)) return null;
    const reg = await navigator.serviceWorker.getRegistration();
    return reg && reg.pushManager ? reg.pushManager.getSubscription() : null;
  }

  async function subscribe() {
    const reg = await navigator.serviceWorker.ready;
    const options = { userVisibleOnly: true, applicationServerKey: base64UrlToBytes(vapidKey()) };
    try {
      return await reg.pushManager.subscribe(options);
    } catch (err) {
      // 舊的訂閱是用另一把公鑰建立的（換過金鑰）：取消後重新訂閱
      const old = await reg.pushManager.getSubscription();
      if (!old) throw err;
      await old.unsubscribe();
      return reg.pushManager.subscribe(options);
    }
  }

  async function save(sub) {
    const keys = subscriptionKeys(sub);
    if (!keys) return { error: { code: "23514", message: "endpoint" } };
    const { error } = await ctx.client.rpc("save_push_subscription", {
      p_endpoint: keys.endpoint,
      p_p256dh: keys.p256dh,
      p_auth: keys.auth,
      p_label: deviceLabel(navigator.userAgent, navigator.maxTouchPoints),
    });
    if (!error) writeDevice(localStorage, { userId: ctx.userId, endpoint: keys.endpoint });
    return { error };
  }

  async function loadRows() {
    const { data, error } = await ctx.client.from("push_subscriptions")
      .select("id,endpoint,label,created_at,last_success_at")
      .order("created_at");
    if (error) throw error;
    return data || [];
  }

  /** 每次打開網站：同步這台裝置（失敗不影響頁面） */
  async function resync(rows, sub) {
    const stored = readDevice(localStorage, ctx.userId);
    const action = resyncAction({
      permission: browserEnv().permission,
      currentEndpoint: sub ? sub.endpoint : null,
      stored,
      rows,
    });
    if (action === "save") {
      await save(sub);
      return sub;
    }
    if (action === "forget") writeDevice(localStorage, null);
    if (action === "resubscribe" && vapidKey()) {
      const fresh = await subscribe();
      const { error } = await save(fresh);
      if (!error && stored.endpoint !== fresh.endpoint) {
        await ctx.client.from("push_subscriptions").delete().eq("endpoint", stored.endpoint);
      }
      return error ? null : fresh;
    }
    return sub;
  }

  function relationTable() {
    return h("div", { class: "push-relation" },
      h("table", {},
        h("caption", {}, "Email 通知和推播的關係"),
        h("thead", {}, h("tr", {}, ...RELATION.head.map((t) => h("th", { scope: "col" }, t)))),
        h("tbody", {}, ...RELATION.rows.map(([k, a, b]) =>
          h("tr", {}, h("th", { scope: "row" }, k), h("td", {}, a), h("td", {}, b))))),
      h("p", { class: "hint" }, RELATION.shared));
  }

  function explainer(onCancel) {
    const ok = h("button", { type: "button", class: "primary", id: "push-confirm" }, "好，開啟");
    ok.addEventListener("click", () => enable(ok));
    return h("div", { class: "push-explainer", id: "push-explainer", role: "region", "aria-label": "開啟推播前的說明" },
      h("ul", {}, ...explainerLines(ctx).map((t) => h("li", {}, t))),
      h("div", { class: "rule-actions" },
        ok,
        h("button", { type: "button", class: "link", onclick: onCancel }, "先不要")));
  }

  /** 「好，開啟」：一定要在按鈕的 click 裡直接呼叫 requestPermission（iPhone 的規定） */
  function enable(button) {
    button.disabled = true;
    const asked = Notification.requestPermission();
    void (async () => {
      const permission = await asked;
      if (permission !== "granted") {
        ctx.say(permission === "denied" ? "你沒有允許通知，推播沒有開啟。" : "推播沒有開啟。", "error");
        return draw();
      }
      try {
        const sub = await subscribe();
        const { error } = await save(sub);
        if (error) {
          await sub.unsubscribe().catch(() => {});
          ctx.say(saveErrorMessage(error), "error");
        } else {
          ctx.say("已開啟這台裝置的推播。");
        }
      } catch {
        ctx.say("無法開啟推播，請稍後再試。", "error");
      }
      return draw();
    })();
  }

  async function removeRow(row, isThis) {
    const { error } = await ctx.client.from("push_subscriptions").delete().eq("id", row.id);
    if (error) {
      ctx.say("無法移除裝置，請稍後再試。", "error");
      return;
    }
    if (isThis) {
      writeDevice(localStorage, null);
      const sub = await currentSubscription().catch(() => null);
      if (sub) await sub.unsubscribe().catch(() => {});
    }
    ctx.say(isThis ? "已關閉這台裝置的推播。" : `已移除「${row.label || "裝置"}」。`);
    await draw();
  }

  function formatTime(iso) {
    const fmt = window.FxApp && window.FxApp.formatTime;
    return fmt ? fmt(iso) : iso;
  }

  function deviceList(rows, thisEndpoint) {
    if (rows.length === 0) return null;
    return h("ul", { class: "push-devices", "aria-label": "接收推播的裝置" },
      ...rows.map((row) => {
        const isThis = row.endpoint === thisEndpoint;
        return h("li", {},
          h("span", { class: "device-name" }, row.label || "裝置", isThis ? "（這台）" : null),
          h("span", { class: "hint" },
            row.last_success_at ? `最後送達 ${formatTime(row.last_success_at)}` : "還沒有送過推播"),
          h("button", { type: "button", class: "link", onclick: () => removeRow(row, isThis) },
            isThis ? "關閉這台" : "移除"));
      }));
  }

  function statusBlock(status, onEnable) {
    if (!vapidKey()) return h("p", { class: "hint" }, "推播還在設定中，暫時不能開啟。");
    if (status === "ios-install") {
      return h("p", { class: "hint" },
        "iPhone／iPad 要先把網站加到主畫面：用 Safari 點「分享」→「加入主畫面」，" +
        "再從主畫面的 FX Alert 圖示打開，就可以在這裡開啟推播。");
    }
    if (status === "unsupported") {
      return h("p", { class: "hint" }, "這個瀏覽器不支援推播。請改用 Safari（iPhone 要先加到主畫面）、Chrome、Edge 或 Firefox。");
    }
    if (status === "denied") {
      const kind = window.FxPwa && window.FxPwa.isIos && window.FxPwa.isIos() ? "ios" : "other";
      return h("p", { class: "hint" }, deniedHelp(kind));
    }
    return h("button", { type: "button", class: "primary", id: "push-enable", onclick: onEnable }, "開啟推播");
  }

  async function draw() {
    if (!ctx || !box || !ctx.isCurrent()) return;
    let rows;
    let sub = null;
    try {
      rows = await loadRows();
      sub = await currentSubscription().catch(() => null);
      sub = await resync(rows, sub).catch(() => sub);
      rows = await loadRows();
    } catch {
      if (!ctx.isCurrent()) return;
      box.replaceChildren(h("h3", { class: "section-title" }, "推播通知"),
        h("p", { class: "hint" }, "讀不到推播設定，請重新整理頁面。"));
      return;
    }
    if (!ctx.isCurrent()) return;
    const thisEndpoint = sub ? sub.endpoint : null;
    const on = rows.some((r) => r.endpoint === thisEndpoint);
    const slot = h("div", { class: "push-action" });
    const showExplainer = () => slot.replaceChildren(explainer(() => slot.replaceChildren(action())));
    const action = () => (on
      ? h("p", { class: "hint" }, "這台裝置已開啟推播。要停止，按下面列表裡的「關閉這台」。")
      : statusBlock(pushStatus(browserEnv()), showExplainer));
    slot.append(action());
    box.replaceChildren(
      h("h3", { class: "section-title" }, "推播通知"),
      h("div", { class: "email-switch" },
        h("span", {}, "這台裝置"),
        h("span", { class: on ? "pill on" : "pill off" }, on ? "開啟" : "未開啟")),
      slot,
      deviceList(rows, thisEndpoint),
      relationTable());
  }

  /**
   * 設定頁的推播區塊。web_push 對這個人沒開時回傳 null（資料庫也會擋）。
   * options: { client, userId, emailOn, windowText, say, isCurrent }
   */
  function section(options) {
    if (!(window.FxFlags && window.FxFlags.isOn("web_push"))) return null;
    ctx = options;
    box = h("section", { class: "push-section", "data-flag": "web_push", "aria-label": "推播通知" });
    void draw();
    return box;
  }

  /** 登出時：停止這台裝置的推播（最多等 3 秒，不擋住登出） */
  async function forgetThisDevice(client) {
    const work = (async () => {
      const sub = await currentSubscription();
      if (sub) {
        await client.from("push_subscriptions").delete().eq("endpoint", sub.endpoint);
        await sub.unsubscribe();
      }
      writeDevice(localStorage, null);
    })().catch(() => {});
    await Promise.race([work, new Promise((r) => setTimeout(r, 3000))]);
    ctx = null;
    box = null;
  }

  const exported = {
    STORAGE_KEY,
    RELATION,
    isAllowedEndpoint,
    pushStatus,
    base64UrlToBytes,
    subscriptionKeys,
    deviceLabel,
    explainerLines,
    deniedHelp,
    saveErrorMessage,
    readDevice,
    writeDevice,
    resyncAction,
  };
  if (typeof module !== "undefined" && module.exports) {
    module.exports = exported;
  } else {
    window.FxPush = { section, forgetThisDevice };
  }
})();
