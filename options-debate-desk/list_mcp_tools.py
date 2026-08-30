"""
Run this FIRST after setting up your .env, before trusting alpaca_tools.py.

Prints every tool Alpaca's MCP server exposes, plus its exact input schema.
alpaca-mcp-server v2 generates tools from Alpaca's OpenAPI specs, so exact
parameter names can differ slightly from what's guessed in alpaca_tools.py
(e.g. "symbol" vs "symbols", date formats, etc). Cross-check the tools you
use — get_account_info, get_stock_latest_quote, get_stock_bars,
get_option_chain, place_option_order — and adjust alpaca_tools.py to match.

Usage:
    python list_mcp_tools.py
    python list_mcp_tools.py get_option_chain     # show one tool's full schema
"""
import asyncio
import json
import sys

from mcp_client import AlpacaMCPClient


async def main():
    filter_name = sys.argv[1] if len(sys.argv) > 1 else None

    async with AlpacaMCPClient() as client:
        resp = await client._session.list_tools()
        for tool in resp.tools:
            if filter_name and tool.name != filter_name:
                continue
            print(f"\n=== {tool.name} ===")
            print(tool.description or "(no description)")
            print(json.dumps(tool.inputSchema, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
