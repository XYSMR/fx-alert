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
  4. 寫出狀態（有變才寫）和 last_check.json（每次都寫）；
     多人版另外把檢查結果寫到 Supabase 的 check_status（gate 和網頁狀態列讀它），
     所以 state 分支只在走勢、狀態、通知紀錄有變時才需要 commit（見 publish.py）

--mode single（預設）：config.yaml + state.json，收件人 MAIL_TO。
--mode multi：Supabase（SUPABASE_URL、SUPABASE_SECRET_KEY），每個人寄給自己；
               搭配 --dry-run 時只讀不寫。

每天寄信上限（config.yaml 的 email.daily_limit）：超過的通知這次不寄、狀態不更新，
下次排程再試；這不算錯誤，但會在 log 和 Actions 顯示警告。

出國換匯計畫（多人版，store 有 load_trips 時）：到價提醒之後，對每趟旅程用 trip_rules.decide 判斷
今天要不要寄、寄哪一種（見 _check_trips）。和到價提醒共用通知時段、每日寄信上限、退訂連結。

公開 log 的原則（LOG_PRIVACY；GitHub Actions 的 log 在 public repo 任何人都看得到）：
  只寫「規則#N」「旅程#N」這類編號、狀態、提醒種類和公開的牌告資訊。
  不寫 email、目標價、buffer、通知時段、外幣金額、出發日或距離出發的天數。
  CLI 印出的每一行還會再經過 redact_emails（萬一錯誤訊息帶了 email）。
  tests/test_log_privacy.py 用含有這些值的假資料跑一次，檢查輸出裡都找不到。

結束碼：0 = 一切正常（含略過、延後）；1 = 有任何錯誤（抓取、解析、寄信、設定）。
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from fx_alert.config import TAIPEI, AppConfig, ConfigFileError, load_config
from fx_alert.currencies import CODES, SPOT_ONLY
from fx_alert.fetcher import FetchError, fetch_rates_json
from fx_alert.history import append_quotes, convert_monthly_history, write_last_check
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
from fx_alert.trip_mail import build_trip_message
from fx_alert.trip_rules import decide as decide_trip
from fx_alert.trip_rules import schedule as trip_schedule
from fx_alert.unsubscribe import DEFAULT_SITE_URL as UNSUB_SITE
from fx_alert.unsubscribe import UnsubscribeConfig, UnsubscribeError


@dataclass
class CheckResult:
    skipped: bool = False
    state_changed: bool = False
    notified: list[str] = field(default_factory=list)
    deferred: list[str] = field(default_factory=list)  # 超過每日上限，延到下次
    outside_window: list[str] = field(default_factory=list)  # 已到價但不在通知時段，時段開始再判斷
    trip_notified: list[str] = field(default_factory=list)  # 換匯計畫：寄出的提醒（label: kind）
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
    track: Iterable[str] = (),
) -> CheckResult:
    """store 沒給就用單人版的 FileStore（config.yaml + state.json，收件人 MAIL_TO）。

    unsubscribe：多人版給；有 user_id 的規則，信裡會加上退訂連結和 List-Unsubscribe 標頭。
    track：除了規則用到的幣別，另外要記錄走勢的幣別（多人版傳入全部 18 種）。
      這些幣別缺報價只記一行注意，不算錯誤；有人設定規則的幣別缺報價才算錯誤。
    """
    result = CheckResult()
    local_now = now.astimezone(TAIPEI)

    if not force and not cfg.hours.contains(local_now):
        log(f"略過：{local_now:%Y-%m-%d %H:%M} 不在檢查時段內")
        result.skipped = True
        return result

    store = store or FileStore(cfg, recipients=email_cfg.to)
    _convert_history(cfg, log)
    try:
        _check_rates(
            cfg, store, notifier, email_cfg, local_now, fetch, result, log, unsubscribe, track
        )
    finally:
        # 不論成功失敗都記錄這次檢查，頁面才看得出系統還活著
        ok, errors = not result.errors, len(result.errors)
        write_last_check(cfg.data_dir, local_now, ok=ok, errors=errors)
        record = getattr(store, "record_check", None)
        if record is not None:
            try:
                record(local_now, ok, errors)
            except StoreError as exc:
                # 寫不進去（例如 check_status 的 migration 還沒套用）不算錯誤：
                # gate 會照常檢查、網頁會退回用 data.json 的時間，只是提醒一下
                log(f"注意：檢查狀態沒有寫到 Supabase（{exc}）")
    return result


