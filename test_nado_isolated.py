"""NADO 격리마진 실전 검증 — 여러 isolated-only 마켓 순회
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

CANDIDATES = [
    # crypto (isolated-only 가능성 있는 것들)
    "HYPE", "MON", "DOGE", "SUI", "AAVE", "PUMP", "TAO",
    "PENGU", "FARTCOIN", "KPEPE", "KBONK", "BERA", "VIRTUAL",
    "LIT", "XPL", "ASTER", "WLFI", "SKR", "USELESS",
    # stocks/ETFs
    "AAPL", "NVDA", "MSFT", "META", "GOOGL", "AMZN", "TSLA",
]


async def try_symbol(client, sym, balance):
    print(f"\n{'#'*60}")
    print(f"  TESTING: {sym}")
    print(f"{'#'*60}")

    price = await client.get_mark_price(sym)
    if not price:
        print(f"  SKIP: {sym} 가격 조회 실패")
        return False

    max_lev = client.get_max_leverage(sym)
    pid = client._product_id(sym)
    inc = client._increments.get(pid, {})
    size_inc = inc.get("size", 5 * 10**15) / 1e18
    print(f"  price=${price:.2f}, max_lev={max_lev:.0f}x, size_inc={size_inc}")

    test_size = math.ceil(110 / price / size_inc) * size_inc
    notional = test_size * price
    margin = notional / max_lev * 2
    print(f"  size={test_size:.4f}, notional=${notional:.2f}, margin=${margin:.2f} (2x for safety)")

    if balance < margin * 1.5:
        print(f"  SKIP: 잔고 부족 ${balance:.2f} < ${margin*1.5:.2f}")
        return False

    # 1. BUY
    buy_price = price * 1.005
    r = await client.place_limit_order(sym, "BUY", test_size, buy_price, isolated_margin=margin)
    print(f"  BUY result: status={r.status}, msg={r.message}")

    if r.status not in ("filled", "matched"):
        print(f"  FAIL: {sym} 매수 실패")
        return False

    print(f"  BUY SUCCESS!")
    await asyncio.sleep(2)

    # 2. Position check
    positions = await client.get_positions(sym)
    print(f"  positions: {positions}")

    # 3. Close
    close_ok = await client.close_position(sym, "LONG", test_size, 0.01)
    print(f"  close result: {close_ok}")
    if not close_ok:
        await asyncio.sleep(2)
        close_ok = await client.close_position(sym, "LONG", test_size, 0.01)
        print(f"  retry close: {close_ok}")

    await asyncio.sleep(2)

    # 4. Final state
    final_pos = await client.get_positions(sym)
    final_bal = await client.get_balance()
    pnl = final_bal - balance
    print(f"  잔여 포지션: {final_pos}")
    print(f"  잔고: ${final_bal:.2f} (변동: ${pnl:+.4f})")

    if not final_pos:
        print(f"\n  ALL PASSED — {sym} 매수/청산 정상!")
    else:
        print(f"\n  WARNING: {sym} 포지션 남아있음!")
    return True


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

    for sym in CANDIDATES:
        try:
            ok = await try_symbol(client, sym, balance)
            if ok:
                break
        except Exception as e:
            print(f"  ERROR: {sym} — {str(e)[:150]}")
            continue
    else:
        print("\nAll candidate markets failed.")

    await client.close()


if __name__ == "__main__":
    asyncio.run(main())
