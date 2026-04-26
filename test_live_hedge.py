"""
NADO x GRVT delta-neutral (yangppang) live order test.

Mirrors nado_grvt_engine._execute_enter pattern:
  1. NADO first (with isolated_margin) -> verify fill
  2. If NADO fails: stop (don't touch GRVT) -> exits OK
  3. If NADO ok: place GRVT opposite -> verify fill
  4. If GRVT fails: rollback NADO -> verify position cleared
  5. If both ok: hold ~5s then close both -> verify positions cleared

Verifies:
- NADO isolated-only fallback (error 2122 -> isolated mode retry, internal in nado_client)
- NADO OI cap (error 2070) defense path (NADO failure stops before GRVT entry)
- GRVT order_id/status parsing fix (metadata.client_order_id, state.status)
- Sequential entry + rollback paths from the actual bot

Usage:
    .venv313/Scripts/python test_live_hedge.py                  # default: DOGE
    .venv313/Scripts/python test_live_hedge.py --pair AAPL
    .venv313/Scripts/python test_live_hedge.py --notional 30
    .venv313/Scripts/python test_live_hedge.py --dry-run        # detect-only
"""
import asyncio
import argparse
import logging
import sys
import time

from config import Config
from exchanges.nado_client import NadoClient
from exchanges.grvt_client import GrvtClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("hedge_test")


def info(name, ok, detail=""):
    tag = "PASS" if ok else "FAIL"
    print(f"  [{tag}] {name}" + (f" - {detail}" if detail else ""))
    return ok


async def get_pair_info(nado: NadoClient, grvt: GrvtClient, pair: str):
    nado_pairs = await nado.get_available_pairs()
    grvt_pairs = await grvt.get_available_pairs()
    nado_ok = pair in nado_pairs
    grvt_ok = pair in grvt_pairs
    info(f"NADO has {pair}", nado_ok)
    info(f"GRVT has {pair}", grvt_ok)
    if not (nado_ok and grvt_ok):
        return None

    nado_price = await nado.get_mark_price(pair)
    grvt_price = await grvt.get_mark_price(pair)
    info(f"prices NADO=${nado_price} GRVT=${grvt_price}", bool(nado_price and grvt_price))

    pid = nado._symbol_map.get(pair.upper())
    nado_inc = nado._increments.get(pid, {})
    nado_size_inc = nado_inc.get("size", 10**16) / 1e18
    # NADO 'min_size' is actually min NOTIONAL in USD (verified via error 2094:
    # "abs(amount) * price must be >= min_size", with both amount and price in x18)
    nado_min_notional = nado_inc.get("min_size", 100 * 10**18) / 1e18
    nado_max_lev = nado_inc.get("max_leverage", 5)

    grvt_sym = grvt._grvt_symbol(pair)
    grvt_market = grvt._api.markets.get(grvt_sym, {})
    grvt_min_size = float(grvt_market.get("min_size", 0.001))
    grvt_min_notional = grvt_min_size * (grvt_price or 0)

    print(f"  NADO size_inc={nado_size_inc} max_lev={nado_max_lev}x min_notional=${nado_min_notional:.2f}")
    print(f"  GRVT min_size={grvt_min_size} min_notional=${grvt_min_notional:.2f}")

    return {
        "nado_price": nado_price,
        "grvt_price": grvt_price,
        "nado_min_notional": nado_min_notional,
        "grvt_min_size": grvt_min_size,
        "grvt_min_notional": grvt_min_notional,
        "nado_max_lev": nado_max_lev,
    }


