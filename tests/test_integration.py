# tests/test_integration.py
import pytest


def test_config_validation():
    from config import Config
    cfg = Config()
    assert cfg.LEVERAGE == 5


def test_full_model_flow():
    from models import BotState, CycleState, OperatingMode, EarnState
    from datetime import datetime, timezone, timedelta

    state = BotState(cycle_state=CycleState.HOLD, mode=OperatingMode.VOLUME, pair="BTC")
    earn = EarnState(
        cycle_start=datetime(2026, 4, 21, tzinfo=timezone.utc),
        cycle_end=datetime(2026, 5, 19, tzinfo=timezone.utc),
        target_volume=300000,
    )
    assert not earn.is_volume_target_met()
    earn.grvt_volume = 300001
    assert earn.is_volume_target_met()


def test_strategy_full_flow():
    from strategy import normalize_funding_to_8h, decide_direction, determine_mode, calc_notional
    nado_8h = normalize_funding_to_8h(0.01, 1)
    grvt_8h = normalize_funding_to_8h(0.005, 8)
    direction = decide_direction(nado_8h, grvt_8h)
    assert direction is not None
    mode = determine_mode(False, True, 20, 100000, 50000)
    assert mode == "VOLUME"
    notional = calc_notional(5000, 5000, 5, 0.95)
    assert notional == 23750


def test_monitor_flow():
    from monitor import CircuitBreaker, check_margin_level, MarginLevel
    cb = CircuitBreaker(max_fails=3)
    for _ in range(3):
        cb.record_failure("test")
    assert cb.is_tripped("test")
    cb.record_success("test")
    assert not cb.is_tripped("test")
    assert check_margin_level(20.0, 15.0, 10.0) == MarginLevel.NORMAL


def test_pair_manager_flow():
    from pair_manager import PairManager
    pm = PairManager("BTC")
    pm.set_available_pairs(["BTC", "ETH"], ["BTC", "ETH", "SOL"])
    assert pm.common_pairs == {"BTC", "ETH"}
    pm.parse_boost_string("BTC:4x")
    assert pm.get_boost("BTC") == {"nado": 4.0, "grvt": 4.0}
