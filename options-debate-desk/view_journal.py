"""
Prints a readable summary of the JSONL journal run_continuous.py appends
to on every iteration — so you can review what it found across a whole
testing session without scrolling back through terminal output.

Usage:
    python view_journal.py                      # show all entries
    python view_journal.py --last 10             # show only the last 10 runs
    python view_journal.py --journal other.jsonl # different journal file
"""
import argparse
import json

from rich.console import Console
from rich.table import Table

console = Console()


def load_journal(path: str) -> list[dict]:
    entries = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    entries.append(json.loads(line))
    except FileNotFoundError:
        console.print(f"[red]No journal found at {path} — run run_continuous.py first.[/red]")
    return entries


def print_journal(entries: list[dict]) -> None:
    table = Table(title="Trade Journal")
    table.add_column("Run")
    table.add_column("Timestamp")
    table.add_column("Symbol")
    table.add_column("Action")
    table.add_column("Qty")
    table.add_column("Approved")
    table.add_column("Real Max Loss")

    for entry in entries:
        run = str(entry.get("run", "?"))
        ts = entry.get("timestamp", "")[:19]

        if entry.get("skipped"):
            table.add_row(run, ts, "-", f"skipped: {entry['skipped']}", "-", "-", "-")
            continue
        if entry.get("error"):
            table.add_row(run, ts, "-", f"[red]ERROR: {str(entry['error'])[:40]}[/red]", "-", "-", "-")
            continue

        results = entry.get("results", [])
        if not results:
            table.add_row(run, ts, "-", "no eligible candidates", "-", "-", "-")
            continue

        for r in results:
            approved = "[green]✓[/green]" if r.get("governor_approved") else "[red]✗[/red]"
            loss = f"${r['real_max_loss_total']:,.2f}" if r.get("real_max_loss_total") is not None else "-"
            table.add_row(
                run, ts, r["symbol"], r.get("action", "?"), str(r.get("qty", 0)), approved, loss,
            )

    console.print(table)

    total_runs = len(entries)
    total_trades = sum(
        1 for e in entries for r in e.get("results", [])
        if r.get("governor_approved") and r.get("action") != "no_trade"
    )
    console.print(f"\n{total_runs} run(s) logged, {total_trades} approved trade(s) executed.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--journal", default="trade_journal.jsonl")
    parser.add_argument("--last", type=int, default=None, help="Only show the last N runs")
    args = parser.parse_args()

    entries = load_journal(args.journal)
    if args.last:
        entries = entries[-args.last:]
    print_journal(entries)


if __name__ == "__main__":
    main()