async def hedge_round_trip(nado: NadoClient, grvt: GrvtClient, pair: str, pinfo: dict, notional: float, slippage: float, direction: str = "A"):
    """
    Mirrors engine._execute_enter sequential pattern.
    direction A: NADO BUY (LONG), GRVT SELL (SHORT)
    direction B: NADO SELL (SHORT), GRVT BUY (LONG)
    """
    nado_price = pinfo["nado_price"]
    grvt_price = pinfo["grvt_price"]

    nado_qty = notional / nado_price
    grvt_qty = notional / grvt_price

    if direction == "A":
        nado_side, grvt_side = "BUY", "SELL"
        nado_pos_side, grvt_pos_side = "LONG", "SHORT"
    else:
        nado_side, grvt_side = "SELL", "BUY"
        nado_pos_side, grvt_pos_side = "SHORT", "LONG"

    nado_order_price = nado_price * (1 + slippage) if nado_side == "BUY" else nado_price * (1 - slippage)
    grvt_order_price = grvt_price * (1 - slippage) if grvt_side == "SELL" else grvt_price * (1 + slippage)

    grvt_sym = grvt._grvt_symbol(pair)
    grvt_order_price, grvt_qty = grvt._align_tick(grvt_sym, grvt_order_price, grvt_qty)

    leverage = min(5, int(pinfo["nado_max_lev"]))
    margin = notional / leverage

    print(f"\n=== Hedge round-trip: {pair} (direction={direction}) ===")
    print(f"  NADO: {nado_side} {nado_qty:.6f} @ ${nado_order_price:.4f} (margin=${margin:.2f}, lev={leverage}x)")
    print(f"  GRVT: {grvt_side} {grvt_qty} @ ${grvt_order_price:.4f}")

    # Step 1: NADO first
    print("\n  --- Step 1: NADO entry ---")
    t0 = time.time()
    nado_res = await nado.place_limit_order(pair, nado_side, nado_qty, nado_order_price, isolated_margin=margin)
    elapsed = time.time() - t0
    nado_ok = nado_res.status in ("filled", "matched")
    info(f"NADO {nado_side} ({elapsed:.1f}s)", nado_ok, f"status={nado_res.status} msg={nado_res.message} digest={nado_res.order_id[:24]}...")

    if not nado_ok:
        # Defense path: NADO failure (could be OI cap 2070 or isolated-only conflict)
        # The bot stops here without touching GRVT. Verify GRVT is untouched.
        await nado.cancel_all_orders(pair)
        grvt_pos_after = await grvt.get_positions(pair)
        info("GRVT untouched after NADO failure", len(grvt_pos_after) == 0, f"positions={grvt_pos_after}")
        return "nado_failed"

    # Verify NADO position actually exists
    await asyncio.sleep(2)
    nado_positions = await nado.get_positions(pair)
    nado_has_pos = any(p["side"] == nado_pos_side and p["size"] > 0 for p in nado_positions)
    info(f"NADO {nado_pos_side} position present", nado_has_pos, f"positions={nado_positions}")

    # Step 2: GRVT
    print("\n  --- Step 2: GRVT entry (opposite direction) ---")
    t0 = time.time()
    grvt_res = await grvt.place_limit_order(pair, grvt_side, grvt_qty, grvt_order_price)
    elapsed = time.time() - t0
    grvt_ok = grvt_res.status == "filled"
    info(f"GRVT {grvt_side} ({elapsed:.1f}s)", grvt_ok, f"status={grvt_res.status} coid={grvt_res.order_id} filled={grvt_res.filled_size}")

    if not grvt_ok:
        # Defense path: GRVT failure -> rollback NADO
        print("\n  --- Step 2b: GRVT failed, rolling back NADO ---")
        actual_size = nado_positions[0]["size"] if nado_has_pos else nado_res.filled_size
        rollback_ok = await nado.close_position(pair, nado_pos_side, actual_size, slippage)
        info("NADO rollback", rollback_ok)
        await grvt.cancel_all_orders(pair)
        await asyncio.sleep(2)
        nado_after = await nado.get_positions(pair)
        info("NADO position cleared after rollback", len(nado_after) == 0 or all(p["size"] < 1e-9 for p in nado_after), f"{nado_after}")
        return "grvt_failed_rollback_ok" if rollback_ok else "grvt_failed_rollback_failed"

    # Verify GRVT position actually exists
    await asyncio.sleep(2)
    grvt_positions = await grvt.get_positions(pair)
    grvt_has_pos = any(p["side"] == grvt_pos_side and p["size"] > 0 for p in grvt_positions)
    info(f"GRVT {grvt_pos_side} position present", grvt_has_pos, f"positions={grvt_positions}")

    # Step 3: hold briefly
    print("\n  --- Step 3: holding 5s ---")
    await asyncio.sleep(5)

    # Step 4: close both
    print("\n  --- Step 4: bilateral close ---")
    nado_close_size = nado_positions[0]["size"] if nado_has_pos else nado_qty
    grvt_close_size = grvt_positions[0]["size"] if grvt_has_pos else grvt_qty
    print(f"  NADO close size: {nado_close_size}, GRVT close size: {grvt_close_size}")

    nado_close_task = nado.close_position(pair, nado_pos_side, nado_close_size, slippage)
    grvt_close_task = grvt.close_position(pair, grvt_pos_side, grvt_close_size, slippage)
    nado_close, grvt_close = await asyncio.gather(nado_close_task, grvt_close_task, return_exceptions=True)

    info("NADO close", nado_close is True, f"{nado_close}")
    info("GRVT close", grvt_close is True, f"{grvt_close}")

    # Cleanup any leftover orders
    await asyncio.gather(
        nado.cancel_all_orders(pair),
        grvt.cancel_all_orders(pair),
        return_exceptions=True,
    )

    # Final verification
    await asyncio.sleep(3)
    nado_after = await nado.get_positions(pair)
    grvt_after = await grvt.get_positions(pair)
    nado_clean = len(nado_after) == 0 or all(p["size"] < 1e-9 for p in nado_after)
    grvt_clean = len(grvt_after) == 0 or all(p["size"] < 1e-9 for p in grvt_after)
    info("NADO position fully closed", nado_clean, f"{nado_after}")
    info("GRVT position fully closed", grvt_clean, f"{grvt_after}")

    return "both_filled" if (nado_clean and grvt_clean) else "close_failed"


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pair", default="DOGE", help="Pair to test (default: DOGE)")
    parser.add_argument("--notional", type=float, default=20, help="Test notional in USD (default: $20)")
    parser.add_argument("--slippage", type=float, default=0.005, help="Slippage pct (default 0.5%)")
    parser.add_argument("--direction", default="A", choices=["A", "B"], help="A: NADO BUY / GRVT SELL, B: NADO SELL / GRVT BUY")
    parser.add_argument("--dry-run", action="store_true", help="Detect pair info only, no orders")
    args = parser.parse_args()

    print("=" * 60)
    print("  NADO x GRVT - Delta-Neutral Live Order Test")
    print("=" * 60)

    cfg = Config()
    nado = NadoClient(cfg.NADO_PRIVATE_KEY)
    grvt = GrvtClient(cfg.GRVT_API_KEY, cfg.GRVT_PRIVATE_KEY, cfg.GRVT_TRADING_ACCOUNT_ID)

    try:
        await asyncio.gather(nado.connect(), grvt.connect())
    except Exception as e:
        print(f"[FATAL] connect failed: {e}")
        sys.exit(1)

    nado_bal = await nado.get_balance()
    grvt_bal = await grvt.get_balance()
    print(f"\nBalances: NADO=${nado_bal:.2f} GRVT=${grvt_bal:.2f}")

    if nado_bal < 5 or grvt_bal < 5:
        print(f"[FATAL] insufficient balance for hedge test")
        await nado.close()
        await grvt.close()
        sys.exit(1)

    print(f"\n=== Pair info: {args.pair} ===")
    pinfo = await get_pair_info(nado, grvt, args.pair)
    if not pinfo:
        await nado.close()
        await grvt.close()
        sys.exit(1)

    # Auto-bump notional if below either min
    min_required = max(pinfo["nado_min_notional"], pinfo["grvt_min_notional"]) * 1.2
    if args.notional < min_required:
        print(f"\n  [adjusting] notional ${args.notional:.2f} -> ${min_required:.2f} (mins enforced)")
        args.notional = min_required

    if args.dry_run:
        print("\n[dry-run] skipping order test")
        await nado.close()
        await grvt.close()
        return

    result = await hedge_round_trip(nado, grvt, args.pair, pinfo, args.notional, args.slippage, args.direction)
    print(f"\n=== Result: {result} ===")

    await nado.close()
    await grvt.close()


if __name__ == "__main__":
    asyncio.run(main())
