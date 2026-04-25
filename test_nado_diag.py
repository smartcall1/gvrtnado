"""NADO 계정 진단 — subaccount summary, health, positions, isolated positions 전체 출력"""
import asyncio
import os
import sys
import logging
from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


async def main():
    pk = os.getenv("NADO_PRIVATE_KEY")
    if not pk:
        print("ERROR: NADO_PRIVATE_KEY not set")
        sys.exit(1)

    from exchanges.nado_client import NadoClient
    client = NadoClient(pk)
    await client.connect()

    subhex = client._subaccount_hex
    print(f"subaccount: {subhex}")

    # 1. Full subaccount summary
    print(f"\n{'='*60}")
    print("SUBACCOUNT SUMMARY")
    print(f"{'='*60}")
    info = await asyncio.to_thread(
        client._client.subaccount.get_engine_subaccount_summary, subhex
    )
    if info:
        # Print all available attributes
        for attr in dir(info):
            if attr.startswith('_'):
                continue
            val = getattr(info, attr)
            if callable(val):
                continue
            if attr == "healths":
                print(f"\n  healths ({len(val)} entries):")
                for i, h in enumerate(val):
                    print(f"    [{i}]:")
                    for ha in dir(h):
                        if ha.startswith('_') or callable(getattr(h, ha)):
                            continue
                        hv = getattr(h, ha)
                        if isinstance(hv, str) and len(hv) > 10:
                            # x18 value
                            try:
                                hv_f = int(hv) / 1e18
                                print(f"      {ha}: {hv} (${hv_f:.4f})")
                            except:
                                print(f"      {ha}: {hv}")
                        else:
                            print(f"      {ha}: {hv}")
            elif attr == "spot_balances":
                print(f"\n  spot_balances ({len(val)} entries):")
                for sb in val:
                    pid = sb.product_id
                    bal = int(sb.balance.amount) / 1e18
                    print(f"    product_id={pid}: balance={bal:.6f}")
            elif attr == "perp_balances":
                print(f"\n  perp_balances ({len(val)} entries):")
                for pb in val:
                    pid = pb.product_id
                    amt = int(pb.balance.amount) / 1e18
                    if abs(amt) > 0:
                        print(f"    product_id={pid}: amount={amt:.6f}")
            else:
                if isinstance(val, (list, tuple)) and len(val) > 0:
                    print(f"\n  {attr} ({len(val)} entries): {val[:3]}...")
                else:
                    print(f"  {attr}: {val}")

    # 2. Check isolated positions
    print(f"\n{'='*60}")
    print("ISOLATED POSITIONS")
    print(f"{'='*60}")
    try:
        iso_pos = await asyncio.to_thread(
            client._client.subaccount.get_isolated_positions, subhex
        )
        if iso_pos:
            for attr in dir(iso_pos):
                if attr.startswith('_') or callable(getattr(iso_pos, attr)):
                    continue
                print(f"  {attr}: {getattr(iso_pos, attr)}")
        else:
            print("  No isolated positions")
    except Exception as e:
        print(f"  Error getting isolated positions: {e}")

    # 3. Try BTC cross-margin order (known to work)
    print(f"\n{'='*60}")
    print("BTC CROSS-MARGIN TEST (tiny order)")
    print(f"{'='*60}")
    btc_price = await client.get_mark_price("BTC")
    print(f"  BTC price: ${btc_price:.2f}")
    btc_size_inc = client._increments.get(2, {}).get("size", 50000000000000) / 1e18
    btc_test_size = 110 / btc_price
    import math
    btc_test_size = math.ceil(btc_test_size / btc_size_inc) * btc_size_inc
    btc_notional = btc_test_size * btc_price
    print(f"  test_size={btc_test_size:.6f} BTC, notional=${btc_notional:.2f}")

    buy_price = btc_price * 1.005
    r = await client.place_limit_order("BTC", "BUY", btc_test_size, buy_price)
    print(f"  BTC BUY result: status={r.status}, msg={r.message}")

    if r.status in ("filled", "matched"):
        print("  BTC cross-margin order works!")
        await asyncio.sleep(1)
        close_ok = await client.close_position("BTC", "LONG", btc_test_size, 0.01)
        print(f"  BTC close result: {close_ok}")
        await asyncio.sleep(1)
    else:
        print("  BTC cross-margin also failed!")

    # 4. Final balance
    bal = await client.get_balance()
    print(f"\n  Final balance: ${bal:.2f}")

    await client.close()


if __name__ == "__main__":
    asyncio.run(main())