def _convert_history(cfg: AppConfig, log: Callable[[str], None]) -> None:
    """舊格式的走勢（每月一檔）拆成每天一檔。只會發生一次；失敗不影響檢查（兩種格式都讀得到）。"""
    try:
        months = convert_monthly_history(cfg.data_dir)
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        log(f"注意：走勢檔案轉換成每天一檔失敗，先沿用舊格式（{type(exc).__name__}：{exc}）")
        return
    if months:
        log(f"走勢：把 {', '.join(months)} 的月份檔拆成每天一檔")


def _check_rates(
    cfg, store, notifier, email_cfg, local_now, fetch, result, log, unsubscribe, track=()
) -> None:
    try:
        watches = store.load_watches()
        if getattr(store, "window_supported", True) is False:
            log("注意：資料庫還沒有通知時段欄位（migration 還沒套用），這次全部當作全天")
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
    # 必要：config 的幣別和有人設定規則的幣別；缺報價就是錯誤（可能漏通知）
    required = dict.fromkeys([r.currency for r in cfg.rules] + [w.rule.currency for w in watches])
    rates = {}
    for code in dict.fromkeys([*required, *track]):
        try:
            rates[code] = get_cash_sell(board, code)
        except ParseError as exc:
            if code in required:
                result.errors.append(str(exc))  # 一個幣別出錯，不影響其他幣別
            else:
                log(f"注意：{exc}（沒有人設定這個幣別，這次只略過）")
    unknown = sorted(set(board.quotes) - set(CODES) - set(SPOT_ONLY))
    if unknown:
        log(f"注意：兆豐牌告出現尚未支援的幣別 {', '.join(unknown)}，要支援請更新 currencies.py")
    if append_quotes(cfg.data_dir, board.quoted_at, rates):
        log(f"走勢：記錄了新的牌告（{len(rates)} 種幣別）")

    for w in watches:
        rule = w.rule
        if rule.currency not in rates:
            continue
        rate = rates[rule.currency]
        decision = decide(rule, rate, w.state, w.state_target)
        prev_label = w.state.value if w.state else "（無紀錄）"
        # 公開的 log：只寫規則編號和狀態，不寫幣別、匯率、目標價（見 LOG_PRIVACY）
        log(f"{w.label}: {prev_label} → {decision.next_state.value}：{decision.reason}")

        try:
            if decision.notify and w.window and not w.window.contains(local_now):
                # 不在通知時段：不寄、狀態不變（還是等待中）。時段開始後的第一次檢查會重新判斷，
                # 那時匯率還在目標以下才寄；已經回升就繼續等待，不補寄
                result.outside_window.append(w.label)
                log(f"{w.label}: 不在通知時段，時段開始後重新判斷")
                continue
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
                if not store.record_alert(w, rate, local_now):
                    # 使用者剛好在檢查期間刪除帳號（或刪除規則）：略過，不算錯誤
                    log(f"{w.label}: 規則已不存在（可能剛刪除帳號），略過記錄")
            elif not store.record_transition(w, decision.next_state, rate, local_now):
                # 規則被刪除、或狀態被使用者改過（例如重新開啟 Email）：這次不更新
                log(f"{w.label}: 規則在檢查期間被變更或刪除，這次不更新")
        except StoreError as exc:
            # 一條規則寫入失敗，不影響其他規則
            result.errors.append(f"{w.label} 狀態寫入失敗：{exc}")

    ctx = (cfg, store, notifier, email_cfg, local_now, board, rates, result, log, unsubscribe)
    remaining = _check_trips(*ctx, remaining)

    if result.deferred:
        log(f"今天已達寄信上限 {cfg.daily_limit} 封，延後 {len(result.deferred)} 封到下次排程")

    try:
        result.state_changed = store.flush()
    except StoreError as exc:
        result.errors.append(f"狀態寫入失敗：{exc}")
        return
    log(f"狀態{'已更新' if result.state_changed else '沒有變化'}")


