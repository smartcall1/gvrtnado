import pytest
from strategy import (
    normalize_funding_to_8h,
    decide_direction,
    should_exit_cycle,
    should_exit_spread,
    calc_notional,
    determine_mode,
    is_entry_favorable,
)


def test_normalize_1h_to_8h():
    assert normalize_funding_to_8h(0.01, 1) == 0.08


def test_normalize_8h_unchanged():
    assert normalize_funding_to_8h(0.01, 8) == 0.01


def test_decide_direction_a_better():
    d = decide_direction(nado_8h=0.01, grvt_8h=0.05)
    assert d == "A"


def test_decide_direction_b_better():
    d = decide_direction(nado_8h=0.05, grvt_8h=0.01)
    assert d == "B"


def test_decide_direction_both_negative():
    d = decide_direction(nado_8h=-0.01, grvt_8h=-0.01)
    assert d is None


def test_decide_direction_below_min_spread():
    d = decide_direction(nado_8h=-0.000043, grvt_8h=0.000100)
    assert d is None


def test_decide_direction_above_min_spread():
    d = decide_direction(nado_8h=-0.001, grvt_8h=0.001)
    assert d == "A"


def test_decide_direction_custom_min_spread():
    d = decide_direction(nado_8h=-0.0001, grvt_8h=0.0001, min_spread=0.0001)
    assert d == "A"
    d2 = decide_direction(nado_8h=-0.0001, grvt_8h=0.0001, min_spread=0.001)
    assert d2 is None


def test_should_exit_spread_profit():
    assert should_exit_spread(55.0, 50.0) is True
    assert should_exit_spread(40.0, 50.0) is False


def test_should_exit_spread_stoploss():
    assert should_exit_spread(-35.0, 50.0, stoploss=-30.0) is True


def test_should_exit_cycle_max_hold():
    reason = should_exit_cycle(
        hold_hours=100, min_hold_hours=24,
        max_hold_days=4, margin_ratio=20.0, margin_emergency=10.0,
    )
    assert reason == "max_hold"


def test_should_exit_cycle_margin_emergency():
    reason = should_exit_cycle(
        hold_hours=1, min_hold_hours=24,
        max_hold_days=4, margin_ratio=8.0, margin_emergency=10.0,
    )
    assert reason == "margin_emergency"


def test_should_exit_cycle_normal():
    reason = should_exit_cycle(
        hold_hours=12, min_hold_hours=24,
        max_hold_days=4, margin_ratio=20.0, margin_emergency=10.0,
    )
    assert reason is None


def test_calc_notional():
    n = calc_notional(5000, 5000, leverage=5, margin_buffer=0.95)
    assert n == 5000 * 5 * 0.95


def test_determine_mode_volume_met():
    mode = determine_mode(
        volume_met=True, trades_met=True,
        days_left=20, volume_remaining=0,
        daily_capacity=50000,
    )
    assert mode == "VOLUME"


def test_determine_mode_urgent():
    mode = determine_mode(
        volume_met=False, trades_met=True,
        days_left=2, volume_remaining=200000,
        daily_capacity=50000,
    )
    assert mode == "VOLUME_URGENT"


def test_determine_mode_volume():
    mode = determine_mode(
        volume_met=False, trades_met=True,
        days_left=20, volume_remaining=100000,
        daily_capacity=50000,
    )
    assert mode == "VOLUME"


def test_is_entry_favorable():
    assert is_entry_favorable("A", nado_price=94990, grvt_price=95010) is True
    assert is_entry_favorable("A", nado_price=95010, grvt_price=94990) is False
    assert is_entry_favorable("B", nado_price=95010, grvt_price=94990) is True
