from decimal import Decimal as D

import pytest

from fx_alert.rules import AlertState, Decision, Rule, decide

ARMED = AlertState.ARMED
TRIG = AlertState.TRIGGERED

JPY = Rule("JPY", target=D("0.205"), buffer=D("0.001"))  # 重新上膛門檻 0.206
KRW = Rule("KRW", target=D("0.0255"), buffer=D("0.0002"))  # 重新上膛門檻 0.0257


# ---------- 單次判斷：表格驅動 ----------


@pytest.mark.parametrize(
    ("prev", "rate", "next_state", "notify", "why"),
    [
        # 第一次執行（沒有狀態）
        (None, "0.2040", TRIG, True, "第一次執行就低於目標 → 要通知"),
        (None, "0.2050", TRIG, True, "第一次執行剛好等於目標 → 要通知"),
        (None, "0.2051", ARMED, False, "第一次執行高於目標 → 不通知"),
        # 待命中
        (ARMED, "0.2050", TRIG, True, "等於目標（≤）→ 通知"),
        (ARMED, "0.1990", TRIG, True, "遠低於目標 → 通知"),
        (ARMED, "0.2051", ARMED, False, "略高於目標 → 不通知"),
        (ARMED, "0.2060", ARMED, False, "在 buffer 區間內 → 維持待命"),
        (ARMED, "0.2200", ARMED, False, "遠高於目標 → 維持待命"),
        # 已通知
        (TRIG, "0.2040", TRIG, False, "持續低於目標 → 不重複通知"),
        (TRIG, "0.2050", TRIG, False, "等於目標 → 不重複通知"),
        (TRIG, "0.2055", TRIG, False, "回升但沒超過 buffer → 不上膛"),
        (TRIG, "0.2060", TRIG, False, "剛好等於 target+buffer → 還不上膛（要 >）"),
        (TRIG, "0.2061", ARMED, False, "超過 target+buffer → 重新上膛，但不寄信"),
    ],
)
def test_decide_table(prev, rate, next_state, notify, why):
    assert decide(JPY, D(rate), prev) == Decision(next_state, notify), why


def test_krw_four_decimal_rates():
    assert decide(KRW, D("0.0255"), ARMED).notify is True
    assert decide(KRW, D("0.0257"), TRIG).next_state is TRIG
    assert decide(KRW, D("0.0258"), TRIG).next_state is ARMED


def test_zero_buffer_rearms_as_soon_as_above_target():
    rule = Rule("JPY", target=D("0.205"), buffer=D("0"))
    assert decide(rule, D("0.2051"), TRIG).next_state is ARMED


def test_decimal_precision_no_float_error():
    # 0.1 + 0.2 用 float 會是 0.30000000000000004；Decimal 必須剛好相等
    rule = Rule("XXX", target=D("0.3"), buffer=D("0.1"))
    assert decide(rule, D("0.1") + D("0.2"), ARMED).notify is True


# ---------- 連續執行情境 ----------


def run(rule, rates, send_ok=None):
    """模擬每次排程執行：只有在「不需寄信」或「寄信成功」時才保存新狀態。

    回傳每次執行是否「成功寄出」通知。
    """
    send_ok = list(send_ok or [])
    state = None
    sent = []
    for rate in rates:
        d = decide(rule, D(rate), state)
        if d.notify:
            ok = send_ok.pop(0) if send_ok else True
            sent.append(ok)
            if ok:
                state = d.next_state
        else:
            sent.append(False)
            state = d.next_state
    return sent


def test_notify_only_once_while_below_target():
    assert run(JPY, ["0.2070", "0.2049", "0.2040", "0.2030", "0.2045"]) == [
        False,
        True,
        False,
        False,
        False,
    ]


def test_notify_again_after_rebound_and_drop():
    rates = ["0.2049", "0.2062", "0.2048"]  # 到價 → 回升超過 0.206 → 再跌回
    assert run(JPY, rates) == [True, False, True]


