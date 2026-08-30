"""
Single source of truth for the trade journal. Previously only
run_continuous.py wrote to trade_journal.jsonl, which meant `main.py SYMBOL
--execute` and `main.py --scan --execute` — real trades — were invisible in
the journal. Fixed by moving logging into orchestrator.py's shared
execution gate (_run_debate_with_client), so every entry point logs
automatically through this one module instead of each script needing to
remember to do it separately.
"""
import json
from datetime import datetime
from pathlib import Path

JOURNAL_PATH = Path("trade_journal.jsonl")


def log_entry(entry: dict, path: Path = JOURNAL_PATH) -> None:
    """Appends one JSON line. Adds a timestamp automatically if the caller
    didn't supply one."""
    entry = {"timestamp": datetime.now().isoformat(), **entry}
    with open(path, "a") as f:
        f.write(json.dumps(entry, default=str) + "\n")
