import json
import time
import tempfile
from pathlib import Path
from datetime import datetime, timezone, timedelta
import pytest
from models import CycleState, OperatingMode, Position, Cycle, EarnState, BotState


def test_cycle_state_values():
    assert CycleState.IDLE.value == "IDLE"
    assert CycleState.HOLD.value == "HOLD"


def test_operating_mode_values():
    assert OperatingMode.HOLD.value == "HOLD"
    assert OperatingMode.VOLUME.value == "VOLUME"
    assert OperatingMode.VOLUME_URGENT.value == "VOLUME_URGENT"


def test_position_pnl_long():
    pos = Position(
        exchange="nado", symbol="BTC", side="LONG",
        notional=25000, entry_price=95000, leverage=5, margin=5000,
    )
    pnl = pos.calc_unrealized_pnl(96000)
    expected = 25000 * (96000 - 95000) / 95000
    assert abs(pnl - expected) < 0.01


def test_position_pnl_short():
    pos = Position(
        exchange="grvt", symbol="BTC", side="SHORT",
        notional=25000, entry_price=95000, leverage=5, margin=5000,
    )
    pnl = pos.calc_unrealized_pnl(94000)
    expected = 25000 * (95000 - 94000) / 95000
    assert abs(pnl - expected) < 0.01


def test_position_margin_ratio():
    pos = Position(
        exchange="nado", symbol="BTC", side="LONG",
        notional=25000, entry_price=95000, leverage=5, margin=5000,
    )
    ratio = pos.calc_margin_ratio(95000)
    assert abs(ratio - 20.0) < 0.01


def test_cycle_to_jsonl():
    c = Cycle(
        cycle_id="c1", pair="BTC", direction="A", notional=25000,
        entered_at=1714000000, exited_at=1714100000,
        entry_nado_price=95000, entry_grvt_price=95010,
        exit_nado_price=95500, exit_grvt_price=95490,
        funding_pnl=12.5, spread_pnl=8.0, fee_cost=5.0,
        exit_reason="spread_profit", volume_generated=50000,
    )
    line = c.to_jsonl()
    data = json.loads(line)
    assert data["pair"] == "BTC"
    assert data["net_pnl"] == 12.5 + 8.0 - 5.0


def test_earn_state_cycle_boundary():
    now = datetime(2026, 5, 19, 0, 0, 0, tzinfo=timezone.utc)
    earn = EarnState(
        cycle_start=datetime(2026, 4, 21, 0, 0, 0, tzinfo=timezone.utc),
        cycle_end=datetime(2026, 5, 19, 0, 0, 0, tzinfo=timezone.utc),
        target_volume=300000, grvt_volume=180000, grvt_trades=14,
    )
    assert earn.is_cycle_expired(now) is True
    assert earn.days_remaining(now) == 0


def test_earn_state_volume_progress():
    earn = EarnState(
        cycle_start=datetime(2026, 4, 21, 0, 0, 0, tzinfo=timezone.utc),
        cycle_end=datetime(2026, 5, 19, 0, 0, 0, tzinfo=timezone.utc),
        target_volume=300000, grvt_volume=180000, grvt_trades=14,
    )
    assert earn.volume_progress() == 0.6
    assert earn.is_volume_target_met() is False
    assert earn.is_trades_target_met() is True


def test_bot_state_save_load(tmp_path):
    path = tmp_path / "state.json"
    state = BotState(
        cycle_state=CycleState.HOLD,
        mode=OperatingMode.VOLUME,
        pair="BTC", direction="A",
    )
    state.save(path)
    loaded = BotState.load(path)
    assert loaded.cycle_state == CycleState.HOLD
    assert loaded.mode == OperatingMode.VOLUME
    assert loaded.pair == "BTC"
