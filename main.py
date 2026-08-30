import argparse
import asyncio

import config
from orchestrator import run_debate, run_scan


def main():
    parser = argparse.ArgumentParser(description="Options Alpha Multi-Agent Debate Desk")
    parser.add_argument("symbol", nargs="?", help="Underlying ticker, e.g. AAPL. Omit when using --scan.")
    parser.add_argument(
        "--scan", action="store_true",
        help="Scan a watchlist, rank by IV-vs-realized-vol edge, and run the full debate on the top candidate.",
    )
    parser.add_argument(
        "--symbols", type=str, default=None,
        help="Comma-separated watchlist for --scan mode (default: config.WATCHLIST).",
    )
    parser.add_argument(
        "--top-n", type=int, default=3,
        help="Run a full debate for up to this many ELIGIBLE top-ranked candidates "
             "(default 3) — diversifies across a few names instead of betting "
             "everything on one pick. Symbols flagged as vol-outliers or too "
             "illiquid are excluded from eligibility regardless of rank.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually submit the winning trade as a PAPER order (requires ALPACA_PAPER=true)",
    )
    parser.add_argument(
        "--ignore-market-hours", action="store_true",
        help="Allow --execute to place orders even when the market's closed. NOT recommended: "
             "after-hours option quotes are often stale/degraded and can distort the risk math "
             "this whole pipeline depends on. Default is to block execution while markets are closed.",
    )
    args = parser.parse_args()

    if args.scan:
        symbols = [s.strip().upper() for s in args.symbols.split(",")] if args.symbols else config.WATCHLIST
        asyncio.run(run_scan(symbols, execute=args.execute, top_n=args.top_n,
                              ignore_market_hours=args.ignore_market_hours))
    else:
        if not args.symbol:
            parser.error("provide a symbol (e.g. `python main.py AAPL`) or use --scan for a watchlist scan")
        asyncio.run(run_debate(args.symbol.upper(), execute=args.execute,
                                ignore_market_hours=args.ignore_market_hours))


if __name__ == "__main__":
    main()
