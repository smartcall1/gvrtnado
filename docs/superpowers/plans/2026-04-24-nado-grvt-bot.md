# NADO×GRVT 델타뉴트럴 양빵봇 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** NADO와 GRVT 거래소 간 BTC 델타뉴트럴 양빵봇 구현. 포인트/볼륨/펀딩비/스프레드 차익 동시 추구.

**Architecture:** 단일 상태머신(IDLE→ANALYZE→ENTER→HOLD→EXIT→COOLDOWN) + PairManager(동적 부스트 추적) + EarnManager(GRVT 4주 사이클 관리). 기존 `D:\Codes\delta_neutral_bot\` 패턴(청크 진입/퇴출, 크래시 복구, 원자적 상태 저장) 재활용.

**Tech Stack:** Python 3.10+, asyncio, aiohttp, nado-protocol SDK, grvt-pysdk (CCXT 호환), python-dotenv

**설계서:** `D:\Codes\nado_grvt_bot\docs\superpowers\specs\2026-04-24-nado-grvt-bot-design.md`

**참조 코드:** `D:\Codes\delta_neutral_bot\` (StandX×Hibachi 봇)

---

## 파일 구조

| 파일 | 책임 | 신규/수정 |
|------|------|----------|
| `config.py` | 환경변수 로딩, 검증, 상수 | 신규 |
| `models.py` | Position, Cycle, BotState, EarnState, CycleState enum | 신규 |
| `exchanges/base_client.py` | 거래소 공통 ABC 인터페이스 | 신규 |
| `exchanges/nado_client.py` | nado-protocol SDK 래퍼 | 신규 |
| `exchanges/grvt_client.py` | grvt-pysdk (CCXT) 래퍼 | 신규 |
| `strategy.py` | 방향 결정, EXIT 판단, 노셔널 계산, 모드 전환 | 신규 |
| `pair_manager.py` | 공통 페어 탐색, 부스트 관리, 스코어링 | 신규 |
| `monitor.py` | Circuit Breaker, 마진 체크, 괴리 감시 | 신규 |
| `telegram_ui.py` | 텔레그램 버튼 UI, 알림 | 신규 |
| `grvtnado.py` | 상태머신, 메인 루프, 청크 진입/퇴출, 크래시 복구 | 신규 |
| `run_bot.py` | Watchdog 엔트리포인트 | 신규 |
| `.env.example` | 환경변수 템플릿 | 신규 |
| `requirements.txt` | 의존성 | 신규 |
| `tests/test_models.py` | 모델 단위 테스트 | 신규 |
| `tests/test_strategy.py` | 전략 로직 테스트 | 신규 |
| `tests/test_pair_manager.py` | 페어 매니저 테스트 | 신규 |
| `tests/test_monitor.py` | 모니터 테스트 | 신규 |
| `tests/test_config.py` | 설정 검증 테스트 | 신규 |

---

## Task 1: 프로젝트 스캐폴딩

**Files:**
- Create: `requirements.txt`
- Create: `.env.example`
- Create: `.gitignore`
- Create: `exchanges/__init__.py`
- Create: `tests/__init__.py`

- [ ] **Step 1: requirements.txt 생성**

```txt
python-dotenv>=1.0.0
aiohttp>=3.9.0
nado-protocol>=0.1.0
grvt-pysdk>=0.2.0
pynacl>=1.5.0
pytest>=8.0.0
pytest-asyncio>=0.23.0
```

- [ ] **Step 2: .env.example 생성**

```env
# === NADO ===
NADO_PRIVATE_KEY=

# === GRVT ===
GRVT_API_KEY=
GRVT_PRIVATE_KEY=
GRVT_TRADING_ACCOUNT_ID=

# === Strategy ===
LEVERAGE=5
PAIR_DEFAULT=BTC
EARN_TARGET_VOLUME=300000
MIN_HOLD_HOURS_HOLD=24
MIN_HOLD_HOURS_VOLUME=2
MIN_HOLD_HOURS_URGENT=0.5
COOLDOWN_HOLD=10800
COOLDOWN_VOLUME=30
COOLDOWN_URGENT=10
SPREAD_EXIT_HOLD=50
SPREAD_STOPLOSS=-30
MAX_HOLD_DAYS=4

# === Monitor ===
POLL_INTERVAL=3
MARGIN_WARNING_PCT=15
MARGIN_EMERGENCY_PCT=10
CIRCUIT_BREAKER_FAILS=5
PRICE_DIVERGENCE_WARN=3
PRICE_DIVERGENCE_EMERGENCY=5

# === Telegram ===
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

# === Logging ===
LOG_SIZE_MB=5
LOG_COUNT=3
```

- [ ] **Step 3: .gitignore 생성**

```
.env
__pycache__/
*.pyc
logs/
.stop_bot
*.egg-info/
```

- [ ] **Step 4: 디렉토리 및 __init__.py 생성**

```bash
mkdir -p exchanges tests logs
touch exchanges/__init__.py tests/__init__.py
```

- [ ] **Step 5: 커밋**

```bash
git add requirements.txt .env.example .gitignore exchanges/__init__.py tests/__init__.py
git commit -m "chore: 프로젝트 스캐폴딩 — 의존성, env 템플릿, gitignore"
```

---

## Task 2: config.py — 설정 관리

**Files:**
- Create: `config.py`
- Create: `tests/test_config.py`

- [ ] **Step 1: 테스트 작성**

```python
# tests/test_config.py
import os
import pytest
from config import Config


def test_defaults():
    cfg = Config()
    assert cfg.LEVERAGE == 5
    assert cfg.POLL_INTERVAL == 3
    assert cfg.SPREAD_EXIT_HOLD == 50.0
    assert cfg.SPREAD_STOPLOSS == -30.0
    assert cfg.EARN_TARGET_VOLUME == 300_000.0


def test_validate_missing_keys():
    cfg = Config()
    errors = cfg.validate()
    assert any("NADO_PRIVATE_KEY" in e for e in errors)
    assert any("GRVT_API_KEY" in e for e in errors)


def test_validate_all_set(monkeypatch):
    monkeypatch.setenv("NADO_PRIVATE_KEY", "0x1234")
    monkeypatch.setenv("GRVT_API_KEY", "key")
    monkeypatch.setenv("GRVT_PRIVATE_KEY", "0x5678")
    monkeypatch.setenv("GRVT_TRADING_ACCOUNT_ID", "acc1")
    cfg = Config()
    errors = cfg.validate()
    assert errors == []


def test_mode_params():
    cfg = Config()
    hold = cfg.mode_params("HOLD")
    assert hold["min_hold_hours"] == 24
    assert hold["cooldown"] == 10800
    vol = cfg.mode_params("VOLUME")
    assert vol["min_hold_hours"] == 2
    assert vol["cooldown"] == 30
    urg = cfg.mode_params("VOLUME_URGENT")
    assert urg["min_hold_hours"] == 0.5
    assert urg["cooldown"] == 10
```

- [ ] **Step 2: 테스트 실행 — 실패 확인**

```bash
cd D:\Codes\nado_grvt_bot && python -m pytest tests/test_config.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'config'`

- [ ] **Step 3: config.py 구현**

```python
# config.py
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()


class Config:
    def __init__(self):
        self.NADO_PRIVATE_KEY = os.getenv("NADO_PRIVATE_KEY", "")
        self.GRVT_API_KEY = os.getenv("GRVT_API_KEY", "")
        self.GRVT_PRIVATE_KEY = os.getenv("GRVT_PRIVATE_KEY", "")
        self.GRVT_TRADING_ACCOUNT_ID = os.getenv("GRVT_TRADING_ACCOUNT_ID", "")

        self.LEVERAGE = int(os.getenv("LEVERAGE", "5"))
        self.PAIR_DEFAULT = os.getenv("PAIR_DEFAULT", "BTC")
        self.EARN_TARGET_VOLUME = float(os.getenv("EARN_TARGET_VOLUME", "300000"))

        self.MIN_HOLD_HOURS_HOLD = float(os.getenv("MIN_HOLD_HOURS_HOLD", "24"))
        self.MIN_HOLD_HOURS_VOLUME = float(os.getenv("MIN_HOLD_HOURS_VOLUME", "2"))
        self.MIN_HOLD_HOURS_URGENT = float(os.getenv("MIN_HOLD_HOURS_URGENT", "0.5"))
        self.COOLDOWN_HOLD = int(os.getenv("COOLDOWN_HOLD", "10800"))
        self.COOLDOWN_VOLUME = int(os.getenv("COOLDOWN_VOLUME", "30"))
        self.COOLDOWN_URGENT = int(os.getenv("COOLDOWN_URGENT", "10"))

        self.SPREAD_EXIT_HOLD = float(os.getenv("SPREAD_EXIT_HOLD", "50"))
        self.SPREAD_STOPLOSS = float(os.getenv("SPREAD_STOPLOSS", "-30"))
        self.MAX_HOLD_DAYS = int(os.getenv("MAX_HOLD_DAYS", "4"))

        self.POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "3"))
        self.MARGIN_WARNING_PCT = float(os.getenv("MARGIN_WARNING_PCT", "15"))
        self.MARGIN_EMERGENCY_PCT = float(os.getenv("MARGIN_EMERGENCY_PCT", "10"))
        self.CIRCUIT_BREAKER_FAILS = int(os.getenv("CIRCUIT_BREAKER_FAILS", "5"))
        self.PRICE_DIVERGENCE_WARN = float(os.getenv("PRICE_DIVERGENCE_WARN", "3"))
        self.PRICE_DIVERGENCE_EMERGENCY = float(os.getenv("PRICE_DIVERGENCE_EMERGENCY", "5"))

        self.TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

        self.LOG_SIZE_MB = int(os.getenv("LOG_SIZE_MB", "5"))
        self.LOG_COUNT = int(os.getenv("LOG_COUNT", "3"))
        self.LOG_DIR = Path("logs")

        self.ENTRY_CHUNKS = 5
        self.EXIT_CHUNKS = 5
        self.CHUNK_RETRY = 2
        self.CHUNK_WAIT = 30
        self.SLIPPAGE_PCT = 0.004
        self.EMERGENCY_SLIPPAGE_PCT = 0.01
        self.MARGIN_BUFFER = 0.95
        self.POLL_BALANCE_SECONDS = 300
        self.POLL_FUNDING_SECONDS = 3600

        self.NADO_FUNDING_PERIOD_H = 1
        self.GRVT_FUNDING_PERIOD_H = 8

        self.NADO_MAKER_FEE_BPS = 1.0
        self.GRVT_MAKER_FEE_BPS = -0.01

    def validate(self) -> list[str]:
        errors = []
        required = {
            "NADO_PRIVATE_KEY": self.NADO_PRIVATE_KEY,
            "GRVT_API_KEY": self.GRVT_API_KEY,
            "GRVT_PRIVATE_KEY": self.GRVT_PRIVATE_KEY,
            "GRVT_TRADING_ACCOUNT_ID": self.GRVT_TRADING_ACCOUNT_ID,
        }
        for name, val in required.items():
            if not val:
                errors.append(f"{name} is not set")
        return errors

    def ensure_dirs(self):
        self.LOG_DIR.mkdir(exist_ok=True)

    def mode_params(self, mode: str) -> dict:
        return {
            "HOLD": {
                "min_hold_hours": self.MIN_HOLD_HOURS_HOLD,
                "cooldown": self.COOLDOWN_HOLD,
                "spread_exit": self.SPREAD_EXIT_HOLD,
            },
            "VOLUME": {
                "min_hold_hours": self.MIN_HOLD_HOURS_VOLUME,
                "cooldown": self.COOLDOWN_VOLUME,
                "spread_exit": 10.0,
            },
            "VOLUME_URGENT": {
                "min_hold_hours": self.MIN_HOLD_HOURS_URGENT,
                "cooldown": self.COOLDOWN_URGENT,
                "spread_exit": 6.0,
            },
        }[mode]

    def estimate_round_trip_fee(self, notional: float) -> float:
        nado_fee = notional * 2 * (self.NADO_MAKER_FEE_BPS / 10_000)
        grvt_fee = notional * 2 * (self.GRVT_MAKER_FEE_BPS / 10_000)
        return nado_fee + grvt_fee
