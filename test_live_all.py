"""
NADO×GRVT 봇 전체 함수 로컬 라이브 검증 스크립트.

실제 API를 호출하여 모든 핵심 함수를 테스트한다.
주문 테스트는 최소 수량으로 실행 후 즉시 청산한다.

사용법:
    .venv313/Scripts/python test_live_all.py [--skip-orders]
"""
import asyncio
import argparse
import logging
import sys
import time

from config import Config
from strategy import (
    normalize_funding_to_8h, decide_direction, calc_notional,
    determine_mode, is_entry_favorable, should_exit_cycle,
    should_exit_spread, is_opposite_direction_better,
)
from models import Position, EarnState, BotState, CycleState, OperatingMode
from exchanges.nado_client import NadoClient
from exchanges.grvt_client import GrvtClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("test_live")

PASS = 0
FAIL = 0
SKIP = 0


def result(name: str, ok: bool, detail: str = ""):
    global PASS, FAIL
    tag = "PASS" if ok else "FAIL"
    if ok:
        PASS += 1
    else:
        FAIL += 1
    print(f"  [{tag}] {name}" + (f" -{detail}" if detail else ""))


def skip(name: str, reason: str = ""):
    global SKIP
    SKIP += 1
    print(f"  [SKIP] {name}" + (f" -{reason}" if reason else ""))


async def test_strategy():
    print("\n=== Strategy Functions ===")

    r = normalize_funding_to_8h(0.001, 1)
    result("normalize_funding_to_8h(1h→8h)", abs(r - 0.008) < 1e-9, f"{r}")

    r = normalize_funding_to_8h(0.001, 8)
    result("normalize_funding_to_8h(8h→8h)", abs(r - 0.001) < 1e-9, f"{r}")

    d = decide_direction(0.001, 0.005)
    result("decide_direction(nado<grvt)", d == "A", f"dir={d}")

    d = decide_direction(0.005, 0.001)
    result("decide_direction(nado>grvt)", d == "B", f"dir={d}")

    d = decide_direction(-0.001, -0.002)
    result("decide_direction(nado=-0.001,grvt=-0.002)", d == "B", f"dir={d}")

    d = decide_direction(-0.001, -0.001)
    result("decide_direction(equal negative→None)", d is None, f"dir={d}")

    n = calc_notional(1000, 1200, 5, 0.65)
    expected = 1000 * 5 * 0.65
    result("calc_notional", abs(n - expected) < 0.01, f"${n:.2f} (expected ${expected:.2f})")

    m = determine_mode(False, False, 10, 200000, 50000)
    result("determine_mode(trades not met)", m == "VOLUME_URGENT", f"mode={m}")

    m = determine_mode(True, True, 10, 0, 50000)
    result("determine_mode(all met)", m == "VOLUME", f"mode={m}")

    m = determine_mode(False, True, 10, 100000, 50000)
    result("determine_mode(volume not met, enough time)", m == "VOLUME", f"mode={m}")

    f = is_entry_favorable("A", 100.0, 101.0)
    result("is_entry_favorable(A, nado<grvt)", f is True, f"{f}")

    f = is_entry_favorable("A", 101.0, 100.0)
    result("is_entry_favorable(A, nado>grvt)", f is False, f"{f}")

    e = should_exit_cycle(25, 24, 4, 20, 10)
    result("should_exit_cycle(normal)", e is None, f"exit={e}")

    e = should_exit_cycle(97, 24, 4, 20, 10)
    result("should_exit_cycle(max_hold)", e == "max_hold", f"exit={e}")

    e = should_exit_cycle(25, 24, 4, 8, 10)
    result("should_exit_cycle(margin_emergency)", e == "margin_emergency", f"exit={e}")

    s = should_exit_spread(55, 50, -30)
    result("should_exit_spread(profit)", s is True, f"{s}")

    s = should_exit_spread(-35, 50, -30)
    result("should_exit_spread(stoploss)", s is True, f"{s}")

    s = should_exit_spread(10, 50, -30)
    result("should_exit_spread(hold)", s is False, f"{s}")


