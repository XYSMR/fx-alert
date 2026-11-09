// 退訂確認頁：打開只顯示按鈕，按下才真的退訂。
// 信箱的連結掃描器會自動打開信裡的連結（GET），所以退訂一定要等使用者按按鈕（POST）。
"use strict";

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const SIG_RE = /^[A-Za-z0-9_-]{43}$/;
const PROJECT_RE = /^[a-z0-9]{20}$/;

/** 從網址讀出 u（使用者）、s（簽章）、p（Supabase 專案代號）；格式不對回傳 null */
function parseLink(search) {
  const q = new URLSearchParams(search);
  const u = (q.get("u") || "").toLowerCase();
  const s = q.get("s") || "";
  const p = q.get("p") || "";
  if (!UUID_RE.test(u) || !SIG_RE.test(s) || !PROJECT_RE.test(p)) return null;
  return { u, s, endpoint: `https://${p}.supabase.co/functions/v1/unsubscribe` };
}

/** 依結果決定要顯示的文字 */
function resultView(status) {
  if (status === 200) {
    return {
      tone: "ok",
      title: "已取消 Email 到價通知",
      text: "之後不會再寄到價通知給你。想重新開啟，登入後到「我的提醒」打開 Email 通知即可。",
    };
  }
  if (status === 403) {
    return {
      tone: "error",
      title: "這個退訂連結無效",
      text: "連結可能不完整。請從通知信裡重新點一次「取消訂閱」，或登入後到「我的提醒」關閉 Email 通知。",
    };
  }
  return {
    tone: "error",
    title: "暫時無法處理",
    text: "請稍後再試一次。如果一直失敗，可以登入後到「我的提醒」關閉 Email 通知。",
  };
}

// ---------------------------------------------------------------------------
// 瀏覽器
// ---------------------------------------------------------------------------

function show(view) {
  const box = document.getElementById("result");
  box.hidden = false;
  box.className = `notice ${view.tone}`;
  document.getElementById("result-title").textContent = view.title;
  document.getElementById("result-text").textContent = view.text;
}

function main() {
  const link = parseLink(window.location.search);
  const form = document.getElementById("confirm");
  if (!link) {
    form.hidden = true;
    show(resultView(403));
    return;
  }
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = document.getElementById("confirm-button");
    button.disabled = true;
    button.textContent = "處理中…";
    let status = 0;
    try {
      const res = await fetch(link.endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ u: link.u, s: link.s }),
        referrerPolicy: "no-referrer",
      });
      status = res.status;
    } catch {
      status = 0;
    }
    if (status === 200) form.hidden = true;
    button.disabled = false;
    button.textContent = "取消 Email 到價通知";
    show(resultView(status));
  });
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { parseLink, resultView };
} else {
  main();
}
