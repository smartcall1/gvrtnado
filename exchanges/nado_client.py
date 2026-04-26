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
            products = await asyncio.to_thread(self._client.market.get_all_engine_markets)
            if products and hasattr(products, "perp_products"):
                for p in products.perp_products:
                    long_w = int(p.risk.long_weight_initial_x18) / 1e18
                    short_w = int(p.risk.short_weight_initial_x18) / 1e18
                    long_ml = 1 / (1 - long_w) if long_w < 1 else 100
                    short_ml = 1 / abs(short_w - 1) if abs(short_w - 1) > 0.001 else 100
                    max_lev = min(long_ml, short_ml)
                    self._increments[p.product_id] = {
                        "price_x18": int(p.book_info.price_increment_x18),
                        "size": int(p.book_info.size_increment),
                        "min_size": int(p.book_info.min_size),
                        "max_leverage": max_lev,
                    }
                logger.info(f"NADO loaded increments for {len(self._increments)} perp products")
                lev_summary = {
                    sym: f"{self._increments[pid]['max_leverage']:.1f}x"
                    for sym, pid in self._symbol_map.items()
                    if pid in self._increments
                }
                logger.info(f"NADO max leverage: {lev_summary}")
        except Exception as e:
            logger.warning(f"Failed to load NADO increments: {e}")

    def get_max_leverage(self, symbol: str) -> float:
        pid = self._symbol_map.get(symbol.upper())
        if pid and pid in self._increments:
            return self._increments[pid].get("max_leverage", 5)
        return 5

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
                amount = int(pb.balance.amount) / 1e18
                if abs(amount) > 0:
                    # C4 fix: vQuoteBalance에서 entry_price 추출 (기존: 항상 0)
                    entry_price = 0.0
                    try:
                        vqb = getattr(pb.balance, 'v_quote_balance', None)
                        if vqb is None:
                            vqb = getattr(pb.balance, 'vQuoteBalance', None)
                        if vqb is not None and abs(amount) > 0:
                            entry_price = abs(int(vqb) / 1e18 / amount)
                    except Exception:
                        pass
                    positions.append({
                        "side": "LONG" if amount > 0 else "SHORT",
                        "size": abs(amount),
                        "entry_price": entry_price,
                        "notional": abs(amount) * entry_price if entry_price > 0 else 0,
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

            def _build_order(appx):
                return OrderParams(
                    sender=self._subaccount_hex,
                    amount=amount_x18,
                    nonce=gen_order_nonce(),
                    priceX18=price_x18,
                    expiration=get_expiration_timestamp(300),
                    appendix=appx,
                )

            def _build_params(order_obj):
                return PlaceOrderParams(
                    id=None, product_id=product_id, order=order_obj,
                    digest=None, signature=None, spot_leverage=None,
                )

            appendix = build_appendix(OrderType.DEFAULT)
            try:
                result = await asyncio.to_thread(
                    self._client.market.place_order, _build_params(_build_order(appendix))
                )
            except Exception as cross_err:
                if "2122" in str(cross_err):
                    logger.info(f"NADO {symbol}: isolated-only market, retrying with isolated mode")
                    result = None
                    for _margin_mult in [1, 2, 3]:
                        try:
                            if isolated_margin > 0:
                                actual_margin = isolated_margin * _margin_mult
                                margin_x6 = int(Decimal(str(actual_margin)) * 10**6)
                                appendix = build_appendix(OrderType.DEFAULT, isolated=True, isolated_margin=margin_x6)
                            else:
                                appendix = build_appendix(OrderType.DEFAULT, isolated=True)
                                if _margin_mult > 1:
                                    break
                            result = await asyncio.to_thread(
                                self._client.market.place_order, _build_params(_build_order(appendix))
                            )
                            if _margin_mult > 1:
                                logger.info(f"NADO {symbol}: succeeded with {_margin_mult}x margin")
                            break
                        except Exception as iso_err:
                            iso_err_str = str(iso_err)
                            if "2070" in iso_err_str:
                                logger.error(f"NADO {symbol}: max open interest reached (2070)")
                                raise
                            if "2006" in iso_err_str and _margin_mult < 3:
                                logger.warning(f"NADO {symbol}: margin {_margin_mult}x insufficient (2006), trying {_margin_mult+1}x")
                                continue
                            raise
                else:
                    raise

            if result:
                status_str = str(result.status).lower()
                digest = ""
                if result.data and hasattr(result.data, 'digest'):
                    digest = str(result.data.digest)
                # C1 fix: mark price 기반 실제 체결가 추정 (주문가는 슬리피지 포함이라 부정확)
                actual_price = price
                try:
                    mark = await self.get_mark_price(symbol)
                    if mark and mark > 0:
                        actual_price = mark
                except Exception:
                    pass
                return OrderResult(
                    order_id=digest,
                    status="filled" if "success" in status_str else status_str,
                    filled_size=size,
                    filled_price=actual_price,
                )
        except Exception as e:
            err_str = str(e)
            logger.error(f"NADO place_limit_order: {err_str}")
            msg = "nado_health" if "2006" in err_str else "nado_max_oi" if "2070" in err_str else "order failed"
            return OrderResult(order_id="", status="error", message=msg)
        return OrderResult(order_id="", status="error", message="order failed")

    async def close_position(
        self, symbol: str, side: str, size: float, slippage_pct: float = 0.01,
    ) -> bool:
        # C2 fix: 반대 주문으로 부분 청산 지원 (기존은 size 무시하고 전량 청산)
        try:
            mark = await self.get_mark_price(symbol)
            if not mark:
                return await self._close_all(symbol)
            close_side = "SELL" if side.upper() == "LONG" else "BUY"
            if close_side == "BUY":
                close_price = mark * (1 + slippage_pct)
            else:
                close_price = mark * (1 - slippage_pct)
            res = await self.place_limit_order(symbol, close_side, size, close_price)
            if res.status in ("filled", "matched"):
                return True
            logger.warning(f"NADO partial close failed (status={res.status}), trying full close")
            return await self._close_all(symbol)
        except Exception as e:
            logger.error(f"NADO close_position: {e}")
            return await self._close_all(symbol)

    async def _close_all(self, symbol: str) -> bool:
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
            logger.error(f"NADO _close_all: {e}")
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