def _check_trips(
    cfg, store, notifier, email_cfg, local_now, board, rates, result, log, unsubscribe, remaining
) -> int:
    """出國換匯計畫：每趟旅程今天要不要寄提醒。回傳剩下還能寄幾封。

    * 價格類提醒（相對便宜）只在「今天已經有新牌告」時判斷：早上 8 點時牌告還是昨天的，
      不能拿昨天的價格說「現在」；颱風停班沒有牌告的日子也不會寄價格提醒。
    * 固定日期的提醒（分批、最後、出發前）不看價格，營業日的通知時段內第一次檢查就寄。
    * 不在通知時段：這次不判斷，時段開始後的第一次檢查再判斷（固定提醒當天會補寄）。
    * log 只寫「幣別 旅程#編號」和提醒種類，不寫 email 和金額（Actions 的 log 是公開的）。
    """
    load = getattr(store, "load_trips", None)
    if load is None:
        return remaining
    try:
        trips = load()
    except StoreError as exc:
        result.errors.append(f"讀不到換匯計畫的旅程：{exc}")
        return remaining
    if getattr(store, "trips_supported", True) is False:
        log("注意：資料庫還沒有換匯計畫的表（migration 還沒套用），略過換匯提醒")
        return remaining
    if not trips:
        return remaining

    from fx_alert.bizdays import load_calendar
    from fx_alert.daily import daily_closes, recent_stats

    today = local_now.date()
    try:
        cal = load_calendar()
        closes = daily_closes(cfg.data_dir, until=local_now)
    except (OSError, ValueError) as exc:
        result.errors.append(f"換匯計畫：讀不到營業日曆或收盤價，這次不寄：{type(exc).__name__}")
        return remaining
    if any(t.departure > cal.covered_until for t in trips):
        log(f"注意：營業日曆只到 {cal.covered_until}，請補上之後的國定假日（seed/README.md）")
    fresh = board.quoted_at is not None and board.quoted_at.astimezone(TAIPEI).date() == today

    counted = False
    for t in trips:
        rate = rates.get(t.currency)
        if rate is None:
            try:  # 沒有人設定到價提醒的幣別（單機測試時沒有 track 全部幣別）
                rate = get_cash_sell(board, t.currency)
            except ParseError:
                rate = None  # 沒有報價：只會寄固定日期的提醒
        recent = None
        if fresh and rate is not None and closes.get(t.currency):
            recent = recent_stats(closes[t.currency], rate, today)
        decision = decide_trip(t.as_trip(), today, recent, list(t.sent), cal)
        if decision is None:
            continue
        if t.window and not t.window.contains(local_now):
            log(f"{t.label}: 要寄 {decision.kind}，但不在通知時段，時段開始後再判斷")
            continue
        if not counted:
            # 今天已經寄出的換匯提醒也算進每日上限（只在真的有提醒要寄時才查一次）
            day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
            try:
                remaining -= store.count_trip_sent_since(day_start)
            except StoreError as exc:
                result.errors.append(f"查不到今天已寄出的換匯提醒數量，這次不寄：{exc}")
                return remaining
            counted = True
        if remaining <= 0:
            result.deferred.append(t.label)
            continue
        links = None
        if unsubscribe and t.user_id:
            try:
                links = unsubscribe.links(t.user_id)
            except UnsubscribeError as exc:
                result.errors.append(f"{t.label} 無法產生退訂連結，這次不寄：{exc}")
                continue
        msg = build_trip_message(
            email_cfg,
            currency=t.currency,
            decision=decision,
            sch=trip_schedule(t.departure, cal),
            today=today,
            rate=rate,
            quoted_at=board.quoted_at,
            remaining=t.remaining,
            to=t.recipients,
            unsubscribe=links,
            site_url=unsubscribe.site_url if unsubscribe else UNSUB_SITE,
        )
        try:
            notifier.send(msg)
        except NotifyError as exc:
            result.errors.append(f"{t.label} 換匯提醒寄送失敗：{redact_emails(str(exc))}")
            continue
        remaining -= 1
        result.trip_notified.append(f"{t.label}: {decision.kind}")
        log(f"{t.label}: 寄出換匯提醒 {decision.kind}")
        try:
            if not store.record_trip_notice(t, decision, rate, local_now):
                log(f"{t.label}: 提醒紀錄已存在或旅程剛被刪除，略過記錄")
        except StoreError as exc:
            result.errors.append(f"{t.label} 換匯提醒紀錄寫入失敗：{exc}")
    return remaining