```

- [ ] **Step 4: 테스트 실행 — 통과 확인**

```bash
cd D:\Codes\nado_grvt_bot && python -m pytest tests/test_config.py -v
```

Expected: ALL PASS

- [ ] **Step 5: 커밋**

```bash
git add config.py tests/test_config.py
git commit -m "feat: config.py — 환경변수 설정 관리, 모드별 파라미터, 수수료 계산"
```

---

## Task 3: models.py — 데이터 모델

**Files:**
- Create: `models.py`
- Create: `tests/test_models.py`

- [ ] **Step 1: 테스트 작성**

```python
# tests/test_models.py
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
```

- [ ] **Step 2: 테스트 실행 — 실패 확인**

```bash
cd D:\Codes\nado_grvt_bot && python -m pytest tests/test_models.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'models'`

- [ ] **Step 3: models.py 구현**

```python
# models.py
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
```

- [ ] **Step 4: 테스트 실행 — 통과 확인**

```bash
cd D:\Codes\nado_grvt_bot && python -m pytest tests/test_models.py -v
```

Expected: ALL PASS

- [ ] **Step 5: 커밋**

```bash
git add models.py tests/test_models.py
git commit -m "feat: models.py — Position, Cycle, EarnState, BotState 데이터 모델"
```

---

## Task 4: strategy.py — 전략 로직

**Files:**
- Create: `strategy.py`
- Create: `tests/test_strategy.py`

- [ ] **Step 1: 테스트 작성**

```python
# tests/test_strategy.py
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
    # NADO rate low, GRVT rate high → A (NADO LONG, GRVT SHORT)
    d = decide_direction(nado_8h=0.01, grvt_8h=0.05)
    assert d == "A"


def test_decide_direction_b_better():
    d = decide_direction(nado_8h=0.05, grvt_8h=0.01)
    assert d == "B"


def test_decide_direction_both_negative():
    d = decide_direction(nado_8h=-0.01, grvt_8h=-0.01)
    assert d is None


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
    assert mode == "HOLD"


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
```

- [ ] **Step 2: 테스트 실행 — 실패 확인**

```bash
cd D:\Codes\nado_grvt_bot && python -m pytest tests/test_strategy.py -v
```

Expected: FAIL

- [ ] **Step 3: strategy.py 구현**

```python
# strategy.py
from typing import Optional


def normalize_funding_to_8h(rate: float, period_hours: int) -> float:
    return rate * (8 / period_hours)


def decide_direction(nado_8h: float, grvt_8h: float) -> Optional[str]:
    net_a = grvt_8h - nado_8h  # A: NADO LONG + GRVT SHORT
    net_b = nado_8h - grvt_8h  # B: NADO SHORT + GRVT LONG
    if net_a <= 0 and net_b <= 0:
        return None
    return "A" if net_a >= net_b else "B"


def should_exit_cycle(
    hold_hours: float,
    min_hold_hours: float,
    max_hold_days: int,
    margin_ratio: float,
    margin_emergency: float,
) -> Optional[str]:
    if margin_ratio <= margin_emergency:
        return "margin_emergency"
    if hold_hours < min_hold_hours:
        return None
    if hold_hours >= max_hold_days * 24:
        return "max_hold"
    return None


def should_exit_spread(
    spread_mtm: float,
    threshold: float,
    stoploss: float = -30.0,
) -> bool:
    if spread_mtm >= threshold:
        return True
    if spread_mtm <= stoploss:
        return True
    return False


def is_opposite_direction_better(
    current: str,
    nado_8h: float,
    grvt_8h: float,
    threshold: float = 0.0005,
) -> bool:
    if current == "A":
        current_net = grvt_8h - nado_8h
        opposite_net = nado_8h - grvt_8h
    else:
        current_net = nado_8h - grvt_8h
        opposite_net = grvt_8h - nado_8h
    return opposite_net - current_net > threshold


def calc_notional(
    nado_balance: float,
    grvt_balance: float,
    leverage: int,
    margin_buffer: float = 0.95,
) -> float:
    return min(nado_balance, grvt_balance) * leverage * margin_buffer


def determine_mode(
    volume_met: bool,
    trades_met: bool,
    days_left: int,
    volume_remaining: float,
    daily_capacity: float,
) -> str:
    if not trades_met:
        return "VOLUME_URGENT"
    if volume_met:
        return "HOLD"
    daily_needed = volume_remaining / max(days_left, 1)
    if daily_needed > daily_capacity * 0.7:
        return "VOLUME_URGENT"
    return "VOLUME"


def is_entry_favorable(direction: str, nado_price: float, grvt_price: float) -> bool:
    if direction == "A":  # NADO LONG, GRVT SHORT
        return nado_price < grvt_price
    return nado_price > grvt_price
```

- [ ] **Step 4: 테스트 실행 — 통과 확인**

```bash
cd D:\Codes\nado_grvt_bot && python -m pytest tests/test_strategy.py -v
```

Expected: ALL PASS

- [ ] **Step 5: 커밋**

```bash
git add strategy.py tests/test_strategy.py
git commit -m "feat: strategy.py — 방향 결정, EXIT 판단, 모드 전환, 진입 타이밍"
```

---

## Task 5: monitor.py — Circuit Breaker & 안전장치

**Files:**
- Create: `monitor.py`
- Create: `tests/test_monitor.py`

- [ ] **Step 1: 테스트 작성**

```python
# tests/test_monitor.py
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
```

- [ ] **Step 2: 테스트 실행 — 실패 확인**

```bash
cd D:\Codes\nado_grvt_bot && python -m pytest tests/test_monitor.py -v
```

- [ ] **Step 3: monitor.py 구현**

```python
# monitor.py
from enum import Enum


class MarginLevel(str, Enum):
    NORMAL = "NORMAL"
    WARNING = "WARNING"
    EMERGENCY = "EMERGENCY"


def check_margin_level(
    ratio: float, warning_pct: float, emergency_pct: float,
) -> MarginLevel:
    if ratio <= emergency_pct:
        return MarginLevel.EMERGENCY
    if ratio <= warning_pct:
        return MarginLevel.WARNING
    return MarginLevel.NORMAL


def check_price_divergence(
    price_a: float, price_b: float,
    warn_pct: float, emergency_pct: float,
) -> str:
    if price_a <= 0 or price_b <= 0:
        return "NORMAL"
    divergence = abs(price_a - price_b) / min(price_a, price_b) * 100
    if divergence >= emergency_pct:
        return "EMERGENCY"
    if divergence >= warn_pct:
        return "WARNING"
    return "NORMAL"


class CircuitBreaker:
    def __init__(self, max_fails: int = 5):
        self._max_fails = max_fails
        self._fails: dict[str, int] = {}

    def record_failure(self, exchange: str):
        self._fails[exchange] = self._fails.get(exchange, 0) + 1

    def record_success(self, exchange: str):
        self._fails[exchange] = 0

    def is_tripped(self, exchange: str) -> bool:
        return self._fails.get(exchange, 0) >= self._max_fails

    def any_tripped(self) -> bool:
        return any(v >= self._max_fails for v in self._fails.values())
```

- [ ] **Step 4: 테스트 실행 — 통과 확인**

```bash
cd D:\Codes\nado_grvt_bot && python -m pytest tests/test_monitor.py -v
```

Expected: ALL PASS

- [ ] **Step 5: 커밋**

```bash
git add monitor.py tests/test_monitor.py
git commit -m "feat: monitor.py — CircuitBreaker, 마진 레벨, 가격 괴리 감시"
```

---

## Task 6: pair_manager.py — 동적 페어 부스트 추적

**Files:**
- Create: `pair_manager.py`
- Create: `tests/test_pair_manager.py`

- [ ] **Step 1: 테스트 작성**

```python
# tests/test_pair_manager.py
import pytest
from pair_manager import PairManager


def test_common_pairs():
    pm = PairManager(default_pair="BTC")
    pm.set_available_pairs(
        nado=["BTC", "ETH", "SOL", "BNB", "XRP"],
        grvt=["BTC", "ETH", "SOL"],
    )
    assert pm.common_pairs == {"BTC", "ETH", "SOL"}


