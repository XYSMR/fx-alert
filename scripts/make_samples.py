"""產生 site/samples/*.json：前端在本機預覽用的範例資料。

    python scripts/make_samples.py

全部透過真正的 build_data() 產生，所以格式一定跟正式的 data.json 一樣。
走勢是「假資料」（平滑曲線），只用來看畫面，不代表真實匯率。

| 檔案            | 情境                                                         |
|-----------------|--------------------------------------------------------------|
| waiting.json    | 兩個幣別都在等待中                                           |
| triggered.json  | 日圓已低於目標、已寄通知                                     |
| rebound.json    | 日圓已通知過，但回升到「目標 < 現價 ≤ 回升門檻」之間          |
| empty.json      | 第一次部署，還沒有任何資料                                   |
| error.json      | 最後一次檢查失敗                                             |
"""

from __future__ import annotations

import math
import tempfile
from datetime import datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

from fx_alert.build_site import build_data, write_data_json
from fx_alert.config import TAIPEI, load_config
from fx_alert.history import append_alert, append_quotes, write_last_check
from fx_alert.rules import AlertState
from fx_alert.state_store import CurrencyState, save_states

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "site" / "samples"
NOW = datetime(2026, 11, 9, 16, 12, tzinfo=TAIPEI)  # 週一下午
LAST = datetime(2026, 11, 9, 16, 10, 12)  # 最後一份牌告（兆豐給的是不含時區的台灣時間）
CHECK = datetime(2026, 11, 9, 16, 12, 10, tzinfo=TAIPEI)


def fake_history(d: Path) -> None:
    """30 天、平日每天兩份牌告的假走勢。"""
    day, i = NOW - timedelta(days=30), 0
    while day.date() < NOW.date():
        if day.weekday() < 5:
            for hh, mm in ((9, 5), (13, 35)):
                drift = i / 60
                jpy = round(0.2160 - 0.0105 * drift + 0.0012 * math.sin(i / 3), 4)
                krw = round(0.0268 - 0.0006 * drift + 0.0002 * math.sin(i / 4), 4)
                t = day.replace(hour=hh, minute=mm, second=0, tzinfo=None)
                append_quotes(d, t, {"JPY": D(str(jpy)), "KRW": D(str(krw))})
                i += 1
        day += timedelta(days=1)


def scenario(name, jpy_now, jpy_state, alerts=(), ok=True, earlier_today=None):
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        fake_history(d)
        if earlier_today:  # 今天稍早的一份牌告（例如觸發通知的那一份）
            append_quotes(
                d, LAST.replace(hour=11, minute=5), {"JPY": D(earlier_today), "KRW": D("0.0264")}
            )
        append_quotes(d, LAST, {"JPY": D(jpy_now), "KRW": D("0.0263")})
        states = {"KRW": CurrencyState(AlertState.ARMED, "2026-11-02T09:07:00+08:00", "0.0266")}
        if jpy_state:
            states["JPY"] = jpy_state
        save_states(d / "state.json", states)
        for at, rate in alerts:
            append_alert(d, at, "JPY", D(rate), D("0.205"))
        write_last_check(d, CHECK, ok=ok, errors=0 if ok else 1)
        write(name, d)


def write(name: str, data_dir: Path) -> None:
    cfg = load_config(ROOT / "config.yaml")
    write_data_json(build_data(cfg, data_dir, now=NOW, repo="XYSMR/fx-alert"), OUT / f"{name}.json")
    print(f"已產生 {OUT / name}.json")


def main() -> None:
    armed = CurrencyState(AlertState.ARMED, "2026-11-09T15:46:16+08:00", "0.2119", "0.205")
    triggered = CurrencyState(AlertState.TRIGGERED, "2026-11-09T16:12:10+08:00", "0.2049", "0.205")
    sent = [
        (datetime(2026, 10, 21, 10, 37, tzinfo=TAIPEI), "0.2048"),
        (datetime(2026, 11, 9, 11, 7, tzinfo=TAIPEI), "0.2049"),
    ]

    scenario("waiting", "0.2119", armed)
    scenario("triggered", "0.2049", triggered, alerts=sent)
    # 11:07 到價寄信後，匯率回升到 0.2055（> 目標 0.205，但 ≤ 回升門檻 0.206）
    rebound = CurrencyState(AlertState.TRIGGERED, "2026-11-09T11:07:00+08:00", "0.2049", "0.205")
    scenario("rebound", "0.2055", rebound, alerts=sent, earlier_today="0.2049")
    scenario("error", "0.2119", armed, ok=False)
    with tempfile.TemporaryDirectory() as tmp:
        write("empty", Path(tmp))


if __name__ == "__main__":
    main()
