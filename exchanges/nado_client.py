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
                    filled_size=float(result.get("filled_size", result.get("filled_amount", result.get("executed_qty", size)))),
                    filled_price=float(result.get("average_price", result.get("avg_price", result.get("fill_price", price)))),
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