def test_boost_config():
    pm = PairManager(default_pair="BTC")
    pm.set_available_pairs(nado=["BTC", "ETH"], grvt=["BTC", "ETH"])
    pm.set_boost("BTC", nado=4.0, grvt=1.0)
    pm.set_boost("ETH", nado=1.0, grvt=3.0)
    assert pm.get_boost("BTC") == {"nado": 4.0, "grvt": 1.0}
    assert pm.get_boost("ETH") == {"nado": 1.0, "grvt": 3.0}


def test_score_with_boost():
    pm = PairManager(default_pair="BTC")
    pm.set_available_pairs(nado=["BTC", "ETH"], grvt=["BTC", "ETH"])
    pm.set_boost("BTC", nado=4.0, grvt=1.0)
    pm.set_boost("ETH", nado=1.0, grvt=3.0)
    funding = {"BTC": 0.001, "ETH": 0.002}
    liquidity = {"BTC": 100000, "ETH": 50000}
    best = pm.best_pair(funding_spreads=funding, liquidities=liquidity, min_liquidity=10000)
    assert best in ("BTC", "ETH")


def test_score_excludes_low_liquidity():
    pm = PairManager(default_pair="BTC")
    pm.set_available_pairs(nado=["BTC", "ETH"], grvt=["BTC", "ETH"])
    pm.set_boost("ETH", nado=10.0, grvt=10.0)
    funding = {"BTC": 0.001, "ETH": 0.005}
    liquidity = {"BTC": 100000, "ETH": 500}
    best = pm.best_pair(funding_spreads=funding, liquidities=liquidity, min_liquidity=10000)
    assert best == "BTC"


def test_parse_boost_env():
    pm = PairManager(default_pair="BTC")
    pm.set_available_pairs(nado=["BTC", "ETH"], grvt=["BTC", "ETH"])
    pm.parse_boost_string("BTC:4x,ETH:3x")
    assert pm.get_boost("BTC") == {"nado": 4.0, "grvt": 4.0}
    assert pm.get_boost("ETH") == {"nado": 3.0, "grvt": 3.0}


def test_clear_boost():
    pm = PairManager(default_pair="BTC")
    pm.set_available_pairs(nado=["BTC"], grvt=["BTC"])
    pm.set_boost("BTC", nado=4.0, grvt=1.0)
    pm.clear_boost()
    assert pm.get_boost("BTC") == {"nado": 1.0, "grvt": 1.0}
```

- [ ] **Step 2: 테스트 실행 — 실패 확인**

```bash
cd D:\Codes\nado_grvt_bot && python -m pytest tests/test_pair_manager.py -v
```

- [ ] **Step 3: pair_manager.py 구현**

```python
# pair_manager.py
import math
import logging

logger = logging.getLogger(__name__)


class PairManager:
    def __init__(self, default_pair: str = "BTC"):
        self._default = default_pair
        self._nado_pairs: set[str] = set()
        self._grvt_pairs: set[str] = set()
        self._boosts: dict[str, dict[str, float]] = {}

    @property
    def common_pairs(self) -> set[str]:
        return self._nado_pairs & self._grvt_pairs

    def set_available_pairs(self, nado: list[str], grvt: list[str]):
        self._nado_pairs = set(nado)
        self._grvt_pairs = set(grvt)

    def set_boost(self, pair: str, nado: float = 1.0, grvt: float = 1.0):
        self._boosts[pair] = {"nado": nado, "grvt": grvt}

    def get_boost(self, pair: str) -> dict[str, float]:
        return self._boosts.get(pair, {"nado": 1.0, "grvt": 1.0})

    def clear_boost(self):
        self._boosts.clear()

    def parse_boost_string(self, s: str):
        if not s or s.strip().lower() == "clear":
            self.clear_boost()
            return
        for token in s.split(","):
            token = token.strip()
            if ":" not in token:
                continue
            pair, mult_str = token.split(":", 1)
            mult = float(mult_str.strip().lower().replace("x", ""))
            self.set_boost(pair.strip().upper(), nado=mult, grvt=mult)

    def best_pair(
        self,
        funding_spreads: dict[str, float],
        liquidities: dict[str, float],
        min_liquidity: float = 75000,
    ) -> str:
        candidates = self.common_pairs
        if not candidates:
            return self._default

        best_pair = self._default
        best_score = -999.0

        for pair in candidates:
            liq = liquidities.get(pair, 0)
            if liq < min_liquidity:
                continue
            boost = self.get_boost(pair)
            fund = funding_spreads.get(pair, 0)
            score = (
                (boost["nado"] + boost["grvt"]) * 3.0
                + fund * 1000
                + math.log(max(liq, 1)) * 0.5
            )
            if score > best_score:
                best_score = score
                best_pair = pair

        return best_pair

    def to_dict(self) -> dict:
        return {
            "boosts": self._boosts.copy(),
            "nado_pairs": sorted(self._nado_pairs),
            "grvt_pairs": sorted(self._grvt_pairs),
        }

    def load_boosts(self, d: dict):
        self._boosts = d.get("boosts", {})
```

- [ ] **Step 4: 테스트 실행 — 통과 확인**

```bash
cd D:\Codes\nado_grvt_bot && python -m pytest tests/test_pair_manager.py -v
```

Expected: ALL PASS

- [ ] **Step 5: 커밋**

```bash
git add pair_manager.py tests/test_pair_manager.py
git commit -m "feat: pair_manager.py — 공통 페어 탐색, 부스트 관리, 스코어링"
```

---

## Task 7: exchanges/base_client.py — 거래소 공통 인터페이스

**Files:**
- Create: `exchanges/base_client.py`

- [ ] **Step 1: base_client.py 구현**

```python
# exchanges/base_client.py
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class OrderResult:
    order_id: str
    status: str  # "filled", "partial", "live", "cancelled", "error"
    filled_size: float = 0.0
    filled_price: float = 0.0
    message: str = ""


class BaseExchangeClient(ABC):
    @abstractmethod
    async def connect(self):
        """Initialize SDK/session."""

    @abstractmethod
    async def close(self):
        """Close connections."""

    @abstractmethod
    async def get_balance(self) -> float:
        """Available balance in settlement currency."""

    @abstractmethod
    async def get_positions(self, symbol: str) -> list[dict]:
        """Open positions for symbol. Each dict has: side, size, entry_price."""

    @abstractmethod
    async def get_mark_price(self, symbol: str) -> Optional[float]:
        """Current mark price."""

    @abstractmethod
    async def get_funding_rate(self, symbol: str) -> Optional[float]:
        """Current funding rate (raw, not normalized)."""

    @abstractmethod
    async def place_limit_order(
        self, symbol: str, side: str, size: float, price: float,
    ) -> OrderResult:
        """Place limit/maker order. side: 'BUY' or 'SELL'."""

    @abstractmethod
    async def close_position(
        self, symbol: str, side: str, size: float, slippage_pct: float = 0.01,
    ) -> bool:
        """Market-close a position with slippage tolerance."""

    @abstractmethod
    async def cancel_all_orders(self, symbol: str) -> bool:
        """Cancel all open orders for symbol."""

    @abstractmethod
    async def get_available_pairs(self) -> list[str]:
        """List of tradeable pair symbols (normalized: BTC, ETH, etc.)."""

    @abstractmethod
    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        """Set leverage for symbol."""

    @abstractmethod
    async def get_orderbook_depth(self, symbol: str) -> float:
        """Sum of top-10 bid+ask depth in USD for liquidity scoring."""
```

- [ ] **Step 2: 커밋**

```bash
git add exchanges/base_client.py
git commit -m "feat: base_client.py — 거래소 공통 ABC 인터페이스"
```

---

## Task 8: exchanges/nado_client.py — NADO SDK 래퍼

**Files:**
- Create: `exchanges/nado_client.py`

> **Note:** nado-protocol SDK의 정확한 API는 `pip install nado-protocol` 후 SDK 소스를 읽어 확인 필요. 아래는 SDK 문서 기반 구현이며, SDK API 차이 시 조정 필요.

- [ ] **Step 1: nado_client.py 구현**

```python
# exchanges/nado_client.py
import asyncio
import logging
import aiohttp
from typing import Optional

from exchanges.base_client import BaseExchangeClient, OrderResult

logger = logging.getLogger(__name__)

GATEWAY_PROD = "https://gateway.prod.nado.xyz/v1"
ARCHIVE_PROD = "https://archive.prod.nado.xyz/v1"


