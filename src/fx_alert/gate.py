"""排程去重：自動觸發時，如果剛剛已經成功檢查過，就不再跑。

check-rates 有兩個自動觸發來源：Supabase pg_cron（每 :05、:35，主要）和 GitHub schedule
（每 :07、:37，備援）。兩個都觸發時，第二次會讀到 state 分支的 last_check.json：
    20 分鐘內已經成功檢查過 → run=false
    （不檢查、不 commit、不 ping 監控、不部署，workflow 仍是綠燈）

規則：
  * 手動執行（workflow_dispatch 且 source 不是 pg_cron）一律檢查。
  * 沒有紀錄、紀錄讀不懂、上次檢查有錯誤 → 檢查（寧可多跑一次，也不要漏掉）。
  * 這支程式只用標準函式庫、永遠回傳 0：workflow 不需要先安裝套件，也不會因為它而變紅。

用法（workflow 裡）：
    PYTHONPATH=src python3 -m fx_alert.gate --last-check FILE --event EVENT --source SOURCE
結果寫到 GITHUB_OUTPUT：run=true|false、reason=…
"""

from __future__ import annotations

import argparse
import json
import os
import sys
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
        run, reason = decide(
            _read(args.last_check),
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