def _public_log(line: str) -> None:
    """印到 Actions 的公開 log；保險起見再遮一次 email。"""
    print(redact_emails(line))


def _write_github_output(result: CheckResult) -> None:
    """給 workflow 用的輸出：

    skipped        true = 不在檢查時段內，沒有抓匯率也沒有判斷（workflow 不會 ping 監控「成功」）
    state_changed  true = 有狀態轉換

    設定錯誤時不會走到這裡，所以不會有 skipped；workflow 只在 skipped=false 時 ping 成功。
    """
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"skipped={'true' if result.skipped else 'false'}\n")
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
        log=_public_log,
        store=store,
        unsubscribe=unsubscribe,
        track=CODES if args.mode == "multi" else (),  # 多人版記錄全部 18 種幣別的走勢
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


# 驗證退訂連結用的 user_id：不會是任何人的帳號（UUID v4 格式，但隨機部分全是 0）
UNSUBSCRIBE_PROBE_USER = "00000000-0000-4000-8000-000000000000"
UNSUBSCRIBE_RESULTS = {
    (200, "no_account"): (True, "通過：Edge Function 驗得過簽章（UNSUBSCRIBE_SECRET 兩邊一致）"),
    (403, "invalid_link"): (
        False,
        "簽章不符：GitHub Secret 的 UNSUBSCRIBE_SECRET 和 Edge Function 的不一致，"
        "信裡的退訂連結會失效",
    ),
    (500, "not_configured"): (
        False,
        "Edge Function 沒有設定 UNSUBSCRIBE_SECRET（或少於 32 個字元）",
    ),
    (500, "update_failed"): (
        False,
        "簽章一致，但 Edge Function 寫不進資料庫（檢查 Edge Function 的 FX_SECRET_KEY）",
    ),
}


def check_unsubscribe(cfg: UnsubscribeConfig, post) -> tuple[bool, str]:
    """用一個不存在的帳號產生退訂連結，POST 給 Edge Function：
    簽章一致 → 200 no_account（沒有這個帳號，什麼都不改）；不一致 → 403 invalid_link。
    不會退訂任何人。回傳（是否通過, 說明）；說明不含連結、簽章或密鑰。
    """
    url = cfg.links(UNSUBSCRIBE_PROBE_USER).one_click_url
    try:
        status, body = post(url)
    except Exception as exc:  # noqa: BLE001  連不到也只回報類型
        return False, f"連不到 Edge Function：{type(exc).__name__}"
    key = (status, body.get("status") or body.get("error") if isinstance(body, dict) else None)
    if key in UNSUBSCRIBE_RESULTS:
        return UNSUBSCRIBE_RESULTS[key]
    return False, f"Edge Function 回應 {status}（預期 200 no_account）"


def _post_json(url: str) -> tuple[int, object]:
    import requests

    resp = requests.post(url, timeout=15)
    try:
        return resp.status_code, resp.json()
    except ValueError:
        return resp.status_code, None


