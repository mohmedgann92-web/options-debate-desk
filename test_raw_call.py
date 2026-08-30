"""
Run this against a real ticker to see the RAW output shape of the tools
alpaca_tools.py depends on. The tool schemas we already confirmed only
document inputs, not outputs, so this is how we lock down the exact field
names (bid_price vs bp, close vs c, etc).

Usage:
    python test_raw_call.py AAPL

Paste the output back if anything in alpaca_tools.py isn't parsing correctly.
"""
import asyncio
import json
import sys

from mcp_client import AlpacaMCPClient


async def main():
    symbol = sys.argv[1] if len(sys.argv) > 1 else "AAPL"

    async with AlpacaMCPClient() as client:
        print(f"\n=== get_account_info ===")
        print(json.dumps(await client.call_tool("get_account_info", {}), indent=2)[:1000])

        print(f"\n=== get_stock_latest_quote({symbol}) ===")
        print(json.dumps(await client.call_tool("get_stock_latest_quote", {"symbols": symbol}), indent=2)[:1000])

        print(f"\n=== get_stock_bars({symbol}, 5 days) ===")
        print(json.dumps(
            await client.call_tool("get_stock_bars", {"symbols": symbol, "timeframe": "1Day", "days": 5, "limit": 5}),
            indent=2
        )[:1500])

        print(f"\n=== get_option_chain({symbol}) - ONE FULL CONTRACT (untruncated) ===")
        result = await client.call_tool("get_option_chain", {"underlying_symbol": symbol, "limit": 1})
        data = result.get("data", result)
        snapshots = data.get("snapshots", {})
        if snapshots:
            first_symbol = next(iter(snapshots))
            print(json.dumps({first_symbol: snapshots[first_symbol]}, indent=2))
        else:
            print("No snapshots returned. Full raw result:")
            print(json.dumps(result, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
