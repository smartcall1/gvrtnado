import os
import pytest
from config import Config


def test_defaults():
    """테스트: 기본값이 올바르게 설정되는지 확인"""
    cfg = Config()
    assert cfg.LEVERAGE == 5
    assert cfg.POLL_INTERVAL == 3
    assert cfg.SPREAD_EXIT_HOLD == 50.0
    assert cfg.SPREAD_STOPLOSS == -30.0
    assert cfg.EARN_TARGET_VOLUME == 300_000.0


def test_validate_missing_keys(monkeypatch):
    """테스트: 필수 환경변수가 누락되었을 때 오류 반환"""
    monkeypatch.delenv("NADO_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("GRVT_API_KEY", raising=False)
    monkeypatch.delenv("GRVT_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("GRVT_TRADING_ACCOUNT_ID", raising=False)
    cfg = Config()
    errors = cfg.validate()
    assert any("NADO_PRIVATE_KEY" in e for e in errors)
    assert any("GRVT_API_KEY" in e for e in errors)


def test_validate_all_set(monkeypatch):
    """테스트: 모든 필수 환경변수 설정 시 검증 통과"""
    monkeypatch.setenv("NADO_PRIVATE_KEY", "0x1234")
    monkeypatch.setenv("GRVT_API_KEY", "key")
    monkeypatch.setenv("GRVT_PRIVATE_KEY", "0x5678")
    monkeypatch.setenv("GRVT_TRADING_ACCOUNT_ID", "acc1")
    cfg = Config()
    errors = cfg.validate()
    assert errors == []


def test_mode_params():
    """테스트: 모드별 파라미터가 올바르게 반환되는지 확인"""
    cfg = Config()

    # HOLD 모드
    hold = cfg.mode_params("HOLD")
    assert hold["min_hold_hours"] == 24
    assert hold["cooldown"] == 10800

    # VOLUME 모드
    vol = cfg.mode_params("VOLUME")
    assert vol["min_hold_hours"] == 2
    assert vol["cooldown"] == 30

    # VOLUME_URGENT 모드
    urg = cfg.mode_params("VOLUME_URGENT")
    assert urg["min_hold_hours"] == 0.5
    assert urg["cooldown"] == 10


def test_estimate_round_trip_fee():
    """테스트: 왕복 수수료 계산 — XEMM 우선 경로(NADO maker + GRVT maker rebate)
    OI cap 통과 시 적용. cap 미달 fallback은 NADO maker + GRVT taker로 더 비쌈.
    """
    cfg = Config()
    # NADO maker 1.0 bps + GRVT maker -0.01 bps, 양쪽 진입+청산이라 ×2
    fee = cfg.estimate_round_trip_fee(100.0)
    expected = 100.0 * 2 * (cfg.NADO_MAKER_FEE_BPS / 10_000) + 100.0 * 2 * (cfg.GRVT_MAKER_FEE_BPS / 10_000)
    assert abs(fee - expected) < 0.001


def test_pair_default():
    """테스트: 기본 거래 쌍"""
    cfg = Config()
    assert cfg.PAIR_DEFAULT == "BTC"


def test_custom_env_vars(monkeypatch):
    """테스트: 환경변수로 설정값 오버라이드"""
    monkeypatch.setenv("LEVERAGE", "10")
    monkeypatch.setenv("POLL_INTERVAL", "5")
    monkeypatch.setenv("SPREAD_EXIT_HOLD", "100.0")

    cfg = Config()
    assert cfg.LEVERAGE == 10
    assert cfg.POLL_INTERVAL == 5
    assert cfg.SPREAD_EXIT_HOLD == 100.0
