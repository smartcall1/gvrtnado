"""NADO 격리마진 실전 검증 — isolated_margin x6 포맷 확인
매수(BUY) → 포지션 확인 → 청산(close_position) → 잔고 확인
"""
import asyncio
import math
import os
import sys
import logging

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


async def main():
    pk = os.getenv("NADO_PRIVATE_KEY")
    if not pk:
        print("ERROR: NADO_PRIVATE_KEY not set in .env")
        sys.exit(1)

    from exchanges.nado_client import NadoClient
    client = NadoClient(pk)
    await client.connect()

    balance = await client.get_balance()
    print(f"NADO balance: ${balance:.2f}")

    sym = "TSLA"
    price = await client.get_mark_price(sym)
    if not price:
        print(f"ERROR: {sym} 가격 조회 실패")
        await client.close()
        sys.exit(1)
    print(f"{sym} price: ${price:.2f}")

    max_lev = client.get_max_leverage(sym)
    pid = client._product_id(sym)
    inc = client._increments.get(pid, {})
    size_inc = inc.get("size", 5 * 10**15) / 1e18
    print(f"max_leverage={max_lev:.0f}x, size_inc={size_inc}")

    # 최소 notional $110 이상 (min_size=$100 + 여유)
    test_size = math.ceil(110 / price / size_inc) * size_inc
    notional = test_size * price
    margin = notional / max_lev
    print(f"test_size={test_size:.4f}, notional=${notional:.2f}, margin=${margin:.2f}")

    if balance < margin * 1.5:
        print(f"ERROR: 잔고 ${balance:.2f} < 필요 마진 ${margin*1.5:.2f}")
        await client.close()
        sys.exit(1)

    # ===== 1. 매수 (BUY) =====
    print(f"\n{'='*60}")
    print(f"STEP 1: {sym} BUY (isolated margin, x6 format)")
    print(f"{'='*60}")
    buy_price = price * 1.005
    r = await client.place_limit_order(sym, "BUY", test_size, buy_price, isolated_margin=margin)
    print(f"  result: status={r.status}, msg={r.message}")

    if r.status not in ("filled", "matched"):
        print(f"  FAIL: 매수 실패 — {r.status}: {r.message}")
        await client.close()
        sys.exit(1)

    print("  BUY SUCCESS!")
    await asyncio.sleep(2)

    # ===== 2. 포지션 확인 =====
    print(f"\n{'='*60}")
    print(f"STEP 2: {sym} 포지션 확인")
    print(f"{'='*60}")
    positions = await client.get_positions(sym)
    print(f"  positions: {positions}")
    if not positions:
        print("  WARNING: 포지션이 비어있음 (체결 지연?)")

    # ===== 3. 청산 (CLOSE) =====
    print(f"\n{'='*60}")
    print(f"STEP 3: {sym} 포지션 청산")
    print(f"{'='*60}")
    close_ok = await client.close_position(sym, "LONG", test_size, 0.01)
    print(f"  close result: {close_ok}")

    if not close_ok:
        print("  FAIL: 청산 실패!")
        print("  재시도...")
        await asyncio.sleep(2)
        close_ok = await client.close_position(sym, "LONG", test_size, 0.01)
        print(f"  retry close result: {close_ok}")

    await asyncio.sleep(2)

    # ===== 4. 최종 상태 =====
    print(f"\n{'='*60}")
    print(f"STEP 4: 최종 상태 확인")
    print(f"{'='*60}")
    final_positions = await client.get_positions(sym)
    final_balance = await client.get_balance()
    pnl = final_balance - balance
    print(f"  잔여 포지션: {final_positions}")
    print(f"  최종 잔고: ${final_balance:.2f} (변동: ${pnl:+.4f})")

    if not final_positions:
        print("\n  ALL TESTS PASSED — 매수/청산 모두 정상!")
    else:
        print("\n  WARNING: 포지션이 아직 남아있음!")

    await client.close()


if __name__ == "__main__":
    asyncio.run(main())
