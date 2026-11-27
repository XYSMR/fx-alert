"""排程去重：自動觸發時，如果剛剛已經成功檢查過，就不再跑。

check-rates 有兩個自動觸發來源：Supabase pg_cron（每 :05、:35，主要）和 GitHub schedule
（每 :07、:37，備援）。兩個都觸發時，第二次會讀到上一次的檢查結果：
    先讀 Supabase 的 check_status（publishable key，公開可讀；每次檢查都會更新），
    讀不到（專案暫停、還沒套用 migration、沒設定）就用 state 分支的 last_check.json；
    兩個都有時用比較新的那個。
    20 分鐘內已經成功檢查過 → run=false
    （不檢查、不 commit、不 ping 監控、不部署，workflow 仍是綠燈）

規則：
  * 手動執行（workflow_dispatch 且 source 不是 pg_cron）一律檢查。
  * 沒有紀錄、紀錄讀不懂、上次檢查有錯誤 → 檢查（寧可多跑一次，也不要漏掉）。
  * 這支程式只用標準函式庫、永遠回傳 0：workflow 不需要先安裝套件，也不會因為它而變紅。

用法（workflow 裡）：
    PYTHONPATH=src python3 -m fx_alert.gate --last-check FILE --event EVENT --source SOURCE
    環境變數 SUPABASE_URL、SUPABASE_PUBLISHABLE_KEY（沒有就只看 FILE）
結果寫到 GITHUB_OUTPUT：run=true|false、reason=…
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

WINDOW = timedelta(minutes=20)
CLOCK_SKEW = timedelta(minutes=5)  # 時間戳記比現在晚一點點（不同機器的時鐘誤差），仍視為剛剛


def decide(
    last_check: object, now: datetime, *, event: str, source: str, window: timedelta = WINDOW
) -> tuple[bool, str]:
    """回傳 (要不要檢查, 原因)。"""
    automated = event == "schedule" or (event == "workflow_dispatch" and source == "pg_cron")
    if not automated:
        return True, "手動執行：一律檢查"
    trigger = "GitHub 排程" if event == "schedule" else "pg_cron 觸發"

    if not isinstance(last_check, dict):
        return True, f"{trigger}：沒有上次檢查的紀錄，檢查"
    try:
        at = datetime.fromisoformat(str(last_check["at"]))
    except (KeyError, ValueError):
        return True, f"{trigger}：上次檢查的紀錄讀不懂，檢查"
    if at.tzinfo is None:
        return True, f"{trigger}：上次檢查的時間沒有時區，檢查"
    if last_check.get("ok") is not True:
        return True, f"{trigger}：上次檢查有錯誤，再檢查一次"

    age = now - at
    if age < -CLOCK_SKEW:
        return True, f"{trigger}：上次檢查的時間在未來（{at.isoformat()}），檢查"
    minutes = max(0, int(age.total_seconds() // 60))
    if age <= window:
        return False, (
            f"{trigger}：{minutes} 分鐘前（{at:%H:%M}）已經成功檢查過，略過這次"
            "（不檢查、不 ping 監控、不部署）"
        )
    return True, f"{trigger}：上次成功檢查是 {minutes} 分鐘前，檢查"


def from_check_status(rows: object) -> dict | None:
    """check_status 的回應（[{checked_at, ok, errors}]）→ 和 last_check.json 一樣的格式。"""
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        return None
    row = rows[0]
    if "checked_at" not in row:
        return None
    return {"at": row["checked_at"], "ok": row.get("ok"), "errors": row.get("errors")}


def newest(*records: object) -> object:
    """幾份檢查紀錄裡最新的一份；時間讀不懂的不算。全部都沒有就回傳 None。"""
    best, best_at = None, None
    for r in records:
        try:
            at = datetime.fromisoformat(str(r["at"]))  # type: ignore[index]
        except (TypeError, KeyError, ValueError):
            continue
        if at.tzinfo is None:
            continue
        if best_at is None or at > best_at:
            best, best_at = r, at
    return best


def fetch_check_status(url: str, key: str, timeout: float = 5) -> dict | None:
    """用 publishable key 讀 check_status；任何錯誤都回傳 None（改用 state 分支的紀錄）。"""
    if not url or not key:
        return None
    query = urllib.parse.urlencode({"select": "checked_at,ok,errors", "id": "eq.1"})
    req = urllib.request.Request(
        f"{url.rstrip('/')}/rest/v1/check_status?{query}", headers={"apikey": key}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310  網址來自設定
            return from_check_status(json.loads(resp.read().decode("utf-8")))
    except Exception:  # noqa: BLE001  連不到、401、格式不對都一樣：改用 state 分支
        return None


def _read(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def main(argv: list[str] | None = None, *, now: datetime | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m fx_alert.gate")
    parser.add_argument("--last-check", type=Path, required=True)
    parser.add_argument("--event", default="")
    parser.add_argument("--source", default="")
    try:
        args = parser.parse_args(argv)
        remote = fetch_check_status(
            os.environ.get("SUPABASE_URL", ""), os.environ.get("SUPABASE_PUBLISHABLE_KEY", "")
        )
        local = _read(args.last_check)
        print(
            "上次檢查：Supabase check_status "
            + ("讀得到" if remote else "讀不到，改用 state 分支的 last_check.json")
        )
        run, reason = decide(
            newest(remote, local) if remote else local,
            now or datetime.now(UTC),
            event=args.event,
            source=args.source,
        )
    except SystemExit:  # 參數錯誤也不讓 workflow 變紅：照常檢查
        run, reason = True, "去重判斷的參數錯誤，照常檢查"
    except Exception as exc:  # noqa: BLE001  任何意外都照常檢查
        run, reason = True, f"去重判斷失敗（{type(exc).__name__}），照常檢查"

    print(reason)
    if not run and os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::notice::{reason}")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"run={'true' if run else 'false'}\n")
            f.write(f"reason={reason}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