async def test_models():
    print("\n=== Models ===")

    pos = Position("nado", "BTC", "LONG", 1000, 95000, 5, 200)
    pnl = pos.calc_unrealized_pnl(96000)
    expected_pnl = 1000 * (96000 - 95000) / 95000
    result("Position.unrealized_pnl(LONG)", abs(pnl - expected_pnl) < 0.01, f"${pnl:.2f}")

    pos2 = Position("grvt", "BTC", "SHORT", 1000, 95000, 5, 200)
    pnl2 = pos2.calc_unrealized_pnl(94000)
    expected_pnl2 = 1000 * (95000 - 94000) / 95000
    result("Position.unrealized_pnl(SHORT)", abs(pnl2 - expected_pnl2) < 0.01, f"${pnl2:.2f}")

    ratio = pos.calc_margin_ratio(96000)
    result("Position.margin_ratio", ratio > 0, f"{ratio:.1f}%")

    d = pos.to_dict()
    pos3 = Position.from_dict(d)
    result("Position round-trip", pos3.exchange == "nado" and pos3.side == "LONG", f"{pos3}")

    state = BotState()
    import tempfile, os
    tmp = tempfile.mktemp(suffix=".json")
    state.save(Path(tmp))
    loaded = BotState.load(Path(tmp))
    result("BotState save/load", loaded.cycle_state == CycleState.IDLE, f"state={loaded.cycle_state.value}")
    os.unlink(tmp)


async def test_nado(cfg: Config, skip_orders: bool):
    print("\n=== NADO Client ===")
    nado = NadoClient(cfg.NADO_PRIVATE_KEY)

    try:
        await nado.connect()
        result("NADO connect", True)
    except Exception as e:
        result("NADO connect", False, str(e))
        return None

    try:
        pairs = await nado.get_available_pairs()
        result("NADO get_available_pairs", len(pairs) > 0, f"{pairs[:10]}")
    except Exception as e:
        result("NADO get_available_pairs", False, str(e))

    try:
        bal = await nado.get_balance()
        result("NADO get_balance", bal >= 0, f"${bal:.2f}")
    except Exception as e:
        result("NADO get_balance", False, str(e))
        bal = 0

    pair = cfg.PAIR_DEFAULT
    try:
        price = await nado.get_mark_price(pair)
        result(f"NADO get_mark_price({pair})", price is not None and price > 0, f"${price:,.2f}" if price else "None")
    except Exception as e:
        result(f"NADO get_mark_price({pair})", False, str(e))
        price = None

    try:
        positions = await nado.get_positions(pair)
        result(f"NADO get_positions({pair})", isinstance(positions, list), f"{positions}")
    except Exception as e:
        result(f"NADO get_positions({pair})", False, str(e))

    try:
        rate = await nado.get_funding_rate(pair)
        result(f"NADO get_funding_rate({pair})", rate is not None, f"{rate}")
    except Exception as e:
        result(f"NADO get_funding_rate({pair})", False, str(e))

    try:
        depth = await nado.get_orderbook_depth(pair)
        result(f"NADO get_orderbook_depth({pair})", depth >= 0, f"${depth:,.0f}")
    except Exception as e:
        result(f"NADO get_orderbook_depth({pair})", False, str(e))

    try:
        lev = await nado.set_leverage(pair, cfg.LEVERAGE)
        result(f"NADO set_leverage({pair}, {cfg.LEVERAGE}x)", lev is True, f"{lev}")
    except Exception as e:
        result(f"NADO set_leverage({pair})", False, str(e))

    if skip_orders:
        skip("NADO place_limit_order", "skip-orders")
        skip("NADO close_position", "skip-orders")
        skip("NADO cancel_all_orders", "skip-orders")
    elif price and bal > 10:
        min_notional = 200
        qty = min_notional / price
        buy_price = price * (1 + cfg.SLIPPAGE_PCT)
        logger.info(f"NADO order test: BUY {qty:.6f} {pair} @ ${buy_price:.2f}")
        try:
            margin = min_notional / cfg.LEVERAGE
            res = await nado.place_limit_order(pair, "BUY", qty, buy_price, isolated_margin=margin)
            result(f"NADO place_limit_order(BUY)", res.status != "error", f"status={res.status} id={res.order_id}")

            if res.status in ("filled", "matched"):
                await asyncio.sleep(2)
                close_ok = await nado.close_position(pair, "LONG", qty, 0.01)
                result("NADO close_position", close_ok, f"{close_ok}")
            else:
                cancel_ok = await nado.cancel_all_orders(pair)
                result("NADO cancel_all_orders", cancel_ok, f"{cancel_ok}")
        except Exception as e:
            result("NADO place_limit_order", False, str(e))
            try:
                await nado.cancel_all_orders(pair)
            except Exception:
                pass
    else:
        skip("NADO order tests", f"price={price} bal={bal}")

    return nado