def _cmd_unsubscribe_check(args) -> int:
    """確認 UNSUBSCRIBE_SECRET 和 Edge Function 一致（staging-check 每次都跑）。"""
    try:
        cfg = UnsubscribeConfig.from_env()
    except UnsubscribeError as exc:
        print(f"設定錯誤：{exc}", file=sys.stderr)
        return 1
    ok, message = check_unsubscribe(cfg, _post_json)
    print(f"退訂連結驗證：{message}")
    if not ok and os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::error::退訂連結驗證失敗：{message}")
    return 0 if ok else 1


def _cmd_migrate_me(args) -> int:
    """單人版 → 多人版：把 config.yaml、state.json、alerts.csv 搬到自己的帳號。"""
    from fx_alert.migrate import (
        MigrationClient,
        MigrationError,
        collect,
        compare,
        describe,
        mask_email,
    )

    if os.environ.get("GITHUB_ACTIONS") == "true":
        print("migrate-me 會印出 email，不能在公開的 GitHub Actions log 裡執行", file=sys.stderr)
        return 1
    who = mask_email(args.email)
    try:
        cfg = load_config(args.config)
        plan = collect(cfg, args.data_dir or cfg.data_dir)
        print(f"準備搬到 {who} 的帳號：")
        print("\n".join(describe(plan)))
        if args.dry_run:
            print("（--dry-run：不寫入任何資料）")
            return 0
        client = MigrationClient(SupabaseConfig.from_env())  # secret key 只從環境變數讀
        user_id = client.find_user(args.email)
        result = client.import_plan(user_id, plan, replace=args.replace)
        print(f"已寫入：規則 {result.get('rules')} 條、通知紀錄 {result.get('notifications')} 筆")
        problems = compare(plan, client.fetch(user_id))
    except (ConfigFileError, StoreError, MigrationError) as exc:
        print(f"搬家失敗：{exc}", file=sys.stderr)
        return 1
    if problems:
        print("比對不一致：", file=sys.stderr)
        for p in problems:
            print(f"  ✕ {p}", file=sys.stderr)
        return 1
    print("✓ 比對完全一致（目標價、buffer、狀態、時間、通知紀錄）")
    return 0


def _cmd_export_state(args) -> int:
    """退回方案：把資料庫裡自己的規則狀態、通知紀錄寫回 state.json、alerts.csv。"""
    from fx_alert.export_state import describe_export, plan_export, write_export
    from fx_alert.migrate import MigrationClient, MigrationError, mask_email

    if os.environ.get("GITHUB_ACTIONS") == "true":
        print("export-state 會用到 email，不能在公開的 GitHub Actions log 裡執行", file=sys.stderr)
        return 1
    who = mask_email(args.email)
    try:
        cfg = load_config(args.config)
        data_dir = args.data_dir or cfg.data_dir
        client = MigrationClient(SupabaseConfig.from_env())  # secret key 只從環境變數讀
        db = client.fetch(client.find_user(args.email))
        plan = plan_export(db, cfg, data_dir)
        print(f"從 {who} 的帳號匯出到 {data_dir}：")
        print("\n".join(describe_export(plan)))
        if args.dry_run:
            print("（--dry-run：不寫入任何檔案）")
            return 0
        if not plan.has_changes:
            print("✓ 檔案已經是最新的，不用寫入")
            return 0
        write_export(plan, data_dir)
    except (ConfigFileError, StoreError, MigrationError) as exc:
        print(f"匯出失敗：{exc}", file=sys.stderr)
        return 1
    print("✓ 已寫入。請在 state 分支的資料夾 git add / commit / push")
    return 0


