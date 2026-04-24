import json
import time
import tempfile
from enum import Enum
from pathlib import Path
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta


class CycleState(Enum):
    IDLE = "IDLE"
    ANALYZE = "ANALYZE"
    ENTER = "ENTER"
    HOLD = "HOLD"
    EXIT = "EXIT"
    COOLDOWN = "COOLDOWN"


class OperatingMode(Enum):
    HOLD = "HOLD"
    VOLUME = "VOLUME"
    VOLUME_URGENT = "VOLUME_URGENT"


@dataclass
class Position:
    exchange: str
    symbol: str
    side: str
    notional: float
    entry_price: float
    leverage: int
    margin: float
    opened_at: float = field(default_factory=time.time)

    def calc_unrealized_pnl(self, current_price: float) -> float:
        if self.side == "LONG":
            return self.notional * (current_price - self.entry_price) / self.entry_price
        return self.notional * (self.entry_price - current_price) / self.entry_price

    def calc_margin_ratio(self, current_price: float) -> float:
        pnl = self.calc_unrealized_pnl(current_price)
        return ((self.margin + pnl) / self.notional) * 100

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Position":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class Cycle:
    cycle_id: str
    pair: str
    direction: str
    notional: float
    entered_at: float
    exited_at: float
    entry_nado_price: float
    entry_grvt_price: float
    exit_nado_price: float
    exit_grvt_price: float
    funding_pnl: float
    spread_pnl: float
    fee_cost: float
    exit_reason: str
    volume_generated: float

    @property
    def net_pnl(self) -> float:
        return self.funding_pnl + self.spread_pnl - self.fee_cost

    def to_jsonl(self) -> str:
        d = asdict(self)
        d["net_pnl"] = self.net_pnl
        return json.dumps(d)


@dataclass
class EarnState:
    cycle_start: datetime
    cycle_end: datetime
    target_volume: float
    grvt_volume: float = 0.0
    grvt_trades: int = 0

    def is_cycle_expired(self, now: datetime) -> bool:
        return now >= self.cycle_end

    def days_remaining(self, now: datetime) -> int:
        return max(0, (self.cycle_end - now).days)

    def volume_progress(self) -> float:
        if self.target_volume <= 0:
            return 1.0
        return self.grvt_volume / self.target_volume

    def is_volume_target_met(self) -> bool:
        return self.grvt_volume >= self.target_volume

    def is_trades_target_met(self) -> bool:
        return self.grvt_trades >= 5

    def reset(self):
        old_end = self.cycle_end
        self.cycle_start = old_end
        self.cycle_end = old_end + timedelta(days=28)
        self.grvt_volume = 0.0
        self.grvt_trades = 0

    def to_dict(self) -> dict:
        return {
            "cycle_start": self.cycle_start.isoformat(),
            "cycle_end": self.cycle_end.isoformat(),
            "target_volume": self.target_volume,
            "grvt_volume": self.grvt_volume,
            "grvt_trades": self.grvt_trades,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "EarnState":
        return cls(
            cycle_start=datetime.fromisoformat(d["cycle_start"]),
            cycle_end=datetime.fromisoformat(d["cycle_end"]),
            target_volume=d.get("target_volume", 300000),
            grvt_volume=d.get("grvt_volume", 0),
            grvt_trades=d.get("grvt_trades", 0),
        )


@dataclass
class BotState:
    cycle_state: CycleState = CycleState.IDLE
    mode: OperatingMode = OperatingMode.VOLUME
    pair: str = "BTC"
    direction: str = ""
    cycle_id: str = ""
    entered_at: float = 0.0
    cooldown_until: float = 0.0
    cumulative_funding: float = 0.0
    cumulative_fees: float = 0.0
    nado_balance: float = 0.0
    grvt_balance: float = 0.0
    positions: dict = field(default_factory=dict)
    earn: dict = field(default_factory=dict)
    boost_config: dict = field(default_factory=dict)

    def save(self, path: Path):
        d = {
            "cycle_state": self.cycle_state.value,
            "mode": self.mode.value,
            "pair": self.pair,
            "direction": self.direction,
            "cycle_id": self.cycle_id,
            "entered_at": self.entered_at,
            "cooldown_until": self.cooldown_until,
            "cumulative_funding": self.cumulative_funding,
            "cumulative_fees": self.cumulative_fees,
            "nado_balance": self.nado_balance,
            "grvt_balance": self.grvt_balance,
            "positions": self.positions,
            "earn": self.earn,
            "boost_config": self.boost_config,
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, indent=2))
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path) -> "BotState":
        if not path.exists():
            return cls()
        d = json.loads(path.read_text())
        return cls(
            cycle_state=CycleState(d.get("cycle_state", "IDLE")),
            mode=OperatingMode(d.get("mode", "VOLUME")),
            pair=d.get("pair", "BTC"),
            direction=d.get("direction", ""),
            cycle_id=d.get("cycle_id", ""),
            entered_at=d.get("entered_at", 0),
            cooldown_until=d.get("cooldown_until", 0),
            cumulative_funding=d.get("cumulative_funding", 0),
            cumulative_fees=d.get("cumulative_fees", 0),
            nado_balance=d.get("nado_balance", 0),
            grvt_balance=d.get("grvt_balance", 0),
            positions=d.get("positions", {}),
            earn=d.get("earn", {}),
            boost_config=d.get("boost_config", {}),
        )


@dataclass
class FundingSnapshot:
    nado_rate: float
    grvt_rate: float
    timestamp: float = field(default_factory=time.time)
