# grvtnado.py
import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from config import Config
from models import (
    CycleState, OperatingMode, Position, Cycle,
    EarnState, BotState, FundingSnapshot,
)
from strategy import (
    normalize_funding_to_8h, decide_direction, should_exit_cycle,
    should_exit_spread, is_opposite_direction_better, calc_notional,
    determine_mode, is_entry_favorable,
)
from pair_manager import PairManager
from monitor import MarginLevel, check_margin_level, CircuitBreaker, check_price_divergence
from telegram_ui import TelegramUI, BTN_STATUS, BTN_HISTORY, BTN_EARN, BTN_FUNDING, BTN_REBALANCE, BTN_STOP
from exchanges.nado_client import NadoClient
from exchanges.grvt_client import GrvtClient

logger = logging.getLogger(__name__)
KST = timezone(timedelta(hours=9))


class DeltaNeutralBot:
    def __init__(self):
        self.cfg = Config()
        self.cfg.ensure_dirs()

        self._nado = NadoClient(self.cfg.NADO_PRIVATE_KEY)
        self._grvt = GrvtClient(
            self.cfg.GRVT_API_KEY, self.cfg.GRVT_PRIVATE_KEY,
            self.cfg.GRVT_TRADING_ACCOUNT_ID,
        )
        self._telegram = TelegramUI(self.cfg.TELEGRAM_BOT_TOKEN, self.cfg.TELEGRAM_CHAT_ID)
        self._pair_mgr = PairManager(default_pair=self.cfg.PAIR_DEFAULT)
        self._cb = CircuitBreaker(max_fails=self.cfg.CIRCUIT_BREAKER_FAILS)

        self._state = BotState.load(self.cfg.LOG_DIR / "bot_state.json")
        self._positions: dict[str, Position] = {}
        self._earn = self._init_earn()
        self._running = False

        self._nado_price: Optional[float] = None
        self._grvt_price: Optional[float] = None
        self._last_balance_check = 0.0
        self._last_funding_check = 0.0
        self._last_daily_report = ""
        self._last_margin_warn = 0.0
        self._consecutive_loss_cycles = 0
        self._cycle_history: list[Cycle] = []
        self._idle_since: float = 0.0

    def _init_earn(self) -> EarnState:
        if self._state.earn:
            return EarnState.from_dict(self._state.earn)
        now = datetime.now(timezone.utc)
        cycle_start = datetime(2026, 4, 21, 0, 0, 0, tzinfo=timezone.utc)
        while cycle_start + timedelta(days=28) <= now:
            cycle_start += timedelta(days=28)
        return EarnState(
            cycle_start=cycle_start,
            cycle_end=cycle_start + timedelta(days=28),
            target_volume=self.cfg.EARN_TARGET_VOLUME,
        )

    def _save_state(self):
        self._state.earn = self._earn.to_dict()
        if self._positions:
            self._state.positions = {k: v.to_dict() for k, v in self._positions.items()}
        else:
            self._state.positions = {}
        self._state.boost_config = self._pair_mgr.to_dict().get("boosts", {})
        self._state.save(self.cfg.LOG_DIR / "bot_state.json")

    def _log_jsonl(self, filename: str, data: dict):
        path = self.cfg.LOG_DIR / filename
        with open(path, "a") as f:
            f.write(json.dumps({**data, "ts": time.time()}) + "\n")

    # --- 상태머신 ---

    async def _run_state_machine(self):
        state = self._state.cycle_state

        if state == CycleState.IDLE:
            await self._handle_idle()
        elif state == CycleState.ANALYZE:
            await self._handle_analyze()
        elif state == CycleState.ENTER:
            await self._handle_enter()
        elif state == CycleState.HOLD:
            await self._handle_hold()
        elif state == CycleState.EXIT:
            await self._handle_exit()
        elif state == CycleState.COOLDOWN:
            await self._handle_cooldown()

    async def _handle_idle(self):
        self._check_earn_cycle()
        mode = self._determine_current_mode()
        self._state.mode = OperatingMode(mode)

        funding_spreads = {}
        liquidities = {}
        for pair in self._pair_mgr.common_pairs:
            try:
                nr = await self._nado.get_funding_rate(pair)
                gr = await self._grvt.get_funding_rate(pair)
                if nr is not None and gr is not None:
                    n8 = normalize_funding_to_8h(nr, self.cfg.NADO_FUNDING_PERIOD_H)
                    g8 = normalize_funding_to_8h(gr, self.cfg.GRVT_FUNDING_PERIOD_H)
                    funding_spreads[pair] = abs(n8 - g8)
                nd = await self._nado.get_orderbook_depth(pair)
                gd = await self._grvt.get_orderbook_depth(pair)
                liquidities[pair] = min(nd, gd) if nd and gd else 0
            except Exception as e:
                logger.debug(f"Pair scan {pair}: {e}")

        best_pair = self._pair_mgr.best_pair(
            funding_spreads=funding_spreads,
            liquidities=liquidities,
            min_liquidity=self.cfg.MIN_NOTIONAL * 10,
        )
        self._state.pair = best_pair
        self._state.cycle_state = CycleState.ANALYZE
        self._save_state()

    async def _handle_analyze(self):
        pair = self._state.pair
        nado_rate = await self._nado.get_funding_rate(pair)
        grvt_rate = await self._grvt.get_funding_rate(pair)

        if nado_rate is None or grvt_rate is None:
            return

        nado_8h = normalize_funding_to_8h(nado_rate, self.cfg.NADO_FUNDING_PERIOD_H)
        grvt_8h = normalize_funding_to_8h(grvt_rate, self.cfg.GRVT_FUNDING_PERIOD_H)
        direction = decide_direction(nado_8h, grvt_8h)

        if direction is None:
            mode = self._state.mode
            if mode == OperatingMode.VOLUME_URGENT:
                elapsed = time.time() - self._idle_since if self._idle_since else 0
                if elapsed > 7200 and abs(nado_8h - grvt_8h) < 0.001:
                    direction = "A"
                    logger.info("VOLUME_URGENT: 펀딩 미세 음수 허용, 방향 A 강제 진입")
            if direction is None:
                if not self._idle_since:
                    self._idle_since = time.time()
                return

        self._idle_since = 0
        self._state.direction = direction
        self._state.cycle_state = CycleState.ENTER
        self._save_state()

        self._log_jsonl("funding_history.jsonl", {
            "pair": pair, "nado_rate": nado_rate, "grvt_rate": grvt_rate,
            "nado_8h": nado_8h, "grvt_8h": grvt_8h, "direction": direction,
        })

    async def _handle_enter(self):
        pair = self._state.pair
        direction = self._state.direction

        self._nado_price = await self._nado.get_mark_price(pair)
        self._grvt_price = await self._grvt.get_mark_price(pair)
        if not self._nado_price or not self._grvt_price:
            return

        if not is_entry_favorable(direction, self._nado_price, self._grvt_price):
            mode = self._state.mode
            if mode != OperatingMode.VOLUME_URGENT:
                return
            elapsed = time.time() - self._idle_since if self._idle_since else 0
            if elapsed < 1800:
                return

        nado_bal = await self._nado.get_balance()
        grvt_bal = await self._grvt.get_balance()
        notional = calc_notional(nado_bal, grvt_bal, self.cfg.LEVERAGE, self.cfg.MARGIN_BUFFER)

        nado_depth = await self._nado.get_orderbook_depth(pair)
        grvt_depth = await self._grvt.get_orderbook_depth(pair)
        min_depth = min(nado_depth, grvt_depth) if nado_depth and grvt_depth else 0
        if min_depth > 0:
            liquidity_cap = min_depth * self.cfg.LIQUIDITY_CAP_PCT
            if notional > liquidity_cap:
                logger.info(f"Liquidity cap: ${notional:,.0f} → ${liquidity_cap:,.0f} (depth ${min_depth:,.0f})")
                notional = liquidity_cap

        if notional < self.cfg.MIN_NOTIONAL:
            logger.warning(f"Notional ${notional:,.0f} below minimum, skipping")
            return

        success = await self._execute_enter(pair, direction, notional)
        if success:
            self._state.cycle_id = str(uuid.uuid4())[:8]
            self._state.entered_at = time.time()
            self._state.cumulative_funding = 0.0
            self._state.cumulative_fees = self.cfg.estimate_round_trip_fee(notional)
            self._state.nado_balance = nado_bal
            self._state.grvt_balance = grvt_bal
            self._state.cycle_state = CycleState.HOLD
            self._save_state()

            await self._telegram.send_alert(
                f"[ENTER] {pair} | "
                f"NADO {'LONG' if direction == 'A' else 'SHORT'} / "
                f"GRVT {'SHORT' if direction == 'A' else 'LONG'} | "
                f"${notional:,.0f}"
            )
        else:
            self._state.cycle_state = CycleState.IDLE
            self._save_state()

    async def _handle_hold(self):
        pair = self._state.pair
        self._nado_price = await self._nado.get_mark_price(pair)
        self._grvt_price = await self._grvt.get_mark_price(pair)

        if self._nado_price:
            self._cb.record_success("nado")
        else:
            self._cb.record_failure("nado")
        if self._grvt_price:
            self._cb.record_success("grvt")
        else:
            self._cb.record_failure("grvt")

        if self._cb.any_tripped():
            logger.critical("Circuit Breaker tripped!")
            await self._emergency_exit("circuit_breaker")
            return

        if self._nado_price and self._grvt_price:
            div_level = check_price_divergence(
                self._nado_price, self._grvt_price,
                self.cfg.PRICE_DIVERGENCE_WARN, self.cfg.PRICE_DIVERGENCE_EMERGENCY,
            )
            if div_level == "EMERGENCY":
                await self._emergency_exit("price_divergence")
                return
            if div_level == "WARNING" and time.time() - self._last_margin_warn > 1800:
                self._last_margin_warn = time.time()
                await self._telegram.send_alert(
                    f"[⚠️ DIVERGENCE] {self._nado_price:.1f} vs {self._grvt_price:.1f}"
                )

        nado_pnl = self._positions["nado"].calc_unrealized_pnl(self._nado_price) if "nado" in self._positions and self._nado_price else 0
        grvt_pnl = self._positions["grvt"].calc_unrealized_pnl(self._grvt_price) if "grvt" in self._positions and self._grvt_price else 0
        spread_mtm = nado_pnl + grvt_pnl

        mode_params = self.cfg.mode_params(self._state.mode.value)
        if should_exit_spread(spread_mtm, mode_params["spread_exit"], self.cfg.SPREAD_STOPLOSS):
            reason = "spread_profit" if spread_mtm > 0 else "spread_stoploss"
            self._state.cycle_state = CycleState.EXIT
            self._state.exit_reason = reason
            self._save_state()
            return

        hold_hours = (time.time() - self._state.entered_at) / 3600
        worst_margin = 100.0
        for pos in self._positions.values():
            price = self._nado_price if pos.exchange == "nado" else self._grvt_price
            if price:
                ratio = pos.calc_margin_ratio(price)
                worst_margin = min(worst_margin, ratio)

        if worst_margin <= self.cfg.MARGIN_WARNING_PCT and time.time() - self._last_margin_warn > 1800:
            self._last_margin_warn = time.time()
            await self._telegram.send_alert(f"[⚠️ MARGIN] {worst_margin:.1f}%")

        exit_reason = should_exit_cycle(
            hold_hours, mode_params["min_hold_hours"],
            self.cfg.MAX_HOLD_DAYS, worst_margin, self.cfg.MARGIN_EMERGENCY_PCT,
        )
        if exit_reason:
            self._state.cycle_state = CycleState.EXIT
            self._state.exit_reason = exit_reason
            self._save_state()
            return

        now = time.time()
        if now - self._last_funding_check > self.cfg.POLL_FUNDING_SECONDS:
            self._last_funding_check = now
            nado_rate = await self._nado.get_funding_rate(pair)
            grvt_rate = await self._grvt.get_funding_rate(pair)
            if nado_rate is not None and grvt_rate is not None:
                nado_8h = normalize_funding_to_8h(nado_rate, self.cfg.NADO_FUNDING_PERIOD_H)
                grvt_8h = normalize_funding_to_8h(grvt_rate, self.cfg.GRVT_FUNDING_PERIOD_H)
                notional = self._positions.get("nado", Position("", "", "", 0, 0, 0, 0)).notional
                if self._state.direction == "A":
                    funding_income = notional * (grvt_8h - nado_8h)
                else:
                    funding_income = notional * (nado_8h - grvt_8h)
                self._state.cumulative_funding += funding_income
                self._log_jsonl("funding_history.jsonl", {
                    "pair": pair, "nado_rate": nado_rate, "grvt_rate": grvt_rate,
                    "funding_income": funding_income, "cumulative": self._state.cumulative_funding,
                })

        self._log_jsonl("spread_history.jsonl", {
            "pair": pair, "mode": self._state.mode.value,
            "nado_price": self._nado_price, "grvt_price": self._grvt_price,
            "nado_pnl": nado_pnl, "grvt_pnl": grvt_pnl, "spread_mtm": spread_mtm,
            "hold_hours": hold_hours, "margin": worst_margin,
        })

    async def _handle_exit(self):
        pair = self._state.pair
        exit_reason = self._state.exit_reason or "unknown"
        success = await self._execute_exit(pair)

        nado_pnl = self._positions["nado"].calc_unrealized_pnl(self._nado_price) if "nado" in self._positions and self._nado_price else 0
        grvt_pnl = self._positions["grvt"].calc_unrealized_pnl(self._grvt_price) if "grvt" in self._positions and self._grvt_price else 0

        notional = self._positions.get("nado", self._positions.get("grvt")).notional if self._positions else 0
        volume = notional * 2

        cycle = Cycle(
            cycle_id=self._state.cycle_id, pair=pair,
            direction=self._state.direction, notional=notional,
            entered_at=self._state.entered_at, exited_at=time.time(),
            entry_nado_price=self._positions.get("nado", Position("", "", "", 0, 0, 0, 0)).entry_price,
            entry_grvt_price=self._positions.get("grvt", Position("", "", "", 0, 0, 0, 0)).entry_price,
            exit_nado_price=self._nado_price or 0,
            exit_grvt_price=self._grvt_price or 0,
            funding_pnl=self._state.cumulative_funding,
            spread_pnl=nado_pnl + grvt_pnl,
            fee_cost=self._state.cumulative_fees,
            exit_reason=exit_reason,
            volume_generated=volume,
        )
        self._log_jsonl("cycles.jsonl", json.loads(cycle.to_jsonl()))
        self._cycle_history.append(cycle)
        self._earn.grvt_volume += volume
        self._earn.grvt_trades += 2

        self._log_jsonl("volume_history.jsonl", {
            "grvt_volume": self._earn.grvt_volume,
            "grvt_trades": self._earn.grvt_trades,
            "days_left": self._earn.days_remaining(datetime.now(timezone.utc)),
            "pair": pair,
        })

        self._positions.clear()
        mode_params = self.cfg.mode_params(self._state.mode.value)
        self._state.cooldown_until = time.time() + mode_params["cooldown"]
        self._state.cycle_state = CycleState.COOLDOWN
        self._save_state()

        await self._telegram.send_alert(
            f"[EXIT] {pair} | {exit_reason} | "
            f"PnL: ${cycle.net_pnl:+.2f} | Vol: +${volume:,.0f}"
        )

        if len(self._cycle_history) >= 5:
            avg = sum(c.net_pnl for c in self._cycle_history[-5:]) / 5
            if avg < -3:
                self._state.mode = OperatingMode.HOLD
                await self._telegram.send_alert(
                    f"[⚠️ 적자 감지] 최근 5사이클 평균 ${avg:.1f}. HOLD 전환"
                )

    async def _handle_cooldown(self):
        if time.time() >= self._state.cooldown_until:
            self._state.cycle_state = CycleState.IDLE
            self._save_state()

    # --- 청크 진입/퇴출 ---

    async def _execute_enter(self, pair: str, direction: str, notional: float) -> bool:
        chunk_size = notional / self.cfg.ENTRY_CHUNKS
        nado_side = "BUY" if direction == "A" else "SELL"
        grvt_side = "SELL" if direction == "A" else "BUY"
        filled_notional = 0.0

        nado_depth = await self._nado.get_orderbook_depth(pair)
        grvt_depth = await self._grvt.get_orderbook_depth(pair)
        min_depth = min(nado_depth, grvt_depth) if nado_depth and grvt_depth else float('inf')
        chunk_wait = self.cfg.CHUNK_WAIT * 2 if min_depth < self.cfg.THIN_MARKET_DEPTH else self.cfg.CHUNK_WAIT

        for i in range(self.cfg.ENTRY_CHUNKS):
            nado_price = await self._nado.get_mark_price(pair)
            grvt_price = await self._grvt.get_mark_price(pair)
            if not nado_price or not grvt_price:
                break

            nado_qty = chunk_size / nado_price
            grvt_qty = chunk_size / grvt_price

            slip = self.cfg.SLIPPAGE_PCT
            nado_order_price = nado_price * (1 + slip) if nado_side == "BUY" else nado_price * (1 - slip)
            grvt_order_price = grvt_price * (1 - slip) if grvt_side == "SELL" else grvt_price * (1 + slip)

            success = False
            for attempt in range(self.cfg.CHUNK_RETRY):
                nado_res, grvt_res = await asyncio.gather(
                    self._nado.place_limit_order(pair, nado_side, nado_qty, nado_order_price),
                    self._grvt.place_limit_order(pair, grvt_side, grvt_qty, grvt_order_price),
                )
                nado_ok = nado_res.status in ("filled", "matched")
                grvt_ok = grvt_res.status in ("filled", "closed")

                if nado_ok and grvt_ok:
                    filled_notional += chunk_size
                    success = True
                    break
                elif nado_ok and not grvt_ok:
                    logger.warning(f"Chunk {i+1}: GRVT failed, rolling back NADO")
                    await self._nado.close_position(pair, nado_side, nado_qty, self.cfg.EMERGENCY_SLIPPAGE_PCT)
                    await self._grvt.cancel_all_orders(pair)
                elif grvt_ok and not nado_ok:
                    logger.warning(f"Chunk {i+1}: NADO failed, rolling back GRVT")
                    await self._grvt.close_position(pair, grvt_side, grvt_qty, self.cfg.EMERGENCY_SLIPPAGE_PCT)
                    await self._nado.cancel_all_orders(pair)
                else:
                    await self._nado.cancel_all_orders(pair)
                    await self._grvt.cancel_all_orders(pair)

                if attempt < self.cfg.CHUNK_RETRY - 1:
                    await asyncio.sleep(5)

            if not success:
                logger.error(f"Chunk {i+1}/{self.cfg.ENTRY_CHUNKS} failed after retries")
                break

            if i < self.cfg.ENTRY_CHUNKS - 1:
                await asyncio.sleep(chunk_wait)

        if filled_notional > 0:
            margin = filled_notional / self.cfg.LEVERAGE
            nado_entry = await self._nado.get_mark_price(pair) or 0
            grvt_entry = await self._grvt.get_mark_price(pair) or 0
            self._positions["nado"] = Position(
                exchange="nado", symbol=pair,
                side="LONG" if direction == "A" else "SHORT",
                notional=filled_notional, entry_price=nado_entry,
                leverage=self.cfg.LEVERAGE, margin=margin,
            )
            self._positions["grvt"] = Position(
                exchange="grvt", symbol=pair,
                side="SHORT" if direction == "A" else "LONG",
                notional=filled_notional, entry_price=grvt_entry,
                leverage=self.cfg.LEVERAGE, margin=margin,
            )
            return True
        return False

    async def _execute_exit(self, pair: str) -> bool:
        async def _close_one(pos: Position):
            client = self._nado if pos.exchange == "nado" else self._grvt
            price = await client.get_mark_price(pair)
            size = pos.notional / (price or pos.entry_price)
            return await client.close_position(pair, pos.side, size, self.cfg.EMERGENCY_SLIPPAGE_PCT)

        if self._positions:
            await asyncio.gather(*[_close_one(p) for p in self._positions.values()])

        for attempt in range(3):
            await asyncio.sleep(5)
            nado_pos = await self._nado.get_positions(pair)
            grvt_pos = await self._grvt.get_positions(pair)
            if len(nado_pos) == 0 and len(grvt_pos) == 0:
                return True
            logger.warning(f"Exit retry {attempt+1}/3: positions remain (nado={len(nado_pos)}, grvt={len(grvt_pos)})")
            if nado_pos or grvt_pos:
                remaining = []
                if nado_pos:
                    remaining.append(Position("nado", pair, "LONG", 0, 0, self.cfg.LEVERAGE, 0))
                if grvt_pos:
                    remaining.append(Position("grvt", pair, "LONG", 0, 0, self.cfg.LEVERAGE, 0))
                for pos_info, raw in [(self._nado, nado_pos), (self._grvt, grvt_pos)]:
                    for rp in raw:
                        size = abs(float(rp.get("size", rp.get("contracts", rp.get("amount", 0)))))
                        side = rp.get("side", "LONG").upper()
                        if size > 0:
                            await pos_info.close_position(pair, side, size, self.cfg.EMERGENCY_SLIPPAGE_PCT)
        return False

    async def _emergency_exit(self, reason: str):
        pair = self._state.pair
        logger.critical(f"EMERGENCY EXIT: {reason}")
        await self._nado.cancel_all_orders(pair)
        await self._grvt.cancel_all_orders(pair)
        await self._execute_exit(pair)
        self._positions.clear()
        self._state.cycle_state = CycleState.COOLDOWN
        self._state.cooldown_until = time.time() + 60
        self._save_state()
        await self._telegram.send_alert(f"[🚨 EMERGENCY EXIT] {reason}")

    # --- Earn 관리 ---

    def _check_earn_cycle(self):
        now = datetime.now(timezone.utc)
        if self._earn.is_cycle_expired(now):
            self._earn.reset()
            self._state.mode = OperatingMode.VOLUME
            self._save_state()
            asyncio.create_task(self._telegram.send_alert(
                f"[🔄 NEW CYCLE] {self._earn.cycle_start.date()} ~ {self._earn.cycle_end.date()}"
            ))

    def _determine_current_mode(self) -> str:
        now = datetime.now(timezone.utc)
        notional = calc_notional(
            self._state.nado_balance or 5000,
            self._state.grvt_balance or 5000,
            self.cfg.LEVERAGE, self.cfg.MARGIN_BUFFER,
        )
        daily_capacity = notional * 2 * 8
        return determine_mode(
            volume_met=self._earn.is_volume_target_met(),
            trades_met=self._earn.is_trades_target_met(),
            days_left=self._earn.days_remaining(now),
            volume_remaining=max(0, self._earn.target_volume - self._earn.grvt_volume),
            daily_capacity=daily_capacity,
        )

    # --- 크래시 복구 ---

    async def _recovery_check(self):
        pair = self._state.pair or self.cfg.PAIR_DEFAULT
        if self._state.cycle_state not in (CycleState.HOLD, CycleState.ENTER, CycleState.EXIT):
            return

        nado_pos = await self._nado.get_positions(pair)
        grvt_pos = await self._grvt.get_positions(pair)

        if nado_pos and grvt_pos:
            np = nado_pos[0]
            gp = grvt_pos[0]
            nado_entry = float(np.get("entry_price", 0))
            grvt_entry = float(gp.get("entry_price", 0))
            nado_size = float(np.get("size", np.get("amount", 0)))
            grvt_size = float(gp.get("size", gp.get("contracts", 0)))
            nado_side = "LONG" if nado_size > 0 else "SHORT"
            grvt_side = "LONG" if gp.get("side", "").upper() == "LONG" else "SHORT"
            notional = abs(nado_size) * nado_entry
            margin = notional / self.cfg.LEVERAGE

            self._positions["nado"] = Position(
                "nado", pair, nado_side, notional, nado_entry, self.cfg.LEVERAGE, margin,
            )
            self._positions["grvt"] = Position(
                "grvt", pair, grvt_side, notional, grvt_entry, self.cfg.LEVERAGE, margin,
            )
            self._state.cycle_state = CycleState.HOLD
            self._save_state()
            logger.info(f"Recovery: restored positions for {pair}")
            await self._telegram.send_alert(f"[RECOVERY] 포지션 복원 완료: {pair}")
        elif not nado_pos and not grvt_pos:
            self._state.cycle_state = CycleState.IDLE
            self._positions.clear()
            self._save_state()
            logger.info("Recovery: no positions found, resetting to IDLE")
        else:
            logger.warning("Recovery: one-sided position detected!")
            await self._telegram.send_alert("[⚠️ RECOVERY] 한쪽만 포지션 존재 — 수동 확인 필요")

    # --- Telegram 핸들러 ---

    async def _register_telegram_handlers(self):
        async def on_status():
            mode = self._state.mode.value
            pair = self._state.pair
            boost = self._pair_mgr.get_boost(pair)
            nado_pnl = self._positions.get("nado", Position("", "", "", 0, 0, 0, 0)).calc_unrealized_pnl(self._nado_price or 0) if "nado" in self._positions else 0
            grvt_pnl = self._positions.get("grvt", Position("", "", "", 0, 0, 0, 0)).calc_unrealized_pnl(self._grvt_price or 0) if "grvt" in self._positions else 0
            spread = nado_pnl + grvt_pnl
            lines = [
                "📊 <b>Status</b>",
                "━━━━━━━━━━━━━━━",
                f"모드: {mode} | 페어: {pair} (N:{boost['nado']}x G:{boost['grvt']}x)",
                f"상태: {self._state.cycle_state.value}",
                f"NADO: ${nado_pnl:+.2f} | GRVT: ${grvt_pnl:+.2f}",
                f"스프레드 MTM: ${spread:+.2f}",
            ]
            await self._telegram.send_message("\n".join(lines))

        async def on_earn():
            now = datetime.now(timezone.utc)
            days = self._earn.days_remaining(now)
            prog = self._earn.volume_progress() * 100
            trades_ok = "✅" if self._earn.is_trades_target_met() else "❌"
            vol_ok = "✅" if self._earn.is_volume_target_met() else "⏳"
            lines = [
                "💰 <b>GRVT Earn</b>",
                "━━━━━━━━━━━━━━━",
                f"사이클: {self._earn.cycle_start.date()} ~ {self._earn.cycle_end.date()} ({days}일 남음)",
                f"{trades_ok} 거래: {self._earn.grvt_trades}/5건",
                f"{vol_ok} 볼륨: ${self._earn.grvt_volume:,.0f} / ${self._earn.target_volume:,.0f} ({prog:.0f}%)",
                f"모드: {self._state.mode.value}",
            ]
            await self._telegram.send_message("\n".join(lines))

        async def on_history():
            recent = self._cycle_history[-5:] if self._cycle_history else []
            if not recent:
                await self._telegram.send_message("📋 히스토리 없음")
                return
            lines = ["📋 <b>Recent Cycles</b>", "━━━━━━━━━━━━━━━"]
            for c in reversed(recent):
                lines.append(f"{c.pair} | {c.exit_reason} | ${c.net_pnl:+.2f} | Vol ${c.volume_generated:,.0f}")
            await self._telegram.send_message("\n".join(lines))

        async def on_funding():
            pair = self._state.pair
            nr = await self._nado.get_funding_rate(pair)
            gr = await self._grvt.get_funding_rate(pair)
            lines = [
                "📈 <b>Funding Rates</b>",
                "━━━━━━━━━━━━━━━",
                f"NADO (1h): {nr or 'N/A'}",
                f"GRVT (8h): {gr or 'N/A'}",
                f"누적 펀딩: ${self._state.cumulative_funding:+.2f}",
            ]
            await self._telegram.send_message("\n".join(lines))

        async def on_rebalance():
            if self._state.cycle_state == CycleState.HOLD:
                self._state.cycle_state = CycleState.EXIT
                self._state.exit_reason = "manual_rebalance"
                self._save_state()
                await self._telegram.send_alert("[🔄 REBALANCE] 수동 EXIT 트리거")
            else:
                await self._telegram.send_message("현재 HOLD 상태가 아닙니다")

        async def on_stop():
            self._running = False
            if self._state.cycle_state == CycleState.HOLD:
                await self._execute_exit(self._state.pair)
            await self._telegram.send_alert("[⏹ STOP] 봇 종료")
            Path(".stop_bot").touch()

        async def on_text(text: str):
            if text.startswith("/setboost"):
                args = text[len("/setboost"):].strip()
                if args.lower() == "clear":
                    self._pair_mgr.clear_boost()
                    await self._telegram.send_message("✅ 부스트 초기화 (모두 1x)")
                else:
                    self._pair_mgr.parse_boost_string(args.replace(" ", ","))
                    await self._telegram.send_message(f"✅ 부스트 설정: {args}")
                self._save_state()

        self._telegram.register_callback(BTN_STATUS, on_status)
        self._telegram.register_callback(BTN_EARN, on_earn)
        self._telegram.register_callback(BTN_HISTORY, on_history)
        self._telegram.register_callback(BTN_FUNDING, on_funding)
        self._telegram.register_callback(BTN_REBALANCE, on_rebalance)
        self._telegram.register_callback(BTN_STOP, on_stop)
        self._telegram.register_text_handler(on_text)

    # --- 데일리 리포트 ---

    async def _send_daily_report(self):
        today = datetime.now(KST).strftime("%Y-%m-%d")
        if today == self._last_daily_report:
            return
        now_kst = datetime.now(KST)
        if now_kst.hour != 9:
            return
        self._last_daily_report = today
        total_pnl = sum(c.net_pnl for c in self._cycle_history)
        vol = self._earn.grvt_volume
        days = self._earn.days_remaining(datetime.now(timezone.utc))
        lines = [
            f"📊 <b>Daily Report ({today})</b>",
            "━━━━━━━━━━━━━━━",
            f"NADO: ${self._state.nado_balance:,.0f} | GRVT: ${self._state.grvt_balance:,.0f}",
            f"누적 PnL: ${total_pnl:+.2f} ({len(self._cycle_history)}사이클)",
            "━━━━━━━━━━━━━━━",
            f"📈 볼륨: ${vol:,.0f} / ${self._earn.target_volume:,.0f}",
            f"모드: {self._state.mode.value} | 잔여: {days}일",
        ]
        await self._telegram.send_message("\n".join(lines))

    # --- 메인 루프 ---

    async def run(self):
        errors = self.cfg.validate()
        if errors:
            logger.error(f"Config errors: {errors}")
            return

        self._running = True
        await self._nado.connect()
        await self._grvt.connect()

        nado_pairs = await self._nado.get_available_pairs()
        grvt_pairs = await self._grvt.get_available_pairs()
        self._pair_mgr.set_available_pairs(nado_pairs, grvt_pairs)

        if self._state.boost_config:
            self._pair_mgr.load_boosts({"boosts": self._state.boost_config})

        boost_env = os.environ.get("BOOST_PAIRS", "")
        if boost_env:
            self._pair_mgr.parse_boost_string(boost_env)

        await self._nado.set_leverage(self._state.pair or self.cfg.PAIR_DEFAULT, self.cfg.LEVERAGE)

        await self._register_telegram_handlers()
        await self._telegram.send_message(
            f"[🚀 START] NADO×GRVT 봇 가동\n"
            f"페어: {self._state.pair} | 레버리지: {self.cfg.LEVERAGE}x\n"
            f"모드: {self._state.mode.value}"
        )

        await self._recovery_check()

        while self._running:
            try:
                await self._telegram.poll_updates()
                await self._run_state_machine()
                self._check_earn_cycle()

                now = time.time()
                if now - self._last_balance_check > self.cfg.POLL_BALANCE_SECONDS:
                    self._last_balance_check = now
                    self._state.nado_balance = await self._nado.get_balance()
                    self._state.grvt_balance = await self._grvt.get_balance()

                await self._send_daily_report()

            except Exception as e:
                logger.error(f"Main loop error: {e}", exc_info=True)

            await asyncio.sleep(self.cfg.POLL_INTERVAL)

        await self._nado.close()
        await self._grvt.close()
        await self._telegram.close()
