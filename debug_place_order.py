"""
Places ONE small, real diagnostic paper order using the ACTUAL
alpaca_tools.place_option_spread_order function — not a hand-rolled copy of
it — so this script can never drift out of sync with real fixes again.

Uses a fresh, currently-valid contract fetched right before submitting.

This WILL place a real paper order if it works. That's fine — it's paper
money and the point is to confirm order placement actually works — but
don't run this repeatedly without noticing you're accumulating positions.

Usage:
    python debug_place_order.py AAPL
"""
import asyncio
import sys

from mcp_client import AlpacaMCPClient
import alpaca_tools


async def main():
    symbol = sys.argv[1] if len(sys.argv) > 1 else "AAPL"

    async with AlpacaMCPClient() as client:
        quote = await alpaca_tools.get_stock_quote(client, symbol)
        underlying_price = quote["mid"] or quote["ask"] or quote["bid"]
        print(f"{symbol} underlying price: {underlying_price}")

        chain = await alpaca_tools.get_option_chain(
            client, symbol, underlying_price,
            min_dte=7, max_dte=30, limit=10,
            strike_price_gte=round(underlying_price * 0.97, 2),
            strike_price_lte=round(underlying_price * 1.0, 2),
        )
        calls = [c for c in chain if c["option_type"] == "call" and c["mid"]]
        if not calls:
            print("No suitable call contracts found — try a different symbol.")
            return
        target = min(calls, key=lambda c: c["mid"])
        print(f"Using contract: {target['symbol']} strike={target['strike']} mid={target['mid']}")

        legs = [{"symbol": target["symbol"], "side": "buy", "ratio_qty": 1}]
        chain_by_symbol = {c["symbol"]: c for c in chain}
        limit_price = alpaca_tools.compute_limit_price(legs, chain_by_symbol)
        print(f"Computed limit price: {limit_price}")
        print(f"\nCalling alpaca_tools.place_option_spread_order(legs={legs}, qty=1, limit_price={limit_price})...")

        try:
            result = await alpaca_tools.place_option_spread_order(client, legs, qty=1, limit_price=limit_price)
            print(f"\n✓ SUCCESS: {result}")
        except RuntimeError as e:
            print(f"\n✗ FAILED (raised loudly, as designed): {e}")


if __name__ == "__main__":
    asyncio.run(main())
