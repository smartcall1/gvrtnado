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
        # 즉시 0 리셋 대신 1씩 감소 — 간헐적 503에서 단 1회 성공으로 CB가 클리어되어
        # HOLD_SUSPENDED ↔ HOLD 플랩 루프 방지. 연속 성공해야 완전히 클리어됨.
        current = self._fails.get(exchange, 0)
        self._fails[exchange] = max(0, current - 1)

    def is_tripped(self, exchange: str) -> bool:
        return self._fails.get(exchange, 0) >= self._max_fails

    def any_tripped(self) -> bool:
        return any(v >= self._max_fails for v in self._fails.values())
