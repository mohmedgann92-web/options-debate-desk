"""
Central configuration for the Options Alpha Debate Desk.

Edit RISK_CONFIG to change how conservative the Risk Manager agent is.
Nothing here talks to the network — it's just constants and env loading.
"""
import os
from dotenv import load_dotenv

load_dotenv()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

# "anthropic" (default, best quality), "groq" (free tier, good for testing),
# or "featherless" (hackathon sponsor bonus — $25 free credit, code
# ALPACA26 at featherless.ai, OpenAI-compatible API, no model-size limit,
# context up to 256K — see https://api.featherless.ai/v1)
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "anthropic").lower()
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
FEATHERLESS_API_KEY = os.getenv("FEATHERLESS_API_KEY")

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
ALPACA_PAPER = os.getenv("ALPACA_PAPER", "true").lower() == "true"

# Model used for every agent. Swap per-agent below if you want e.g. a
# cheaper/faster model for Bull/Bear and a stronger one for Decision.
if LLM_PROVIDER == "groq":
    MODEL = "openai/gpt-oss-120b"  # preferred starting model — GROQ_MODEL_ROSTER
                                  # below handles automatic fallback if this
                                  # (or any other) model's quota is exhausted
elif LLM_PROVIDER == "featherless":
    # Taken directly from Featherless's own quickstart docs, so reasonably
    # safe to assume it exists in the catalog — but their catalog is huge
    # (37,000+ models) and changes, so run list_featherless_models.py
    # against your real key before trusting this blindly, same as we did
    # for Groq's model list.
    MODEL = "Qwen/Qwen2.5-7B-Instruct"
else:
    MODEL = "claude-sonnet-4-6"

AGENT_MODELS = {
    "bull": MODEL,
    "bear": MODEL,
    "risk_manager": MODEL,
    "decision": MODEL,
}

# Fallback rosters: if the current model hits a rate limit that's too long
# to wait out automatically (e.g. a daily quota — see
# llm_client.MAX_AUTO_RETRY_WAIT_SECONDS), call_agent() tries the next
# model in the roster for the active provider instead of just failing.
# Only used for the currently active LLM_PROVIDER; ignored for the others.
# Order = preference (best/largest first). Re-verify against your real
# account before a long unattended run — provider catalogs change.
GROQ_MODEL_ROSTER = [
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
    "qwen/qwen3.6-27b",
    "groq/compound-mini",
]

# All three verified or sourced from a real account's live catalog
# (see conversation history — Qwen2.5-7B-Instruct confirmed working
# directly; meta-llama's official repo turned out to be GATED, needing
# HuggingFace OAuth connected on Featherless's side, so swapped for
# Unsloth's ungated re-upload of the same model, a common pattern for
# accessing otherwise-gated Meta models freely).
FEATHERLESS_MODEL_ROSTER = [
    "Qwen/Qwen2.5-7B-Instruct",
    "unsloth/Llama-3.3-70B-Instruct",
    "deepseek-ai/DeepSeek-R1-0528",  # reasoning model — more prone to
                                     # empty-content-under-length than the
                                     # other two; kept last in the roster
                                     # for that reason, not removed, since
                                     # the empty-content fallback already
                                     # handles it gracefully either way
]

# Maps the active provider name to its roster, for llm_client.py's generic
# lookup. Anthropic has no roster — a single paid model with high limits
# doesn't have the "try a different model" failure mode Groq/Featherless do.
MODEL_ROSTERS = {
    "groq": GROQ_MODEL_ROSTER,
    "featherless": FEATHERLESS_MODEL_ROSTER,
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
    # Semiconductors — different demand drivers than NVDA/AMD (memory,
    # networking, industrial chips), all genuinely liquid options
    "QCOM", "TXN", "MU", "INTC",
    # Financials — rounds JPM/BAC/V out into real sector breadth
    "GS", "MS", "WFC", "C", "MA",
    # Healthcare — its own news cycle (drug trials, FDA events),
    # uncorrelated with tech/macro. LLY specifically has very active options.
    "PFE", "JNJ", "ABBV", "LLY",
    # Consumer discretionary — different demand drivers (housing, apparel,
    # dining, discount retail) than WMT/COST alone
    "HD", "NKE", "SBUX", "MCD", "TGT",
    # Consistently (not just occasionally) active options volume — good
    # scanner candidates for a genuine, non-outlier IV/RV edge
    "PYPL", "SHOP", "UBER", "COIN", "PLTR",
    # Broad index / sector / asset-class ETFs — GLD/TLT specifically add
    # exposure to macro drivers (gold, rates) with no equity correlation
    "SPY", "QQQ", "IWM", "XLF", "XLE", "XLK", "XLV", "GLD", "TLT",
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
