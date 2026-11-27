// PWA：註冊 service worker、保持 sw.js 是最新版、顯示「加入主畫面」說明。
//
// 這個檔案不處理推播；推播的訂閱介面在之後的 PR。
"use strict";

(function () {
  // 註冊時的設定。updateViaCache: "none"：檢查 sw.js 有沒有新版時一律跳過 HTTP 快取，
  // 不受 GitHub Pages 的 Cache-Control（max-age=600）影響。
  const REGISTER_OPTIONS = Object.freeze({ scope: "./", updateViaCache: "none" });

  // 從背景切回來時，最多每 10 分鐘主動檢查一次 sw.js
  const UPDATE_INTERVAL_MS = 10 * 60 * 1000;

  /** iPhone、iPod、iPad（iPadOS 會假裝成 Mac，用觸控點數分辨） */
  function isIos(userAgent, maxTouchPoints) {
    const ua = String(userAgent || "");
    if (/iPhone|iPad|iPod/.test(ua)) return true;
    return /Macintosh/.test(ua) && Number(maxTouchPoints) > 1;
  }

  /** 是不是從主畫面圖示打開（網頁 App 模式） */
  function isStandalone(win) {
    try {
      if (win.matchMedia && win.matchMedia("(display-mode: standalone)").matches) return true;
    } catch (_) {
      // 舊瀏覽器沒有 display-mode，當作不是
    }
    return Boolean(win.navigator && win.navigator.standalone === true);
  }

  /**
   * 要顯示哪一種「加入主畫面」說明：
   *   null      已經從主畫面打開，不用說明
   *   "ios"     iPhone／iPad：要用「分享 → 加入主畫面」，之後推播也只能在這個模式開啟
   *   "other"   Android、電腦：瀏覽器選單裡的「安裝應用程式」或「加到主畫面」
   */
  function installHint(win) {
    if (isStandalone(win)) return null;
    const nav = win.navigator || {};
    return isIos(nav.userAgent, nav.maxTouchPoints) ? "ios" : "other";
  }

  /** 是否該再檢查一次 sw.js（距離上次檢查超過 UPDATE_INTERVAL_MS） */
  function shouldCheckUpdate(lastCheckedAt, now) {
    return lastCheckedAt === null || now - lastCheckedAt >= UPDATE_INTERVAL_MS;
  }

  /**
   * 註冊 service worker，並在頁面從背景切回來時檢查更新。
   * iPhone 的網頁 App 常常是「從背景叫回來」而不是重新載入，所以光靠開頁時的檢查不夠。
   * 回傳 registration；瀏覽器不支援或註冊失敗時回傳 null（網站其他功能照常）。
   */
  async function register(win, now) {
    const nav = win.navigator;
    if (!nav || !("serviceWorker" in nav)) return null;
    const clock = now || Date.now;
    let reg;
    try {
      reg = await nav.serviceWorker.register("sw.js", REGISTER_OPTIONS);
    } catch (err) {
      console.warn("service worker 註冊失敗", err);
      return null;
    }
    let lastCheckedAt = clock();
    const doc = win.document;
    if (doc && doc.addEventListener) {
      doc.addEventListener("visibilitychange", () => {
        if (doc.visibilityState !== "visible") return;
        const t = clock();
        if (!shouldCheckUpdate(lastCheckedAt, t)) return;
        lastCheckedAt = t;
        reg.update().catch(() => {}); // 離線時檢查失敗沒關係，下次再試
      });
    }
    return reg;
  }

  /** 問目前生效的 service worker 是哪一版（除錯用）；沒有就回傳 null */
  function activeVersion(nav) {
    const ctrl = nav && nav.serviceWorker && nav.serviceWorker.controller;
    if (!ctrl || typeof MessageChannel === "undefined") return Promise.resolve(null);
    return new Promise((resolve) => {
      const ch = new MessageChannel();
      const timer = setTimeout(() => resolve(null), 2000);
      ch.port1.onmessage = (e) => {
        clearTimeout(timer);
        resolve((e.data && e.data.version) || null);
      };
      ctrl.postMessage({ type: "version" }, [ch.port2]);
    });
  }

  /** 依裝置顯示頁尾的「加入主畫面」說明 */
  function showInstallHelp(win) {
    const box = win.document.getElementById("install-help");
    if (!box) return;
    const kind = installHint(win);
    if (!kind) return;
    for (const el of box.querySelectorAll("[data-install]")) {
      el.hidden = el.getAttribute("data-install") !== kind;
    }
    box.hidden = false;
  }

  const exported = {
    REGISTER_OPTIONS,
    UPDATE_INTERVAL_MS,
    isIos,
    isStandalone,
    installHint,
    shouldCheckUpdate,
    register,
    activeVersion,
    showInstallHelp,
  };
  if (typeof module !== "undefined" && module.exports) {
    module.exports = exported;
  } else {
    window.FxPwa = {
      activeVersion: () => activeVersion(navigator),
      installHint: () => installHint(window),
      isIos: () => isIos(navigator.userAgent, navigator.maxTouchPoints),
    };
    showInstallHelp(window);
    register(window);
  }
})();
