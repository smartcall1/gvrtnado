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

        # max_open_interest_x18 추가 캐시 — get_symbols에서 가져옴 (PerpProduct에는 없음)
        try:
            pids = list(self._symbol_map.values())
            if not pids:
                return
            symbols_data = await asyncio.to_thread(
                self._client.context.engine_client.get_symbols,
                product_type="perp", product_ids=pids,
            )
            if symbols_data and hasattr(symbols_data, "symbols"):
                count_with_cap = 0
                for _key, s in symbols_data.symbols.items():
                    pid = int(s.product_id)
                    if pid not in self._increments:
                        continue
                    if s.max_open_interest_x18:
                        max_oi = int(s.max_open_interest_x18) / 1e18
                        self._increments[pid]["max_oi"] = max_oi
                        count_with_cap += 1
                logger.info(f"NADO max_oi cached for {count_with_cap}/{len(self._increments)} products")
        except Exception as e:
            logger.warning(f"Failed to cache NADO max_oi: {e}")

    async def get_open_interest_capacity(self, symbol: str) -> tuple[float, float, float]:
        """현재 OI, 최대 OI, 가용 capacity (코인 수량 단위) 반환.

        - max_oi가 캐시에 없거나 0이면 (-1, -1, inf) — cap 무제한으로 간주
        - 조회 실패 시 (-1, max_oi, 0) — 안전 차단 (사전 진입 시도 안 함)
        """
        pid = self._symbol_map.get(symbol.upper())
        if pid is None:
            return (-1.0, -1.0, 0.0)

        max_oi = self._increments.get(pid, {}).get("max_oi")
        if not max_oi or max_oi <= 0:
            return (-1.0, -1.0, float("inf"))  # cap 정보 없음 → 무제한 가정

        try:
            products = await asyncio.to_thread(self._client.market.get_all_engine_markets)
            if products and hasattr(products, "perp_products"):
                for p in products.perp_products:
                    if p.product_id != pid:
                        continue
                    if p.state and p.state.open_interest:
                        current_oi = int(p.state.open_interest) / 1e18
                        available = max(0.0, max_oi - current_oi)
                        return (current_oi, max_oi, available)
        except Exception as e:
            logger.error(f"NADO get_open_interest_capacity({symbol}): {e}")
        return (-1.0, max_oi, 0.0)

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
        # healths schema: [0]=initial_weighted, [1]=maintenance_weighted, [2]=unweighted.
        # unweighted.health = collateral + 모든 perp 미실현 PnL → 진짜 equity
        try:
            info = await asyncio.to_thread(
                self._client.subaccount.get_engine_subaccount_summary,
                self._subaccount_hex,
            )
            if info and info.healths and len(info.healths) >= 3:
                return int(info.healths[2].health) / 1e18
            if info and info.healths:
                return int(info.healths[0].assets) / 1e18  # legacy fallback
        except Exception as e:
            logger.error(f"NADO get_balance: {e}")
        return 0.0

    def _parse_positions(self, info, product_id: int) -> list[dict]:
        positions = []
        for pb in info.perp_balances:
            if pb.product_id != product_id:
                continue
            amount = int(pb.balance.amount) / 1e18
            if abs(amount) > 0:
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

    async def get_positions(self, symbol: str) -> list[dict]:
        try:
            product_id = self._product_id(symbol)
            info = await asyncio.to_thread(
                self._client.subaccount.get_engine_subaccount_summary,
                self._subaccount_hex,
            )
            if not info:
                return []
            return self._parse_positions(info, product_id)
        except Exception as e:
            logger.error(f"NADO get_positions: {e}")
        return []

    async def get_positions_strict(self, symbol: str) -> Optional[list[dict]]:
        """None = API failure, [] = genuine empty."""
        try:
            product_id = self._product_id(symbol)
            info = await asyncio.to_thread(
                self._client.subaccount.get_engine_subaccount_summary,
                self._subaccount_hex,
            )
            if not info:
                return []
            return self._parse_positions(info, product_id)
        except Exception as e:
            logger.error(f"NADO get_positions_strict FAILED (returning None): {e}")
            return None

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

    async def get_bbo(self, symbol: str) -> dict:
        try:
            product_id = self._product_id(symbol)
            data = await asyncio.to_thread(
                self._client.market.get_latest_market_price, product_id
            )
            if data:
                bid = int(data.bid_x18) / 1e18
                ask = int(data.ask_x18) / 1e18
                return {"bid": bid, "ask": ask, "mark": (bid + ask) / 2}
        except Exception as e:
            logger.error(f"NADO get_bbo: {e}")
        return {"bid": 0.0, "ask": 0.0, "mark": 0.0}

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
        isolated_margin: float = 0, post_only: bool = False,
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

            order_type = OrderType.POST_ONLY if post_only else OrderType.DEFAULT
            appendix = build_appendix(order_type)
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
                                appendix = build_appendix(order_type, isolated=True, isolated_margin=margin_x6)
                            else:
                                appendix = build_appendix(order_type, isolated=True)
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
                # entry_price = mark (체결 시점). limit은 슬리피지 포함이라 spread_mtm 과장 표시함.
                # 실제 체결가는 mark에 가까움. 진짜 슬리피지 비용은 real_pnl(잔고 기반)에서 자동 반영.
                actual_price = price  # fallback: limit
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
