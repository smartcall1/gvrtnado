import pytest
from monitor import MarginLevel, check_margin_level, CircuitBreaker, check_price_divergence


def test_margin_normal():
    assert check_margin_level(20.0, 15.0, 10.0) == MarginLevel.NORMAL


def test_margin_warning():
    assert check_margin_level(13.0, 15.0, 10.0) == MarginLevel.WARNING


def test_margin_emergency():
    assert check_margin_level(8.0, 15.0, 10.0) == MarginLevel.EMERGENCY


def test_circuit_breaker_no_trip():
    cb = CircuitBreaker(max_fails=5)
    for _ in range(4):
        cb.record_failure("nado")
    assert cb.is_tripped("nado") is False


def test_circuit_breaker_trips():
    cb = CircuitBreaker(max_fails=5)
    for _ in range(5):
        cb.record_failure("nado")
    assert cb.is_tripped("nado") is True


def test_circuit_breaker_reset():
    cb = CircuitBreaker(max_fails=5)
    for _ in range(5):
        cb.record_failure("nado")
    cb.record_success("nado")
    assert cb.is_tripped("nado") is False


def test_price_divergence_normal():
    level = check_price_divergence(95000, 95050, warn_pct=3, emergency_pct=5)
    assert level == "NORMAL"


def test_price_divergence_warn():
    level = check_price_divergence(95000, 97900, warn_pct=3, emergency_pct=5)
    assert level == "WARNING"


def test_price_divergence_emergency():
    level = check_price_divergence(95000, 100000, warn_pct=3, emergency_pct=5)
    assert level == "EMERGENCY"
