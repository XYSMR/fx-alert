"""命令列入口。

    python -m fx_alert check [--mode single|multi] [--dry-run] [--force] [--config PATH]
    python -m fx_alert validate-config [--config PATH]
    python -m fx_alert build-site [--config PATH] [--data-dir DIR] [--out site/data.json]

check 每次執行：
  1. 不在檢查時段就直接結束（--force 可略過），什麼檔案都不寫
  2. 從 store 讀規則和狀態 → 抓兆豐匯率 → 解析 → 記錄走勢（同一份牌告只記一次）
     （store 預設是 FileStore：config.yaml + state.json；多人版用 SupabaseStore）
  3. 每個幣別用狀態機判斷；要通知就寄信
     - 寄信成功 → 更新狀態、記一筆通知紀錄
     - 寄信失敗 → 狀態維持原樣（下次執行再試），本次結束碼為 1
  4. 寫出狀態（有變才寫）和 last_check.json（每次都寫）

--mode single（預設）：config.yaml + state.json，收件人 MAIL_TO。
--mode multi：Supabase（SUPABASE_URL、SUPABASE_SECRET_KEY），每個人寄給自己；
               搭配 --dry-run 時只讀不寫。

每天寄信上限（config.yaml 的 email.daily_limit）：超過的通知這次不寄、狀態不更新，
下次排程再試；這不算錯誤，但會在 log 和 Actions 顯示警告。

結束碼：0 = 一切正常（含略過、延後）；1 = 有任何錯誤（抓取、解析、寄信、設定）。
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from fx_alert.config import TAIPEI, AppConfig, ConfigFileError, load_config
from fx_alert.fetcher import FetchError, fetch_rates_json
from fx_alert.history import append_quotes, write_last_check
from fx_alert.notifier import (
    ConfigError,
    DryRunNotifier,
    EmailConfig,
    Notifier,
    NotifyError,
    SmtpNotifier,
    build_alert_message,
    redact_emails,
)
from fx_alert.parser import ParseError, get_cash_sell, parse_rates
from fx_alert.rules import decide
from fx_alert.store import (
    FileStore,
    ReadOnlyStore,
    RuleStore,
    StoreError,
    SupabaseConfig,
    SupabaseStore,
)
from fx_alert.unsubscribe import UnsubscribeConfig, UnsubscribeError


@dataclass
class CheckResult:
    skipped: bool = False
    state_changed: bool = False
    notified: list[str] = field(default_factory=list)
    deferred: list[str] = field(default_factory=list)  # 超過每日上限，延到下次
    errors: list[str] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        return 1 if self.errors else 0


def run_check(
    cfg: AppConfig,
    notifier: Notifier,
    email_cfg: EmailConfig,
    *,
    now: datetime,
    fetch: Callable[[], str] | None = None,
    force: bool = False,
    log: Callable[[str], None] = print,
    store: RuleStore | None = None,
    unsubscribe: UnsubscribeConfig | None = None,
) -> CheckResult:
    """store 沒給就用單人版的 FileStore（config.yaml + state.json，收件人 MAIL_TO）。

    unsubscribe：多人版給；有 user_id 的規則，信裡會加上退訂連結和 List-Unsubscribe 標頭。
    """
    result = CheckResult()
    local_now = now.astimezone(TAIPEI)

    if not force and not cfg.hours.contains(local_now):
        log(f"略過：{local_now:%Y-%m-%d %H:%M} 不在檢查時段內")
        result.skipped = True
        return result

    store = store or FileStore(cfg, recipients=email_cfg.to)
    try:
        _check_rates(cfg, store, notifier, email_cfg, local_now, fetch, result, log, unsubscribe)
    finally:
        # 不論成功失敗都記錄這次檢查，頁面才看得出系統還活著
        write_last_check(cfg.data_dir, local_now, ok=not result.errors, errors=len(result.errors))
    return result


def _check_rates(
    cfg, store, notifier, email_cfg, local_now, fetch, result, log, unsubscribe
) -> None:
    try:
        watches = store.load_watches()
    except StoreError as exc:
        result.errors.append(str(exc))
        return  # 讀不到狀態就整個停下來，避免重複寄信

    try:
        board = parse_rates((fetch or fetch_rates_json)())
    except (FetchError, ParseError) as exc:
        result.errors.append(str(exc))
        return

    day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        remaining = cfg.daily_limit - store.count_sent_since(day_start)
    except StoreError as exc:
        result.errors.append(f"查不到今天已寄出的數量，這次不寄信：{exc}")
        return

    log(f"牌告時間：{board.quoted_at or '（未提供）'}")
    # 走勢記錄 config 追蹤的幣別；規則用到的幣別也一併取價
    codes = dict.fromkeys([r.currency for r in cfg.rules] + [w.rule.currency for w in watches])
    rates = {}
    for code in codes:
        try:
            rates[code] = get_cash_sell(board, code)
        except ParseError as exc:
            result.errors.append(str(exc))  # 一個幣別出錯，不影響其他幣別
    if append_quotes(cfg.data_dir, board.quoted_at, rates):
        log("走勢：記錄了新的牌告")

    for w in watches:
        rule = w.rule
        if rule.currency not in rates:
            continue
        rate = rates[rule.currency]
        decision = decide(rule, rate, w.state, w.state_target)
        prev_label = w.state.value if w.state else "（無紀錄）"
        log(
            f"{w.label}: 現金賣出 {rate}（目標 ≤ {rule.target}）"
            f" {prev_label} → {decision.next_state.value}：{decision.reason}"
        )

        try:
            if decision.notify:
                if remaining <= 0:
                    result.deferred.append(w.label)  # 狀態不更新，下次再試
                    continue
                links = None
                if unsubscribe and w.user_id:
                    try:
                        links = unsubscribe.links(w.user_id)
                    except UnsubscribeError as exc:
                        result.errors.append(f"{w.label} 無法產生退訂連結，這次不寄：{exc}")
                        continue
                msg = build_alert_message(
                    email_cfg, rule, rate, board.quoted_at, to=w.recipients, unsubscribe=links
                )
                try:
                    notifier.send(msg)
                except NotifyError as exc:
                    result.errors.append(
                        f"{w.label} 通知失敗，狀態不更新：{redact_emails(str(exc))}"
                    )
                    continue
                remaining -= 1
                result.notified.append(w.label)
                store.record_alert(w, rate, local_now)
            elif not store.record_transition(w, decision.next_state, rate, local_now):
                log(f"{w.label}: 狀態在檢查期間被變更，這次不更新")
        except StoreError as exc:
            # 一條規則寫入失敗，不影響其他規則
            result.errors.append(f"{w.label} 狀態寫入失敗：{exc}")

    if result.deferred:
        log(f"今天已達寄信上限 {cfg.daily_limit} 封，延後 {len(result.deferred)} 封到下次排程")

    try:
        result.state_changed = store.flush()
    except StoreError as exc:
        result.errors.append(f"狀態寫入失敗：{exc}")
        return
    log(f"狀態{'已更新' if result.state_changed else '沒有變化'}")


def _write_github_output(result: CheckResult) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"state_changed={'true' if result.state_changed else 'false'}\n")


def _cmd_check(args) -> int:
    store: RuleStore | None = None
    unsubscribe: UnsubscribeConfig | None = None
    try:
        cfg = load_config(args.config)
        if args.mode == "multi":
            store = SupabaseStore(SupabaseConfig.from_env())
            unsubscribe = _unsubscribe_config(required=not args.dry_run)
            if args.dry_run:
                store = ReadOnlyStore(store)
        if args.dry_run:
            email_cfg = EmailConfig(
                user="dry-run@example.com", password="", to=("you@example.com",)
            )
            notifier: Notifier = DryRunNotifier()
        else:
            email_cfg = EmailConfig.from_env(require_to=args.mode == "single")
            notifier = SmtpNotifier(email_cfg)
    except (ConfigFileError, ConfigError, StoreError, UnsubscribeError) as exc:
        print(f"設定錯誤：{exc}", file=sys.stderr)
        return 1

    result = run_check(
        cfg,
        notifier,
        email_cfg,
        now=datetime.now(TAIPEI),
        force=args.force,
        store=store,
        unsubscribe=unsubscribe,
    )
    for err in result.errors:
        print(f"錯誤：{redact_emails(err)}", file=sys.stderr)
    if result.deferred and os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::warning::今天已達寄信上限，延後 {len(result.deferred)} 封通知到下次排程")
    _write_github_output(result)
    return result.exit_code


def _unsubscribe_config(*, required: bool) -> UnsubscribeConfig | None:
    """多人版正式寄信一定要有退訂連結；dry-run 沒設定密鑰也可以跑。"""
    if not required and not os.environ.get("UNSUBSCRIBE_SECRET", "").strip():
        print("（沒有 UNSUBSCRIBE_SECRET：dry-run 不產生退訂連結）")
        return None
    return UnsubscribeConfig.from_env()


def _cmd_unsubscribe_link(args) -> int:
    """只在自己的電腦用：印出某個使用者的退訂連結（測試用，不要在 Actions 執行）。"""
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print("這個指令會印出退訂連結，不能在公開的 GitHub Actions log 裡執行", file=sys.stderr)
        return 1
    try:
        links = UnsubscribeConfig.from_env().links(args.user_id)
    except UnsubscribeError as exc:
        print(f"設定錯誤：{exc}", file=sys.stderr)
        return 1
    print("確認頁（給人點）：")
    print(f"  {links.page_url}")
    print("一鍵退訂（信箱按鈕會 POST 到這裡）：")
    print(f"  {links.one_click_url}")
    return 0


def _cmd_validate_config(args) -> int:
    try:
        cfg = load_config(args.config)
    except ConfigFileError as exc:
        print(f"config.yaml 驗證失敗：{exc}", file=sys.stderr)
        return 1
    h = cfg.hours
    days = "平日" if h.weekdays_only else "每天"
    print(f"config.yaml 驗證通過：檢查時段 {days} {h.start:%H:%M}–{h.end:%H:%M}")
    print(f"  每天最多寄 {cfg.daily_limit} 封到價通知")
    for r in cfg.rules:
        rearm = f"回升到 > {r.rearm_above} 重新上膛"
        print(f"  {r.currency}: 目標 ≤ {r.target}，buffer {r.buffer}（{rearm}）")
    return 0


def _cmd_build_site(args) -> int:
    from fx_alert.build_site import build_data, write_data_json
    from fx_alert.state_store import StateError

    try:
        cfg = load_config(args.config)
        data_dir = args.data_dir or cfg.data_dir
        data = build_data(cfg, data_dir, now=datetime.now(TAIPEI))
    except (ConfigFileError, StateError) as exc:
        print(f"產生 data.json 失敗：{exc}", file=sys.stderr)
        return 1
    write_data_json(data, args.out)
    codes = ", ".join(f"{c['code']} {c['rate']}" for c in data["currencies"])
    print(f"已產生 {args.out}（{codes}；通知紀錄 {len(data['alerts'])} 筆）")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m fx_alert", description="匯率到價提醒")
    sub = ap.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="檢查匯率並在到價時寄信")
    check.add_argument("--config", type=Path, default=Path("config.yaml"))
    check.add_argument(
        "--mode",
        choices=("single", "multi"),
        default="single",
        help="single = config.yaml + MAIL_TO（預設）；multi = Supabase 多人版",
    )
    check.add_argument("--dry-run", action="store_true", help="不真的寄信，只印出來")
    check.add_argument("--force", action="store_true", help="略過檢查時段限制")
    check.set_defaults(func=_cmd_check)

    unsub = sub.add_parser("unsubscribe-link", help="（本機測試用）印出某個使用者的退訂連結")
    unsub.add_argument("--user-id", required=True, help="Supabase 使用者的 UUID")
    unsub.set_defaults(func=_cmd_unsubscribe_link)

    validate = sub.add_parser("validate-config", help="只驗證 config.yaml 格式")
    validate.add_argument("--config", type=Path, default=Path("config.yaml"))
    validate.set_defaults(func=_cmd_validate_config)

    build = sub.add_parser("build-site", help="產生前端用的 data.json")
    build.add_argument("--config", type=Path, default=Path("config.yaml"))
    build.add_argument("--data-dir", type=Path, default=None, help="預設用 config 的 data_dir")
    build.add_argument("--out", type=Path, default=Path("site/data.json"))
    build.set_defaults(func=_cmd_build_site)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
