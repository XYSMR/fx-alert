"""找 repo 裡的資料檔（seed/ 底下的回補資料、營業日曆）。

workflow 用 `pip install .` 安裝（不是 editable），程式在 site-packages 裡，
用 Path(__file__) 往上找會找到 Python 的安裝目錄，而不是 repo。所以依序找：

  1. 環境變數 FX_ALERT_ROOT（有設定的話）
  2. 目前的工作目錄（workflow 都在 repo 根目錄執行）
  3. 程式所在位置往上兩層（editable 安裝、直接在 repo 裡跑測試）

都找不到就回傳第 3 個，讀檔時會明確失敗（不會默默當成沒有資料）。
"""

from __future__ import annotations

import os
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[2]


def candidates(relative: str) -> list[Path]:
    roots = []
    if os.environ.get("FX_ALERT_ROOT"):
        roots.append(Path(os.environ["FX_ALERT_ROOT"]))
    roots += [Path.cwd(), PACKAGE_ROOT]
    return [root / relative for root in roots]


def repo_file(relative: str) -> Path:
    found = [p for p in candidates(relative) if p.is_file()]
    return found[0] if found else PACKAGE_ROOT / relative
