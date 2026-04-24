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
    "AAPL": "AAPL_USDT_Perp",
    "AMZN": "AMZN_USDT_Perp",
    "MSFT": "MSFT_USDT_Perp",
    "META": "META_USDT_Perp",
    "GOOGL": "GOOGL_USDT_Perp",
    "NVDA": "NVDA_USDT_Perp",
    "TSLA": "TSLA_USDT_Perp",
}


class GrvtClient(BaseExchangeClient):
    def __init__(self, api_key: str, private_key: str, trading_account_id: str):
        self._api_key = api_key
        self._private_key = private_key
        self._account_id = trading_account_id
        self._api = None
        self._ws_prices: dict[str, float] = {}
        self._ws_prices_ts: dict[str, float] = {}
        self._price_cache_ttl = 3.0

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
        import time
        cached = self._ws_prices.get(symbol)
        cached_ts = self._ws_prices_ts.get(symbol, 0)
        if cached and (time.time() - cached_ts) < self._price_cache_ttl:
            return cached
        try:
            grvt_sym = self._grvt_symbol(symbol)
            ticker = await self._retry(self._api.fetch_ticker, grvt_sym)
            if ticker:
                price = float(ticker.get("mark", ticker.get("last", 0)))
                self._ws_prices[symbol] = price
                self._ws_prices_ts[symbol] = time.time()
                return price
        except Exception as e:
            logger.error(f"GRVT get_mark_price: {e}")
        return None

    async def get_funding_rate(self, symbol: str) -> Optional[float]:
        try:
            grvt_sym = self._grvt_symbol(symbol)
            try:
                result = await self._retry(self._api.fetch_funding_rate, grvt_sym)
                if result:
                    rate = result.get("fundingRate")
                    if rate is not None:
                        return float(rate)
            except (AttributeError, Exception):
                pass
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
        try:
            grvt_sym = self._grvt_symbol(symbol)
            result = await self._retry(
                self._api.create_order,
                grvt_sym, "limit", close_side, size, close_price,
            )
            if result:
                status = result.get("status", "")
                return status in ("closed", "filled")
        except Exception as e:
            logger.error(f"GRVT close_position: {e}")
        return False

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
