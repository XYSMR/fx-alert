// Feature flags（A10）：登入後問資料庫 public.is_feature_on(flag)，決定畫面要不要顯示功能。
//
// - 這裡只管「畫面」。權限在資料庫：RLS policy 呼叫同一個 is_feature_on()，直接打 API 也會被擋。
//   隱藏按鈕不算權限控管。
// - 判斷（rollout 分桶、admin/beta、kill switch）全部在資料庫做，前端不自己算，結果才會和 RLS 一致。
// - 結果寫在 <html data-flag-<flag>="on|off">。style.css 會隱藏 data-flag="<flag>" 的元素，
//   除非 <html> 上那個 flag 是 on。所以 app.js 重新產生的卡片（例如 quick_target 的按鈕）
//   只要加上 data-flag="quick_target"，不用自己判斷，也不怕產生的順序。
// - 沒登入：全部關閉（和 is_feature_on() 相同）。
// - 問不到（網路錯誤、資料庫還沒有這個函式）：用 FALLBACK。
//   已上線的 trip_plan 維持開啟（寫入仍由資料庫判斷）；新功能 quick_target、web_push 關閉。
// - 改了 flag 之後，使用者重新整理頁面才會看到變化（不用重新部署）。
"use strict";

(function () {
  const FLAGS = ["trip_plan", "quick_target", "web_push"];
  const FALLBACK = { trip_plan: true, quick_target: false, web_push: false };

  function allOff() {
    return Object.fromEntries(FLAGS.map((f) => [f, false]));
  }

  /** 每個 flag 呼叫一次 rpc('is_feature_on')；失敗的用 FALLBACK */
  async function fetchFlags(client) {
    const entries = await Promise.all(FLAGS.map(async (flag) => {
      try {
        const { data, error } = await client.rpc("is_feature_on", { p_flag: flag });
        if (error) return [flag, FALLBACK[flag]];
        return [flag, data === true];
      } catch {
        return [flag, FALLBACK[flag]];
      }
    }));
    return Object.fromEntries(entries);
  }

  /** <html data-flag-trip_plan="on" data-flag-quick_target="off"> */
  function applyTo(root, flags) {
    for (const flag of FLAGS) {
      root.setAttribute(`data-flag-${flag}`, flags[flag] ? "on" : "off");
    }
  }

  let current = allOff();

  async function load(client) {
    current = await fetchFlags(client);
    applyTo(document.documentElement, current);
    return { ...current };
  }

  function clear() {
    current = allOff();
    applyTo(document.documentElement, current);
  }

  function isOn(flag) {
    return current[flag] === true;
  }

  const exported = { FLAGS, FALLBACK, allOff, fetchFlags, applyTo };
  if (typeof module !== "undefined" && module.exports) {
    module.exports = exported;
  } else {
    window.FxFlags = { load, clear, isOn };
  }
})();
