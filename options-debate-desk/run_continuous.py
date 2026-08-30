"""
Runs the scan + multi-candidate debate loop repeatedly at a fixed interval,
so you can leave it running to catch opportunities as market data changes
throughout the day rather than invoking `python main.py --scan` by hand.

Each run's results are appended to a journal file (JSONL — one JSON object
per line) so you build a persistent record across the whole testing/judging
window. Use view_journal.py to review it.

COST NOTE: every iteration runs up to --top-n full four-agent debates. At
the default interval and top-n, expect real LLM API usage (or Groq
rate-limit waits) roughly every --interval minutes — don't set the interval
too low, especially on Groq's free tier.

SAFETY NOTE on --execute: with auto-execute on, this WILL place real (paper)
orders unattended. skip_held_positions defaults to True specifically so it
diversifies across runs instead of repeatedly stacking into whatever ranks
#1 that hour — but the underlying get_all_positions parsing is unverified
(see alpaca_tools.get_held_underlying_symbols' docstring), so this script
prints what it detects as currently held at the START of every run. Watch
that line for the first few runs before trusting an unattended overnight
session.

Usage:
    python run_continuous.py                                  # scan default watchlist every 60 min
    python run_continuous.py --interval 30 --top-n 2           # tighter loop, fewer candidates per run
    python run_continuous.py --execute                         # also place real paper trades each run
    python run_continuous.py --market-hours-only               # skip scans when the market's closed
    python run_continuous.py --max-runs 3                       # stop after 3 iterations (good for testing this script)

Stop anytime with Ctrl+C — it exits cleanly after the current iteration.
"""
import argparse
import asyncio
import json
from datetime import datetime

import config
import alpaca_tools
from mcp_client import AlpacaMCPClient
from orchestrator import run_scan

DEFAULT_JOURNAL_PATH = "trade_journal.jsonl"


def _append_journal(entry: dict, path: str):
    with open(path, "a") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def _summarize_run(scan_result: dict) -> list[dict]:
    """Boils a full run_scan() result down to the compact fields worth
    keeping in a long-running journal, rather than the full transcripts
    (those are still in each run's debug_market_data_*.json if you need
    the full detail on a specific decision)."""
    debates = scan_result.get("debates", [])
    summary = []
    for d in debates:
        final = d["debate"]["final_decision"]
        gov = d["debate"]["governor_result"]
        summary.append({
            "symbol": d["symbol"],
            "action": final["action"],
            "qty": final.get("qty", 0),
            "governor_approved": gov["approved"],
            "real_max_loss_total": gov.get("real_max_loss_total"),
            "real_max_loss_pct_equity": gov.get("real_max_loss_pct_equity"),
        })
    return summary


async def main():
    parser = argparse.ArgumentParser(description="Run the scan+debate pipeline on a repeating interval")
    parser.add_argument("--interval", type=int, default=60, help="Minutes between scans (default 60)")
    parser.add_argument("--top-n", type=int, default=3, help="Eligible candidates to debate per run (default 3)")
    parser.add_argument("--symbols", type=str, default=None, help="Comma-separated watchlist override")
    parser.add_argument("--execute", action="store_true", help="Actually place approved trades as PAPER orders")
    parser.add_argument("--no-skip-held", action="store_true",
                         help="Allow re-trading symbols you already hold (default: skip them)")
    parser.add_argument("--market-hours-only", action="store_true",
                         help="Skip scanning when the market's closed (checked via Alpaca's clock)")
    parser.add_argument("--max-runs", type=int, default=None, help="Stop after this many iterations (default: run forever)")
    parser.add_argument("--journal", type=str, default=DEFAULT_JOURNAL_PATH, help="Path to the JSONL journal file")
    args = parser.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",")] if args.symbols else config.WATCHLIST
    skip_held = not args.no_skip_held

    run_count = 0
    print(f"Starting continuous runner: every {args.interval} min, top-n={args.top_n}, "
          f"execute={args.execute}, skip_held_positions={skip_held}, journal={args.journal}")
    print("Press Ctrl+C to stop.\n")

    try:
        while args.max_runs is None or run_count < args.max_runs:
            run_count += 1
            timestamp = datetime.now().isoformat()
            print(f"\n{'='*70}\n[{timestamp}] Run #{run_count}\n{'='*70}\n")

            if args.market_hours_only:
                async with AlpacaMCPClient() as client:
                    clock = await alpaca_tools.get_market_clock(client)
                if clock.get("is_open") is False:
                    print(f"Market is closed (next open: {clock.get('next_open', 'unknown')}). Skipping this run.")
                    _append_journal({"timestamp": timestamp, "run": run_count, "skipped": "market_closed"}, args.journal)
                    await asyncio.sleep(args.interval * 60)
                    continue
                elif clock.get("is_open") is None:
                    print("Could not determine market status (unverified get_clock parsing) — proceeding anyway.")

            try:
                scan_result = await run_scan(symbols, execute=args.execute, top_n=args.top_n,
                                              skip_held_positions=skip_held)
                summary = _summarize_run(scan_result)
                _append_journal({"timestamp": timestamp, "run": run_count, "results": summary}, args.journal)
                print(f"\nRun #{run_count} logged to {args.journal}")
            except Exception as exc:
                print(f"Run #{run_count} FAILED: {exc}")
                _append_journal({"timestamp": timestamp, "run": run_count, "error": str(exc)}, args.journal)

            if args.max_runs is None or run_count < args.max_runs:
                print(f"\nSleeping {args.interval} minutes until next run...")
                await asyncio.sleep(args.interval * 60)

    except KeyboardInterrupt:
        print(f"\n\nStopped by user after {run_count} run(s). Journal saved to {args.journal}.")


if __name__ == "__main__":
    asyncio.run(main())
