"""產生前端用的 data.json。

    python -m fx_alert build-site [--config config.yaml] [--data-dir data] [--out site/data.json]
                                  [--public]

--public（多人版，部署時 AUTH_ENABLED=true 就會加）：只輸出公開行情。
拿掉每個幣別的目標價、buffer、距離目標、狀態，也不輸出通知紀錄和「編輯 config.yaml」連結。
多人版的目標價和通知紀錄只存在 Supabase，登入後才看得到自己的。

只讀兩個地方：config.yaml（目標價等設定）和資料資料夾（state 分支的內容）。
完全不讀環境變數，所以 Secrets 不可能出現在輸出裡；測試另外再檢查一次。

計算都在這裡做完（距離目標幾 %、狀態、30 天範圍），前端只負責顯示。

輸出拆成兩種檔案（全部 18 種幣別的走勢放在一個檔案會超過 1 MB）：
  * data.json：每個幣別的現價、30 天最高最低、期間變動等（約幾 KB），頁面一打開就載入
  * history/<幣別>.json：該幣別的走勢，壓縮格式（見 compact_history），前端選了那個幣別才載入
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from fx_alert.bizdays import calendar_json, load_calendar
from fx_alert.config import TAIPEI, AppConfig
from fx_alert.currencies import CATALOG, SPOT_ONLY, TICK, format_price
from fx_alert.daily import SEED_PATH, daily_closes, recent_stats
from fx_alert.history import first_quote_at, read_alerts, read_last_check, read_quotes
from fx_alert.notifier import BANK_LABEL, RATE_PAGE
from fx_alert.rules import AlertState
from fx_alert.state_store import load_states

SCHEMA_VERSION = 1
HISTORY_DAYS = 30
MAX_ALERTS = 20
DEFAULT_REPO = "XYSMR/fx-alert"

STATUS = {AlertState.ARMED: "waiting", AlertState.TRIGGERED: "triggered"}


def _iso(dt: datetime) -> str:
    return dt.astimezone(TAIPEI).isoformat(timespec="seconds")


# 主要排程是 Supabase pg_cron 在每個整點後第 5、35 分觸發（migration 的 cron "5,35 ..."；
# GitHub schedule 的 :07、:37 只是備援），前端用來顯示「下次更新：週一 09:05」。
# tests/test_build_site.py 會檢查和 migration 一致。
SCHEDULE_OFFSET_MINUTES = 5
# 檢查間隔（:05、:35）。前端用來說明「兩次檢查之間短暫到價又回升，不會通知」
SCHEDULE_INTERVAL_MINUTES = 30


def distance_pct(rate: Decimal, target: Decimal) -> float:
    """還要跌多少 %：(現價 − 目標) ÷ 現價 × 100。

    除以「現價」，意思是「從現在的價格還要再跌多少」。保留小數 3 位，
    讓前端能判斷「不到 0.1%」；顯示時再四捨五入到 1 位。負數 = 已低於目標。
    """
    pct = (rate - target) / rate * 100
    return float(pct.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP))


def _extreme(series: list, pick) -> dict[str, str] | None:
    """30 天內的最高或最低價；同樣的價格出現多次時取最早的那一次。"""
    if not series:
        return None
    best = pick(series, key=lambda q: q.cash_sell)
    first = next(q for q in series if q.cash_sell == best.cash_sell)
    return {"rate": format_price(first.cash_sell), "at": _iso(first.quoted_at)}


def change_pct(series: list) -> float | None:
    """這段期間的變動 %：第一筆 → 最後一筆，小數 2 位（給「全部幣別一覽」用，不用載入走勢）。"""
    if len(series) < 2:
        return None
    first, last = series[0].cash_sell, series[-1].cash_sell
    pct = (last - first) / first * 100
    return float(pct.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)) + 0.0  # 不要 -0.0


def _add_minutes(t, minutes: int):
    return (datetime.combine(datetime(2000, 1, 1), t) + timedelta(minutes=minutes)).time()


def _repo() -> str:
    # GitHub Actions 會提供 GITHUB_REPOSITORY（例如 XYSMR/fx-alert）；只取這一個公開的值
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    return repo if repo.count("/") == 1 else DEFAULT_REPO


# 多人版公開頁面不能出現的欄位（每個幣別）；前端 site/app.js 的 PERSONAL_FIELDS 也是這些
PERSONAL_FIELDS = (
    "target",
    "buffer",
    "rearm_above",
    "distance_pct",
    "status",
    "status_since",
)


def public_problems(data: dict[str, Any]) -> list[str]:
    """公開版 data.json 的檢查：回傳不該出現的東西（空的 = 沒問題）。"""
    problems = [
        f"{c.get('code')}.{k}"
        for c in data.get("currencies", [])
        for k in PERSONAL_FIELDS
        if k in c
    ]
    if data.get("alerts"):
        problems.append("alerts")
    if "edit_config_url" in data:
        problems.append("edit_config_url")
    return problems


def build_data(
    cfg: AppConfig,
    data_dir: Path,
    *,
    now: datetime,
    repo: str | None = None,
    public: bool = False,
    seed_path: Path = SEED_PATH,
) -> dict[str, Any]:
    repo = repo or _repo()
    # 每日收盤價（兆豐官方回補 + 我們自己的資料）：「現在的價格在近 30 天是第幾低」
    closes = daily_closes(data_dir, seed_path=seed_path, until=now)
    today = now.astimezone(TAIPEI).date()
    states = load_states(data_dir / "state.json")
    quotes = read_quotes(data_dir, since=now - timedelta(days=HISTORY_DAYS))

    # 最早的一筆牌告：資料不滿 30 天時，前端顯示「資料從 11/09 開始累積」
    since = first_quote_at(data_dir)

    by_code: dict[str, list] = {}
    for q in quotes:
        by_code.setdefault(q.currency, []).append(q)

    # 依兆豐牌告的順序列出：config.yaml 設定的幣別，加上有走勢資料的幣別（多人版是全部 18 種）
    rules = {r.currency: r for r in cfg.rules}
    currencies = []
    for cur in CATALOG:
        series = by_code.get(cur.code, [])
        rule = rules.get(cur.code)
        if rule is None and not series:
            continue
        latest = series[-1] if series else None
        c: dict[str, Any] = {
            "code": cur.code,
            "name": cur.name_zh,
            "sort_order": cur.sort_order,
            "popular_rank": cur.popular_rank,
            "rate": format_price(latest.cash_sell) if latest else None,
            "rate_at": _iso(latest.quoted_at) if latest else None,
            "change_pct": change_pct(series),
            "high": _extreme(series, max),
            "low": _extreme(series, min),
            "history": [[_iso(q.quoted_at), format_price(q.cash_sell)] for q in series],
            # 近 30 天（日曆天）的營業日收盤價裡，有幾天比現價低；資料不足 15 天時 enough=false
            "recent": (
                recent_stats(closes[cur.code], latest.cash_sell, today).as_dict()
                if latest and closes.get(cur.code)
                else None
            ),
        }
        if rule is not None:
            entry = states.get(cur.code)
            c.update(
                {
                    "target": format_price(rule.target),
                    "buffer": format_price(rule.buffer),
                    "rearm_above": format_price(rule.rearm_above),
                    "distance_pct": (
                        distance_pct(latest.cash_sell, rule.target) if latest else None
                    ),
                    "status": STATUS[entry.state] if entry else "waiting",
                    "status_since": entry.changed_at if entry else None,
                }
            )
        currencies.append(c)

    alerts = [
        {
            "sent_at": _iso(a.sent_at),
            "currency": a.currency,
            "rate": str(a.rate),
            "target": str(a.target),
        }
        for a in read_alerts(data_dir)[:MAX_ALERTS]
    ]

    data = {
        "schema": SCHEMA_VERSION,
        "generated_at": _iso(now),
        "last_check": read_last_check(data_dir),
        "quoted_at": _iso(quotes[-1].quoted_at) if quotes else None,
        "history_days": HISTORY_DAYS,
        "history_since": _iso(since) if since else None,
        "source": {"bank": "兆豐銀行", "rate_type": BANK_LABEL.split()[-1], "url": RATE_PAGE},
        "repo_url": f"https://github.com/{repo}",
        "edit_config_url": f"https://github.com/{repo}/edit/main/config.yaml",
        "actions_url": f"https://github.com/{repo}/actions/workflows/check-rates.yml",
        # 前端用來判斷「資料是不是太久沒更新」：只有在檢查時段內才算過期
        "business_hours": {
            "weekdays_only": cfg.hours.weekdays_only,
            "start": f"{cfg.hours.start:%H:%M}",
            "end": f"{cfg.hours.end:%H:%M}",
            "first_check": f"{_add_minutes(cfg.hours.start, SCHEDULE_OFFSET_MINUTES):%H:%M}",
            "interval_minutes": SCHEDULE_INTERVAL_MINUTES,
        },
        "currencies": currencies,
        # 兆豐只有即期匯率、不支援的幣別（網頁下拉選單下方註明）
        "unsupported": [{"code": k, "name": v} for k, v in SPOT_ONLY.items()],
        # 銀行營業日的例外日期：前端算出國換匯計畫的提醒日期（和 trip_rules.schedule 一樣）
        "bank_calendar": calendar_json(load_calendar()),
        "alerts": alerts,
    }
    if public:
        for c in currencies:
            for k in PERSONAL_FIELDS:
                c.pop(k, None)
        data["alerts"] = []
        del data["edit_config_url"]
    return data


HISTORY_KEYS = {"code", "start", "dt", "v"}
SCALE = int(1 / TICK)  # 價格 × 10⁴ 存成整數（兆豐牌告都是小數 4 位）


def compact_history(code: str, history: list[list[str]]) -> dict[str, Any]:
    """走勢的壓縮格式：

        {"code": "JPY", "start": "2026-11-09T09:05:00+08:00",
         "dt": [0, 1800, 480, ...],     # 和前一個點相差幾秒（第一個是 0）
         "v":  [2114, 2115, 2107, ...]} # 價格 × 10⁴ 的整數

    每個點約 9 bytes（原本約 41 bytes）；前端 decodeHistory() 還原成 [時間, "0.2114"]。
    """
    if not history:
        return {"code": code, "start": None, "dt": [], "v": []}
    times = [datetime.fromisoformat(t) for t, _ in history]
    dt = [0] + [int((b - a).total_seconds()) for a, b in zip(times, times[1:], strict=False)]
    values = []
    for _, rate in history:
        scaled = Decimal(rate) * SCALE
        if scaled != scaled.to_integral_value():
            raise ValueError(f"{code} 的價格 {rate} 不是牌告的 4 位小數")
        values.append(int(scaled))
    return {"code": code, "start": history[0][0], "dt": dt, "v": values}


def history_problems(obj: dict[str, Any]) -> list[str]:
    """公開的 history/<幣別>.json 只能有走勢（code、start、dt、v），不能有其他欄位。"""
    extra = sorted(set(obj) - HISTORY_KEYS)
    return [f"{obj.get('code')}.{k}" for k in extra]


def _write_json(obj: Any, path: Path, *, indent: int | None = 2) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    seps = None if indent else (",", ":")
    text = json.dumps(obj, ensure_ascii=False, indent=indent, separators=seps) + "\n"
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def write_data_json(data: dict[str, Any], out: Path, history_dir: Path | None = None) -> None:
    """寫出 data.json（不含走勢）和 history/<幣別>.json。

    history_dir 預設是 data.json 旁邊的 history/；history_url 是相對於 data.json 的路徑。
    舊的走勢檔（已經不在清單上的幣別）會先清掉。
    """
    history_dir = history_dir or out.parent / "history"
    history_dir.mkdir(parents=True, exist_ok=True)
    for old in history_dir.glob("*.json"):
        old.unlink()
    slim = {**data, "currencies": []}
    for c in data["currencies"]:
        c = dict(c)
        history = c.pop("history", [])
        path = history_dir / f"{c['code']}.json"
        _write_json(compact_history(c["code"], history), path, indent=None)
        c["history_url"] = path.relative_to(out.parent).as_posix()
        slim["currencies"].append(c)
    _write_json(slim, out)
