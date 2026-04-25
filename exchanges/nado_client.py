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
        self._symbol_map: dict[str, int] = {}

    async def connect(self):
        try:
            from nado_protocol.client import create_nado_client, NadoClientMode
            self._client = create_nado_client(NadoClientMode.MAINNET, self._private_key)
        except ImportError:
            logger.error("nado-protocol SDK not installed. Run: pip install nado-protocol")
            raise
        await self._init_symbol_map()

    async def close(self):
        self._client = None

    async def _init_symbol_map(self):
        try:
            result = await asyncio.to_thread(self._client.market.get_all_product_symbols)
            if result:
                data = result.data if hasattr(result, 'data') else result
                if isinstance(data, list):
                    for item in data:
                        if hasattr(item, 'product_id') and hasattr(item, 'symbol'):
                            name = item.symbol.split("-")[0].upper()
                            self._symbol_map[name] = item.product_id
                        elif isinstance(item, dict):
                            pid = item.get("product_id")
                            sym = item.get("symbol", "")
                            name = sym.split("-")[0].upper() if "-" in sym else sym.upper()
                            if pid is not None:
                                self._symbol_map[name] = pid
                elif isinstance(data, dict):
                    for sym, pid in data.items():
                        name = sym.split("-")[0].upper() if "-" in sym else sym.upper()
                        self._symbol_map[name] = pid
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
        try:
            result = await asyncio.to_thread(
                self._client.perp.get_subaccount_summary,
                self._client.context.signer_subaccount,
            )
            if result:
                data = result.data if hasattr(result, 'data') else result
                if hasattr(data, 'balance'):
                    return float(data.balance) / 1e18 if float(data.balance) > 1e15 else float(data.balance)
                if isinstance(data, dict):
                    for key in ("equity", "balance", "available_balance", "collateral"):
                        if key in data:
                            val = float(data[key])
                            return val / 1e18 if val > 1e15 else val
        except Exception as e:
            logger.error(f"NADO get_balance: {e}")
        return 0.0

    async def get_positions(self, symbol: str) -> list[dict]:
        try:
            product_id = self._product_id(symbol)
            result = await asyncio.to_thread(
                self._client.perp.get_subaccount_summary,
                self._client.context.signer_subaccount,
            )
            if result:
                data = result.data if hasattr(result, 'data') else result
                perps = getattr(data, 'perp_balances', None) or (data.get('perp_balances') if isinstance(data, dict) else None)
                if perps:
                    positions = []
                    for p in perps:
                        pid = getattr(p, 'product_id', None) or (p.get('product_id') if isinstance(p, dict) else None)
                        if pid != product_id:
                            continue
                        amount = float(getattr(p, 'amount', 0) or (p.get('amount', 0) if isinstance(p, dict) else 0))
                        if amount > 1e15:
                            amount = amount / 1e18
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
        try:
            product_id = self._product_id(symbol)
            result = await asyncio.to_thread(self._client.market.get_latest_market_price, product_id)
            if result:
                data = result.data if hasattr(result, 'data') else result
                price = None
                if hasattr(data, 'bid'):
                    bid = float(data.bid)
                    ask = float(getattr(data, 'ask', bid))
                    price = (bid + ask) / 2
                elif isinstance(data, dict):
                    for key in ("mark_price", "bid", "price"):
                        val = data.get(key)
                        if val:
                            price = float(val)
                            break
                if price and price > 1e15:
                    price = price / 1e18
                return price
        except Exception as e:
            logger.error(f"NADO get_mark_price: {e}")
        return None

    async def get_funding_rate(self, symbol: str) -> Optional[float]:
        try:
            product_id = self._product_id(symbol)
            result = await asyncio.to_thread(self._client.market.get_perp_funding_rate, product_id)
            if result:
                data = result.data if hasattr(result, 'data') else result
                if hasattr(data, 'funding_rate'):
                    return float(data.funding_rate)
                if isinstance(data, dict):
                    rate = data.get("funding_rate", data.get("rate"))
                    if rate is not None:
                        return float(rate)
        except Exception as e:
            logger.error(f"NADO get_funding_rate: {e}")
        return None

    async def place_limit_order(
        self, symbol: str, side: str, size: float, price: float,
    ) -> OrderResult:
        try:
            from nado_protocol.utils.backend import to_x18
            from nado_protocol.utils.nonce import gen_order_nonce
            from nado_protocol.utils.expiration import get_expiration_timestamp
            from nado_protocol.engine_client.types.execute import PlaceOrderParams
            from nado_protocol.utils.execute import OrderParams

            product_id = self._product_id(symbol)
            amount = int(to_x18(size)) if side.upper() == "BUY" else -int(to_x18(size))

            order = OrderParams(
                sender=self._client.context.signer_subaccount,
                amount=amount,
                nonce=gen_order_nonce(),
                priceX18=int(to_x18(price)),
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

            result = await asyncio.to_thread(self._client.market.place_order, params)
            if result:
                data = result.data if hasattr(result, 'data') else result
                status_val = getattr(result, 'status', None) or (data.get('status') if isinstance(data, dict) else None)
                status_str = str(status_val).lower() if status_val else ""
                return OrderResult(
                    order_id=str(getattr(data, 'digest', '') if hasattr(data, 'digest') else (data.get('digest', '') if isinstance(data, dict) else '')),
                    status="filled" if "success" in status_str or "filled" in status_str else status_str,
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
                self._client.context.signer_subaccount,
                product_id,
            )
            if result:
                status_val = getattr(result, 'status', None)
                return "success" in str(status_val).lower() if status_val else False
        except Exception as e:
            logger.error(f"NADO close_position: {e}")
        return False

    async def cancel_all_orders(self, symbol: str) -> bool:
        try:
            from nado_protocol.engine_client.types.execute import CancelProductOrdersParams
            product_id = self._product_id(symbol)
            params = CancelProductOrdersParams(
                sender=self._client.context.signer_subaccount,
                product_ids=[product_id],
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
        try:
            product_id = self._product_id(symbol)
            result = await asyncio.to_thread(self._client.market.get_market_liquidity, product_id, 10)
            if result:
                data = result.data if hasattr(result, 'data') else result
                bids = getattr(data, 'bids', []) if hasattr(data, 'bids') else (data.get('bids', []) if isinstance(data, dict) else [])
                asks = getattr(data, 'asks', []) if hasattr(data, 'asks') else (data.get('asks', []) if isinstance(data, dict) else [])
                bid_depth = sum(abs(float(getattr(b, 'size', 0) if hasattr(b, 'size') else (b[1] if isinstance(b, (list, tuple)) else b.get('size', 0)))) for b in bids)
                ask_depth = sum(abs(float(getattr(a, 'size', 0) if hasattr(a, 'size') else (a[1] if isinstance(a, (list, tuple)) else a.get('size', 0)))) for a in asks)
                mark = await self.get_mark_price(symbol) or 0
                return (bid_depth + ask_depth) * mark
        except Exception as e:
            logger.error(f"NADO get_orderbook_depth: {e}")
        return 0.0
