# exchanges/nado_client.py
import asyncio
import logging
from typing import Optional

from exchanges.base_client import BaseExchangeClient, OrderResult

logger = logging.getLogger(__name__)


class NadoClient(BaseExchangeClient):
    def __init__(self, private_key: str):
        self._private_key = private_key
        self._client = None
        self._subaccount_hex: str = ""
        self._symbol_map: dict[str, int] = {}

    async def connect(self):
        try:
            from nado_protocol.client import create_nado_client, NadoClientMode
            self._client = create_nado_client(NadoClientMode.MAINNET, self._private_key)
        except ImportError:
            logger.error("nado-protocol SDK not installed. Run: pip install nado-protocol")
            raise
        from nado_protocol.utils.bytes32 import subaccount_to_hex
        self._subaccount_hex = subaccount_to_hex(
            self._client.context.signer.address, "default"
        )
        logger.info(f"NADO subaccount: {self._subaccount_hex}")
        await self._init_symbol_map()

    async def close(self):
        self._client = None

    async def _init_symbol_map(self):
        try:
            # ProductSymbolsData = list[ProductSymbol(product_id, symbol)]
            result = await asyncio.to_thread(self._client.market.get_all_product_symbols)
            if isinstance(result, list):
                for item in result:
                    name = item.symbol.split("-")[0].upper()
                    self._symbol_map[name] = item.product_id
            if self._symbol_map:
                logger.info(f"NADO symbol map: {self._symbol_map}")
            else:
                raise ValueError("Empty symbol map")
        except Exception as e:
            logger.warning(f"Failed to init NADO symbol map: {e}, using defaults")
            self._symbol_map = {"BTC": 1, "ETH": 2, "SOL": 3}

    def _product_id(self, symbol: str) -> int:
        pid = self._symbol_map.get(symbol.upper())
        if pid is None:
            raise ValueError(f"Unknown NADO symbol: {symbol}")
        return pid

    async def get_balance(self) -> float:
        # SubaccountInfoData.healths[0].assets is x18 string
        try:
            info = await asyncio.to_thread(
                self._client.subaccount.get_engine_subaccount_summary,
                self._subaccount_hex,
            )
            if info and info.healths:
                return int(info.healths[0].assets) / 1e18
        except Exception as e:
            logger.error(f"NADO get_balance: {e}")
        return 0.0

    async def get_positions(self, symbol: str) -> list[dict]:
        try:
            product_id = self._product_id(symbol)
            info = await asyncio.to_thread(
                self._client.subaccount.get_engine_subaccount_summary,
                self._subaccount_hex,
            )
            if not info:
                return []
            positions = []
            for pb in info.perp_balances:
                if pb.product_id != product_id:
                    continue
                # PerpProductBalance.balance.amount is x18 string
                amount = int(pb.balance.amount) / 1e18
                if abs(amount) > 0:
                    positions.append({
                        "side": "LONG" if amount > 0 else "SHORT",
                        "size": abs(amount),
                        "entry_price": 0,
                        "notional": 0,
                    })
            return positions
        except Exception as e:
            logger.error(f"NADO get_positions: {e}")
        return []

    async def get_mark_price(self, symbol: str) -> Optional[float]:
        # MarketPriceData: bid_x18, ask_x18 (x18 strings)
        try:
            product_id = self._product_id(symbol)
            data = await asyncio.to_thread(
                self._client.market.get_latest_market_price, product_id
            )
            if data:
                bid = int(data.bid_x18) / 1e18
                ask = int(data.ask_x18) / 1e18
                return (bid + ask) / 2
        except Exception as e:
            logger.error(f"NADO get_mark_price: {e}")
        return None

    async def get_funding_rate(self, symbol: str) -> Optional[float]:
        # IndexerFundingRateData: funding_rate_x18 (x18 string)
        try:
            product_id = self._product_id(symbol)
            data = await asyncio.to_thread(
                self._client.market.get_perp_funding_rate, product_id
            )
            if data:
                return int(data.funding_rate_x18) / 1e18
        except Exception as e:
            logger.error(f"NADO get_funding_rate: {e}")
        return None

    async def place_limit_order(
        self, symbol: str, side: str, size: float, price: float,
    ) -> OrderResult:
        try:
            from nado_protocol.utils.nonce import gen_order_nonce
            from nado_protocol.utils.expiration import get_expiration_timestamp
            from nado_protocol.engine_client.types.execute import PlaceOrderParams
            from nado_protocol.utils.execute import OrderParams

            product_id = self._product_id(symbol)
            amount_x18 = int(size * 1e18)
            if side.upper() != "BUY":
                amount_x18 = -amount_x18

            order = OrderParams(
                sender=self._subaccount_hex,
                amount=amount_x18,
                nonce=gen_order_nonce(),
                priceX18=int(price * 1e18),
                expiration=get_expiration_timestamp(),
                appendix=0,
            )

            params = PlaceOrderParams(
                id=None,
                product_id=product_id,
                order=order,
                digest=None,
                signature=None,
                spot_leverage=None,
            )

            # ExecuteResponse: status (ResponseStatus), data, error
            result = await asyncio.to_thread(self._client.market.place_order, params)
            if result:
                status_str = str(result.status).lower()
                digest = ""
                if result.data and hasattr(result.data, 'digest'):
                    digest = str(result.data.digest)
                return OrderResult(
                    order_id=digest,
                    status="filled" if "success" in status_str else status_str,
                    filled_size=size,
                    filled_price=price,
                )
        except Exception as e:
            logger.error(f"NADO place_limit_order: {e}")
        return OrderResult(order_id="", status="error", message="order failed")

    async def close_position(
        self, symbol: str, side: str, size: float, slippage_pct: float = 0.01,
    ) -> bool:
        try:
            product_id = self._product_id(symbol)
            result = await asyncio.to_thread(
                self._client.market.close_position,
                self._subaccount_hex,
                product_id,
            )
            if result:
                return "success" in str(result.status).lower()
        except Exception as e:
            logger.error(f"NADO close_position: {e}")
        return False

    async def cancel_all_orders(self, symbol: str) -> bool:
        try:
            from nado_protocol.engine_client.types.execute import CancelProductOrdersParams
            product_id = self._product_id(symbol)
            # CancelProductOrdersParams: sender, productIds (camelCase!), nonce, signature, digest
            params = CancelProductOrdersParams(
                sender=self._subaccount_hex,
                productIds=[product_id],
                nonce=None,
                signature=None,
                digest=None,
            )
            result = await asyncio.to_thread(self._client.market.cancel_product_orders, params)
            return result is not None
        except Exception as e:
            logger.error(f"NADO cancel_all_orders: {e}")
            return False

    async def get_available_pairs(self) -> list[str]:
        return list(self._symbol_map.keys())

    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        logger.info(f"NADO leverage is set per subaccount, not per API call. Requested: {leverage}x")
        return True

    async def get_orderbook_depth(self, symbol: str) -> float:
        # MarketLiquidityData: bids/asks = list[MarketLiquidity] where MarketLiquidity = [price_x18, size_x18]
        try:
            product_id = self._product_id(symbol)
            data = await asyncio.to_thread(
                self._client.market.get_market_liquidity, product_id, 10
            )
            if data:
                bid_depth = sum(abs(int(b[1])) / 1e18 for b in data.bids)
                ask_depth = sum(abs(int(a[1])) / 1e18 for a in data.asks)
                mark = await self.get_mark_price(symbol) or 0
                return (bid_depth + ask_depth) * mark
        except Exception as e:
            logger.debug(f"NADO get_orderbook_depth({symbol}): {e}")
        return 0.0
