"""migration 部署到 staging／正式專案（.github/workflows/deploy-migrations.yml 使用）。

部署目標用環境變數 FX_DEPLOY_TARGET 指定：production（預設）或 staging。
兩邊的差別：
  * 基準清單：正式專案 supabase/migrations_baseline.txt、
    staging supabase/migrations_baseline_staging.txt
  * 資料庫紀錄裡有、repo 沒有的版本：正式專案停止；staging 只警告
    （可能是還沒合併的 PR 先套用到 staging，見 README〈staging 多出來的 migration〉）
  * staging 另外比對內容：紀錄裡存的 SQL 和 repo 的檔案不同（PR 套用到 staging 之後又改過）→ 停止

用法（連線字串放在環境變數 DB_URL，永遠不印出來）：
    python -m fx_alert.migrations_deploy plan       # 算出要補紀錄、要套用的版本（只讀）
    python -m fx_alert.migrations_deploy precheck   # 物件檢查＋事前檢查（只讀）
    python -m fx_alert.migrations_deploy apply      # 補紀錄＋套用（唯一會寫入的指令）
    python -m fx_alert.migrations_deploy verify     # 套用後確認沒有遺漏（只讀）

apply 寫入的方式和 Supabase CLI 相同：紀錄寫在 supabase_migrations.schema_migrations
（version、name、statements），之後用 CLI 的 migration list 等指令也看得懂。
  * 補紀錄：所有版本在同一個交易裡寫入（只寫紀錄，不執行 SQL）
  * 套用：每個 migration 一個交易，SQL 和紀錄一起 commit；失敗就整個 rollback，不會只套用一半
  * 寫入前會重新比對計畫（必須和第 1 步相同），並在同一個連線上再跑一次物件檢查和事前檢查
不用 Supabase CLI 寫入：supabase@3.0.0 的 --remote 無法解析正式專案的 Session pooler 連線字串
（"failed to parse connection string"），而 psycopg 可以正常連線。

事前檢查（precheck）
  supabase/prechecks/<版本>.sql 是一段 select，列出「不符合這個 migration 新限制」的資料。
  在唯讀交易裡執行；回傳任何一列就失敗、整個部署停止（什麼都不寫）。
  log 只印列數，不印內容（repo 是公開的，資料可能含目標價）。

基準清單（supabase/migrations_baseline.txt）
  自動部署開始之前，已經手動套用到正式專案的版本。紀錄裡沒有的會用 repair 補上紀錄，不會重新執行。
  補紀錄之前，先跑 supabase/baseline_checks/<版本>_*.sql 確認這個版本建立的物件真的存在
  （函式、cron job…）：每一列 (item, ok) 都要是 true，有任何 false、查詢出錯、或沒有檢查檔就停止。
  避免「其實沒套用、卻被標成已套用」，之後永遠不會再套用。
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
BASELINE_CHECKS_DIR = ROOT / "supabase" / "baseline_checks"
BASELINE_FILE = ROOT / "supabase" / "migrations_baseline.txt"
STAGING_BASELINE_FILE = ROOT / "supabase" / "migrations_baseline_staging.txt"

# 部署目標：基準清單、GitHub 上連線字串的 secret 名稱（只用在錯誤訊息）
TARGETS = {
    "production": (BASELINE_FILE, "production environment 的 PRODUCTION_DB_URL"),
    "staging": (STAGING_BASELINE_FILE, "staging environment 的 STAGING_DB_URL"),
}

MIGRATION_FILE = re.compile(r"^(\d{14})_([a-z0-9_]+)\.sql$")
VERSION = re.compile(r"^\d{14}$")
PRECHECK_TIMEOUT = "30s"
APPLY_LOCK_TIMEOUT = "30s"  # 等不到鎖就放棄，不要卡住排程或網站
APPLY_STATEMENT_TIMEOUT = "5min"
HISTORY_TABLE = "supabase_migrations.schema_migrations"


class DeployError(Exception):
    """部署不能繼續：設定錯誤、紀錄對不上、事前檢查沒通過。訊息不含連線字串。"""


@dataclass(frozen=True)
class Plan:
    repair: tuple[str, ...]  # 已經手動套用、要補紀錄的版本
    pending: tuple[str, ...]  # 要用 db push 套用的版本
    unknown: tuple[str, ...] = ()  # 只有 staging：紀錄裡有、repo 沒有（只警告）

    def steps(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """會寫入的部分（補紀錄、套用）；和第 1 步的計畫比對時只看這兩項。"""
        return self.repair, self.pending


def target() -> str:
    name = os.environ.get("FX_DEPLOY_TARGET", "production").strip() or "production"
    if name not in TARGETS:
        raise DeployError(f"FX_DEPLOY_TARGET 只能是 {' 或 '.join(TARGETS)}：{name!r}")
    return name


def baseline_file(name: str | None = None) -> Path:
    return TARGETS[name or target()][0]


def migration_file(version: str, migrations_dir: Path = MIGRATIONS_DIR) -> Path:
    matches = sorted(migrations_dir.glob(f"{version}_*.sql"))
    if len(matches) != 1:
        raise DeployError(f"找不到 {version} 的 migration 檔（或不只一個）")
    return matches[0]


def migration_name(path: Path) -> str:
    m = MIGRATION_FILE.match(path.name)
    if not m:
        raise DeployError(f"migration 檔名格式不對：{path.name}")
    return m.group(2)


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


def make_plan(
    local: list[str], recorded: set[str], baseline: list[str], *, allow_unknown: bool = False
) -> Plan:
    """比對 repo 裡的 migration 和資料庫的紀錄。

    * 紀錄裡有、repo 裡沒有 → 正式專案停止（資料庫被套用過不在 repo 的變更，要先查清楚）；
      staging（allow_unknown）只記在 plan.unknown，不影響其他版本
    * 基準清單裡、紀錄沒有 → 補紀錄（repair）
    * 其他 repo 有、紀錄沒有 → 待套用（db push）；必須比所有已套用的版本都新，避免順序錯亂
      （staging 的 unknown 不算：還沒合併的 PR 版本號可能比較新）
    """
    unknown = tuple(sorted(recorded - set(local)))
    if unknown and not allow_unknown:
        raise DeployError(f"資料庫紀錄裡有 repo 沒有的版本：{', '.join(unknown)}")
    missing_baseline = sorted(set(baseline) - set(local))
    if missing_baseline:
        raise DeployError(f"基準清單裡的版本在 repo 找不到：{', '.join(missing_baseline)}")

    repair = tuple(v for v in local if v in baseline and v not in recorded)
    pending = tuple(v for v in local if v not in recorded and v not in baseline)
    applied = (recorded - set(unknown)) | set(repair)
    if pending and applied and pending[0] < max(applied):
        raise DeployError(
            f"待套用的 {pending[0]} 比已套用的 {max(applied)} 還舊：順序可能錯亂，請先確認"
        )
    return Plan(repair=repair, pending=pending, unknown=unknown)


def _check_file(version: str, folder: Path) -> Path | None:
    matches = sorted(folder.glob(f"{version}_*.sql")) if folder.exists() else []
    if len(matches) > 1:
        raise DeployError(f"{version} 在 {folder.name} 有不只一個檢查檔")
    return matches[0] if matches else None


def precheck_path(version: str, prechecks_dir: Path = PRECHECKS_DIR) -> Path | None:
    return _check_file(version, prechecks_dir)


def baseline_check_path(version: str, folder: Path = BASELINE_CHECKS_DIR) -> Path | None:
    return _check_file(version, folder)


def failed_items(rows: list[tuple]) -> list[str]:
    """物件檢查的結果 (item, ok)：回傳沒通過的項目；一列都沒有也算沒通過。"""
    if not rows:
        return ["（檢查沒有回傳任何一列）"]
    return [str(item) for item, ok in rows if ok is not True]


# ---------------------------------------------------------------------------
# 資料庫（只讀）
# ---------------------------------------------------------------------------


def db_url() -> str:
    url = os.environ.get("DB_URL", "").strip()
    if not url:
        raise DeployError(f"沒有 DB_URL：請在 GitHub 設定 {TARGETS[target()][1]}")
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
    conn.rollback()  # 從新的交易開始，才能設定唯讀
    with conn.cursor() as cur:
        cur.execute("set transaction read only")
        cur.execute(f"set local statement_timeout = '{PRECHECK_TIMEOUT}'")
        cur.execute(sql)
        count = len(cur.fetchall()) if cur.description else 0
    conn.rollback()
    return count


def run_object_check(conn, sql: str) -> list[str]:
    """在唯讀交易裡執行物件檢查，回傳沒通過的項目（空 list = 全部存在）。

    查詢本身出錯（例如 cron schema 根本不存在）也算沒通過。
    """
    import psycopg

    conn.rollback()  # 從新的交易開始，才能設定唯讀
    try:
        with conn.cursor() as cur:
            cur.execute("set transaction read only")
            cur.execute(f"set local statement_timeout = '{PRECHECK_TIMEOUT}'")
            cur.execute(sql)
            rows = cur.fetchall()
    except psycopg.Error as exc:
        conn.rollback()
        return [f"檢查查詢出錯：{str(exc).splitlines()[0]}"]
    conn.rollback()
    return failed_items(rows)


def recorded_statements(conn) -> dict[str, list[str] | None]:
    """每個版本紀錄裡存的 SQL（沒有 statements 欄位時回傳空 dict）。"""
    if not history_has_statements(conn):
        return {}
    with conn.cursor() as cur:
        cur.execute("select version, statements from supabase_migrations.schema_migrations")
        return {row[0]: row[1] for row in cur.fetchall()}


def changed_versions(
    statements: dict[str, list[str] | None], local: list[str], since: str | None
) -> list[str]:
    """紀錄裡的 SQL 和 repo 檔案不同的版本（只比 since 以後、由這個工具寫的紀錄）。

    這個工具寫紀錄時 statements = [整個檔案的內容]；更早由 Supabase CLI 寫的紀錄會拆成多段，
    不能直接比，所以只比 since（基準清單的第一個版本）以後的。
    """
    if since is None:
        return []
    changed = []
    for version in local:
        if version < since or version not in statements:
            continue
        saved = statements[version]
        current = migration_file(version).read_text(encoding="utf-8")
        if saved != [current]:
            changed.append(version)
    return changed


def history_has_statements(conn) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "select exists (select 1 from information_schema.columns "
            "where table_schema = 'supabase_migrations' and table_name = 'schema_migrations' "
            "and column_name = 'statements')"
        )
        return bool(cur.fetchone()[0])


def record_applied(conn, version: str, name: str, sql: str, *, with_statements: bool) -> None:
    """寫一筆紀錄（不 commit）。statements 存整個檔案的內容（CLI 的 migration fetch 可以還原）。"""
    with conn.cursor() as cur:
        if with_statements:
            cur.execute(
                f"insert into {HISTORY_TABLE} (version, name, statements) values (%s, %s, %s)",
                (version, name, [sql]),
            )
        else:
            cur.execute(
                f"insert into {HISTORY_TABLE} (version, name) values (%s, %s)", (version, name)
            )


def apply_migration(conn, version: str, name: str, sql: str, *, with_statements: bool) -> None:
    """在目前的交易裡執行 migration 並寫紀錄（不 commit；呼叫端決定 commit 或 rollback）。"""
    with conn.cursor() as cur:
        cur.execute(f"set local lock_timeout = '{APPLY_LOCK_TIMEOUT}'")
        cur.execute(f"set local statement_timeout = '{APPLY_STATEMENT_TIMEOUT}'")
        cur.execute(sql)  # 沒有參數：一次送出整個檔案（含 do $$ 區塊）
    record_applied(conn, version, name, sql, with_statements=with_statements)


def run_checks(conn, plan: Plan) -> list[str]:
    """物件檢查（要補紀錄的版本）＋事前檢查（待套用的版本）。回傳沒通過的版本，過程印在 log。"""
    failed = []
    # 1. 要補紀錄的版本：物件必須真的存在，否則不補（補了之後就永遠不會再套用）
    for version in plan.repair:
        path = baseline_check_path(version)  # 正式專案和 staging 共用同一組檢查檔
        if path is None:
            failed.append(version)
            print(f"::error::{version}：沒有 supabase/baseline_checks/{version}_*.sql，不補紀錄")
            continue
        missing = run_object_check(conn, path.read_text(encoding="utf-8"))
        if missing:
            failed.append(version)
            for item in missing:
                why = item if item.startswith(("檢查查詢出錯", "（")) else f"{item} 不存在"
                print(f"::error::{version}：{why}，不補紀錄")
        else:
            print(f"{version}：物件都存在，可以補紀錄")
    # 2. 待套用的版本：資料要符合新限制
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
    return failed


STOPPED_HINT = (
    "已停止，資料庫沒有任何變更。物件不存在：先確認那個 migration 是否真的套用過；"
    "資料不符合：在 SQL Editor 執行事前檢查 SQL 找出資料、修正後再重跑這個 workflow。"
)


def expected_plan() -> Plan | None:
    """第 1 步算出的計畫（workflow 透過環境變數傳入）；沒有就回傳 None。"""
    if "EXPECT_REPAIR" not in os.environ and "EXPECT_PENDING" not in os.environ:
        return None
    return Plan(
        repair=tuple(os.environ.get("EXPECT_REPAIR", "").split()),
        pending=tuple(os.environ.get("EXPECT_PENDING", "").split()),
    )


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
    """目前的計畫。staging 另外確認紀錄的內容和 repo 一致（不一致就停止）。"""
    name = target()
    baseline = read_baseline(baseline_file(name))
    local = local_versions()
    plan = make_plan(local, recorded_versions(conn), baseline, allow_unknown=name == "staging")
    if name == "staging":
        changed = changed_versions(recorded_statements(conn), local, min(baseline, default=None))
        if changed:
            raise DeployError(
                f"staging 紀錄裡的內容和 repo 不同：{', '.join(changed)}"
                "（PR 套用到 staging 之後又改過？見 README〈staging 多出來的 migration〉）"
            )
    return plan


def _warn_unknown(plan: Plan) -> None:
    if plan.unknown:
        print(
            "::warning::staging 有 main 沒有的 migration（還沒合併的 PR？）："
            f"{' '.join(plan.unknown)}；PR 不合併的話要清理，"
            "見 README〈staging 多出來的 migration〉"
        )


def cmd_plan() -> int:
    url = db_url()
    mask_password(url)
    with connect(url) as conn:
        plan = _current_plan(conn)
    print(f"部署目標：{target()}")
    print(f"要補紀錄（以前手動套用過，只寫紀錄不執行）：{' '.join(plan.repair) or '（無）'}")
    print(f"要套用：{' '.join(plan.pending) or '（無）'}")
    _warn_unknown(plan)
    _write_output(repair=" ".join(plan.repair), pending=" ".join(plan.pending))
    _summary(
        [f"### migration 部署計畫（{target()}）", ""]
        + [f"- 補紀錄：`{v}`" for v in plan.repair]
        + [f"- 套用：`{v}`" for v in plan.pending]
        + ([] if plan.repair or plan.pending else ["- 沒有需要套用的 migration"])
        + [f"- ⚠️ staging 有 main 沒有的版本：`{v}`" for v in plan.unknown]
    )
    return 0


def cmd_precheck() -> int:
    url = db_url()
    with connect(url) as conn:
        failed = run_checks(conn, _current_plan(conn))
    if failed:
        print(STOPPED_HINT)
        return 1
    return 0


def cmd_apply() -> int:
    """唯一會寫入的指令：補紀錄、依序套用。"""
    url = db_url()
    expected = expected_plan()
    if expected is None and os.environ.get("GITHUB_ACTIONS") == "true":
        raise DeployError("缺少 EXPECT_REPAIR／EXPECT_PENDING（第 1 步的計畫），不寫入")
    with connect(url, read_only=False) as conn:
        plan = _current_plan(conn)
        conn.rollback()
        if expected is not None and plan.steps() != expected.steps():
            raise DeployError(
                "資料庫的紀錄和第 1 步看到的不同（可能同時有其他人在部署），不寫入；請重跑 workflow"
            )
        # 寫入前，在同一個連線上再確認一次
        if run_checks(conn, plan):
            print(STOPPED_HINT)
            return 1
        with_statements = history_has_statements(conn)
        conn.rollback()

        if plan.repair:
            for version in plan.repair:
                path = migration_file(version)
                record_applied(
                    conn,
                    version,
                    migration_name(path),
                    path.read_text(encoding="utf-8"),
                    with_statements=with_statements,
                )
            conn.commit()
            print(f"補上紀錄：{' '.join(plan.repair)}（只寫紀錄，沒有執行 SQL）")

        import psycopg

        for version in plan.pending:
            path = migration_file(version)
            try:
                apply_migration(
                    conn,
                    version,
                    migration_name(path),
                    path.read_text(encoding="utf-8"),
                    with_statements=with_statements,
                )
                conn.commit()
            except psycopg.Error as exc:
                conn.rollback()
                raise DeployError(
                    f"{path.name} 套用失敗，這個 migration 已整個 rollback："
                    f"{str(exc).splitlines()[0]}"
                ) from None
            print(f"套用：{path.name}")
    return 0


def cmd_verify() -> int:
    url = db_url()
    with connect(url) as conn:
        plan = _current_plan(conn)
    if plan.repair or plan.pending:
        print(f"::error::還有沒套用的版本：{' '.join(plan.repair + plan.pending)}")
        return 1
    _warn_unknown(plan)
    print(f"確認完成（{target()}）：repo 裡的 migration 都已套用、也都有紀錄")
    return 0


COMMANDS = {"plan": cmd_plan, "precheck": cmd_precheck, "apply": cmd_apply, "verify": cmd_verify}


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
