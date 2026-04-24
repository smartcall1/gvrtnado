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