class NadoClient(BaseExchangeClient):
    def __init__(self, private_key: str):
        self._private_key = private_key
        self._client = None
        self._symbol_map: dict[str, int] = {}  # BTC -> product_id
        self._session: Optional[aiohttp.ClientSession] = None

    async def connect(self):
        try:
            from nado_protocol.client import create_nado_client, NadoClientMode
            self._client = create_nado_client(NadoClientMode.MAINNET, self._private_key)
        except ImportError:
            logger.error("nado-protocol SDK not installed. Run: pip install nado-protocol")
            raise
        self._session = aiohttp.ClientSession()
        await self._init_symbol_map()

    async def close(self):
        if self._session:
            await self._session.close()
            self._session = None

    async def _init_symbol_map(self):
        try:
            resp = await self._query("all_products", {})
            if resp:
                for product in resp:
                    pid = product.get("product_id")
                    symbol = product.get("symbol", "")
                    name = symbol.split("-")[0].upper() if "-" in symbol else symbol.upper()
                    if pid is not None:
                        self._symbol_map[name] = pid
                logger.info(f"NADO symbol map: {self._symbol_map}")
        except Exception as e:
            logger.warning(f"Failed to init NADO symbol map: {e}")
            self._symbol_map = {"BTC": 1, "ETH": 2, "SOL": 3}

    def _product_id(self, symbol: str) -> int:
        pid = self._symbol_map.get(symbol.upper())
        if pid is None:
            raise ValueError(f"Unknown NADO symbol: {symbol}")
        return pid

    async def _query(self, endpoint: str, params: dict) -> Optional[dict]:
        url = f"{GATEWAY_PROD}/query"
        payload = {"type": endpoint, **params}
        for attempt in range(3):
            try:
                async with self._session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data.get("data", data)
                    logger.warning(f"NADO query {endpoint} status={resp.status}")
            except Exception as e:
                logger.warning(f"NADO query {endpoint} attempt {attempt+1}: {e}")
                if attempt < 2:
                    await asyncio.sleep(1 * (attempt + 1))
        return None

    async def _execute(self, action: str, params: dict) -> Optional[dict]:
        if not self._client:
            raise RuntimeError("NADO client not connected")
        try:
            result = await asyncio.to_thread(
                getattr(self._client.market, action), params
            )
            return result if isinstance(result, dict) else {"result": result}
        except Exception as e:
            logger.error(f"NADO execute {action}: {e}")
            return None

    async def get_balance(self) -> float:
        try:
            resp = await self._query("account_info", {})
            if resp:
                for key in ("equity", "balance", "available_balance", "collateral"):
                    if key in resp:
                        return float(resp[key])
        except Exception as e:
            logger.error(f"NADO get_balance: {e}")
        return 0.0

    async def get_positions(self, symbol: str) -> list[dict]:
        try:
            resp = await self._query("positions", {"product_id": self._product_id(symbol)})
            if resp and isinstance(resp, list):
                return [p for p in resp if abs(float(p.get("size", p.get("amount", 0)))) > 0]
        except Exception as e:
            logger.error(f"NADO get_positions: {e}")
        return []

    async def get_mark_price(self, symbol: str) -> Optional[float]:
        try:
            resp = await self._query("market_info", {"product_id": self._product_id(symbol)})
            if resp:
                for key in ("mark_price", "markPrice", "price"):
                    val = resp.get(key)
                    if val:
                        price = float(val)
                        if price > 1e15:
                            price = price / 1e18
                        return price
        except Exception as e:
            logger.error(f"NADO get_mark_price: {e}")
        return None

    async def get_funding_rate(self, symbol: str) -> Optional[float]:
        try:
            resp = await self._query("funding_rate", {"product_id": self._product_id(symbol)})
            if resp:
                rate = resp.get("funding_rate", resp.get("rate"))
                if rate is not None:
                    return float(rate)
        except Exception as e:
            logger.error(f"NADO get_funding_rate: {e}")
        return None

    async def place_limit_order(
        self, symbol: str, side: str, size: float, price: float,
    ) -> OrderResult:
        try:
            from nado_protocol.utils import to_x18, gen_order_nonce, get_expiration_timestamp
            product_id = self._product_id(symbol)
            amount_x18 = to_x18(size if side == "BUY" else -size)
            price_x18 = to_x18(price)

            order_params = {
                "product_id": product_id,
                "priceX18": str(price_x18),
                "amount": str(amount_x18),
                "nonce": str(gen_order_nonce()),
                "expiration": str(get_expiration_timestamp()),
            }
            result = await self._execute("place_order", order_params)
            if result:
                return OrderResult(
                    order_id=str(result.get("id", result.get("order_id", ""))),
                    status="filled" if result.get("status") in ("filled", "matched") else "live",
                    filled_size=size,
                    filled_price=price,
                )
        except Exception as e:
            logger.error(f"NADO place_limit_order: {e}")
        return OrderResult(order_id="", status="error", message="order failed")

    async def close_position(
        self, symbol: str, side: str, size: float, slippage_pct: float = 0.01,
    ) -> bool:
        price = await self.get_mark_price(symbol)
        if not price:
            return False
        if side == "LONG":
            close_side, close_price = "SELL", price * (1 - slippage_pct)
        else:
            close_side, close_price = "BUY", price * (1 + slippage_pct)
        result = await self.place_limit_order(symbol, close_side, size, close_price)
        return result.status in ("filled", "matched")

    async def cancel_all_orders(self, symbol: str) -> bool:
        try:
            product_id = self._product_id(symbol)
            result = await self._execute("cancel_all_orders", {"product_id": product_id})
            return result is not None
        except Exception as e:
            logger.error(f"NADO cancel_all_orders: {e}")
            return False

    async def get_available_pairs(self) -> list[str]:
        return list(self._symbol_map.keys())

    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        try:
            result = await self._execute("set_leverage", {
                "product_id": self._product_id(symbol),
                "leverage": leverage,
            })
            return result is not None
        except Exception as e:
            logger.error(f"NADO set_leverage: {e}")
            return False

    async def get_orderbook_depth(self, symbol: str) -> float:
        try:
            resp = await self._query("orderbook", {"product_id": self._product_id(symbol), "depth": 10})
            if resp:
                bids = sum(float(b.get("size", b.get("amount", 0))) for b in resp.get("bids", []))
                asks = sum(float(a.get("size", a.get("amount", 0))) for a in resp.get("asks", []))
                mark = await self.get_mark_price(symbol) or 0
                return (bids + asks) * mark
        except Exception as e:
            logger.error(f"NADO get_orderbook_depth: {e}")
        return 0.0
```

- [ ] **Step 2: 커밋**

```bash
git add exchanges/nado_client.py
git commit -m "feat: nado_client.py — NADO SDK 래퍼 (REST query/execute, EIP-712)"
```

> **구현 시 확인 필요:**
> - `nado-protocol` SDK 설치 후 `create_nado_client`의 실제 API 확인
> - `to_x18`, `gen_order_nonce`, `get_expiration_timestamp` 정확한 import 경로
> - product_id 매핑 정확성 (all_products 응답 구조)
> - 가격 X18 변환이 필요한 필드들 목록

---

## Task 9: exchanges/grvt_client.py — GRVT SDK 래퍼

**Files:**
- Create: `exchanges/grvt_client.py`

> **Note:** grvt-pysdk는 CCXT 호환 인터페이스를 제공. `GrvtCcxtWS` 클래스 사용.

- [ ] **Step 1: grvt_client.py 구현**

```python
# exchanges/grvt_client.py
import asyncio
import logging
from typing import Optional

from exchanges.base_client import BaseExchangeClient, OrderResult

logger = logging.getLogger(__name__)

SYMBOL_MAP = {
    "BTC": "BTC_USDT_Perp",
    "ETH": "ETH_USDT_Perp",
    "SOL": "SOL_USDT_Perp",
}


