"""
Bypasses MCP entirely and hits Alpaca's paper REST API directly with the
keys loaded from .env. Isolates whether the 401 is a bad-key problem or a
problem with how mcp_client.py passes env vars to the MCP subprocess.

Usage:
    python check_alpaca_keys.py
"""
import urllib.request
import urllib.error
import json

import config


def mask(s: str) -> str:
    if not s:
        return "(empty!)"
    return s[:4] + "..." + s[-4:] + f"  (length={len(s)})"


print("Loaded from .env:")
print("  ALPACA_API_KEY:   ", mask(config.ALPACA_API_KEY))
print("  ALPACA_SECRET_KEY:", mask(config.ALPACA_SECRET_KEY))
print("  ALPACA_PAPER:     ", config.ALPACA_PAPER)
print()

url = "https://paper-api.alpaca.markets/v2/account"
req = urllib.request.Request(url)
req.add_header("APCA-API-KEY-ID", config.ALPACA_API_KEY or "")
req.add_header("APCA-API-SECRET-KEY", config.ALPACA_SECRET_KEY or "")

try:
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())
        print("SUCCESS — direct REST call worked. Account status:", data.get("status"))
        print("Equity:", data.get("equity"))
except urllib.error.HTTPError as e:
    print(f"FAILED — HTTP {e.code}")
    print(e.read().decode())