async def test_grvt(cfg: Config, skip_orders: bool):
    print("\n=== GRVT Client ===")
    grvt = GrvtClient(cfg.GRVT_API_KEY, cfg.GRVT_PRIVATE_KEY, cfg.GRVT_TRADING_ACCOUNT_ID)

    try:
        await grvt.connect()
        result("GRVT connect", True)
    except Exception as e:
        result("GRVT connect", False, str(e))
        return None

    try:
        pairs = await grvt.get_available_pairs()
        result("GRVT get_available_pairs", len(pairs) > 0, f"{pairs[:10]}")
    except Exception as e:
        result("GRVT get_available_pairs", False, str(e))

    try:
        bal = await grvt.get_balance()
        result("GRVT get_balance", bal > 0, f"${bal:.2f}")
    except Exception as e:
        result("GRVT get_balance", False, str(e))
        bal = 0

    pair = cfg.PAIR_DEFAULT
    try:
        price = await grvt.get_mark_price(pair)
        result(f"GRVT get_mark_price({pair})", price is not None and price > 0, f"${price:,.2f}" if price else "None")
    except Exception as e:
        result(f"GRVT get_mark_price({pair})", False, str(e))
        price = None

    try:
        positions = await grvt.get_positions(pair)
        result(f"GRVT get_positions({pair})", isinstance(positions, list), f"{positions}")
    except Exception as e:
        result(f"GRVT get_positions({pair})", False, str(e))

    try:
        rate = await grvt.get_funding_rate(pair)
        result(f"GRVT get_funding_rate({pair})", rate is not None, f"{rate}")
    except Exception as e:
        result(f"GRVT get_funding_rate({pair})", False, str(e))

    try:
        depth = await grvt.get_orderbook_depth(pair)
        result(f"GRVT get_orderbook_depth({pair})", depth >= 0, f"${depth:,.0f}")
    except Exception as e:
        result(f"GRVT get_orderbook_depth({pair})", False, str(e))

    try:
        lev = await grvt.set_leverage(pair, cfg.LEVERAGE)
        result(f"GRVT set_leverage({pair}, {cfg.LEVERAGE}x)", lev is True, f"{lev}")
    except Exception as e:
        result(f"GRVT set_leverage({pair})", False, str(e))

    if skip_orders:
        skip("GRVT place_limit_order", "skip-orders")
        skip("GRVT close_position", "skip-orders")
        skip("GRVT cancel_all_orders", "skip-orders")
    elif price and bal > 10:
        test_notional = 200
        test_qty = test_notional / price
        buy_price = price * (1 + cfg.SLIPPAGE_PCT)
        buy_price, test_qty = grvt._align_tick(grvt._grvt_symbol(pair), buy_price, test_qty)
        logger.info(f"GRVT order test: BUY {test_qty} {pair} @ ${buy_price:.2f} (notional ~${test_qty * buy_price:.0f})")
        try:
            res = await grvt.place_limit_order(pair, "BUY", test_qty, buy_price)
            result(f"GRVT place_limit_order(BUY)", res.status != "error", f"status={res.status} id={res.order_id}")

            if res.status in ("filled", "closed"):
                await asyncio.sleep(2)
                close_ok = await grvt.close_position(pair, "LONG", test_qty, 0.01)
                result("GRVT close_position", close_ok, f"{close_ok}")
            else:
                cancel_ok = await grvt.cancel_all_orders(pair)
                result("GRVT cancel_all_orders", cancel_ok, f"{cancel_ok}")
        except Exception as e:
            result("GRVT place_limit_order", False, str(e))
            try:
                await grvt.cancel_all_orders(pair)
            except Exception:
                pass
    else:
        skip("GRVT order tests", f"price={price} bal={bal}")

    return grvt


