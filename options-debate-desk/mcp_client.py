"""
Connects to Alpaca's OFFICIAL MCP server (alpacahq/alpaca-mcp-server, v2)
over the Model Context Protocol — this is the literal integration the
hackathon calls "the core of the hackathon theme": an AI assistant talking
to Alpaca through structured MCP tools, not a direct SDK call.

The server is launched as a subprocess via `uvx alpaca-mcp-server` and
communicates over stdio, exactly as it would if you'd wired it into Claude
Desktop or Cursor — we're just driving it programmatically instead.

Requires `uv`/`uvx` on PATH: https://docs.astral.sh/uv/getting-started/installation/
"""
import json
from contextlib import AsyncExitStack

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import config


class AlpacaMCPClient:
    """Async context manager wrapping a live session with Alpaca's MCP server.

    Usage:
        async with AlpacaMCPClient() as client:
            account = await client.call_tool("get_account_info", {})
    """

    def __init__(self):
        self._session: ClientSession | None = None
        self._stack: AsyncExitStack | None = None

    async def __aenter__(self) -> "AlpacaMCPClient":
        self._stack = AsyncExitStack()
        server_params = StdioServerParameters(
            command="uvx",
            args=["alpaca-mcp-server"],
            env={
                "ALPACA_API_KEY": config.ALPACA_API_KEY or "",
                "ALPACA_SECRET_KEY": config.ALPACA_SECRET_KEY or "",
                "ALPACA_PAPER_TRADE": "true" if config.ALPACA_PAPER else "false",
            },
        )
        read, write = await self._stack.enter_async_context(stdio_client(server_params))
        self._session = await self._stack.enter_async_context(ClientSession(read, write))
        await self._session.initialize()
        return self

    async def __aexit__(self, *exc_info):
        await self._stack.aclose()

    async def list_tools(self) -> list[str]:
        resp = await self._session.list_tools()
        return [t.name for t in resp.tools]

    async def call_tool(self, name: str, arguments: dict) -> dict | list:
        """
        Calls an MCP tool and parses its text content as JSON where possible.
        Raises with the tool's own error message if the server reports one.
        """
        result = await self._session.call_tool(name, arguments)
        if getattr(result, "isError", False):
            raise RuntimeError(f"MCP tool '{name}' returned an error: {result.content}")

        text_parts = [
            block.text for block in result.content if getattr(block, "type", None) == "text"
        ]
        raw = "\n".join(text_parts)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"raw": raw}
