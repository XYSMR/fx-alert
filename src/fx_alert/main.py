"""把所有模組串起來：python -m fx_alert check [--dry-run] [--force] [--config PATH]

每次執行：
  1. 不在檢查時段就直接結束（--force 可略過）
  2. 讀狀態檔 → 抓兆豐匯率 → 解析
  3. 每個幣別用狀態機判斷；要通知就寄信
     - 寄信成功 → 更新狀態
     - 寄信失敗 → 狀態維持原樣（下次執行再試），本次結束碼為 1
  4. 狀態有變化才寫檔；在 GitHub Actions 上輸出 state_changed=true/false

結束碼：0 = 一切正常（含略過）；1 = 有任何錯誤（抓取、解析、寄信、設定）。
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
from fx_alert.notifier import (
    ConfigError,
    DryRunNotifier,
    EmailConfig,
    Notifier,
    NotifyError,
    SmtpNotifier,
    build_alert_message,
)
from fx_alert.parser import ParseError, get_cash_sell, parse_rates
from fx_alert.rules import decide
from fx_alert.state_store import (
    StateError,
    current,
    load_states,
    save_states,
    with_transition,
)


@dataclass
class CheckResult:
    skipped: bool = False
    state_changed: bool = False
    notified: list[str] = field(default_factory=list)
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
) -> CheckResult:
    result = CheckResult()
    local_now = now.astimezone(TAIPEI)

    if not force and not cfg.hours.contains(local_now):
        log(f"略過：{local_now:%Y-%m-%d %H:%M} 不在檢查時段內")
        result.skipped = True
        return result

    try:
        states = load_states(cfg.state_file)
    except StateError as exc:
        result.errors.append(str(exc))
        return result  # 狀態檔壞了就整個停下來，避免重複寄信

    try:
        board = parse_rates((fetch or fetch_rates_json)())
    except (FetchError, ParseError) as exc:
        result.errors.append(str(exc))
        return result

    log(f"牌告時間：{board.quoted_at or '（未提供）'}")
    new_states = states
    for rule in cfg.rules:
        try:
            rate = get_cash_sell(board, rule.currency)
        except ParseError as exc:
            result.errors.append(str(exc))
            continue  # 一個幣別出錯，不影響其他幣別

        prev = current(new_states, rule.currency)
        decision = decide(rule, rate, prev)
        prev_label = prev.value if prev else "（無紀錄）"
        log(
            f"{rule.currency}: 現金賣出 {rate}（目標 ≤ {rule.target}）"
            f" {prev_label} → {decision.next_state.value}"
            f"{'，寄通知' if decision.notify else ''}"
        )

        if decision.notify:
            msg = build_alert_message(email_cfg, rule, rate, board.quoted_at)
            try:
                notifier.send(msg)
            except NotifyError as exc:
                result.errors.append(f"{rule.currency} 通知失敗，狀態不更新：{exc}")
                continue
            result.notified.append(rule.currency)

        new_states = with_transition(
            new_states, rule.currency, decision.next_state, rate, local_now
        )

    result.state_changed = save_states(cfg.state_file, new_states)
    log(f"狀態檔{'已更新' if result.state_changed else '沒有變化'}：{cfg.state_file}")
    return result


def _write_github_output(result: CheckResult) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"state_changed={'true' if result.state_changed else 'false'}\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m fx_alert", description="匯率到價提醒")
    sub = ap.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="檢查匯率並在到價時寄信")
    check.add_argument("--config", type=Path, default=Path("config.yaml"))
    check.add_argument("--dry-run", action="store_true", help="不真的寄信，只印出來")
    check.add_argument("--force", action="store_true", help="略過檢查時段限制")
    args = ap.parse_args(argv)

    try:
        cfg = load_config(args.config)
        if args.dry_run:
            email_cfg = EmailConfig(
                user="dry-run@example.com", password="", to=("you@example.com",)
            )
            notifier: Notifier = DryRunNotifier()
        else:
            email_cfg = EmailConfig.from_env()
            notifier = SmtpNotifier(email_cfg)
    except (ConfigFileError, ConfigError) as exc:
        print(f"設定錯誤：{exc}", file=sys.stderr)
        return 1

    result = run_check(cfg, notifier, email_cfg, now=datetime.now(TAIPEI), force=args.force)
    for err in result.errors:
        print(f"錯誤：{err}", file=sys.stderr)
    _write_github_output(result)
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