async def test_cross_exchange(cfg: Config, nado: NadoClient, grvt: GrvtClient):
    print("\n=== Cross-Exchange Integration ===")
    if not nado or not grvt:
        skip("cross-exchange tests", "one or both clients failed to connect")
        return

    pair = cfg.PAIR_DEFAULT
    nado_price = await nado.get_mark_price(pair)
    grvt_price = await grvt.get_mark_price(pair)

    if nado_price and grvt_price:
        spread_pct = (nado_price - grvt_price) / grvt_price * 100
        result("price spread", abs(spread_pct) < 5, f"NADO=${nado_price:,.2f} GRVT=${grvt_price:,.2f} spread={spread_pct:+.3f}%")

        direction = decide_direction(
            normalize_funding_to_8h(await nado.get_funding_rate(pair) or 0, cfg.NADO_FUNDING_PERIOD_H),
            normalize_funding_to_8h(await grvt.get_funding_rate(pair) or 0, cfg.GRVT_FUNDING_PERIOD_H),
        )
        result("funding direction", True, f"direction={direction}")

        nado_bal = await nado.get_balance()
        grvt_bal = await grvt.get_balance()
        notional = calc_notional(nado_bal, grvt_bal, cfg.LEVERAGE, cfg.MARGIN_BUFFER)
        result("calc_notional", notional > 0, f"NADO=${nado_bal:.2f} GRVT=${grvt_bal:.2f} → notional=${notional:,.0f}")

        fee = cfg.estimate_round_trip_fee(notional)
        result("round_trip_fee", fee >= 0, f"${fee:.2f}")
    else:
        result("price comparison", False, f"NADO={nado_price} GRVT={grvt_price}")


async def test_config():
    print("\n=== Config ===")
    cfg = Config()

    errors = cfg.validate()
    result("config.validate", len(errors) == 0, f"errors={errors}" if errors else "all keys set")

    fee = cfg.estimate_round_trip_fee(1000)
    result("estimate_round_trip_fee(1000)", fee > 0, f"${fee:.4f}")

    for mode in ["HOLD", "VOLUME", "VOLUME_URGENT"]:
        params = cfg.mode_params(mode)
        result(f"mode_params({mode})", "min_hold_hours" in params and "cooldown" in params, f"{params}")


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-orders", action="store_true", help="Skip order placement tests")
    args = parser.parse_args()

    print("=" * 60)
    print("  NADO x GRVT Bot - Full Function Verification")
    print("=" * 60)

    cfg = Config()

    await test_config()
    await test_strategy()
    await test_models()

    nado = await test_nado(cfg, args.skip_orders)
    grvt = await test_grvt(cfg, args.skip_orders)

    await test_cross_exchange(cfg, nado, grvt)

    if nado:
        await nado.close()
    if grvt:
        await grvt.close()

    print("\n" + "=" * 60)
    print(f"  Results: {PASS} PASS / {FAIL} FAIL / {SKIP} SKIP")
    print("=" * 60)

    if FAIL > 0:
        sys.exit(1)


from pathlib import Path

if __name__ == "__main__":
    asyncio.run(main())
