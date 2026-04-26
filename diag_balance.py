"""Balance diagnostic (read-only)."""
import asyncio
import json
import os
import sys
from dotenv import load_dotenv

load_dotenv()
sys.stdout.reconfigure(encoding="utf-8")


async def diag_nado():
    print("\n" + "=" * 70)
    print("NADO DIAGNOSTIC")
    print("=" * 70)
    pk = os.getenv("NADO_PRIVATE_KEY")
    if not pk:
        print("  NADO_PRIVATE_KEY missing -- skip")
        return None

    from exchanges.nado_client import NadoClient
    c = NadoClient(pk)
    await c.connect()

    info = await asyncio.to_thread(
        c._client.subaccount.get_engine_subaccount_summary, c._subaccount_hex
    )

    # 1. healths array (likely [initial, maintenance, unweighted])
    print("\n[healths]")
    for i, h in enumerate(info.healths):
        attrs = {}
        for a in dir(h):
            if a.startswith("_") or callable(getattr(h, a)):
                continue
            v = getattr(h, a)
            if isinstance(v, str) and v.lstrip("-").isdigit() and len(v) > 6:
                try:
                    attrs[a] = f"{v} (${int(v) / 1e18:+,.4f})"
                except Exception:
                    attrs[a] = v
            else:
                attrs[a] = v
        print(f"  [{i}]: {attrs}")

    # 2. health_contributions (per product, 3 cols)
    print("\n[health_contributions: non-zero rows]")
    print("  cols probably: [initial_weighted, maintenance_weighted, unweighted]")
    for pid, row in enumerate(info.health_contributions):
        if any(int(x) != 0 for x in row):
            vals = [f"{int(x) / 1e18:+,.4f}" for x in row]
            print(f"  pid={pid:3d}: [{vals[0]}, {vals[1]}, {vals[2]}]")

    # 3. unweighted total = sum of col 2
    total_unweighted = sum(int(row[2]) for row in info.health_contributions) / 1e18
    total_initial = sum(int(row[0]) for row in info.health_contributions) / 1e18
    total_maint = sum(int(row[1]) for row in info.health_contributions) / 1e18
    print(f"\n  sum col[0] (initial_weighted):     ${total_initial:+,.4f}")
    print(f"  sum col[1] (maintenance_weighted): ${total_maint:+,.4f}")
    print(f"  sum col[2] (unweighted = equity?): ${total_unweighted:+,.4f}")

    # 4. perp_balance for SOL
    print("\n[SOL perp_balance]")
    sol_pid = c._product_id("SOL")
    for pb in info.perp_balances:
        if pb.product_id == sol_pid:
            amt = int(pb.balance.amount) / 1e18
            vqb = int(pb.balance.v_quote_balance) / 1e18
            funding = int(pb.balance.last_cumulative_funding_x18) / 1e18
            print(f"  pid={pb.product_id}  size={amt:+.4f} SOL")
            print(f"  v_quote_balance: ${vqb:+,.4f}")
            print(f"  last_cumulative_funding: {funding:+,.6f}")
            mark = await c.get_mark_price("SOL")
            theoretical = amt * mark + vqb
            print(f"  current mark: ${mark:,.4f}")
            print(f"  theoretical unrealized = size*mark + vqb = ${theoretical:+,.4f}")
            break

    bal_via_bot = await c.get_balance()
    print(f"\n  bot.get_balance() = ${bal_via_bot:,.4f}")
    print(f"  (patched to use healths[2].health = unweighted equity)")

    await c.close()
    return bal_via_bot, total_unweighted


async def diag_grvt():
    print("\n" + "=" * 70)
    print("GRVT DIAGNOSTIC")
    print("=" * 70)
    api_key = os.getenv("GRVT_API_KEY")
    pk = os.getenv("GRVT_PRIVATE_KEY")
    acct = os.getenv("GRVT_TRADING_ACCOUNT_ID")
    if not (api_key and pk and acct):
        print("  GRVT credentials missing -- skip")
        return None

    from exchanges.grvt_client import GrvtClient
    c = GrvtClient(api_key, pk, acct)
    await c.connect()

    print("\n[fetch_balance() raw]")
    bal_raw = await c._retry(c._api.fetch_balance)
    print(json.dumps(bal_raw, indent=2, default=str))

    print("\n[fetch_positions() SOL raw]")
    sol_sym = c._grvt_symbol("SOL")
    poss = await c._retry(c._api.fetch_positions, [sol_sym])
    print(json.dumps(poss, indent=2, default=str))

    # candidates
    print("\n[unrealized PnL candidate fields]")
    cands = []
    for p in poss or []:
        for k, v in p.items():
            if k == "info" or v is None:
                continue
            if any(s in k.lower() for s in ("unreal", "upnl", "pnl")):
                cands.append(("position", k, v))
        info_p = p.get("info") or {}
        if isinstance(info_p, dict):
            for k, v in info_p.items():
                if any(s in k.lower() for s in ("unreal", "upnl", "pnl", "equity")):
                    cands.append(("position.info", k, v))
    if isinstance(bal_raw, dict):
        for top, val in bal_raw.items():
            if isinstance(val, dict):
                for k, v in val.items():
                    if any(s in k.lower() for s in ("unreal", "upnl", "pnl", "equity")):
                        cands.append((f"balance.{top}", k, v))
        info_b = bal_raw.get("info")
        if isinstance(info_b, list):
            for item in info_b:
                if isinstance(item, dict):
                    for k, v in item.items():
                        if any(s in k.lower() for s in ("unreal", "upnl", "pnl", "equity", "balance")):
                            cands.append(("balance.info[]", k, v))
    for src, k, v in cands:
        print(f"  {src}: {k} = {v}")

    bal_via_bot = await c.get_balance()
    print(f"\n  bot.get_balance() = ${bal_via_bot:,.4f}  (patched: wallet + Σ positions.unrealized_pnl)")

    # candidate equity = total + unrealized_pnl from info[]
    info_b = bal_raw.get("info") if isinstance(bal_raw, dict) else None
    if isinstance(info_b, list) and info_b:
        for item in info_b:
            if item.get("currency") == "USDT":
                bal = float(item.get("balance", 0))
                upnl = float(item.get("unrealized_pnl", 0))
                print(f"  candidate equity = balance + unrealized_pnl = ${bal:+.4f} + ${upnl:+.4f} = ${bal + upnl:+,.4f}")
                break

    await c.close()
    return bal_via_bot


async def main():
    nado_res = await diag_nado()
    grvt_bal = await diag_grvt()

    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    if nado_res:
        nado_bal, nado_unweighted = nado_res
        print(f"  NADO bot.get_balance():        ${nado_bal:+,.2f}")
        print(f"  NADO sum unweighted_health:    ${nado_unweighted:+,.2f}  (candidate true equity)")
    if grvt_bal:
        print(f"  GRVT bot.get_balance():        ${grvt_bal:+,.2f}")


if __name__ == "__main__":
    asyncio.run(main())
