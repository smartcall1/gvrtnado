# nado_grvt_engine.py
import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logging.getLogger("pysdk").setLevel(logging.WARNING)
logging.getLogger("aiohttp").setLevel(logging.WARNING)

from config import Config
from models import (
    CycleState, OperatingMode, Position, Cycle,
    EarnState, BotState,
)
from strategy import (
    normalize_funding_to_8h, decide_direction, should_exit_cycle,
    should_exit_spread, calc_notional,
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
        if self._state.positions:
            for k, v in self._state.positions.items():
                try:
                    self._positions[k] = Position.from_dict(v)
                except Exception as e:
                    logger.warning(f"Position deserialization failed for {k}: {e}")
            if not self._positions and self._state.cycle_state in (CycleState.HOLD, CycleState.EXIT):
                logger.warning("Position restore failed, will rely on recovery check")
                self._state.positions = {}
        self._earn = self._init_earn()
        self._running = False

        self._nado_price: Optional[float] = None
        self._grvt_price: Optional[float] = None
        self._last_balance_check = 0.0
        self._last_funding_check = time.time()  # init to now — 0.0 caused 56년치 오accumulation on restart
        self._last_daily_report = ""
        self._last_margin_warn = 0.0
        self._cycle_history: list[Cycle] = []
        self._idle_since: float = 0.0
        self._enter_since: float = 0.0
        self._oi_blocked: dict[str, float] = {}  # pair → unblock_at timestamp

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

    def _prune_oi_blocked(self):
        now = time.time()
        expired = [p for p, t in self._oi_blocked.items() if t <= now]
        for p in expired:
            del self._oi_blocked[p]

    async def _handle_idle(self):
        await self._check_earn_cycle()
        mode = self._determine_current_mode()
        self._state.mode = OperatingMode(mode)

        self._prune_oi_blocked()
        if self._oi_blocked:
            logger.info(f"[IDLE] OI blocked pairs: {list(self._oi_blocked.keys())}")

        funding_spreads = {}
        liquidities = {}
        for pair in self._pair_mgr.common_pairs:
            if pair in self._oi_blocked:
                continue
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
                if pair == "BTC":
                    logger.info(f"[DIAG] BTC depth: NADO=${nd:,.0f} GRVT=${gd:,.0f}")
            except Exception as e:
                logger.warning(f"Pair scan {pair}: {e}")

        if funding_spreads:
            top = sorted(funding_spreads.items(), key=lambda x: x[1], reverse=True)[:5]
            logger.info(f"[IDLE] funding spreads (top5): {[(p, f'{s:.6f}') for p, s in top]}")
            liq_valid = {p: f'${v:,.0f}' for p, v in liquidities.items() if v > 0}
            logger.info(f"[IDLE] liquidity: {liq_valid}")
        else:
            logger.warning("[IDLE] No funding spreads found for any pair")

        # M1 fix: 예상 notional의 3배를 최소 유동성으로 설정 (기존: MIN_NOTIONAL×10=$1K)
        est_notional = calc_notional(
            self._state.nado_balance or 1000,
            self._state.grvt_balance or 1000,
            self.cfg.LEVERAGE, self.cfg.MARGIN_BUFFER,
        )
        min_liq = max(est_notional * 3, 10000)
        best_pair = self._pair_mgr.best_pair(
            funding_spreads=funding_spreads,
            liquidities=liquidities,
            min_liquidity=min_liq,
        )
        logger.info(f"[IDLE] selected pair: {best_pair}")
        self._state.pair = best_pair
        self._state.cycle_state = CycleState.ANALYZE
        self._save_state()

    async def _handle_analyze(self):
        pair = self._state.pair
        nado_rate = await self._nado.get_funding_rate(pair)
        grvt_rate = await self._grvt.get_funding_rate(pair)

        if nado_rate is None or grvt_rate is None:
            logger.info(f"[ANALYZE] {pair} funding: NADO={nado_rate} GRVT={grvt_rate} — skipping (None)")
            return

        nado_8h = normalize_funding_to_8h(nado_rate, self.cfg.NADO_FUNDING_PERIOD_H)
        grvt_8h = normalize_funding_to_8h(grvt_rate, self.cfg.GRVT_FUNDING_PERIOD_H)
        direction = decide_direction(nado_8h, grvt_8h)
        logger.info(f"[ANALYZE] {pair} NADO_8h={nado_8h:.6f} GRVT_8h={grvt_8h:.6f} dir={direction}")

        if direction is None:
            mode = self._state.mode
            if mode == OperatingMode.VOLUME_URGENT:
                elapsed = time.time() - self._idle_since if self._idle_since else 0
                if elapsed > 7200 and (nado_8h != 0 or grvt_8h != 0):
                    direction = "A" if grvt_8h >= nado_8h else "B"
                    logger.info(f"VOLUME_URGENT: 스프레드 미세, direction={direction} 강제 진입")
            if direction is None:
                if not self._idle_since:
                    self._idle_since = time.time()
                return

        self._idle_since = 0
        self._enter_since = time.time()
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
            logger.info(f"[ENTER] {pair} prices: NADO={self._nado_price} GRVT={self._grvt_price} — skipping (None)")
            return

        favorable = is_entry_favorable(direction, self._nado_price, self._grvt_price)
        if not favorable:
            mode = self._state.mode
            if mode != OperatingMode.VOLUME_URGENT:
                logger.info(f"[ENTER] {pair} dir={direction} NADO=${self._nado_price:.1f} GRVT=${self._grvt_price:.1f} favorable=False — waiting")
                return
            elapsed = time.time() - self._enter_since if self._enter_since else 0
            if elapsed < 60:
                logger.info(f"[ENTER] {pair} dir={direction} NADO=${self._nado_price:.1f} GRVT=${self._grvt_price:.1f} URGENT bypass in {60 - elapsed:.0f}s")
                return
            spread_pct = (self._nado_price - self._grvt_price) / self._grvt_price * 100
            logger.info(f"[ENTER] VOLUME_URGENT bypass! {pair} dir={direction} spread={spread_pct:+.3f}% — 강제 진입")
        else:
            logger.info(f"[ENTER] {pair} dir={direction} NADO=${self._nado_price:.1f} GRVT=${self._grvt_price:.1f} favorable=True — 진입")

        nado_bal = await self._nado.get_balance()
        grvt_bal = await self._grvt.get_balance()
        nado_max_lev = self._nado.get_max_leverage(pair)
        effective_lev = min(self.cfg.LEVERAGE, nado_max_lev)
        notional = calc_notional(nado_bal, grvt_bal, effective_lev, self.cfg.MARGIN_BUFFER)
        logger.info(f"[ENTER] balance NADO=${nado_bal:.2f} GRVT=${grvt_bal:.2f} notional=${notional:.0f} (NADO max_lev={nado_max_lev:.1f}x, eff={effective_lev:.1f}x)")

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

        result = await self._execute_enter(pair, direction, notional)

        if result == "nado_max_oi":
            self._oi_blocked[pair] = time.time() + 3600
            logger.warning(f"[ENTER] NADO {pair} max OI — 1시간 차단, 다음 마켓 탐색")
            await self._telegram.send_alert(f"[⛔ MAX OI] NADO {pair} OI 한도 — 1h 차단, 다른 마켓 탐색")
            self._state.cycle_state = CycleState.IDLE
            self._save_state()
            return
        elif result == "nado_health":
            reduced = notional * 0.4
            if reduced >= self.cfg.MIN_NOTIONAL:
                logger.info(f"[ENTER] NADO 마진 부족, notional ${notional:.0f} → ${reduced:.0f} 줄여 재시도")
                await self._telegram.send_alert(f"[⚠️ MARGIN] NADO {pair} 마진 부족 — notional ${notional:.0f}→${reduced:.0f} 재시도")
                result = await self._execute_enter(pair, direction, reduced)
                notional = reduced

        if result == "ok":
            self._state.cycle_id = str(uuid.uuid4())[:8]
            self._state.entered_at = time.time()
            self._state.cumulative_funding = 0.0
            self._state.cumulative_fees = self.cfg.estimate_round_trip_fee(notional)
            self._last_funding_check = time.time()
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
            self._state.cycle_state = CycleState.COOLDOWN
            self._state.cooldown_until = time.time() + 300
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

        # H1 fix: 손절은 즉시, 익절은 min_hold 이후에만 (기존: 둘 다 즉시)
        if spread_mtm <= self.cfg.SPREAD_STOPLOSS:
            self._state.cycle_state = CycleState.EXIT
            self._state.exit_reason = "spread_stoploss"
            self._save_state()
            return

        hold_hours = (time.time() - self._state.entered_at) / 3600

        if hold_hours >= mode_params["min_hold_hours"] and spread_mtm >= mode_params["spread_exit"]:
            self._state.cycle_state = CycleState.EXIT
            self._state.exit_reason = "spread_profit"
            self._save_state()
            return

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
            delta_seconds = now - self._last_funding_check
            self._last_funding_check = now
            # Sanity clamp: 1시간 이상 갭은 재시작/시계오류로 간주, 누적 스킵
            if delta_seconds > 3600:
                logger.warning(f"Funding check delta {delta_seconds:.0f}s 비정상, 누적 스킵")
            else:
                elapsed_hours = delta_seconds / 3600
                nado_rate = await self._nado.get_funding_rate(pair)
                grvt_rate = await self._grvt.get_funding_rate(pair)
                if nado_rate is not None and grvt_rate is not None:
                    nado_8h = normalize_funding_to_8h(nado_rate, self.cfg.NADO_FUNDING_PERIOD_H)
                    grvt_8h = normalize_funding_to_8h(grvt_rate, self.cfg.GRVT_FUNDING_PERIOD_H)
                    pos = self._positions.get("nado") or self._positions.get("grvt")
                    notional = pos.notional if pos else 0
                    if self._state.direction == "A":
                        rate_diff = grvt_8h - nado_8h
                    else:
                        rate_diff = nado_8h - grvt_8h
                    # H2: 연속 근사 — 실제 펀딩은 이산 지급(NADO 1h, GRVT 8h)이라 누적 시 소폭 괴리 가능
                    funding_income = notional * rate_diff * (elapsed_hours / 8)
                    # Sanity: 1폴링당 누적이 notional의 1% 초과면 비정상
                    if abs(funding_income) > notional * 0.01:
                        logger.error(f"Funding income ${funding_income:.2f} > notional 1% ({notional*0.01:.2f}), 누적 스킵")
                    else:
                        self._state.cumulative_funding += funding_income
                    # Sanity: cumulative이 notional의 10배 초과면 corrupt → 리셋
                    if abs(self._state.cumulative_funding) > notional * 10:
                        logger.critical(f"cumulative_funding ${self._state.cumulative_funding:,.0f} corrupt, 0으로 리셋")
                        await self._telegram.send_alert(f"[🚨 RESET] 손상된 누적 펀딩 (${self._state.cumulative_funding:,.0f}) 0으로 초기화")
                        self._state.cumulative_funding = 0.0
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

        self._nado_price = await self._nado.get_mark_price(pair) or self._nado_price
        self._grvt_price = await self._grvt.get_mark_price(pair) or self._grvt_price

        success = await self._execute_exit(pair)

        if not success:
            logger.warning("Exit failed, will retry next tick")
            await self._telegram.send_alert(f"[⚠️ EXIT RETRY] {pair} 청산 재시도 예정")
            self._save_state()
            return

        nado_p = self._positions.get("nado")
        grvt_p = self._positions.get("grvt")
        nado_pnl = nado_p.calc_unrealized_pnl(self._nado_price) if nado_p and self._nado_price else 0
        grvt_pnl = grvt_p.calc_unrealized_pnl(self._grvt_price) if grvt_p and self._grvt_price else 0

        pos = nado_p or grvt_p
        notional = pos.notional if pos else 0
        grvt_volume = (grvt_p.notional * 2) if grvt_p else 0

        cycle = Cycle(
            cycle_id=self._state.cycle_id, pair=pair,
            direction=self._state.direction, notional=notional,
            entered_at=self._state.entered_at, exited_at=time.time(),
            entry_nado_price=nado_p.entry_price if nado_p else 0,
            entry_grvt_price=grvt_p.entry_price if grvt_p else 0,
            exit_nado_price=self._nado_price or 0,
            exit_grvt_price=self._grvt_price or 0,
            funding_pnl=self._state.cumulative_funding,
            spread_pnl=nado_pnl + grvt_pnl,
            fee_cost=self._state.cumulative_fees,
            exit_reason=exit_reason,
            volume_generated=grvt_volume,
        )
        self._log_jsonl("cycles.jsonl", json.loads(cycle.to_jsonl()))
        self._cycle_history.append(cycle)
        self._earn.grvt_volume += grvt_volume
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
            f"PnL: ${cycle.net_pnl:+.2f} | Vol: +${grvt_volume:,.0f}"
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

    async def _execute_enter(self, pair: str, direction: str, notional: float) -> str:
        lev_ok = await self._grvt.set_leverage(pair, self.cfg.LEVERAGE)
        if not lev_ok:
            await self._telegram.send_alert(
                f"[⚠️ LEVERAGE] GRVT {pair} 레버리지를 {self.cfg.LEVERAGE}x로 웹UI에서 설정해주세요"
            )
            return "failed"

        chunk_size = notional / self.cfg.ENTRY_CHUNKS
        nado_side = "BUY" if direction == "A" else "SELL"
        grvt_side = "SELL" if direction == "A" else "BUY"
        nado_pos_side = "LONG" if nado_side == "BUY" else "SHORT"
        grvt_pos_side = "LONG" if grvt_side == "BUY" else "SHORT"

        nado_filled_qty = 0.0
        nado_filled_cost = 0.0
        grvt_filled_qty = 0.0
        grvt_filled_cost = 0.0

        nado_depth = await self._nado.get_orderbook_depth(pair)
        grvt_depth = await self._grvt.get_orderbook_depth(pair)
        min_depth = min(nado_depth, grvt_depth) if nado_depth and grvt_depth else float('inf')
        chunk_wait = self.cfg.CHUNK_WAIT * 2 if min_depth < self.cfg.THIN_MARKET_DEPTH else self.cfg.CHUNK_WAIT
        nado_health_fail = False
        nado_oi_fail = False

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
            nado_max_lev = self._nado.get_max_leverage(pair)
            nado_eff_lev = min(self.cfg.LEVERAGE, nado_max_lev)
            for attempt in range(self.cfg.CHUNK_RETRY):
                nado_margin = chunk_size / nado_eff_lev

                # NADO 먼저 체결 — OI cap/마진 실패 시 GRVT 롤백 비용 회피
                nado_res = await self._nado.place_limit_order(
                    pair, nado_side, nado_qty, nado_order_price, isolated_margin=nado_margin,
                )
                nado_ok = nado_res.status in ("filled", "matched")

                if not nado_ok:
                    logger.warning(f"Chunk {i+1}: NADO failed ({nado_res.message}), skipping GRVT")
                    await self._nado.cancel_all_orders(pair)
                    if attempt < self.cfg.CHUNK_RETRY - 1:
                        await asyncio.sleep(5)
                    continue

                # NADO 성공 → GRVT 진입
                grvt_res = await self._grvt.place_limit_order(
                    pair, grvt_side, grvt_qty, grvt_order_price,
                )
                grvt_ok = grvt_res.status in ("filled", "closed")

                if nado_ok and grvt_ok:
                    nado_filled_qty += nado_res.filled_size
                    nado_filled_cost += nado_res.filled_size * nado_res.filled_price
                    grvt_filled_qty += grvt_res.filled_size
                    grvt_filled_cost += grvt_res.filled_size * grvt_res.filled_price
                    success = True
                    break
                elif nado_ok and not grvt_ok:
                    logger.warning(f"Chunk {i+1}: GRVT failed (status={grvt_res.status}, msg={grvt_res.message}), rolling back NADO")
                    rollback_ok = await self._nado.close_position(
                        pair, nado_pos_side, nado_res.filled_size, self.cfg.EMERGENCY_SLIPPAGE_PCT,
                    )
                    await self._grvt.cancel_all_orders(pair)
                    if not rollback_ok:
                        logger.critical(f"Chunk {i+1}: NADO rollback FAILED")
                        await self._telegram.send_alert(f"[🚨 ROLLBACK FAIL] NADO {pair} 수동 확인 필요")

                if attempt < self.cfg.CHUNK_RETRY - 1:
                    await asyncio.sleep(5)

            if not success:
                nado_msg = nado_res.message or ""
                if "nado_health" in nado_msg:
                    nado_health_fail = True
                elif "nado_max_oi" in nado_msg:
                    nado_oi_fail = True
                logger.error(f"Chunk {i+1}/{self.cfg.ENTRY_CHUNKS} failed after retries{' (nado_health)' if nado_health_fail else ''}{' (max_oi)' if nado_oi_fail else ''}")
                break

            if i < self.cfg.ENTRY_CHUNKS - 1:
                await asyncio.sleep(chunk_wait)

        nado_notional = nado_filled_cost
        grvt_notional = grvt_filled_cost
        if nado_notional > 0 and grvt_notional > 0:
            avg_notional = (nado_notional + grvt_notional) / 2
            imbalance = abs(nado_notional - grvt_notional) / avg_notional if avg_notional > 0 else 0
            if imbalance > 0.05:
                logger.warning(f"Notional imbalance {imbalance:.1%}: NADO=${nado_notional:,.0f} GRVT=${grvt_notional:,.0f} — rolling back all")
                await self._telegram.send_alert(f"[⚠️ IMBALANCE] {imbalance:.1%} — 전량 롤백")
                await self._nado.cancel_all_orders(pair)
                await self._grvt.cancel_all_orders(pair)
                await asyncio.gather(
                    self._nado.close_position(pair, nado_pos_side, nado_filled_qty, self.cfg.EMERGENCY_SLIPPAGE_PCT),
                    self._grvt.close_position(pair, grvt_pos_side, grvt_filled_qty, self.cfg.EMERGENCY_SLIPPAGE_PCT),
                )
                return "failed"
        elif nado_notional > 0 and grvt_notional == 0:
            logger.warning("Only NADO filled, GRVT empty — rolling back NADO")
            rollback_ok = await self._nado.close_position(pair, nado_pos_side, nado_filled_qty, self.cfg.EMERGENCY_SLIPPAGE_PCT)
            if not rollback_ok:
                # Rollback failed — register the orphan so the next EXIT cycle keeps trying
                nado_vwap = nado_filled_cost / nado_filled_qty if nado_filled_qty > 0 else 0
                self._positions["nado"] = Position(
                    exchange="nado", symbol=pair, side=nado_pos_side,
                    notional=nado_notional, entry_price=nado_vwap,
                    leverage=self.cfg.LEVERAGE, margin=nado_notional / self.cfg.LEVERAGE,
                )
                logger.critical(f"NADO rollback failed — orphan position registered for retry")
                await self._telegram.send_alert(
                    f"[🚨 ROLLBACK FAIL] NADO {pair} 단방향 잔존, 다음 EXIT cycle 재시도"
                )
            return "failed"
        elif grvt_notional > 0 and nado_notional == 0:
            logger.warning("Only GRVT filled, NADO empty — rolling back GRVT")
            rollback_ok = await self._grvt.close_position(pair, grvt_pos_side, grvt_filled_qty, self.cfg.EMERGENCY_SLIPPAGE_PCT)
            if not rollback_ok:
                grvt_vwap = grvt_filled_cost / grvt_filled_qty if grvt_filled_qty > 0 else 0
                self._positions["grvt"] = Position(
                    exchange="grvt", symbol=pair, side=grvt_pos_side,
                    notional=grvt_notional, entry_price=grvt_vwap,
                    leverage=self.cfg.LEVERAGE, margin=grvt_notional / self.cfg.LEVERAGE,
                )
                logger.critical(f"GRVT rollback failed — orphan position registered for retry")
                await self._telegram.send_alert(
                    f"[🚨 ROLLBACK FAIL] GRVT {pair} 단방향 잔존, 다음 EXIT cycle 재시도"
                )
            return "nado_max_oi" if nado_oi_fail else "nado_health" if nado_health_fail else "failed"

        if nado_notional > 0 and grvt_notional > 0:
            nado_vwap = nado_filled_cost / nado_filled_qty if nado_filled_qty > 0 else 0
            grvt_vwap = grvt_filled_cost / grvt_filled_qty if grvt_filled_qty > 0 else 0
            nado_margin = nado_notional / self.cfg.LEVERAGE
            grvt_margin = grvt_notional / self.cfg.LEVERAGE
            self._positions["nado"] = Position(
                exchange="nado", symbol=pair,
                side="LONG" if direction == "A" else "SHORT",
                notional=nado_notional, entry_price=nado_vwap,
                leverage=self.cfg.LEVERAGE, margin=nado_margin,
            )
            self._positions["grvt"] = Position(
                exchange="grvt", symbol=pair,
                side="SHORT" if direction == "A" else "LONG",
                notional=grvt_notional, entry_price=grvt_vwap,
                leverage=self.cfg.LEVERAGE, margin=grvt_margin,
            )
            return "ok"
        return "nado_max_oi" if nado_oi_fail else "nado_health" if nado_health_fail else "failed"

    async def _execute_exit(self, pair: str) -> bool:
        if not self._positions:
            return True

        nado_pos = self._positions.get("nado")
        grvt_pos = self._positions.get("grvt")
        chunks = self.cfg.EXIT_CHUNKS

        nado_real = await self._nado.get_positions(pair)
        grvt_real = await self._grvt.get_positions(pair)
        nado_total_size = abs(float(nado_real[0].get("size", nado_real[0].get("amount", 0)))) if nado_real else 0
        grvt_total_size = abs(float(grvt_real[0].get("size", grvt_real[0].get("contracts", 0)))) if grvt_real else 0

        for i in range(chunks):
            tasks = []
            labels = []
            if nado_pos and nado_total_size > 0:
                chunk_qty = nado_total_size / chunks
                tasks.append(self._nado.close_position(pair, nado_pos.side, chunk_qty, self.cfg.EMERGENCY_SLIPPAGE_PCT))
                labels.append("nado")
            if grvt_pos and grvt_total_size > 0:
                chunk_qty = grvt_total_size / chunks
                tasks.append(self._grvt.close_position(pair, grvt_pos.side, chunk_qty, self.cfg.EMERGENCY_SLIPPAGE_PCT))
                labels.append("grvt")
            if tasks:
                # gather results — bool is what each close_position returns; failures need attention
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for label, r in zip(labels, results):
                    if isinstance(r, Exception):
                        logger.error(f"Exit chunk {i+1} {label} close raised: {r}")
                    elif r is False:
                        logger.warning(f"Exit chunk {i+1} {label} close returned False")
            if i < chunks - 1:
                await asyncio.sleep(self.cfg.CHUNK_WAIT)

        for attempt in range(3):
            await asyncio.sleep(5)
            nado_remaining = await self._nado.get_positions(pair)
            grvt_remaining = await self._grvt.get_positions(pair)
            if not nado_remaining and not grvt_remaining:
                return True
            logger.warning(f"Exit retry {attempt+1}/3: positions remain (nado={len(nado_remaining)}, grvt={len(grvt_remaining)})")
            for rp in nado_remaining:
                size = abs(float(rp.get("size", rp.get("amount", 0))))
                side = rp.get("side", "LONG").upper()
                if size > 0:
                    await self._nado.close_position(pair, side, size, self.cfg.EMERGENCY_SLIPPAGE_PCT)
            for rp in grvt_remaining:
                size = abs(float(rp.get("size", rp.get("contracts", 0))))
                side = rp.get("side", "LONG").upper()
                if size > 0:
                    await self._grvt.close_position(pair, side, size, self.cfg.EMERGENCY_SLIPPAGE_PCT)
        return False

    async def _emergency_exit(self, reason: str):
        pair = self._state.pair
        logger.critical(f"EMERGENCY EXIT: {reason}")
        await self._nado.cancel_all_orders(pair)
        await self._grvt.cancel_all_orders(pair)
        success = await self._execute_exit(pair)
        if success:
            self._positions.clear()
        else:
            logger.critical("Emergency exit FAILED — positions may still be open!")
            await self._telegram.send_alert(f"[🚨 EXIT FAILED] {pair} 수동 청산 필요!")
        self._state.cycle_state = CycleState.COOLDOWN
        self._state.cooldown_until = time.time() + 60
        self._save_state()
        await self._telegram.send_alert(f"[🚨 EMERGENCY EXIT] {reason}")

    # --- Earn 관리 ---

    async def _check_earn_cycle(self):
        now = datetime.now(timezone.utc)
        if self._earn.is_cycle_expired(now):
            self._earn.reset()
            self._state.mode = OperatingMode.VOLUME
            self._save_state()
            await self._telegram.send_alert(
                f"[🔄 NEW CYCLE] {self._earn.cycle_start.date()} ~ {self._earn.cycle_end.date()}"
            )

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
            # 두 클라이언트 모두 size=abs(), side="LONG"/"SHORT" 반환 — side 필드 사용
            nado_side = (np.get("side") or "").upper()
            grvt_side = (gp.get("side") or "").upper()
            if nado_side not in ("LONG", "SHORT") or grvt_side not in ("LONG", "SHORT"):
                logger.error(f"Recovery: invalid side fields nado={nado_side} grvt={grvt_side}, np={np}, gp={gp}")
                return

            # H3 fix: 양쪽 포지션이 반대 방향인지 검증 (기존: 무조건 복원)
            if nado_side == grvt_side:
                logger.critical(f"Recovery: NADO={nado_side} GRVT={grvt_side} — 같은 방향! 수동 확인 필요")
                await self._telegram.send_alert(f"[🚨 RECOVERY] 양쪽 동일 방향({nado_side}) — 수동 확인 필요")
                return

            # entry_price가 0이면 mark price로 대체 (C4 fix 전 데이터 호환)
            if nado_entry <= 0:
                nado_entry = await self._nado.get_mark_price(pair) or 0
            if grvt_entry <= 0:
                grvt_entry = await self._grvt.get_mark_price(pair) or 0

            nado_notional = abs(nado_size) * nado_entry if nado_entry > 0 else 0
            grvt_notional = abs(grvt_size) * grvt_entry if grvt_entry > 0 else 0
            notional = max(nado_notional, grvt_notional)
            margin = notional / self.cfg.LEVERAGE if notional > 0 else 0

            self._positions["nado"] = Position(
                "nado", pair, nado_side, nado_notional or notional, nado_entry, self.cfg.LEVERAGE, margin,
            )
            self._positions["grvt"] = Position(
                "grvt", pair, grvt_side, grvt_notional or notional, grvt_entry, self.cfg.LEVERAGE, margin,
            )
            self._last_funding_check = time.time()  # 복구 시 펀딩 카운터 재설정
            if not self._state.direction:
                self._state.direction = "A" if nado_side == "LONG" else "B"
            self._state.cycle_state = CycleState.HOLD
            self._save_state()
            logger.info(f"Recovery: restored positions for {pair}, direction={self._state.direction}")
            await self._telegram.send_alert(f"[RECOVERY] 포지션 복원 완료: {pair} (direction={self._state.direction})")
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
        def _fmt_price(p: float) -> str:
            if p is None or p <= 0:
                return "N/A"
            if p >= 1000:
                return f"${p:,.2f}"
            if p >= 1:
                return f"${p:,.4f}"
            return f"${p:.6f}"

        async def on_status():
            mode = self._state.mode.value
            pair = self._state.pair
            cycle = self._state.cycle_state.value

            nado_pos = self._positions.get("nado") if "nado" in self._positions else None
            grvt_pos = self._positions.get("grvt") if "grvt" in self._positions else None
            nado_bal = self._state.nado_balance
            grvt_bal = self._state.grvt_balance

            # 헤더: 상태 + 페어 + 보유시간을 한 줄로
            hold_seconds = (time.time() - self._state.entered_at) if (self._state.entered_at and nado_pos and grvt_pos) else 0
            hold_str = ""
            if hold_seconds > 0:
                hold_str = f" · 보유 {hold_seconds/3600:.1f}h" if hold_seconds >= 3600 else f" · 보유 {int(hold_seconds/60)}분"

            boost = self._pair_mgr.get_boost(pair)
            boost_str = ""
            if boost.get("nado", 1.0) != 1.0 or boost.get("grvt", 1.0) != 1.0:
                boost_str = f" · 부스트 N{boost['nado']:.1f}× G{boost['grvt']:.1f}×"

            lines = [
                f"📊 <b>{cycle}</b> · {pair} · {mode}{hold_str}{boost_str}",
                "━━━━━━━━━━━━━━━",
                f"💼 잔고: NADO ${nado_bal:,.0f} + GRVT ${grvt_bal:,.0f} = <b>${nado_bal+grvt_bal:,.0f}</b>",
            ]

            if nado_pos and grvt_pos:
                avg_notional = (nado_pos.notional + grvt_pos.notional) / 2
                imbalance = abs(nado_pos.notional - grvt_pos.notional) / avg_notional * 100 if avg_notional > 0 else 0
                delta_emoji = "✅" if imbalance <= 5 else "⚠️"

                nado_curr = self._nado_price or 0
                grvt_curr = self._grvt_price or 0
                nado_chg = ((nado_curr - nado_pos.entry_price) / nado_pos.entry_price * 100) if nado_pos.entry_price > 0 else 0
                grvt_chg = ((grvt_curr - grvt_pos.entry_price) / grvt_pos.entry_price * 100) if grvt_pos.entry_price > 0 else 0

                # 실효 레버리지: NADO는 cross(notional/잔고), GRVT는 isolated(notional/margin = cfg.LEVERAGE)
                nado_eff_lev = nado_pos.notional / nado_bal if nado_bal > 0 else 0
                grvt_eff_lev = self.cfg.LEVERAGE

                funding = self._state.cumulative_funding
                fees = self._state.cumulative_fees
                spread_mtm = nado_pos.calc_unrealized_pnl(nado_curr) + grvt_pos.calc_unrealized_pnl(grvt_curr)
                net_pnl = spread_mtm + funding - fees
                pnl_emoji = "🟢" if net_pnl >= 0 else "🔴"

                lines.append("")
                lines.append(f"📍 <b>포지션</b> (헷지 {imbalance:.1f}% {delta_emoji})")
                lines.append(f"   NADO {nado_pos.side} ${nado_pos.notional:,.0f} · 실효 {nado_eff_lev:.1f}x cross")
                lines.append(f"   GRVT {grvt_pos.side} ${grvt_pos.notional:,.0f} · {grvt_eff_lev}x isolated")
                lines.append(f"   진입 → 현재")
                lines.append(f"   N {_fmt_price(nado_pos.entry_price)} → {_fmt_price(nado_curr)} ({nado_chg:+.2f}%)")
                lines.append(f"   G {_fmt_price(grvt_pos.entry_price)} → {_fmt_price(grvt_curr)} ({grvt_chg:+.2f}%)")
                lines.append(f"   <i>※ NADO UI {self._nado.get_max_leverage(pair):.0f}x = 시장 max, 실제 노출 = 잔고 비율</i>")

                try:
                    nr = await self._nado.get_funding_rate(pair)
                    gr = await self._grvt.get_funding_rate(pair)
                    if nr is not None and gr is not None:
                        nado_8h = normalize_funding_to_8h(nr, self.cfg.NADO_FUNDING_PERIOD_H)
                        grvt_8h = normalize_funding_to_8h(gr, self.cfg.GRVT_FUNDING_PERIOD_H)
                        rate_diff = (grvt_8h - nado_8h) if self._state.direction == "A" else (nado_8h - grvt_8h)
                        apr = rate_diff * (365 * 24 / 8) * 100
                        apr_emoji = "🚀" if apr >= 30 else "✅" if apr >= 10 else "⚠️" if apr >= 0 else "🔻"

                        now_utc = datetime.now(timezone.utc)
                        next_nado_dt = now_utc.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
                        nado_min = max(0, int((next_nado_dt - now_utc).total_seconds() / 60))
                        next_grvt_h = ((now_utc.hour // 8) + 1) * 8
                        if next_grvt_h >= 24:
                            next_grvt_dt = now_utc.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
                        else:
                            next_grvt_dt = now_utc.replace(hour=next_grvt_h, minute=0, second=0, microsecond=0)
                        grvt_min = max(0, int((next_grvt_dt - now_utc).total_seconds() / 60))

                        lines.append("")
                        lines.append(f"📈 펀딩 8h: N {nado_8h:+.4f} / G {grvt_8h:+.4f}")
                        lines.append(f"   {apr_emoji} 스프레드 {rate_diff:+.4f} (연 APR {apr:+.0f}%)")
                        lines.append(f"   다음 정산: N {nado_min}분 / G {grvt_min//60}h{grvt_min%60}m")
                except Exception as e:
                    logger.debug(f"Status funding fetch: {e}")

                lines.append("")
                lines.append(f"💰 PnL: {pnl_emoji} <b>${net_pnl:+,.2f}</b>")
                lines.append(f"   스프레드 ${spread_mtm:+,.2f} + 펀딩 ${funding:+,.2f} - 수수료 ${fees:,.2f}")

                try:
                    mp = self.cfg.mode_params(mode)
                    min_hold_sec = mp['min_hold_hours'] * 3600
                    min_remaining = max(0, min_hold_sec - hold_seconds)
                    min_str = "✓" if min_remaining == 0 else (f"{min_remaining/3600:.1f}h" if min_remaining >= 3600 else f"{int(min_remaining/60)}분")
                    max_remaining_h = max(0, self.cfg.MAX_HOLD_DAYS * 86400 - hold_seconds) / 3600
                    progress = (spread_mtm / mp['spread_exit'] * 100) if (mp['spread_exit'] > 0 and spread_mtm > 0) else 0

                    lines.append("")
                    lines.append(f"⏱ 익절 ≥ +${mp['spread_exit']:.0f} ({progress:.0f}%) · 손절 ≤ ${self.cfg.SPREAD_STOPLOSS:.0f}")
                    lines.append(f"   min_hold {mp['min_hold_hours']:.1f}h ({min_str}) · max_hold {max_remaining_h:.0f}h 남음")
                except Exception:
                    pass

                try:
                    now = datetime.now(timezone.utc)
                    days = self._earn.days_remaining(now)
                    prog = self._earn.volume_progress() * 100
                    t_emoji = "✅" if self._earn.is_trades_target_met() else "❌"
                    v_emoji = "✅" if self._earn.is_volume_target_met() else "⏳"
                    lines.append(f"💎 Earn: {t_emoji} {self._earn.grvt_trades}/5건 · {v_emoji} ${self._earn.grvt_volume:,.0f}/${self._earn.target_volume:,.0f} ({prog:.0f}%) · {days}일 남음")
                except Exception:
                    pass
            else:
                lines.append("(포지션 없음)")

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
            if self._positions and self._state.pair:
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
        # M2 fix: 9시 이후 첫 폴링에서 발송 (기존: 정각 hour==9만 매칭)
        if now_kst.hour < 9:
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
        logger.info("Connecting to NADO...")
        await self._nado.connect()
        logger.info("Connecting to GRVT...")
        await self._grvt.connect()
        logger.info("Both exchanges connected")

        nado_pairs = await self._nado.get_available_pairs()
        grvt_pairs = await self._grvt.get_available_pairs()
        self._pair_mgr.set_available_pairs(nado_pairs, grvt_pairs)
        logger.info(f"Common pairs: {self._pair_mgr.common_pairs}")

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

        self._last_status_log = 0.0
        try:
            while self._running:
                try:
                    await self._telegram.poll_updates()
                    await self._run_state_machine()

                    now = time.time()
                    if now - self._last_balance_check > self.cfg.POLL_BALANCE_SECONDS:
                        self._last_balance_check = now
                        self._state.nado_balance = await self._nado.get_balance()
                        self._state.grvt_balance = await self._grvt.get_balance()

                    if now - self._last_status_log > 60:
                        self._last_status_log = now
                        pos_str = ""
                        if self._positions:
                            parts = []
                            for k, p in self._positions.items():
                                parts.append(f"{k}:{p.side}/${p.notional:.0f}@{p.entry_price:.1f}")
                            pos_str = " | ".join(parts)
                        else:
                            pos_str = "none"
                        logger.info(
                            f"[STATUS] {self._state.cycle_state.value} | "
                            f"mode={self._state.mode.value} | pair={self._state.pair} | "
                            f"NADO=${self._state.nado_balance:.2f} GRVT=${self._state.grvt_balance:.2f} | "
                            f"pos={pos_str}"
                        )

                    await self._send_daily_report()

                except Exception as e:
                    logger.error(f"Main loop error: {e}", exc_info=True)

                await asyncio.sleep(self.cfg.POLL_INTERVAL)
        except (KeyboardInterrupt, asyncio.CancelledError):
            logger.info("Shutting down...")
        finally:
            await self._nado.close()
            await self._grvt.close()
            await self._telegram.close()