class GrvtClient(BaseExchangeClient):
    def __init__(self, api_key: str, private_key: str, trading_account_id: str):
        self._api_key = api_key
        self._private_key = private_key
        self._account_id = trading_account_id
        self._api = None
        self._ws_prices: dict[str, float] = {}

    def _grvt_symbol(self, symbol: str) -> str:
        return SYMBOL_MAP.get(symbol.upper(), f"{symbol.upper()}_USDT_Perp")

    async def connect(self):
        try:
            from grvt_pysdk import GrvtCcxtWS
            self._api = GrvtCcxtWS(
                env="prod",
                private_key=self._private_key,
                trading_account_id=self._account_id,
                api_key=self._api_key,
            )
            await asyncio.to_thread(self._api.login)
            logger.info("GRVT connected and logged in")
        except ImportError:
            logger.error("grvt-pysdk not installed. Run: pip install grvt-pysdk")
            raise

    async def close(self):
        if self._api:
            try:
                await asyncio.to_thread(self._api.close)
            except Exception:
                pass
            self._api = None

    async def _retry(self, fn, *args, max_retries=3, **kwargs):
        for attempt in range(max_retries):
            try:
                return await asyncio.to_thread(fn, *args, **kwargs)
            except Exception as e:
                logger.warning(f"GRVT retry {attempt+1}/{max_retries}: {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(1 * (attempt + 1))
                else:
                    raise

    async def get_balance(self) -> float:
        try:
            result = await self._retry(self._api.fetch_balance)
            if isinstance(result, dict):
                for key in ("equity", "total", "free", "USDT"):
                    if key in result:
                        val = result[key]
                        return float(val) if not isinstance(val, dict) else float(val.get("total", 0))
            return float(result) if result else 0.0
        except Exception as e:
            logger.error(f"GRVT get_balance: {e}")
            return 0.0

    async def get_positions(self, symbol: str) -> list[dict]:
        try:
            grvt_sym = self._grvt_symbol(symbol)
            result = await self._retry(self._api.fetch_positions, [grvt_sym])
            if result:
                return [
                    {
                        "side": p.get("side", "").upper(),
                        "size": abs(float(p.get("contracts", p.get("contractSize", 0)))),
                        "entry_price": float(p.get("entryPrice", 0)),
                        "notional": abs(float(p.get("notional", 0))),
                    }
                    for p in result
                    if abs(float(p.get("contracts", p.get("contractSize", 0)))) > 0
                ]
        except Exception as e:
            logger.error(f"GRVT get_positions: {e}")
        return []

    async def get_mark_price(self, symbol: str) -> Optional[float]:
        cached = self._ws_prices.get(symbol)
        if cached:
            return cached
        try:
            grvt_sym = self._grvt_symbol(symbol)
            ticker = await self._retry(self._api.fetch_ticker, grvt_sym)
            if ticker:
                price = float(ticker.get("mark", ticker.get("last", 0)))
                self._ws_prices[symbol] = price
                return price
        except Exception as e:
            logger.error(f"GRVT get_mark_price: {e}")
        return None

    async def get_funding_rate(self, symbol: str) -> Optional[float]:
        try:
            grvt_sym = self._grvt_symbol(symbol)
            result = await self._retry(self._api.fetch_funding_rate_history, grvt_sym, None, 1)
            if result and len(result) > 0:
                return float(result[0].get("fundingRate", 0))
        except Exception as e:
            logger.error(f"GRVT get_funding_rate: {e}")
        return None

    async def place_limit_order(
        self, symbol: str, side: str, size: float, price: float,
    ) -> OrderResult:
        try:
            grvt_sym = self._grvt_symbol(symbol)
            result = await self._retry(
                self._api.create_order,
                grvt_sym, "limit", side.lower(), size, price,
                {"post_only": True},
            )
            if result:
                status = result.get("status", "")
                return OrderResult(
                    order_id=str(result.get("id", "")),
                    status="filled" if status in ("closed", "filled") else status,
                    filled_size=float(result.get("filled", 0)),
                    filled_price=float(result.get("average", price)),
                )
        except Exception as e:
            logger.error(f"GRVT place_limit_order: {e}")
        return OrderResult(order_id="", status="error", message="order failed")

    async def close_position(
        self, symbol: str, side: str, size: float, slippage_pct: float = 0.01,
    ) -> bool:
        price = await self.get_mark_price(symbol)
        if not price:
            return False
        if side == "LONG":
            close_side, close_price = "sell", price * (1 - slippage_pct)
        else:
            close_side, close_price = "buy", price * (1 + slippage_pct)
        result = await self.place_limit_order(symbol, close_side, size, close_price)
        return result.status in ("filled", "closed")

    async def cancel_all_orders(self, symbol: str) -> bool:
        try:
            grvt_sym = self._grvt_symbol(symbol)
            await self._retry(self._api.cancel_all_orders, grvt_sym)
            return True
        except Exception as e:
            logger.error(f"GRVT cancel_all_orders: {e}")
            return False

    async def get_available_pairs(self) -> list[str]:
        try:
            markets = await self._retry(self._api.fetch_all_markets)
            pairs = []
            for m in markets:
                sym = m.get("id", m.get("symbol", ""))
                if sym.endswith("_Perp") or sym.endswith("_USDT_Perp"):
                    base = sym.split("_")[0]
                    pairs.append(base)
            return pairs
        except Exception as e:
            logger.error(f"GRVT get_available_pairs: {e}")
            return list(SYMBOL_MAP.keys())

    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        logger.info(f"GRVT leverage is set per account tier, not per API call. Requested: {leverage}x")
        return True

    async def get_orderbook_depth(self, symbol: str) -> float:
        try:
            grvt_sym = self._grvt_symbol(symbol)
            book = await self._retry(self._api.fetch_order_book, grvt_sym, 10)
            if book:
                bid_depth = sum(b[1] for b in book.get("bids", []))
                ask_depth = sum(a[1] for a in book.get("asks", []))
                mark = await self.get_mark_price(symbol) or 0
                return (bid_depth + ask_depth) * mark
        except Exception as e:
            logger.error(f"GRVT get_orderbook_depth: {e}")
        return 0.0
```

- [ ] **Step 2: 커밋**

```bash
git add exchanges/grvt_client.py
git commit -m "feat: grvt_client.py — GRVT CCXT 래퍼 (PostOnly Maker, WS 가격 캐시)"
```

> **구현 시 확인 필요:**
> - `grvt-pysdk` 설치 후 `GrvtCcxtWS` 실제 import 경로 (`from grvt_pysdk` vs `from grvt_pysdk.ccxt`)
> - `login()` 메서드 존재 여부 (세션 쿠키 발급)
> - `fetch_positions` 응답 필드명 (`contracts`, `side`, `entryPrice`)
> - `fetch_balance` 응답 구조
> - PostOnly 파라미터 전달 방식

---

## Task 10: telegram_ui.py — 텔레그램 UI

**Files:**
- Create: `telegram_ui.py`

- [ ] **Step 1: telegram_ui.py 구현**

```python
# telegram_ui.py
import logging
import aiohttp
from typing import Callable, Awaitable, Optional

logger = logging.getLogger(__name__)

BTN_STATUS = "📊 Status"
BTN_HISTORY = "📋 History"
BTN_EARN = "💰 Earn"
BTN_FUNDING = "📈 Funding"
BTN_REBALANCE = "🔄 Rebalance"
BTN_STOP = "⏹ Stop"
BTN_SETBOOST = "🎯 SetBoost"

KEYBOARD = {
    "keyboard": [
        [BTN_STATUS, BTN_HISTORY, BTN_EARN],
        [BTN_FUNDING, BTN_REBALANCE, BTN_STOP],
        [BTN_SETBOOST],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
}


class TelegramUI:
    def __init__(self, token: str, chat_id: str):
        self._token = token
        self._chat_id = chat_id
        self._base = f"https://api.telegram.org/bot{token}"
        self._session: Optional[aiohttp.ClientSession] = None
        self._offset = 0
        self._callbacks: dict[str, Callable[..., Awaitable]] = {}
        self._text_handler: Optional[Callable[..., Awaitable]] = None
        self.enabled = bool(token and chat_id)

    async def _ensure_session(self):
        if not self._session:
            self._session = aiohttp.ClientSession()

    async def close(self):
        if self._session:
            await self._session.close()
            self._session = None

    def register_callback(self, button: str, handler: Callable[..., Awaitable]):
        self._callbacks[button] = handler

    def register_text_handler(self, handler: Callable[..., Awaitable]):
        self._text_handler = handler

    async def send_message(self, text: str, with_keyboard: bool = True):
        if not self.enabled:
            return
        await self._ensure_session()
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "HTML",
        }
        if with_keyboard:
            import json
            payload["reply_markup"] = json.dumps(KEYBOARD)
        try:
            async with self._session.post(
                f"{self._base}/sendMessage", json=payload,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"Telegram send failed: {resp.status}")
        except Exception as e:
            logger.warning(f"Telegram send error: {e}")

    async def send_alert(self, text: str):
        await self.send_message(text, with_keyboard=False)

    async def poll_updates(self):
        if not self.enabled:
            return
        await self._ensure_session()
        try:
            async with self._session.get(
                f"{self._base}/getUpdates",
                params={"offset": self._offset, "timeout": 1},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status != 200:
                    return
                data = await resp.json()
                for update in data.get("result", []):
                    self._offset = update["update_id"] + 1
                    msg = update.get("message", {})
                    if str(msg.get("chat", {}).get("id")) != self._chat_id:
                        continue
                    text = msg.get("text", "")
                    if text in self._callbacks:
                        await self._callbacks[text]()
                    elif text.startswith("/") and self._text_handler:
                        await self._text_handler(text)
        except Exception as e:
            logger.debug(f"Telegram poll: {e}")
```

- [ ] **Step 2: 커밋**

```bash
git add telegram_ui.py
git commit -m "feat: telegram_ui.py — 버튼 UI, 폴링, 알림"
```

---

## Task 11: grvtnado.py — 상태머신 & 메인 루프

**Files:**
- Create: `grvtnado.py`

> 이 파일은 프로젝트에서 가장 큰 파일이오. 기존 delta_neutral_bot의 grvtnado.py 패턴을 따르되, NADO/GRVT 특화 로직과 EarnManager/PairManager를 통합.

- [ ] **Step 1: grvtnado.py 구현 — Part 1: 초기화 및 상태 관리**

```python
# grvtnado.py
import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from config import Config
from models import (
    CycleState, OperatingMode, Position, Cycle,
    EarnState, BotState, FundingSnapshot,
)
from strategy import (
    normalize_funding_to_8h, decide_direction, should_exit_cycle,
    should_exit_spread, is_opposite_direction_better, calc_notional,
    determine_mode, is_entry_favorable,
)
from pair_manager import PairManager
from monitor import MarginLevel, check_margin_level, CircuitBreaker, check_price_divergence
from telegram_ui import TelegramUI, BTN_STATUS, BTN_HISTORY, BTN_EARN, BTN_FUNDING, BTN_REBALANCE, BTN_STOP
from exchanges.nado_client import NadoClient
from exchanges.grvt_client import GrvtClient

logger = logging.getLogger(__name__)
KST = timezone(timedelta(hours=9))


class DeltaNeutralBot:
    def __init__(self):
        self.cfg = Config()
        self.cfg.ensure_dirs()

        self._nado = NadoClient(self.cfg.NADO_PRIVATE_KEY)
        self._grvt = GrvtClient(
            self.cfg.GRVT_API_KEY, self.cfg.GRVT_PRIVATE_KEY,
            self.cfg.GRVT_TRADING_ACCOUNT_ID,
        )
        self._telegram = TelegramUI(self.cfg.TELEGRAM_BOT_TOKEN, self.cfg.TELEGRAM_CHAT_ID)
        self._pair_mgr = PairManager(default_pair=self.cfg.PAIR_DEFAULT)
        self._cb = CircuitBreaker(max_fails=self.cfg.CIRCUIT_BREAKER_FAILS)

        self._state = BotState.load(self.cfg.LOG_DIR / "bot_state.json")
        self._positions: dict[str, Position] = {}
        self._earn = self._init_earn()
        self._running = False

        self._nado_price: Optional[float] = None
        self._grvt_price: Optional[float] = None
        self._last_balance_check = 0.0
        self._last_funding_check = 0.0
        self._last_daily_report = ""
        self._last_margin_warn = 0.0
        self._consecutive_loss_cycles = 0
        self._cycle_history: list[Cycle] = []
        self._idle_since: float = 0.0

    def _init_earn(self) -> EarnState:
        if self._state.earn:
            return EarnState.from_dict(self._state.earn)
        now = datetime.now(timezone.utc)
        cycle_start = datetime(2026, 4, 21, 0, 0, 0, tzinfo=timezone.utc)
        while cycle_start + timedelta(days=28) <= now:
            cycle_start += timedelta(days=28)
        return EarnState(
            cycle_start=cycle_start,
            cycle_end=cycle_start + timedelta(days=28),
            target_volume=self.cfg.EARN_TARGET_VOLUME,
        )

    def _save_state(self):
        self._state.earn = self._earn.to_dict()
        if self._positions:
            self._state.positions = {k: v.to_dict() for k, v in self._positions.items()}
        else:
            self._state.positions = {}
        self._state.boost_config = self._pair_mgr.to_dict().get("boosts", {})
        self._state.save(self.cfg.LOG_DIR / "bot_state.json")

    def _log_jsonl(self, filename: str, data: dict):
        path = self.cfg.LOG_DIR / filename
        with open(path, "a") as f:
            f.write(json.dumps({**data, "ts": time.time()}) + "\n")
```

- [ ] **Step 2: grvtnado.py — Part 2: 상태머신**

```python
    # --- 상태머신 (grvtnado.py에 이어서) ---

    async def _run_state_machine(self):
        state = self._state.cycle_state

        if state == CycleState.IDLE:
            await self._handle_idle()
        elif state == CycleState.ANALYZE:
            await self._handle_analyze()
        elif state == CycleState.ENTER:
            await self._handle_enter()
        elif state == CycleState.HOLD:
            await self._handle_hold()
        elif state == CycleState.EXIT:
            await self._handle_exit()
        elif state == CycleState.COOLDOWN:
            await self._handle_cooldown()

    async def _handle_idle(self):
        self._check_earn_cycle()
        mode = self._determine_current_mode()
        self._state.mode = OperatingMode(mode)

        best_pair = self._pair_mgr.best_pair(
            funding_spreads={}, liquidities={}, min_liquidity=0,
        )
        self._state.pair = best_pair
        self._state.cycle_state = CycleState.ANALYZE
        self._save_state()

    async def _handle_analyze(self):
        pair = self._state.pair
        nado_rate = await self._nado.get_funding_rate(pair)
        grvt_rate = await self._grvt.get_funding_rate(pair)

        if nado_rate is None or grvt_rate is None:
            return

        nado_8h = normalize_funding_to_8h(nado_rate, self.cfg.NADO_FUNDING_PERIOD_H)
        grvt_8h = normalize_funding_to_8h(grvt_rate, self.cfg.GRVT_FUNDING_PERIOD_H)
        direction = decide_direction(nado_8h, grvt_8h)

        if direction is None:
            mode = self._state.mode
            if mode == OperatingMode.VOLUME_URGENT:
                elapsed = time.time() - self._idle_since if self._idle_since else 0
                if elapsed > 7200 and abs(nado_8h - grvt_8h) < 0.001:
                    direction = "A"
                    logger.info("VOLUME_URGENT: 펀딩 미세 음수 허용, 방향 A 강제 진입")
            if direction is None:
                if not self._idle_since:
                    self._idle_since = time.time()
                return

        self._idle_since = 0
        self._state.direction = direction
        self._state.cycle_state = CycleState.ENTER
        self._save_state()

        self._log_jsonl("funding_history.jsonl", {
            "pair": pair, "nado_rate": nado_rate, "grvt_rate": grvt_rate,
            "nado_8h": nado_8h, "grvt_8h": grvt_8h, "direction": direction,
        })

    async def _handle_enter(self):
        pair = self._state.pair
        direction = self._state.direction

        self._nado_price = await self._nado.get_mark_price(pair)
        self._grvt_price = await self._grvt.get_mark_price(pair)
        if not self._nado_price or not self._grvt_price:
            return

        if not is_entry_favorable(direction, self._nado_price, self._grvt_price):
            mode = self._state.mode
            if mode != OperatingMode.VOLUME_URGENT:
                return
            elapsed = time.time() - self._idle_since if self._idle_since else 0
            if elapsed < 1800:
                return

        nado_bal = await self._nado.get_balance()
        grvt_bal = await self._grvt.get_balance()
        notional = calc_notional(nado_bal, grvt_bal, self.cfg.LEVERAGE, self.cfg.MARGIN_BUFFER)
        if notional < 100:
            logger.warning("Notional too small, skipping")
            return

        success = await self._execute_enter(pair, direction, notional)
        if success:
            self._state.cycle_id = str(uuid.uuid4())[:8]
            self._state.entered_at = time.time()
            self._state.cumulative_funding = 0.0
            self._state.cumulative_fees = self.cfg.estimate_round_trip_fee(notional)
            self._state.nado_balance = nado_bal
            self._state.grvt_balance = grvt_bal
            self._state.cycle_state = CycleState.HOLD
            self._save_state()

            await self._telegram.send_alert(
                f"[ENTER] {pair} | "
                f"NADO {'LONG' if direction == 'A' else 'SHORT'} / "
                f"GRVT {'SHORT' if direction == 'A' else 'LONG'} | "
                f"${notional:,.0f}"
            )
        else:
            self._state.cycle_state = CycleState.IDLE
            self._save_state()

    async def _handle_hold(self):
        pair = self._state.pair
        self._nado_price = await self._nado.get_mark_price(pair)
        self._grvt_price = await self._grvt.get_mark_price(pair)

        if self._nado_price:
            self._cb.record_success("nado")
        else:
            self._cb.record_failure("nado")
        if self._grvt_price:
            self._cb.record_success("grvt")
        else:
            self._cb.record_failure("grvt")

        if self._cb.any_tripped():
            logger.critical("Circuit Breaker tripped!")
            await self._emergency_exit("circuit_breaker")
            return

        if self._nado_price and self._grvt_price:
            div_level = check_price_divergence(
                self._nado_price, self._grvt_price,
                self.cfg.PRICE_DIVERGENCE_WARN, self.cfg.PRICE_DIVERGENCE_EMERGENCY,
            )
            if div_level == "EMERGENCY":
                await self._emergency_exit("price_divergence")
                return
            if div_level == "WARNING" and time.time() - self._last_margin_warn > 1800:
                self._last_margin_warn = time.time()
                await self._telegram.send_alert(
                    f"[⚠️ DIVERGENCE] {self._nado_price:.1f} vs {self._grvt_price:.1f}"
                )

        nado_pnl = self._positions["nado"].calc_unrealized_pnl(self._nado_price) if "nado" in self._positions and self._nado_price else 0
        grvt_pnl = self._positions["grvt"].calc_unrealized_pnl(self._grvt_price) if "grvt" in self._positions and self._grvt_price else 0
        spread_mtm = nado_pnl + grvt_pnl

        mode_params = self.cfg.mode_params(self._state.mode.value)
        if should_exit_spread(spread_mtm, mode_params["spread_exit"], self.cfg.SPREAD_STOPLOSS):
            reason = "spread_profit" if spread_mtm > 0 else "spread_stoploss"
            self._state.cycle_state = CycleState.EXIT
            self._state._exit_reason = reason
            self._save_state()
            return

        hold_hours = (time.time() - self._state.entered_at) / 3600
        worst_margin = 100.0
        for pos in self._positions.values():
            price = self._nado_price if pos.exchange == "nado" else self._grvt_price
            if price:
                ratio = pos.calc_margin_ratio(price)
                worst_margin = min(worst_margin, ratio)

        if worst_margin <= self.cfg.MARGIN_WARNING_PCT and time.time() - self._last_margin_warn > 1800:
            self._last_margin_warn = time.time()
            await self._telegram.send_alert(f"[⚠️ MARGIN] {worst_margin:.1f}%")

        exit_reason = should_exit_cycle(
            hold_hours, mode_params["min_hold_hours"],
            self.cfg.MAX_HOLD_DAYS, worst_margin, self.cfg.MARGIN_EMERGENCY_PCT,
        )
        if exit_reason:
            self._state.cycle_state = CycleState.EXIT
            self._state._exit_reason = exit_reason
            self._save_state()
            return

        self._log_jsonl("spread_history.jsonl", {
            "pair": pair, "mode": self._state.mode.value,
            "nado_price": self._nado_price, "grvt_price": self._grvt_price,
            "nado_pnl": nado_pnl, "grvt_pnl": grvt_pnl, "spread_mtm": spread_mtm,
            "hold_hours": hold_hours, "margin": worst_margin,
        })

    async def _handle_exit(self):
        pair = self._state.pair
        exit_reason = getattr(self._state, "_exit_reason", "unknown")
        success = await self._execute_exit(pair)

        nado_pnl = self._positions["nado"].calc_unrealized_pnl(self._nado_price) if "nado" in self._positions and self._nado_price else 0
        grvt_pnl = self._positions["grvt"].calc_unrealized_pnl(self._grvt_price) if "grvt" in self._positions and self._grvt_price else 0

        notional = self._positions.get("nado", self._positions.get("grvt")).notional if self._positions else 0
        volume = notional * 2

        cycle = Cycle(
            cycle_id=self._state.cycle_id, pair=pair,
            direction=self._state.direction, notional=notional,
            entered_at=self._state.entered_at, exited_at=time.time(),
            entry_nado_price=self._positions.get("nado", Position("","","",0,0,0,0)).entry_price,
            entry_grvt_price=self._positions.get("grvt", Position("","","",0,0,0,0)).entry_price,
            exit_nado_price=self._nado_price or 0,
            exit_grvt_price=self._grvt_price or 0,
            funding_pnl=self._state.cumulative_funding,
            spread_pnl=nado_pnl + grvt_pnl,
            fee_cost=self._state.cumulative_fees,
            exit_reason=exit_reason,
            volume_generated=volume,
        )
        self._log_jsonl("cycles.jsonl", json.loads(cycle.to_jsonl()))
        self._cycle_history.append(cycle)
        self._earn.grvt_volume += volume
        self._earn.grvt_trades += 2  # open + close = 2 trades

        self._log_jsonl("volume_history.jsonl", {
            "grvt_volume": self._earn.grvt_volume,
            "grvt_trades": self._earn.grvt_trades,
            "days_left": self._earn.days_remaining(datetime.now(timezone.utc)),
            "pair": pair,
        })

        self._positions.clear()
        mode_params = self.cfg.mode_params(self._state.mode.value)
        self._state.cooldown_until = time.time() + mode_params["cooldown"]
        self._state.cycle_state = CycleState.COOLDOWN
        self._save_state()

        await self._telegram.send_alert(
            f"[EXIT] {pair} | {exit_reason} | "
            f"PnL: ${cycle.net_pnl:+.2f} | Vol: +${volume:,.0f}"
        )

        if len(self._cycle_history) >= 5:
            avg = sum(c.net_pnl for c in self._cycle_history[-5:]) / 5
            if avg < -3:
                self._state.mode = OperatingMode.HOLD
                await self._telegram.send_alert(
                    f"[⚠️ 적자 감지] 최근 5사이클 평균 ${avg:.1f}. HOLD 전환"
                )

    async def _handle_cooldown(self):
        if time.time() >= self._state.cooldown_until:
            self._state.cycle_state = CycleState.IDLE
            self._save_state()
```

- [ ] **Step 3: grvtnado.py — Part 3: 청크 진입/퇴출**

```python
    # --- 청크 진입/퇴출 (grvtnado.py에 이어서) ---

    async def _execute_enter(self, pair: str, direction: str, notional: float) -> bool:
        chunk_size = notional / self.cfg.ENTRY_CHUNKS
        nado_side = "BUY" if direction == "A" else "SELL"
        grvt_side = "SELL" if direction == "A" else "BUY"
        filled_notional = 0.0

        for i in range(self.cfg.ENTRY_CHUNKS):
            nado_price = await self._nado.get_mark_price(pair)
            grvt_price = await self._grvt.get_mark_price(pair)
            if not nado_price or not grvt_price:
                break

            nado_qty = chunk_size / nado_price
            grvt_qty = chunk_size / grvt_price

            slip = self.cfg.SLIPPAGE_PCT
            nado_order_price = nado_price * (1 + slip) if nado_side == "BUY" else nado_price * (1 - slip)
            grvt_order_price = grvt_price * (1 - slip) if grvt_side == "SELL" else grvt_price * (1 + slip)

            success = False
            for attempt in range(self.cfg.CHUNK_RETRY):
                nado_res, grvt_res = await asyncio.gather(
                    self._nado.place_limit_order(pair, nado_side, nado_qty, nado_order_price),
                    self._grvt.place_limit_order(pair, grvt_side, grvt_qty, grvt_order_price),
                )
                nado_ok = nado_res.status in ("filled", "matched")
                grvt_ok = grvt_res.status in ("filled", "closed")

                if nado_ok and grvt_ok:
                    filled_notional += chunk_size
                    success = True
                    break
                elif nado_ok and not grvt_ok:
                    logger.warning(f"Chunk {i+1}: GRVT failed, rolling back NADO")
                    await self._nado.close_position(pair, nado_side, nado_qty, self.cfg.EMERGENCY_SLIPPAGE_PCT)
                    await self._grvt.cancel_all_orders(pair)
                elif grvt_ok and not nado_ok:
                    logger.warning(f"Chunk {i+1}: NADO failed, rolling back GRVT")
                    await self._grvt.close_position(pair, grvt_side, grvt_qty, self.cfg.EMERGENCY_SLIPPAGE_PCT)
                    await self._nado.cancel_all_orders(pair)
                else:
                    await self._nado.cancel_all_orders(pair)
                    await self._grvt.cancel_all_orders(pair)

                if attempt < self.cfg.CHUNK_RETRY - 1:
                    await asyncio.sleep(5)

            if not success:
                logger.error(f"Chunk {i+1}/{self.cfg.ENTRY_CHUNKS} failed after retries")
                break

            if i < self.cfg.ENTRY_CHUNKS - 1:
                await asyncio.sleep(self.cfg.CHUNK_WAIT)

        if filled_notional > 0:
            margin = filled_notional / self.cfg.LEVERAGE
            nado_entry = await self._nado.get_mark_price(pair) or 0
            grvt_entry = await self._grvt.get_mark_price(pair) or 0
            self._positions["nado"] = Position(
                exchange="nado", symbol=pair,
                side="LONG" if direction == "A" else "SHORT",
                notional=filled_notional, entry_price=nado_entry,
                leverage=self.cfg.LEVERAGE, margin=margin,
            )
            self._positions["grvt"] = Position(
                exchange="grvt", symbol=pair,
                side="SHORT" if direction == "A" else "LONG",
                notional=filled_notional, entry_price=grvt_entry,
                leverage=self.cfg.LEVERAGE, margin=margin,
            )
            return True
        return False

    async def _execute_exit(self, pair: str) -> bool:
        for pos in self._positions.values():
            price = await (self._nado if pos.exchange == "nado" else self._grvt).get_mark_price(pair)
            size = pos.notional / (price or pos.entry_price)
            await (self._nado if pos.exchange == "nado" else self._grvt).close_position(
                pair, pos.side, size, self.cfg.EMERGENCY_SLIPPAGE_PCT,
            )
        await asyncio.sleep(5)
        nado_pos = await self._nado.get_positions(pair)
        grvt_pos = await self._grvt.get_positions(pair)
        return len(nado_pos) == 0 and len(grvt_pos) == 0

    async def _emergency_exit(self, reason: str):
        pair = self._state.pair
        logger.critical(f"EMERGENCY EXIT: {reason}")
        await self._nado.cancel_all_orders(pair)
        await self._grvt.cancel_all_orders(pair)
        await self._execute_exit(pair)
        self._positions.clear()
        self._state.cycle_state = CycleState.COOLDOWN
        self._state.cooldown_until = time.time() + 60
        self._save_state()
        await self._telegram.send_alert(f"[🚨 EMERGENCY EXIT] {reason}")
```

- [ ] **Step 4: grvtnado.py — Part 4: 메인 루프, Telegram 핸들러, 크래시 복구**

```python
    # --- 메인 루프 및 핸들러 (grvtnado.py에 이어서) ---

    def _check_earn_cycle(self):
        now = datetime.now(timezone.utc)
        if self._earn.is_cycle_expired(now):
            self._earn.reset()
            self._state.mode = OperatingMode.VOLUME
            self._save_state()
            asyncio.create_task(self._telegram.send_alert(
                f"[🔄 NEW CYCLE] {self._earn.cycle_start.date()} ~ {self._earn.cycle_end.date()}"
            ))

    def _determine_current_mode(self) -> str:
        now = datetime.now(timezone.utc)
        notional = calc_notional(
            self._state.nado_balance or 5000,
            self._state.grvt_balance or 5000,
            self.cfg.LEVERAGE, self.cfg.MARGIN_BUFFER,
        )
        daily_capacity = notional * 2 * 8  # 8 cycles/day max
        return determine_mode(
            volume_met=self._earn.is_volume_target_met(),
            trades_met=self._earn.is_trades_target_met(),
            days_left=self._earn.days_remaining(now),
            volume_remaining=max(0, self._earn.target_volume - self._earn.grvt_volume),
            daily_capacity=daily_capacity,
        )

    async def _recovery_check(self):
        pair = self._state.pair or self.cfg.PAIR_DEFAULT
        if self._state.cycle_state not in (CycleState.HOLD, CycleState.ENTER, CycleState.EXIT):
            return

        nado_pos = await self._nado.get_positions(pair)
        grvt_pos = await self._grvt.get_positions(pair)

        if nado_pos and grvt_pos:
            np = nado_pos[0]
            gp = grvt_pos[0]
            nado_entry = float(np.get("entry_price", 0))
            grvt_entry = float(gp.get("entry_price", 0))
            nado_size = float(np.get("size", np.get("amount", 0)))
            grvt_size = float(gp.get("size", gp.get("contracts", 0)))
            nado_side = "LONG" if nado_size > 0 else "SHORT"
            grvt_side = "LONG" if gp.get("side", "").upper() == "LONG" else "SHORT"
            notional = abs(nado_size) * nado_entry
            margin = notional / self.cfg.LEVERAGE

            self._positions["nado"] = Position(
                "nado", pair, nado_side, notional, nado_entry, self.cfg.LEVERAGE, margin,
            )
            self._positions["grvt"] = Position(
                "grvt", pair, grvt_side, notional, grvt_entry, self.cfg.LEVERAGE, margin,
            )
            self._state.cycle_state = CycleState.HOLD
            self._save_state()
            logger.info(f"Recovery: restored positions for {pair}")
            await self._telegram.send_alert(f"[RECOVERY] 포지션 복원 완료: {pair}")
        elif not nado_pos and not grvt_pos:
            self._state.cycle_state = CycleState.IDLE
            self._positions.clear()
            self._save_state()
            logger.info("Recovery: no positions found, resetting to IDLE")
        else:
            logger.warning("Recovery: one-sided position detected!")
            await self._telegram.send_alert("[⚠️ RECOVERY] 한쪽만 포지션 존재 — 수동 확인 필요")

    async def _register_telegram_handlers(self):
        async def on_status():
            mode = self._state.mode.value
            pair = self._state.pair
            boost = self._pair_mgr.get_boost(pair)
            nado_pnl = self._positions.get("nado", Position("","","",0,0,0,0)).calc_unrealized_pnl(self._nado_price or 0) if "nado" in self._positions else 0
            grvt_pnl = self._positions.get("grvt", Position("","","",0,0,0,0)).calc_unrealized_pnl(self._grvt_price or 0) if "grvt" in self._positions else 0
            spread = nado_pnl + grvt_pnl
            lines = [
                "📊 <b>Status</b>",
                "━━━━━━━━━━━━━━━",
                f"모드: {mode} | 페어: {pair} (N:{boost['nado']}x G:{boost['grvt']}x)",
                f"상태: {self._state.cycle_state.value}",
                f"NADO: ${nado_pnl:+.2f} | GRVT: ${grvt_pnl:+.2f}",
                f"스프레드 MTM: ${spread:+.2f}",
            ]
            await self._telegram.send_message("\n".join(lines))

        async def on_earn():
            now = datetime.now(timezone.utc)
            days = self._earn.days_remaining(now)
            prog = self._earn.volume_progress() * 100
            trades_ok = "✅" if self._earn.is_trades_target_met() else "❌"
            vol_ok = "✅" if self._earn.is_volume_target_met() else "⏳"
            lines = [
                "💰 <b>GRVT Earn</b>",
                "━━━━━━━━━━━━━━━",
                f"사이클: {self._earn.cycle_start.date()} ~ {self._earn.cycle_end.date()} ({days}일 남음)",
                f"{trades_ok} 거래: {self._earn.grvt_trades}/5건",
                f"{vol_ok} 볼륨: ${self._earn.grvt_volume:,.0f} / ${self._earn.target_volume:,.0f} ({prog:.0f}%)",
                f"모드: {self._state.mode.value}",
            ]
            await self._telegram.send_message("\n".join(lines))

        async def on_history():
            recent = self._cycle_history[-5:] if self._cycle_history else []
            if not recent:
                await self._telegram.send_message("📋 히스토리 없음")
                return
            lines = ["📋 <b>Recent Cycles</b>", "━━━━━━━━━━━━━━━"]
            for c in reversed(recent):
                lines.append(f"{c.pair} | {c.exit_reason} | ${c.net_pnl:+.2f} | Vol ${c.volume_generated:,.0f}")
            await self._telegram.send_message("\n".join(lines))

        async def on_funding():
            pair = self._state.pair
            nr = await self._nado.get_funding_rate(pair)
            gr = await self._grvt.get_funding_rate(pair)
            lines = [
                "📈 <b>Funding Rates</b>",
                "━━━━━━━━━━━━━━━",
                f"NADO (1h): {nr or 'N/A'}",
                f"GRVT (8h): {gr or 'N/A'}",
                f"누적 펀딩: ${self._state.cumulative_funding:+.2f}",
            ]
            await self._telegram.send_message("\n".join(lines))

        async def on_rebalance():
            if self._state.cycle_state == CycleState.HOLD:
                self._state.cycle_state = CycleState.EXIT
                self._state._exit_reason = "manual_rebalance"
                self._save_state()
                await self._telegram.send_alert("[🔄 REBALANCE] 수동 EXIT 트리거")
            else:
                await self._telegram.send_message("현재 HOLD 상태가 아닙니다")

        async def on_stop():
            self._running = False
            if self._state.cycle_state == CycleState.HOLD:
                await self._execute_exit(self._state.pair)
            await self._telegram.send_alert("[⏹ STOP] 봇 종료")
            Path(".stop_bot").touch()

        async def on_text(text: str):
            if text.startswith("/setboost"):
                args = text[len("/setboost"):].strip()
                if args.lower() == "clear":
                    self._pair_mgr.clear_boost()
                    await self._telegram.send_message("✅ 부스트 초기화 (모두 1x)")
                else:
                    self._pair_mgr.parse_boost_string(args.replace(" ", ","))
                    await self._telegram.send_message(f"✅ 부스트 설정: {args}")
                self._save_state()

        self._telegram.register_callback(BTN_STATUS, on_status)
        self._telegram.register_callback(BTN_EARN, on_earn)
        self._telegram.register_callback(BTN_HISTORY, on_history)
        self._telegram.register_callback(BTN_FUNDING, on_funding)
        self._telegram.register_callback(BTN_REBALANCE, on_rebalance)
        self._telegram.register_callback(BTN_STOP, on_stop)
        self._telegram.register_text_handler(on_text)

    async def _send_daily_report(self):
        today = datetime.now(KST).strftime("%Y-%m-%d")
        if today == self._last_daily_report:
            return
        now_kst = datetime.now(KST)
        if now_kst.hour != 9:
            return
        self._last_daily_report = today
        total_pnl = sum(c.net_pnl for c in self._cycle_history)
        vol = self._earn.grvt_volume
        days = self._earn.days_remaining(datetime.now(timezone.utc))
        lines = [
            f"📊 <b>Daily Report ({today})</b>",
            "━━━━━━━━━━━━━━━",
            f"NADO: ${self._state.nado_balance:,.0f} | GRVT: ${self._state.grvt_balance:,.0f}",
            f"누적 PnL: ${total_pnl:+.2f} ({len(self._cycle_history)}사이클)",
            "━━━━━━━━━━━━━━━",
            f"📈 볼륨: ${vol:,.0f} / ${self._earn.target_volume:,.0f}",
            f"모드: {self._state.mode.value} | 잔여: {days}일",
        ]
        await self._telegram.send_message("\n".join(lines))

    async def run(self):
        errors = self.cfg.validate()
        if errors:
            logger.error(f"Config errors: {errors}")
            return

        self._running = True
        await self._nado.connect()
        await self._grvt.connect()

        nado_pairs = await self._nado.get_available_pairs()
        grvt_pairs = await self._grvt.get_available_pairs()
        self._pair_mgr.set_available_pairs(nado_pairs, grvt_pairs)

        if self._state.boost_config:
            self._pair_mgr.load_boosts({"boosts": self._state.boost_config})

        boost_env = os.environ.get("BOOST_PAIRS", "")
        if boost_env:
            self._pair_mgr.parse_boost_string(boost_env)

        await self._nado.set_leverage(self._state.pair or self.cfg.PAIR_DEFAULT, self.cfg.LEVERAGE)

        await self._register_telegram_handlers()
        await self._telegram.send_message(
            f"[🚀 START] NADO×GRVT 봇 가동\n"
            f"페어: {self._state.pair} | 레버리지: {self.cfg.LEVERAGE}x\n"
            f"모드: {self._state.mode.value}"
        )

        await self._recovery_check()

        while self._running:
            try:
                await self._telegram.poll_updates()
                await self._run_state_machine()
                self._check_earn_cycle()

                now = time.time()
                if now - self._last_balance_check > self.cfg.POLL_BALANCE_SECONDS:
                    self._last_balance_check = now
                    self._state.nado_balance = await self._nado.get_balance()
                    self._state.grvt_balance = await self._grvt.get_balance()

                await self._send_daily_report()

            except Exception as e:
                logger.error(f"Main loop error: {e}", exc_info=True)

            await asyncio.sleep(self.cfg.POLL_INTERVAL)

        await self._nado.close()
        await self._grvt.close()
        await self._telegram.close()
```

- [ ] **Step 5: grvtnado.py 상단에 os import 추가**

`import os`를 import 블록에 추가.

- [ ] **Step 6: 커밋**

```bash
git add grvtnado.py
git commit -m "feat: grvtnado.py — 상태머신, 청크 진입/퇴출, Earn 관리, 텔레그램 UI, 크래시 복구"
```

---

## Task 12: run_bot.py — Watchdog 엔트리포인트

**Files:**
- Create: `run_bot.py`

- [ ] **Step 1: run_bot.py 구현**

```python
# run_bot.py
import sys
import time
import subprocess
import logging
from pathlib import Path
from logging.handlers import RotatingFileHandler

MAX_CRASHES = 10
CRASH_WINDOW = 300
STOP_FILE = ".stop_bot"
LOG_FILE = "logs/bot.log"
LOG_SIZE = 5_000_000
LOG_BACKUPS = 3

Path("logs").mkdir(exist_ok=True)

handler = RotatingFileHandler(LOG_FILE, maxBytes=LOG_SIZE, backupCount=LOG_BACKUPS)
handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
console = logging.StreamHandler()
console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s: %(message)s"))
logging.basicConfig(level=logging.INFO, handlers=[handler, console])
logger = logging.getLogger("watchdog")

stop_path = Path(STOP_FILE)
if stop_path.exists():
    stop_path.unlink()


def main():
    crashes: list[float] = []

    while True:
        if stop_path.exists():
            logger.info("Stop file detected, exiting")
            break

        logger.info("Starting bot process...")
        try:
            proc = subprocess.run(
                [sys.executable, "-u", "-c",
                 "import asyncio; from bot_core import DeltaNeutralBot; asyncio.run(DeltaNeutralBot().run())"],
                timeout=None,
            )
            if proc.returncode == 0:
                logger.info("Bot exited cleanly")
                if stop_path.exists():
                    break
        except KeyboardInterrupt:
            logger.info("Keyboard interrupt, exiting")
            break
        except Exception as e:
            logger.error(f"Bot crashed: {e}")

        now = time.time()
        crashes.append(now)
        crashes = [t for t in crashes if now - t < CRASH_WINDOW]

        if len(crashes) >= MAX_CRASHES:
            logger.critical(f"{MAX_CRASHES} crashes in {CRASH_WINDOW}s, permanent exit")
            break

        logger.info("Restarting in 5 seconds...")
        time.sleep(5)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: 커밋**

```bash
git add run_bot.py
git commit -m "feat: run_bot.py — Watchdog 엔트리포인트 (크래시 자동 재시작)"
```

---

## Task 13: 통합 테스트 & SDK 연동 검증

**Files:**
- Create: `tests/test_integration.py`

> SDK가 설치된 환경에서 실행. testnet에서 먼저 검증.

- [ ] **Step 1: SDK 설치 테스트**

```bash
cd D:\Codes\nado_grvt_bot && pip install nado-protocol grvt-pysdk
```

- [ ] **Step 2: SDK import 검증 스크립트**

```python
# tests/test_integration.py
import pytest


def test_nado_sdk_import():
    from nado_protocol.client import create_nado_client, NadoClientMode
    assert NadoClientMode.MAINNET is not None


def test_grvt_sdk_import():
    from grvt_pysdk import GrvtCcxtWS
    assert GrvtCcxtWS is not None


def test_nado_utils_import():
    from nado_protocol.utils import to_x18, gen_order_nonce, get_expiration_timestamp
    val = to_x18(95000.0)
    assert val > 0


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
```

- [ ] **Step 3: 테스트 실행**

```bash
cd D:\Codes\nado_grvt_bot && python -m pytest tests/test_integration.py -v
```

- [ ] **Step 4: SDK API 차이 발견 시 nado_client.py / grvt_client.py 수정**

SDK 소스를 읽어 실제 메서드명, import 경로, 응답 구조를 확인하고 클라이언트 코드 수정.

- [ ] **Step 5: 커밋**

```bash
git add tests/test_integration.py
git commit -m "test: SDK 연동 검증 및 통합 테스트"
```

---

## Task 14: .env 설정 및 최종 실행 테스트

- [ ] **Step 1: .env 파일 생성 (David 대감이 키 입력)**

```bash
cp .env.example .env
# David 대감이 직접 NADO_PRIVATE_KEY, GRVT_API_KEY 등 입력
```

- [ ] **Step 2: 전체 테스트 실행**

```bash
cd D:\Codes\nado_grvt_bot && python -m pytest tests/ -v
```

- [ ] **Step 3: 드라이런 (짧은 시간 실행 후 중단)**

```bash
cd D:\Codes\nado_grvt_bot && timeout 30 python -u run_bot.py
```

텔레그램에 `[🚀 START]` 메시지 수신 확인, 양쪽 거래소 연결 확인.

- [ ] **Step 4: 프로덕션 실행**

```bash
# Termux
termux-wake-lock
cd D:\Codes\nado_grvt_bot && python3 -u run_bot.py > logs/bot_live.log 2>&1 &
```

- [ ] **Step 5: 최종 커밋**

```bash
git add -A
git commit -m "chore: 최종 정리 — 테스트 통과, 실행 준비 완료"
```
