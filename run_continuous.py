"""
Runs the scan + multi-candidate debate loop repeatedly at a fixed interval,
so you can leave it running to catch opportunities as market data changes
throughout the day rather than invoking `python main.py --scan` by hand.

Every individual trade decision is logged automatically to
trade_journal.jsonl by orchestrator.py's shared execution gate — this
applies to EVERY entry point (single-symbol, --scan, and this script), not
just here. This script additionally logs its own run-level events (skipped
runs, hard failures) that happen outside any single symbol's debate. Use
view_journal.py to review the whole thing.

COST NOTE: every iteration runs up to --top-n full four-agent debates. At
the default interval and top-n, expect real LLM API usage (or Groq
rate-limit waits) roughly every --interval minutes — don't set the interval
too low, especially on Groq's free tier.

MARKET HOURS: protection is ON BY DEFAULT — this script skips the ENTIRE
scan (saving LLM cost) when markets are closed, AND orchestrator.py's
execute gate independently blocks any actual order placement when closed,
as a backstop. This exists because of a real incident during testing: an
after-hours run showed 70-120% implied vol on calm mega-caps (stale/
degraded quotes distorting the Black-Scholes math), and would have executed
a trade off that bad data if a hallucinated leg symbol hadn't happened to
block it for an unrelated reason. Use --ignore-market-hours to disable both
layers if you deliberately want to test outside market hours (fine for
--scan without --execute; NOT recommended with --execute).

SAFETY NOTE on --execute: with auto-execute on, this WILL place real (paper)
orders unattended. skip_held_positions defaults to True specifically so it
diversifies across runs instead of repeatedly stacking into whatever ranks
#1 that hour — but the underlying get_all_positions parsing is unverified
(see alpaca_tools.get_held_underlying_symbols' docstring), so this script
prints what it detects as currently held at the START of every run. Watch
that line for the first few runs before trusting an unattended overnight
session.

Usage:
    python run_continuous.py                                  # scan default watchlist every 60 min, market-hours only
    python run_continuous.py --interval 30 --top-n 2           # tighter loop, fewer candidates per run
    python run_continuous.py --execute                         # also place real paper trades each run
    python run_continuous.py --ignore-market-hours              # also scan/trade while markets are closed (not recommended with --execute)
    python run_continuous.py --max-runs 3                       # stop after 3 iterations (good for testing this script)

Stop anytime with Ctrl+C — it exits cleanly after the current iteration.
"""
import argparse
import asyncio
from datetime import datetime

import config
import alpaca_tools
import trade_journal
from mcp_client import AlpacaMCPClient
from orchestrator import run_scan


async def main():
    parser = argparse.ArgumentParser(description="Run the scan+debate pipeline on a repeating interval")
    parser.add_argument("--interval", type=int, default=60, help="Minutes between scans (default 60)")
    parser.add_argument("--top-n", type=int, default=3, help="Eligible candidates to debate per run (default 3)")
    parser.add_argument("--symbols", type=str, default=None, help="Comma-separated watchlist override")
    parser.add_argument("--execute", action="store_true", help="Actually place approved trades as PAPER orders")
    parser.add_argument("--no-skip-held", action="store_true",
                         help="Allow re-trading symbols you already hold (default: skip them)")
    parser.add_argument(
        "--ignore-market-hours", action="store_true",
        help="Scan (and if --execute is on, trade) even when markets are closed. NOT recommended "
             "with --execute: after-hours option quotes are often stale/degraded and can distort "
             "the risk math this whole pipeline depends on. Default is market-hours-only.",
    )
    parser.add_argument("--max-runs", type=int, default=None, help="Stop after this many iterations (default: run forever)")
    args = parser.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",")] if args.symbols else config.WATCHLIST
    skip_held = not args.no_skip_held

    run_count = 0
    print(f"Starting continuous runner: every {args.interval} min, top-n={args.top_n}, "
          f"execute={args.execute}, skip_held_positions={skip_held}, "
          f"market_hours_only={not args.ignore_market_hours}, journal={trade_journal.JOURNAL_PATH}")
    print("Press Ctrl+C to stop.\n")

    try:
        while args.max_runs is None or run_count < args.max_runs:
            run_count += 1
            print(f"\n{'='*70}\n[{datetime.now().isoformat()}] Run #{run_count}\n{'='*70}\n")

            if not args.ignore_market_hours:
                async with AlpacaMCPClient() as client:
                    clock = await alpaca_tools.get_market_clock(client)
                if clock.get("is_open") is False:
                    print(f"Market is closed (next open: {clock.get('next_open', 'unknown')}). Skipping this run "
                          f"(pass --ignore-market-hours to scan anyway).")
                    trade_journal.log_entry({"kind": "run_skipped", "run": run_count, "reason": "market_closed"})
                    await asyncio.sleep(args.interval * 60)
                    continue
                elif clock.get("is_open") is None:
                    print("Could not determine market status (unverified get_clock parsing) — proceeding anyway.")

            try:
                await run_scan(symbols, execute=args.execute, top_n=args.top_n,
                                skip_held_positions=skip_held, ignore_market_hours=args.ignore_market_hours)
                print(f"\nRun #{run_count} complete — individual trade decisions logged to "
                      f"{trade_journal.JOURNAL_PATH}")
            except Exception as exc:
                print(f"Run #{run_count} FAILED: {exc}")
                trade_journal.log_entry({"kind": "run_error", "run": run_count, "error": str(exc)})

            if args.max_runs is None or run_count < args.max_runs:
                print(f"\nSleeping {args.interval} minutes until next run...")
                await asyncio.sleep(args.interval * 60)

    except KeyboardInterrupt:
        print(f"\n\nStopped by user after {run_count} run(s). Journal saved to {trade_journal.JOURNAL_PATH}.")


if __name__ == "__main__":
    asyncio.run(main())