def test_bouncing_inside_buffer_does_not_spam():
    rates = ["0.2050", "0.2058", "0.2050", "0.2059", "0.2049"]
    assert run(JPY, rates) == [True, False, False, False, False]


def test_failed_send_keeps_state_and_retries_next_run():
    # 第一次寄信失敗 → 狀態不更新 → 下一次仍判定要寄 → 這次成功 → 之後不再寄
    rates = ["0.2040", "0.2040", "0.2040"]
    assert run(JPY, rates, send_ok=[False, True]) == [False, True, False]


# ---------- 參數檢查 ----------


@pytest.mark.parametrize(
    ("target", "buffer", "exc"),
    [
        (D("0"), D("0.001"), ValueError),
        (D("-0.2"), D("0.001"), ValueError),
        (D("0.205"), D("-0.001"), ValueError),
        (0.205, D("0.001"), TypeError),
        (D("0.205"), 0.001, TypeError),
    ],
)
def test_rule_validation(target, buffer, exc):
    with pytest.raises(exc):
        Rule("JPY", target=target, buffer=buffer)


@pytest.mark.parametrize(("rate", "exc"), [(0.204, TypeError), (D("0"), ValueError)])
def test_decide_rejects_bad_rate(rate, exc):
    with pytest.raises(exc):
        decide(JPY, rate, ARMED)


def test_state_values_are_json_friendly():
    assert AlertState("armed") is ARMED
    assert TRIG.value == "triggered"


# ---------- 修改目標價 ----------


def test_target_changed_while_triggered_and_rate_above_new_target_rearms():
    # 在 0.205 觸發（現價 0.2049）→ 使用者把目標改成 0.203 → 現價高於新目標 → 回到待命，不寄信
    new_rule = Rule("JPY", target=D("0.203"), buffer=D("0.001"))
    d = decide(new_rule, D("0.2049"), TRIG, prev_target=D("0.205"))
    assert d == Decision(ARMED, False)
    assert "目標已調整" in d.reason


def test_target_changed_rearm_ignores_buffer():
    # 現價 0.2035 只比新目標 0.203 高一點（在 buffer 內），仍然要回到待命
    new_rule = Rule("JPY", target=D("0.203"), buffer=D("0.001"))
    assert decide(new_rule, D("0.2035"), TRIG, prev_target=D("0.205")).next_state is ARMED


def test_target_changed_but_rate_still_at_or_below_new_target_stays_triggered():
    # 改了目標，但現價已經 ≤ 新目標：這次到價已通知過，不重寄
    new_rule = Rule("JPY", target=D("0.2045"), buffer=D("0.001"))
    assert decide(new_rule, D("0.2040"), TRIG, prev_target=D("0.205")) == Decision(TRIG, False)


def test_after_target_change_rearm_next_drop_notifies():
    new_rule = Rule("JPY", target=D("0.203"), buffer=D("0.001"))
    state = decide(new_rule, D("0.2049"), TRIG, prev_target=D("0.205")).next_state
    assert decide(new_rule, D("0.2030"), state).notify is True


def test_same_target_keeps_buffer_behaviour():
    assert decide(JPY, D("0.2055"), TRIG, prev_target=D("0.205")) == Decision(TRIG, False)


def test_unknown_previous_target_keeps_old_behaviour():
    # 舊的 state.json 沒有 target 欄位 → 照原本的 buffer 規則
    new_rule = Rule("JPY", target=D("0.203"), buffer=D("0.001"))
    assert decide(new_rule, D("0.2035"), TRIG, prev_target=None).next_state is TRIG


def test_target_change_while_armed_has_no_special_effect():
    new_rule = Rule("JPY", target=D("0.210"), buffer=D("0.001"))
    assert decide(new_rule, D("0.2099"), ARMED, prev_target=D("0.205")) == Decision(TRIG, True)
