"""
Central configuration for the Options Alpha Debate Desk.

Edit RISK_CONFIG to change how conservative the Risk Manager agent is.
Nothing here talks to the network — it's just constants and env loading.
"""
import os
from dotenv import load_dotenv

load_dotenv()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

# "anthropic" (default, best quality) or "groq" (free tier, good for testing)
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "anthropic").lower()
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
ALPACA_PAPER = os.getenv("ALPACA_PAPER", "true").lower() == "true"

# Model used for every agent. Swap per-agent below if you want e.g. a
# cheaper/faster model for Bull/Bear and a stronger one for Decision.
if LLM_PROVIDER == "groq":
    MODEL = "openai/gpt-oss-120b"  # free tier; verified available via your Groq key's /models list
else:
    MODEL = "claude-sonnet-4-6"

AGENT_MODELS = {
    "bull": MODEL,
    "bear": MODEL,
    "risk_manager": MODEL,
    "decision": MODEL,
}

# Risk Manager guardrails. The agent is instructed to hard-veto anything
# that violates these regardless of how compelling the Bull/Bear case is.
RISK_CONFIG = {
    # Max % of account equity allowed in max-loss on a single trade idea
    "max_risk_pct_of_equity": 0.01,       # 1% — lowered from 2% for continuous
                                           # auto-execute mode, where the same
                                           # symbol/risk could otherwise stack
                                           # across multiple hourly runs
    # Max number of contracts per leg regardless of sizing math
    "max_contracts_per_leg": 10,
    # Only allow defined-risk strategies (spreads, not naked options)
    "defined_risk_only": True,
    # Minimum days to expiry considered (avoid 0DTE gamma risk by default)
    "min_days_to_expiry": 7,
    # Maximum days to expiry considered
    "max_days_to_expiry": 60,
    # Max NEW positions opened per calendar day, across every entry point
    # (single-symbol, --scan, run_continuous.py). Persisted via
    # position_tracker.py so it holds across separate process runs.
    "max_new_positions_per_day": 4,
}

# Default watchlist for --scan mode. Liquid, optionable large caps and
# major ETFs — override with --symbols on the command line for a different
# set. Expanded from the original 10 for more real candidates to choose
# from before applying the liquidity/outlier filters below.
WATCHLIST = [
    # Mega-cap tech
    "AAPL", "MSFT", "NVDA", "AMD", "GOOGL", "AMZN", "META", "TSLA", "AVGO", "NFLX",
    # Other liquid large caps with active options markets
    "JPM", "BAC", "XOM", "DIS", "CRM", "ORCL", "COST", "WMT", "UNH", "V",
    # Broad index / sector ETFs — usually lower vol, useful diversification
    "SPY", "QQQ", "IWM", "XLF", "XLE",
]

# Liquidity filter used by get_iv_edge() when scoring near-ATM contracts.
# A contract with a bid-ask spread this wide relative to its mid price is
# treated as too illiquid to trust for edge scoring — a "cheap" option on
# a wide spread often isn't real profit, since you pay the spread crossing
# it both ways.
LIQUIDITY_CONFIG = {
    "max_spread_pct": 0.20,       # skip contracts where (ask-bid)/mid > 20%
    "min_liquid_contracts": 3,    # need at least this many after filtering to trust the edge
}
