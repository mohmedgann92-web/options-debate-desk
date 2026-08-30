"""
Checks your real, recent orders directly via Alpaca's MCP get_orders tool —
bypassing place_option_spread_order's (possibly wrong) response parsing
entirely. Use this any time you're not sure whether an order actually went
through.

Usage:
    python check_recent_orders.py
"""
import asyncio
import json

from mcp_client import AlpacaMCPClient


async def main():
    async with AlpacaMCPClient() as client:
        result = await client.call_tool(
            "get_orders",
            {"status": "all", "limit": 10, "direction": "desc"},
        )
        data = result.get("data", result)
        print(json.dumps(data, indent=2, default=str)[:6000])


if __name__ == "__main__":
    asyncio.run(main())