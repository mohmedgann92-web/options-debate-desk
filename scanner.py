"""
Scans a watchlist and ranks symbols by options-mispricing edge — the same
signal (implied vol vs realized vol) your Bull/Bear agents already reason
about, just used here to pick WHICH ticker deserves a full debate rather
than debating one you named manually.

Deliberately sequential (not asyncio.gather) — the MCP session is a single
stdio subprocess connection, and pipelining untested concurrent requests
over it isn't worth the risk for what's already a fast per-symbol call
(one quote, one bars call, one narrow near-ATM chain call).
"""
from rich.console import Console
from rich.table import Table

import alpaca_tools
from mcp_client import AlpacaMCPClient

console = Console()


async def scan_watchlist(client: AlpacaMCPClient, symbols: list[str], min_dte: int = 7, max_dte: int = 60) -> list[dict]:
    """Returns candidates sorted by candidate_score descending. Symbols with
    no solvable edge (illiquid near-ATM options, bad quote, etc.) are still
    included but sorted last, with candidate_score=None, so you can see
    what got skipped and why."""
    results = []
    for symbol in symbols:
        console.print(f"[dim]Scanning {symbol}...[/dim]")
        try:
            edge_data = await alpaca_tools.get_iv_edge(client, symbol, min_dte=min_dte, max_dte=max_dte)
        except Exception as exc:
            edge_data = {"symbol": symbol, "error": str(exc), "candidate_score": None}
        results.append(edge_data)

    results.sort(key=lambda r: (r["candidate_score"] is None, -(r["candidate_score"] or 0)))
    return results


def get_eligible_candidates(results: list[dict], top_n: int, exclude_symbols: set[str] = None) -> list[dict]:
    """
    Filters scan results down to what's actually TRADABLE, not just ranked.
    A symbol is excluded from being selected for a debate (even if it has
    a high score) when:
      - it has no candidate_score (bad quote, no liquid data, error), or
      - its edge is likely a measurement artifact from a single outlier
        day dominating realized vol (see detect_vol_outlier_days) — this
        is a HARD filter now, not just a warning, because trading a fake
        edge is worse for P&L than not trading at all, or
      - it's in exclude_symbols (e.g. a position you already hold) — used
        by continuous/auto-execute mode to diversify across runs instead
        of repeatedly stacking into whatever's ranked #1 this hour.

    Still-ranked-but-excluded symbols remain visible in the printed table
    for transparency; they're just not eligible to be picked here.
    """
    exclude_symbols = exclude_symbols or set()
    eligible = [
        r for r in results
        if r.get("candidate_score") is not None
        and not r.get("vol_outlier_warning")
        and r["symbol"] not in exclude_symbols
    ]
    return eligible[:top_n]


def print_scan_table(results: list[dict]) -> None:
    table = Table(title="Watchlist Scan — Ranked by |IV − Realized Vol| Edge")
    table.add_column("Symbol", style="bold")
    table.add_column("Price")
    table.add_column("Realized Vol")
    table.add_column("Mean ATM IV")
    table.add_column("Edge")
    table.add_column("Score", style="bold cyan")
    table.add_column("Avg Spread")
    table.add_column("Note")

    for r in results:
        if r.get("candidate_score") is None:
            table.add_row(
                r["symbol"], "—", "—", "—", "—", "—", "—",
                r.get("error") or r.get("note") or "no data"
            )
        else:
            direction = "options rich" if r["edge"] > 0 else "options cheap"
            if r.get("vol_outlier_warning"):
                note = "[bold red]⚠ vol outlier — EXCLUDED[/bold red]"
            else:
                note = f"{r['contracts_used']} contracts"
            table.add_row(
                r["symbol"],
                f"${r['underlying_price']:.2f}",
                f"{r['realized_volatility']:.1%}",
                f"{r['mean_atm_iv']:.1%}",
                f"{r['edge']:+.1%} ({direction})",
                f"{r['candidate_score']:.1f}",
                f"{r.get('avg_spread_pct', 0):.1%}" if r.get("avg_spread_pct") is not None else "—",
                note,
            )
    console.print(table)
    if any(r.get("vol_outlier_warning") for r in results):
        console.print("[yellow]⚠ Symbols marked 'vol outlier' are EXCLUDED from candidate selection — "
                       "their realized vol is dominated by a single outlier day (likely an earnings gap "
                       "in the lookback window), which can manufacture a fake edge rather than reflect a "
                       "genuine mispricing.[/yellow]")
