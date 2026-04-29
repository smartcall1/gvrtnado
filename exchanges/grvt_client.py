# exchanges/grvt_client.py
import asyncio
import logging
import math
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
            await self._api.initialize()
            logger.info(f"GRVT connected, {len(self._api.markets)} markets loaded")
        except ImportError:
            logger.error("grvt-pysdk not installed. Run: pip install grvt-pysdk")
            raise

    async def close(self):
        if self._api:
            try:
                # L1 fix: WS 채널 + HTTP 세션 모두 정리 (기존: _session만 시도)
                for ws_type in ['mdg', 'tdg', 'mdg_rpc_full', 'tdg_rpc_full']:
                    ws = getattr(self._api, 'ws', {})
                    if isinstance(ws, dict):
                        conn = ws.get(ws_type)
                    else:
                        conn = None
                    if conn:
                        try:
                            await conn.close()
                        except Exception:
                            pass
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
        # fetch_balance.total = wallet only (포지션 미실현 PnL 미포함).
        # 정확한 equity = wallet + Σ(open positions.unrealized_pnl)
        try:
            result = await self._retry(self._api.fetch_balance)
            wallet = 0.0
            if isinstance(result, dict):
                usdt = result.get("USDT")
                if isinstance(usdt, dict):
                    wallet = float(usdt.get("total", 0))
                else:
                    total = result.get("total")
                    if isinstance(total, dict):
                        wallet = float(total.get("USDT", 0))
            upnl = 0.0
            try:
                poss = await self._retry(self._api.fetch_positions)
                for p in poss or []:
                    # GRVT raw 응답은 snake_case 'unrealized_pnl' 를 최상위에 둠
                    v = p.get("unrealizedPnl")
                    if v is None:
                        v = p.get("unrealized_pnl")
                    if v is None:
                        info = p.get("info") or {}
                        if isinstance(info, dict):
                            v = info.get("unrealized_pnl")
                    if v not in (None, ""):
                        upnl += float(v)
            except Exception as e:
                logger.warning(f"GRVT get_balance upnl fetch: {e}")
            return wallet + upnl
        except Exception as e:
            logger.error(f"GRVT get_balance: {e}")
            return 0.0

    async def get_cumulative_fees(self) -> float:
        # 모든 미체결 포지션의 cumulative_fee 합 — 진입 + 부분청산 등 실제 누적 수수료.
        # config의 maker BPS 추정값과 다를 수 있음 (실제 fill이 taker일 가능성).
        try:
            poss = await self._retry(self._api.fetch_positions)
            total = 0.0
            for p in poss or []:
                v = p.get("cumulative_fee")
                if v in (None, ""):
                    info = p.get("info") or {}
                    if isinstance(info, dict):
                        v = info.get("cumulative_fee")
                if v not in (None, ""):
                    total += float(v)
            return total
        except Exception as e:
            logger.warning(f"GRVT get_cumulative_fees: {e}")
            return 0.0

    def _parse_positions(self, result: list) -> list[dict]:
        positions = []
        for p in result:
            size_raw = p.get("size", p.get("contracts", p.get("contractSize", 0)))
            size_signed = float(size_raw) if size_raw not in (None, "") else 0.0
            if abs(size_signed) <= 0:
                continue
            side = (p.get("side") or "").upper()
            if not side:
                side = "LONG" if size_signed > 0 else "SHORT"
            entry = p.get("entry_price", p.get("entryPrice", 0))
            positions.append({
                "side": side,
                "size": abs(size_signed),
                "entry_price": float(entry) if entry not in (None, "") else 0.0,
                "notional": abs(float(p.get("notional", 0) or 0)),
            })
        return positions

    async def get_positions(self, symbol: str) -> list[dict]:
        # GRVT raw response uses 'size' (signed), 'entry_price', 'notional' — NOT
        # CCXT-translated 'contracts'/'entryPrice'. Parse raw fields with CCXT fallback.
        try:
            grvt_sym = self._grvt_symbol(symbol)
            result = await self._retry(self._api.fetch_positions, [grvt_sym])
            if not result:
                return []
            return self._parse_positions(result)
        except Exception as e:
            logger.error(f"GRVT get_positions: {e}")
        return []

    async def get_positions_strict(self, symbol: str) -> Optional[list[dict]]:
        """None = API failure, [] = genuine empty."""
        try:
            grvt_sym = self._grvt_symbol(symbol)
            result = await self._retry(self._api.fetch_positions, [grvt_sym])
            if not result:
                return []
            return self._parse_positions(result)
        except Exception as e:
            logger.error(f"GRVT get_positions_strict FAILED (returning None): {e}")
            return None

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

    async def get_bbo(self, symbol: str) -> dict:
        try:
            grvt_sym = self._grvt_symbol(symbol)
            book = await self._retry(self._api.fetch_order_book, grvt_sym, 1)
            if isinstance(book, dict):
                bids = book.get("bids", [])
                asks = book.get("asks", [])
                bid = float(bids[0]["price"] if isinstance(bids[0], dict) else bids[0][0]) if bids else 0.0
                ask = float(asks[0]["price"] if isinstance(asks[0], dict) else asks[0][0]) if asks else 0.0
                mark = (bid + ask) / 2 if bid > 0 and ask > 0 else await self.get_mark_price(symbol) or 0.0
                return {"bid": bid, "ask": ask, "mark": mark}
        except Exception as e:
            logger.error(f"GRVT get_bbo: {e}")
        return {"bid": 0.0, "ask": 0.0, "mark": 0.0}

    async def get_funding_rate(self, symbol: str) -> Optional[float]:
        """
        Returns 8h-normalized decimal funding rate.
        GRVT API: funding_rate는 percentage per cycle, funding_interval_hours는 cycle 시간.
        예: MON funding_rate=-0.0381 (= -0.0381% per 4h), funding_interval_hours=4
        → decimal per 8h = (-0.0381 / 100) × (8 / 4) = -0.000762
        """
        try:
            grvt_sym = self._grvt_symbol(symbol)
            since_ns = int((time.time() - 86400) * 1e9)
            result = await self._retry(self._api.fetch_funding_rate_history, grvt_sym, since_ns, 1)
            entries = result.get("result", []) if isinstance(result, dict) else (result if isinstance(result, list) else [])
            if not entries:
                return None
            latest = entries[-1] if isinstance(entries, list) else entries
            raw_rate = latest.get("funding_rate", latest.get("fundingRate"))
            if raw_rate is None:
                return None
            # GRVT가 직접 제공하는 cycle period 사용 (BTC=8, MON=4 등)
            cycle_hours = float(latest.get("funding_interval_hours", 8))
            # percentage per cycle → decimal per 8h
            rate_decimal_per_cycle = float(raw_rate) / 100.0
            rate_8h = rate_decimal_per_cycle * (8.0 / cycle_hours)
            return rate_8h
        except Exception as e:
            logger.error(f"GRVT get_funding_rate: {e}")
        return None

    def _align_tick(self, grvt_sym: str, price: float, size: float) -> tuple[float, float]:
        market = self._api.markets.get(grvt_sym, {})
        tick = float(market.get("tick_size", 0.01))
        min_sz = float(market.get("min_size", 0.01))
        price = round(round(price / tick) * tick, 10)
        size = round(math.floor(size / min_sz) * min_sz, 10)
        return price, size

    @staticmethod
    def _parse_order_response(result: dict) -> tuple[str, str, float]:
        if not isinstance(result, dict):
            return "", "", 0.0
        state = result.get("state") or {}
        status = state.get("status") or ""
        coid = str((result.get("metadata") or {}).get("client_order_id", "") or "")
        traded = state.get("traded_size") or []
        if isinstance(traded, str):
            traded = [traded]
        filled = 0.0
        if isinstance(traded, list):
            try:
                filled = sum(abs(float(s)) for s in traded if s not in (None, ""))
            except (TypeError, ValueError):
                filled = 0.0
        return coid, status, filled

    def _is_filled(self, status: str, filled: float) -> bool:
        return status in ("FILLED", "closed", "filled") or filled > 0

    async def place_limit_order(
        self, symbol: str, side: str, size: float, price: float,
        post_only: bool = False, poll_count: int = 3, poll_interval: float = 2.0,
    ) -> OrderResult:
        try:
            grvt_sym = self._grvt_symbol(symbol)
            price, size = self._align_tick(grvt_sym, price, size)
            if size <= 0:
                return OrderResult(order_id="", status="error", message="size too small after tick alignment")
            params = {"post_only": True} if post_only else {}
            result = await self._retry(
                self._api.create_order,
                grvt_sym, "limit", side.lower(), size, price,
                params,
            )
            if result:
                coid, status, filled = self._parse_order_response(result)

                if not self._is_filled(status, filled):
                    if coid:
                        logger.info(f"GRVT {symbol} order coid={coid} status={status} (post_only={post_only}), waiting for fill...")
                        for _poll in range(poll_count):
                            await asyncio.sleep(poll_interval)
                            try:
                                order_info = await self._retry(
                                    self._api.fetch_order, params={"client_order_id": coid},
                                )
                                if order_info:
                                    inner = order_info.get("result", order_info)
                                    _, status, filled = self._parse_order_response(inner)
                                    logger.info(f"GRVT {symbol} order poll {_poll+1}: status={status}, filled={filled}")
                                    if self._is_filled(status, filled):
                                        break
                            except Exception as poll_err:
                                logger.warning(f"GRVT {symbol} order poll failed: {poll_err}")

                        if not self._is_filled(status, filled):
                            logger.warning(f"GRVT {symbol} order coid={coid} still unfilled, cancelling")
                            try:
                                await self._api.cancel_order(params={"client_order_id": coid})
                            except Exception:
                                pass
                    else:
                        # No coid means we can't track or cancel by id - cancel all on this symbol as safety
                        logger.warning(f"GRVT {symbol} response missing client_order_id, falling back to cancel_all_orders")
                        try:
                            await self._api.cancel_all_orders({"kind": "PERPETUAL", "base": symbol.upper()})
                        except Exception as ce:
                            logger.error(f"GRVT {symbol} cancel_all_orders fallback failed: {ce}")

                if not self._is_filled(status, filled):
                    logger.warning(f"GRVT {symbol} order final status={status}, result={result}")
                is_done = self._is_filled(status, filled)
                # entry_price = mark (체결 시점). limit은 슬리피지 포함이라 spread_mtm 과장 표시함.
                # 실제 체결가는 mark에 가까움. 진짜 슬리피지 비용은 real_pnl(잔고 기반)에서 자동 반영.
                actual_price = price
                if is_done:
                    try:
                        mark = await self.get_mark_price(symbol)
                        if mark and mark > 0:
                            actual_price = mark
                    except Exception:
                        pass
                return OrderResult(
                    order_id=coid,
                    status="filled" if is_done else status,
                    filled_size=(filled if filled > 0 else size) if is_done else 0.0,
                    filled_price=actual_price,
                    message=f"grvt_status={status}",
                )
            else:
                logger.warning(f"GRVT {symbol} create_order returned None/empty")
        except Exception as e:
            logger.error(f"GRVT place_limit_order: {e}")
        return OrderResult(order_id="", status="error", message="order failed")

    async def close_position(
        self, symbol: str, side: str, size: float, slippage_pct: float = 0.01,
        post_only: bool = False, poll_count: int = 3, poll_interval: float = 2.0,
    ) -> bool:
        price = await self.get_mark_price(symbol)
        if not price:
            return False
        # post_only=True 면 close_side가 maker-side에 놓이도록 부호 반전.
        # LONG 청산은 SELL인데, post_only면 mark 위쪽(maker)에 호가, 그렇지 않으면 mark 아래(taker).
        if post_only:
            if side.upper() == "LONG":
                close_side, close_price = "sell", price * (1 + slippage_pct)
            else:
                close_side, close_price = "buy", price * (1 - slippage_pct)
        else:
            if side.upper() == "LONG":
                close_side, close_price = "sell", price * (1 - slippage_pct)
            else:
                close_side, close_price = "buy", price * (1 + slippage_pct)
        try:
            grvt_sym = self._grvt_symbol(symbol)
            close_price, size = self._align_tick(grvt_sym, close_price, size)
            if size <= 0:
                return False
            params = {"post_only": True} if post_only else {}
            result = await self._retry(
                self._api.create_order,
                grvt_sym, "limit", close_side, size, close_price, params,
            )
            if result:
                coid, status, filled = self._parse_order_response(result)

                if not self._is_filled(status, filled):
                    if coid:
                        logger.info(f"GRVT close {symbol} coid={coid} status={status} (post_only={post_only}), polling...")
                        for _poll in range(poll_count):
                            await asyncio.sleep(poll_interval)
                            try:
                                order_info = await self._retry(
                                    self._api.fetch_order, params={"client_order_id": coid},
                                )
                                if order_info:
                                    inner = order_info.get("result", order_info)
                                    _, status, filled = self._parse_order_response(inner)
                                    if self._is_filled(status, filled):
                                        break
                            except Exception as poll_err:
                                logger.warning(f"GRVT close poll failed: {poll_err}")

                        if not self._is_filled(status, filled):
                            logger.warning(f"GRVT close {symbol} coid={coid} unfilled, cancelling")
                            try:
                                await self._api.cancel_order(params={"client_order_id": coid})
                            except Exception:
                                pass
                    else:
                        logger.warning(f"GRVT close {symbol} response missing client_order_id, cancel_all fallback")
                        try:
                            await self._api.cancel_all_orders({"kind": "PERPETUAL", "base": symbol.upper()})
                        except Exception as ce:
                            logger.error(f"GRVT close {symbol} cancel_all_orders fallback failed: {ce}")

                return self._is_filled(status, filled)
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
        ok = await self.check_leverage(symbol, leverage)
        if ok:
            logger.info(f"GRVT {symbol} leverage already {leverage}x")
            return True
        try:
            grvt_sym = self._grvt_symbol(symbol)
            sub_id = self._api._trading_account_id
            path = "https://trades.grvt.io/full/v1/set_initial_leverage"
            payload = {
                "sub_account_id": str(sub_id),
                "instrument": grvt_sym,
                "leverage": str(leverage),
            }
            result = await self._retry(self._api._auth_and_post, path, payload)
            if result and isinstance(result, dict) and result.get("success"):
                logger.info(f"GRVT {symbol} leverage set to {leverage}x")
                return True
            logger.warning(f"GRVT set_leverage response: {result}")
        except Exception as e:
            logger.warning(f"GRVT set_leverage failed: {e}")
        return False

    async def check_leverage(self, symbol: str, expected: int) -> bool:
        try:
            sub_id = self._api._trading_account_id
            grvt_sym = self._grvt_symbol(symbol)
            path = "https://trades.grvt.io/full/v1/get_all_initial_leverage"
            payload = {"sub_account_id": str(sub_id)}
            result = await self._retry(self._api._auth_and_post, path, payload)
            if result and isinstance(result, dict):
                leverages = result.get("results", result.get("result", []))
                if isinstance(leverages, dict):
                    leverages = leverages.get("results", leverages.get("result", []))
                for item in leverages:
                    if item.get("instrument") == grvt_sym:
                        current = int(float(item.get("leverage", 0)))
                        if current == expected:
                            return True
                        logger.warning(f"GRVT {symbol} leverage={current}x (expected {expected}x)")
                        return False
            logger.warning(f"GRVT {symbol} leverage info not found")
            return False
        except Exception as e:
            logger.warning(f"GRVT check_leverage failed: {e}")
            return False

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
