"""
Directly checks whether alpaca_tools.get_held_underlying_symbols() and
get_market_clock() correctly parse your REAL account state — the two
functions flagged as unverified (unlike everything else in alpaca_tools.py,
these haven't been cross-checked against a real response the way
test_raw_call.py verified the others).

Usage:
    python check_held_positions.py
"""
import asyncio
import json

from mcp_client import AlpacaMCPClient
import alpaca_tools


async def main():
    async with AlpacaMCPClient() as client:
        print("=== Raw get_all_positions response ===")
        raw = await client.call_tool("get_all_positions", {})
        print(json.dumps(raw, indent=2, default=str)[:3000])

        print("\n=== What get_held_underlying_symbols() extracted from it ===")
        held = await alpaca_tools.get_held_underlying_symbols(client)
        print(f"Held underlying symbols: {held}")

        print("\n=== Raw get_clock response ===")
        raw_clock = await client.call_tool("get_clock", {})
        print(json.dumps(raw_clock, indent=2, default=str)[:1000])

        print("\n=== What get_market_clock() extracted from it ===")
        clock = await alpaca_tools.get_market_clock(client)
        print(clock)


if __name__ == "__main__":
    asyncio.run(main())