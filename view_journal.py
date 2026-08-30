"""
Prints a readable summary of trade_journal.jsonl — now populated by every
entry point (single-symbol, --scan, and run_continuous.py all log through
the shared orchestrator.py execution gate as of this version).

Handles THREE entry shapes for backward compatibility with journal data
from before this change:
  1. NEW per-symbol entries — one line per debate, from orchestrator.py.
  2. NEW run-level meta entries (kind="run_skipped"/"run_error") — from
     run_continuous.py, for things that happen outside any one symbol's
     debate (a whole run skipped for closed markets, a hard crash).
  3. OLD batch-format entries (a "results" list, or "skipped"/"error" at
     the top level without "kind") — from before this fix, when only
     run_continuous.py logged, one line per BATCH rather than per symbol.

Usage:
    python view_journal.py                      # show all entries
    python view_journal.py --last 10             # show only the last 10
    python view_journal.py --journal other.jsonl # different journal file
"""
import argparse
import json

from rich.console import Console
from rich.table import Table

import trade_journal

console = Console()


def load_journal(path) -> list[dict]:
    entries = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    entries.append(json.loads(line))
    except FileNotFoundError:
        console.print(f"[red]No journal found at {path} — run a debate with --execute, or "
                      f"run_continuous.py, first.[/red]")
    return entries


def _row_for_new_entry(table: Table, e: dict) -> None:
    outcome = e.get("execution_outcome", "?")
    if outcome == "executed":
        outcome_display = f"[bold green]✓ executed[/bold green] ({e.get('order_id', '?')})"
    elif outcome.startswith("blocked_"):
        outcome_display = f"[bold red]✗ {outcome}[/bold red]"
    elif outcome == "not_requested":
        outcome_display = "[dim]research only[/dim]"
    else:
        outcome_display = outcome
    if e.get("market_was_closed"):
        outcome_display += "\n[yellow]⚠ market closed[/yellow]"
    loss = f"${e['real_max_loss_total']:,.2f}" if e.get("real_max_loss_total") is not None else "-"
    table.add_row(
        e.get("timestamp", "")[:19], e.get("symbol", "-"), e.get("action", "?"),
        str(e.get("qty", 0)), outcome_display, loss,
    )


def _rows_for_old_batch_entry(table: Table, e: dict) -> None:
    ts = e.get("timestamp", "")[:19]
    if e.get("skipped"):
        table.add_row(ts, "-", f"[dim]skipped: {e['skipped']}[/dim]", "-", "-", "-")
        return
    if e.get("error"):
        table.add_row(ts, "-", f"[red]ERROR: {str(e['error'])[:50]}[/red]", "-", "-", "-")
        return
    results = e.get("results", [])
    if not results:
        table.add_row(ts, "-", "[dim]no eligible candidates[/dim]", "-", "-", "-")
        return
    for r in results:
        approved = "[green]✓ approved[/green]" if r.get("governor_approved") else "[red]✗ blocked[/red]"
        loss = f"${r['real_max_loss_total']:,.2f}" if r.get("real_max_loss_total") is not None else "-"
        table.add_row(ts, r.get("symbol", "-"), r.get("action", "?"), str(r.get("qty", 0)), approved, loss)


def print_journal(entries: list[dict]) -> None:
    table = Table(title="Trade Journal")
    table.add_column("Timestamp")
    table.add_column("Symbol")
    table.add_column("Action")
    table.add_column("Qty")
    table.add_column("Outcome")
    table.add_column("Real Max Loss")

    executed_count = 0
    old_format_count = 0

    for e in entries:
        if e.get("kind") == "run_skipped":
            table.add_row(e.get("timestamp", "")[:19], "-", f"[dim]run skipped: {e.get('reason')}[/dim]",
                          "-", "-", "-")
        elif e.get("kind") == "run_error":
            table.add_row(e.get("timestamp", "")[:19], "-", f"[red]run error: {str(e.get('error'))[:50]}[/red]",
                          "-", "-", "-")
        elif "symbol" in e and "execution_outcome" in e:
            _row_for_new_entry(table, e)
            if e.get("execution_outcome") == "executed":
                executed_count += 1
        else:
            # Old batch-format entry from before this fix
            old_format_count += 1
            _rows_for_old_batch_entry(table, e)
            executed_count += sum(
                1 for r in e.get("results", [])
                if r.get("governor_approved") and r.get("action") != "no_trade"
            )

    console.print(table)
    console.print(f"\n{len(entries)} journal line(s), {executed_count} real order(s) executed.")
    if old_format_count:
        console.print(f"[dim]({old_format_count} entries were in the old pre-fix batch format — "
                      f"only run_continuous.py logged back then, so single-symbol/--scan trades from "
                      f"before this fix won't appear here even if you placed them.)[/dim]")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--journal", default=str(trade_journal.JOURNAL_PATH))
    parser.add_argument("--last", type=int, default=None, help="Only show the last N lines")
    args = parser.parse_args()

    entries = load_journal(args.journal)
    if args.last:
        entries = entries[-args.last:]
    print_journal(entries)


if __name__ == "__main__":
    main()
