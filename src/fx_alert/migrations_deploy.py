"""正式專案的 migration 部署（.github/workflows/deploy-migrations.yml 使用）。

這個模組只做「讀」：比對紀錄、跑事前檢查、最後確認。真正寫入資料庫的兩件事交給 Supabase CLI：
  * supabase migration repair --status applied：
    補上「以前手動套用、但沒有紀錄」的版本（只寫紀錄，不執行）
  * supabase db push：依序套用還沒套用的 migration，並寫入紀錄

用法（連線字串放在環境變數 DB_URL，永遠不印出來）：
    python -m fx_alert.migrations_deploy plan       # 算出要補紀錄、要套用的版本，寫到 GITHUB_OUTPUT
    python -m fx_alert.migrations_deploy precheck   # 跑待套用版本的 supabase/prechecks/<版本>_*.sql
    python -m fx_alert.migrations_deploy verify     # 套用後確認沒有遺漏

事前檢查（precheck）
  supabase/prechecks/<版本>.sql 是一段 select，列出「不符合這個 migration 新限制」的資料。
  在唯讀交易裡執行；回傳任何一列就失敗、整個部署停止（什麼都不寫）。
  log 只印列數，不印內容（repo 是公開的，資料可能含目標價）。

基準清單（supabase/migrations_baseline.txt）
  自動部署開始之前，已經手動套用到正式專案的版本。紀錄裡沒有的會用 repair 補上紀錄，不會重新執行。
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = ROOT / "supabase" / "migrations"
PRECHECKS_DIR = ROOT / "supabase" / "prechecks"
BASELINE_FILE = ROOT / "supabase" / "migrations_baseline.txt"

MIGRATION_FILE = re.compile(r"^(\d{14})_([a-z0-9_]+)\.sql$")
VERSION = re.compile(r"^\d{14}$")
PRECHECK_TIMEOUT = "30s"


class DeployError(Exception):
    """部署不能繼續：設定錯誤、紀錄對不上、事前檢查沒通過。訊息不含連線字串。"""


@dataclass(frozen=True)
class Plan:
    repair: tuple[str, ...]  # 已經手動套用、要補紀錄的版本
    pending: tuple[str, ...]  # 要用 db push 套用的版本


def local_versions(migrations_dir: Path = MIGRATIONS_DIR) -> list[str]:
    versions = []
    for path in sorted(migrations_dir.glob("*.sql")):
        m = MIGRATION_FILE.match(path.name)
        if not m:
            raise DeployError(f"migration 檔名格式不對：{path.name}（要是 <14 位數字>_<名稱>.sql）")
        versions.append(m.group(1))
    if len(versions) != len(set(versions)):
        raise DeployError("有兩個 migration 用了同一個版本號")
    return versions


def read_baseline(path: Path = BASELINE_FILE) -> list[str]:
    if not path.exists():
        return []
    versions = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if not VERSION.match(line):
            raise DeployError(f"{path.name} 裡有不是版本號的內容：{line!r}")
        versions.append(line)
    return versions


def make_plan(local: list[str], recorded: set[str], baseline: list[str]) -> Plan:
    """比對 repo 裡的 migration 和資料庫的紀錄。

    * 紀錄裡有、repo 裡沒有 → 停止（資料庫被套用過不在 repo 的變更，要先查清楚）
    * 基準清單裡、紀錄沒有 → 補紀錄（repair）
    * 其他 repo 有、紀錄沒有 → 待套用（db push）；必須比所有已套用的版本都新，避免順序錯亂
    """
    unknown = sorted(recorded - set(local))
    if unknown:
        raise DeployError(f"資料庫紀錄裡有 repo 沒有的版本：{', '.join(unknown)}")
    missing_baseline = sorted(set(baseline) - set(local))
    if missing_baseline:
        raise DeployError(f"基準清單裡的版本在 repo 找不到：{', '.join(missing_baseline)}")

    repair = tuple(v for v in local if v in baseline and v not in recorded)
    pending = tuple(v for v in local if v not in recorded and v not in baseline)
    applied = recorded | set(repair)
    if pending and applied and pending[0] < max(applied):
        raise DeployError(
            f"待套用的 {pending[0]} 比已套用的 {max(applied)} 還舊：順序可能錯亂，請先確認"
        )
    return Plan(repair=repair, pending=pending)


def precheck_path(version: str, prechecks_dir: Path = PRECHECKS_DIR) -> Path | None:
    matches = sorted(prechecks_dir.glob(f"{version}_*.sql")) if prechecks_dir.exists() else []
    if len(matches) > 1:
        raise DeployError(f"{version} 有不只一個事前檢查檔")
    return matches[0] if matches else None


# ---------------------------------------------------------------------------
# 資料庫（只讀）
# ---------------------------------------------------------------------------


def db_url() -> str:
    url = os.environ.get("DB_URL", "").strip()
    if not url:
        raise DeployError(
            "沒有 DB_URL：請在 GitHub 的 production environment 設定 PRODUCTION_DB_URL"
        )
    return url


def mask_password(url: str) -> None:
    """在 GitHub Actions 的 log 裡遮住密碼（整串 secret 已經會被遮，密碼單獨出現也要遮）。"""
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    password = urlsplit(url).password
    if password:
        for value in {password, unquote(password)}:
            print(f"::add-mask::{value}")


def connect(url: str, *, read_only: bool = True):
    import psycopg

    try:
        conn = psycopg.connect(url, connect_timeout=15, autocommit=False)
    except psycopg.Error as exc:
        # 錯誤訊息可能帶主機名稱，但不會有密碼；仍只取第一行
        raise DeployError(f"連不上資料庫：{str(exc).splitlines()[0]}") from None
    conn.read_only = read_only
    return conn


def recorded_versions(conn) -> set[str]:
    with conn.cursor() as cur:
        cur.execute("select to_regclass('supabase_migrations.schema_migrations') is not null")
        if not cur.fetchone()[0]:
            raise DeployError("找不到 supabase_migrations.schema_migrations：這不是預期的資料庫")
        cur.execute("select version from supabase_migrations.schema_migrations")
        return {row[0] for row in cur.fetchall()}


def run_precheck(conn, sql: str) -> int:
    """在唯讀交易裡執行事前檢查，回傳不符合的列數（不回傳內容）。"""
    with conn.cursor() as cur:
        cur.execute(f"set local statement_timeout = '{PRECHECK_TIMEOUT}'")
        cur.execute(sql)
        count = len(cur.fetchall()) if cur.description else 0
    conn.rollback()
    return count


# ---------------------------------------------------------------------------
# 指令
# ---------------------------------------------------------------------------


def _write_output(**values: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            for k, v in values.items():
                f.write(f"{k}={v}\n")


def _summary(lines: list[str]) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")


def _current_plan(conn) -> Plan:
    return make_plan(local_versions(), recorded_versions(conn), read_baseline())


def cmd_plan() -> int:
    url = db_url()
    mask_password(url)
    with connect(url) as conn:
        plan = _current_plan(conn)
    print(f"要補紀錄（以前手動套用過，只寫紀錄不執行）：{' '.join(plan.repair) or '（無）'}")
    print(f"要套用：{' '.join(plan.pending) or '（無）'}")
    _write_output(repair=" ".join(plan.repair), pending=" ".join(plan.pending))
    _summary(
        ["### migration 部署計畫", ""]
        + [f"- 補紀錄：`{v}`" for v in plan.repair]
        + [f"- 套用：`{v}`" for v in plan.pending]
        + ([] if plan.repair or plan.pending else ["- 沒有需要套用的 migration"])
    )
    return 0


def cmd_precheck() -> int:
    url = db_url()
    with connect(url) as conn:
        plan = _current_plan(conn)
        failed = []
        for version in plan.pending:
            path = precheck_path(version)
            if path is None:
                print(f"{version}：沒有事前檢查")
                continue
            count = run_precheck(conn, path.read_text(encoding="utf-8"))
            if count:
                failed.append(version)
                print(f"::error::{version}：有 {count} 列資料不符合新限制（{path.name}），不套用")
            else:
                print(f"{version}：事前檢查通過（0 列）")
    if failed:
        print("請先在 SQL Editor 執行上面的事前檢查 SQL 找出這些資料、修正後再重跑這個 workflow。")
        return 1
    return 0


def cmd_verify() -> int:
    url = db_url()
    with connect(url) as conn:
        plan = _current_plan(conn)
    if plan.repair or plan.pending:
        print(f"::error::還有沒套用的版本：{' '.join(plan.repair + plan.pending)}")
        return 1
    print("確認完成：repo 裡的 migration 都已套用、也都有紀錄")
    return 0


COMMANDS = {"plan": cmd_plan, "precheck": cmd_precheck, "verify": cmd_verify}


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or args[0] not in COMMANDS:
        print(
            f"用法：python -m fx_alert.migrations_deploy {{{'|'.join(COMMANDS)}}}", file=sys.stderr
        )
        return 2
    try:
        return COMMANDS[args[0]]()
    except DeployError as exc:
        print(f"::error::{exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
