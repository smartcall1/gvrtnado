# exchanges/nado_client.py
import asyncio
import logging
from decimal import Decimal
from typing import Optional

from exchanges.base_client import BaseExchangeClient, OrderResult

logger = logging.getLogger(__name__)


def _patch_nado_eip712():
    try:
        import nado_protocol.contracts.eip712.sign as _sign_mod
        from eth_account.messages import encode_typed_data
        import inspect
        sig = inspect.signature(encode_typed_data)
        if "full_message" in sig.parameters and "domain_data" in sig.parameters:
            _orig = encode_typed_data
            def _compat(data):
                return _orig(full_message=data)
            _sign_mod.encode_typed_data = _compat
            logger.debug("Patched nado EIP-712 signing for eth_account 0.13+")
    except Exception:
        pass


class NadoClient(BaseExchangeClient):
    def __init__(self, private_key: str):
        self._private_key = private_key
        self._client = None
        self._subaccount_hex: str = ""
        self._symbol_map: dict[str, int] = {}
        self._increments: dict[int, dict] = {}

    async def connect(self):
        try:
            from nado_protocol.client import create_nado_client, NadoClientMode
            self._client = create_nado_client(NadoClientMode.MAINNET, self._private_key)
        except ImportError:
            logger.error("nado-protocol SDK not installed. Run: pip install nado-protocol")
            raise
        _patch_nado_eip712()
        from nado_protocol.utils.bytes32 import subaccount_to_hex
        self._subaccount_hex = subaccount_to_hex(
            self._client.context.signer.address, "default"
        )
        logger.info(f"NADO subaccount: {self._subaccount_hex}")
        await self._init_symbol_map()
        await self._init_increments()

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

    async def _init_increments(self):
        try:
            products = await asyncio.to_thread(self._client.market.get_all_products)
            if products and hasattr(products, "perp_products"):
                for p in products.perp_products:
                    self._increments[p.product_id] = {
                        "price_x18": int(p.book_info.price_increment_x18),
                        "size": int(p.book_info.size_increment),
                        "min_size": int(p.book_info.min_size),
                    }
                logger.info(f"NADO loaded increments for {len(self._increments)} perp products")
        except Exception as e:
            logger.warning(f"Failed to load NADO increments: {e}")

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
        isolated_margin: float = 0,
    ) -> OrderResult:
        try:
            from nado_protocol.utils.nonce import gen_order_nonce
            from nado_protocol.utils.expiration import get_expiration_timestamp, OrderType
            from nado_protocol.utils.order import build_appendix
            from nado_protocol.engine_client.types.execute import PlaceOrderParams
            from nado_protocol.utils.execute import OrderParams

            product_id = self._product_id(symbol)
            inc = self._increments.get(product_id, {})
            price_inc = inc.get("price_x18", 10**16)
            size_inc = inc.get("size", 10**16)

            price_x18 = int(Decimal(str(price)) * 10**18)
            price_x18 = price_x18 - price_x18 % price_inc

            amount_x18 = int(Decimal(str(size)) * 10**18)
            amount_x18 = amount_x18 - amount_x18 % size_inc
            if side.upper() != "BUY":
                amount_x18 = -amount_x18

            if isolated_margin > 0:
                margin_x18 = int(Decimal(str(isolated_margin)) * 10**18)
                margin_x18 = margin_x18 - margin_x18 % size_inc if size_inc else margin_x18
                appendix = build_appendix(OrderType.DEFAULT, isolated=True, isolated_margin=margin_x18)
            else:
                appendix = build_appendix(OrderType.DEFAULT, isolated=True)

            order = OrderParams(
                sender=self._subaccount_hex,
                amount=amount_x18,
                nonce=gen_order_nonce(),
                priceX18=price_x18,
                expiration=get_expiration_timestamp(300),
                appendix=appendix,
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