def _cmd_site_config(args) -> int:
    from fx_alert.site_config import SiteConfigError, write_site_config

    try:
        cfg = write_site_config(args.out)
    except SiteConfigError as exc:
        print(f"產生 config.js 失敗：{exc}", file=sys.stderr)
        return 1
    if cfg["authEnabled"]:
        print(f"已產生 {args.out}：登入功能開啟（{cfg['supabaseUrl']}）")
    else:
        print(f"已產生 {args.out}：登入功能關閉（不含任何 Supabase 設定）")
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
    # CI 的 log 是公開的：只列幣別，不印目標價和 buffer
    codes = "、".join(r.currency for r in cfg.rules) or "（無）"
    print(f"  config.yaml 的規則：{len(cfg.rules)} 條（{codes}）")
    return 0


def _cmd_build_site(args) -> int:
    from fx_alert.build_site import build_data, write_data_json
    from fx_alert.state_store import StateError

    try:
        cfg = load_config(args.config)
        data_dir = args.data_dir or cfg.data_dir
        data = build_data(cfg, data_dir, now=datetime.now(TAIPEI), public=args.public)
    except (ConfigFileError, StateError) as exc:
        print(f"產生 data.json 失敗：{exc}", file=sys.stderr)
        return 1
    write_data_json(data, args.out)
    codes = ", ".join(f"{c['code']} {c['rate']}" for c in data["currencies"])
    if args.public:
        print(f"已產生 {args.out}（公開版，不含目標價與通知紀錄；{codes}）")
    else:
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

    ucheck = sub.add_parser(
        "unsubscribe-check",
        help="確認 UNSUBSCRIBE_SECRET 和 Edge Function 一致（用不存在的帳號測，不會退訂任何人）",
    )
    ucheck.set_defaults(func=_cmd_unsubscribe_check)

    mig = sub.add_parser("migrate-me", help="（本機）把單人版的設定搬到自己的 Supabase 帳號")
    mig.add_argument("--email", required=True, help="你用來 Google 登入的 email")
    mig.add_argument("--config", type=Path, default=Path("config.yaml"))
    mig.add_argument(
        "--data-dir", type=Path, default=None, help="state 分支的資料夾（有 state.json）"
    )
    mig.add_argument("--dry-run", action="store_true", help="只顯示要搬什麼，不寫入")
    mig.add_argument(
        "--replace",
        action="store_true",
        help="帳號已有提醒時，連同通知紀錄一起覆蓋（同一個交易）",
    )
    mig.set_defaults(func=_cmd_migrate_me)

    exp = sub.add_parser(
        "export-state", help="（本機，退回方案用）把資料庫裡自己的狀態寫回 state.json、alerts.csv"
    )
    exp.add_argument("--email", required=True, help="你用來 Google 登入的 email")
    exp.add_argument("--config", type=Path, default=Path("config.yaml"))
    exp.add_argument(
        "--data-dir", type=Path, default=None, help="state 分支的資料夾（有 state.json）"
    )
    exp.add_argument("--dry-run", action="store_true", help="只顯示要改什麼，不寫入檔案")
    exp.set_defaults(func=_cmd_export_state)

    site_cfg = sub.add_parser(
        "site-config", help="產生前端的 config.js（讀 AUTH_ENABLED 等環境變數）"
    )
    site_cfg.add_argument("--out", type=Path, default=Path("site/config.js"))
    site_cfg.set_defaults(func=_cmd_site_config)

    validate = sub.add_parser("validate-config", help="只驗證 config.yaml 格式")
    validate.add_argument("--config", type=Path, default=Path("config.yaml"))
    validate.set_defaults(func=_cmd_validate_config)

    build = sub.add_parser("build-site", help="產生前端用的 data.json")
    build.add_argument("--config", type=Path, default=Path("config.yaml"))
    build.add_argument("--data-dir", type=Path, default=None, help="預設用 config 的 data_dir")
    build.add_argument("--out", type=Path, default=Path("site/data.json"))
    build.add_argument(
        "--public",
        action="store_true",
        help="多人版：只輸出公開行情（不含目標價、狀態、通知紀錄）",
    )
    build.set_defaults(func=_cmd_build_site)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
