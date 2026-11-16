"""要不要 commit state 分支、部署網頁？

原本每次檢查都 commit（至少 last_check.json 會變）並部署 GitHub Pages。多人版把「最後檢查時間」
寫到 Supabase 的 check_status 之後（網頁狀態列直接讀它），只有下面這些情況才需要：

  * 走勢、狀態、通知紀錄等「last_check.json 以外」的檔案有變（例如記到新的牌告）
  * 檢查的成功／失敗和上次 commit 的不一樣（壞掉或恢復時，data.json 也要跟著更新，
    Supabase 讀不到時網頁還能看到正確的狀態；單人版沒有 check_status，靠這個顯示錯誤）

只有 last_check.json 的時間變了 → 不 commit、不部署。

用法（workflow 裡，在 data/ 目錄 git add -A 之後）：
    PYTHONPATH=$GITHUB_WORKSPACE/src python3 -m fx_alert.publish
stdout 印 true 或 false，原因印在 stderr。只用標準函式庫；任何意外都印 true（寧可多部署一次）。
"""

from __future__ import annotations

import json
import subprocess
import sys

LAST_CHECK = "last_check.json"


def decide(changed: list[str], now: object, before: object) -> tuple[bool, str]:
    """changed：這次有變的檔案（相對於 state 分支根目錄）。now／before：last_check.json 的內容。"""
    others = sorted(f for f in changed if f != LAST_CHECK)
    if others:
        shown = "、".join(others[:5]) + ("…" if len(others) > 5 else "")
        return True, f"資料有變更（{shown}），commit 並部署"
    if not changed:
        return False, "沒有任何變更，不 commit、不部署"
    ok_now = now.get("ok") if isinstance(now, dict) else None
    ok_before = before.get("ok") if isinstance(before, dict) else None
    if ok_now != ok_before:
        what = "恢復正常" if ok_now is True else "檢查失敗" if ok_now is False else "狀態改變"
        return True, f"{what}（上次 commit 的是 ok={ok_before}），commit 並部署"
    return False, "只有最後檢查時間改變（已寫到 Supabase），不 commit、不部署"


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout


def main() -> int:
    try:
        changed = [line for line in _git("diff", "--cached", "--name-only").splitlines() if line]
        try:
            with open(LAST_CHECK, encoding="utf-8") as f:
                now = json.load(f)
        except (OSError, ValueError):
            now = None
        try:
            before = json.loads(_git("show", f"HEAD:{LAST_CHECK}"))
        except (subprocess.CalledProcessError, ValueError):
            before = None  # 第一次 commit、或上次沒有這個檔案
        publish, reason = decide(changed, now, before)
    except Exception as exc:  # noqa: BLE001  任何意外都照常 commit、部署
        publish, reason = True, f"判斷失敗（{type(exc).__name__}），照常 commit 並部署"
    print(reason, file=sys.stderr)
    print("true" if publish else "false")
    return 0


if __name__ == "__main__":
    sys.exit(main())
