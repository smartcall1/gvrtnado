# exchanges/grvt_client.py
import asyncio
import logging
import time
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
        self._ob_diag_done = False

    def _grvt_symbol(self, symbol: str) -> str:
        return SYMBOL_MAP.get(symbol.upper(), f"{symbol.upper()}_USDT_Perp")

    async def connect(self):
        try:
            from pysdk.grvt_ccxt_ws import GrvtCcxtWS
            from pysdk.grvt_ccxt_env import GrvtEnv

            loop = asyncio.get_running_loop()
            self._api = GrvtCcxtWS(
                env=GrvtEnv.PROD,
                loop=loop,
                parameters={
                    "private_key": self._private_key,
                    "api_key": self._api_key,
                    "trading_account_id": self._account_id,
                },
            )
            logger.info("GRVT connected")
        except ImportError:
            logger.error("grvt-pysdk not installed. Run: pip install grvt-pysdk")
            raise

    async def close(self):
        if self._api:
            try:
                session = getattr(self._api, '_session', None)
                if session and not session.closed:
                    await session.close()
            except Exception:
                pass
            self._api = None

    async def _retry(self, coro_fn, *args, max_retries=3, **kwargs):
        for attempt in range(max_retries):
            try:
                return await coro_fn(*args, **kwargs)
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
                usdt = result.get("USDT")
                if isinstance(usdt, dict):
                    return float(usdt.get("total", 0))
                total = result.get("total")
                if isinstance(total, dict):
                    return float(total.get("USDT", 0))
            return 0.0
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
        cached_ts = self._ws_prices_ts.get(symbol, 0)
        if cached and (time.time() - cached_ts) < self._price_cache_ttl:
            return cached
        try:
            grvt_sym = self._grvt_symbol(symbol)
            ticker = await self._retry(self._api.fetch_ticker, grvt_sym)
            if ticker:
                price = float(ticker.get("mark_price", ticker.get("last_price", 0)))
                self._ws_prices[symbol] = price
                self._ws_prices_ts[symbol] = time.time()
                return price
        except Exception as e:
            logger.error(f"GRVT get_mark_price: {e}")
        return None

    async def get_funding_rate(self, symbol: str) -> Optional[float]:
        try:
            grvt_sym = self._grvt_symbol(symbol)
            since_ns = int((time.time() - 86400) * 1e9)
            result = await self._retry(self._api.fetch_funding_rate_history, grvt_sym, since_ns, 1)
            if isinstance(result, dict):
                entries = result.get("result", [])
            else:
                entries = result if isinstance(result, list) else []
            if entries and len(entries) > 0:
                entry = entries[-1] if isinstance(entries, list) else entries
                rate = entry.get("funding_rate", entry.get("fundingRate"))
                if rate is not None:
                    return float(rate)
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
                {},
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
            base = symbol.upper()
            await self._retry(self._api.cancel_all_orders, {"kind": "PERPETUAL", "base": base})
            return True
        except Exception as e:
            logger.error(f"GRVT cancel_all_orders: {e}")
            return False

    async def get_available_pairs(self) -> list[str]:
        try:
            markets = await self._retry(self._api.fetch_all_markets)
            pairs = []
            for m in markets:
                sym = m.get("instrument", m.get("id", m.get("symbol", "")))
                if sym.endswith("_Perp"):
                    base = sym.split("_")[0]
                    pairs.append(base)
            logger.info(f"GRVT available pairs: {pairs}")
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
            if not isinstance(book, dict):
                return 0.0
            bids = book.get("bids", [])
            asks = book.get("asks", [])
            if not self._ob_diag_done and bids:
                self._ob_diag_done = True
                sample = bids[0]
                logger.info(f"[DIAG] GRVT orderbook format: {type(sample).__name__} = {sample}")
            if not bids and not asks:
                return 0.0
            bid_depth = sum(
                float(b["size"] if isinstance(b, dict) else b[1]) for b in bids
            )
            ask_depth = sum(
                float(a["size"] if isinstance(a, dict) else a[1]) for a in asks
            )
            mark = await self.get_mark_price(symbol) or 0
            return (bid_depth + ask_depth) * mark
        except Exception as e:
            logger.debug(f"GRVT get_orderbook_depth({symbol}): {e}")
        return 0.0
