"""
Tracks how many NEW positions the app has opened today, persisted to a
local JSON file so the count survives across separate process invocations
— each `python main.py X --execute` run, or each iteration of
run_continuous.py, is a fresh process with no in-memory state to share
with earlier runs today.

This enforces one half of the daily risk policy:
  1. No more than RISK_CONFIG["max_new_positions_per_day"] new positions
     opened per calendar day, across ALL entry points (single-symbol,
     --scan, run_continuous.py).

The other half — no new position in a symbol you already hold, until the
existing one is liquidated — is enforced separately in orchestrator.py via
a LIVE check against your actual Alpaca account
(alpaca_tools.get_held_underlying_symbols), not this file, since "what do
I currently hold" needs to be the real, current truth, not a local log
that could drift from reality (e.g. if a position was manually closed).

Uses local calendar dates (date.today()), matching the rest of the
codebase's date handling (e.g. days-to-expiry calculations).
"""
import json
from datetime import date
from pathlib import Path

LOG_PATH = Path("positions_opened_log.json")


def _load_log() -> list[dict]:
    if not LOG_PATH.exists():
        return []
    try:
        with open(LOG_PATH) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []  # corrupt/unreadable log — fail open rather than crash the app


def _save_log(entries: list[dict]) -> None:
    with open(LOG_PATH, "w") as f:
        json.dump(entries, f, indent=2, default=str)


def record_position_opened(symbol: str, order_id: str = None) -> None:
    """Call this ONLY after a real order is confirmed placed (a real
    order_id came back) — never speculatively before knowing it succeeded."""
    entries = _load_log()
    entries.append({
        "date": date.today().isoformat(),
        "symbol": symbol,
        "order_id": order_id,
    })
    _save_log(entries)


def count_positions_opened_today() -> int:
    today = date.today().isoformat()
    return sum(1 for e in _load_log() if e.get("date") == today)


def symbols_opened_today() -> set[str]:
    today = date.today().isoformat()
    return {e["symbol"] for e in _load_log() if e.get("date") == today}
