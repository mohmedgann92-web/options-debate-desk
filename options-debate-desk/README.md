# Options Alpha Debate Desk

A multi-agent options trading desk built for the **Alpaca AI Trading Agents
Hackathon** (Options Alpha Agents track). Instead of one LLM call deciding a
trade, four agents with distinct roles argue it out using real market data
pulled through Alpaca's official MCP server, with implied vol and Greeks
computed locally via Black-Scholes:

```
                          ┌──────────────────┐
                          │  📡 SCANNER       │  optional: ranks a watchlist
                          │  (--scan mode)    │  by IV-vs-realized-vol edge
                          └────────┬─────────┘
                                   │ picks top symbol
                                   ▼
            ┌────────────┐        ┌────────────┐
 market ───▶│ 🐂 Bull     │        │ 🐻 Bear     │
 data       │ agent       │        │ agent       │
            └─────┬──────┘        └──────┬─────┘
                  │  proposals            │
                  └───────────┬───────────┘
                               ▼
                     ┌───────────────────┐
                     │ 🛡️ Risk Manager    │  ← hard-vetoes anything that
                     │ agent              │    breaks the risk budget
                     └─────────┬─────────┘
                               ▼
                     ┌───────────────────┐
                     │ ✅ Decision agent  │  → execute_bull / execute_bear
                     │ (Desk Head)        │     / no_trade
                     └─────────┬─────────┘
                               ▼
                   optional: submit paper order
                     via Alpaca's MCP server
```

Every agent's output is structured JSON, so the whole debate is logged and
auditable — you can show judges exactly why the desk did (or didn't) trade,
not just the final number. Every trade decision is also automatically
**fact-checked** against the real option chain data after the fact, to catch
hallucinated contract symbols.

> **Verified live against a real Alpaca paper account**: order ID
> `7639d467-f129-4337-9766-5e5e5741cb2b`, `AAPL260902C00310000`, limit
> $4.46, status `accepted` — confirmed independently on Alpaca's own
> dashboard. This isn't a theoretical architecture; the full pipeline (MCP
> connection → scan → debate → risk governor → real order) has actually run
> end to end.

## Why this fits the track

- **Talks to Alpaca through its official MCP server** (`alpacahq/alpaca-mcp-server`
  v2), not a direct SDK call — this is the literal integration the hackathon
  page calls "the core of the hackathon theme." `mcp_client.py` opens a real
  Model Context Protocol session over stdio; every data pull and order in
  `alpaca_tools.py` is an MCP tool call (`get_account_info`,
  `get_stock_latest_quote`, `get_stock_bars`, `get_option_chain`,
  `place_option_order`, ...).
- **Computes its own IV and Greeks** (`black_scholes.py`) — this account/data
  tier doesn't get Greeks or implied volatility from Alpaca's API at all
  (confirmed empirically), so the desk solves for IV from each contract's
  market price via Black-Scholes rather than depending on data that isn't
  there. Real quant work, not a passthrough.
- **Scans a watchlist to find its own opportunities** (`scanner.py`,
  `--scan` mode) — ranks symbols by `|mean near-ATM IV − realized vol|`
  before ever starting a debate, so the desk picks what to look at, not just
  what you typed in.
- Genuinely **agentic**: independent reasoning steps with adversarial
  incentives (Bull vs Bear), not a single prompt pretending to be an agent.
- **Risk-aware by construction** — the Risk Manager can veto both sides
  (including catching structurally invalid proposals, like mismatched-expiry
  "spreads"), and the Decision agent is explicitly allowed to say "no trade."
- **Self-verifying** — after every decision, `_fact_check_legs()` looks up
  each chosen contract against the real chain data and flags anything that
  doesn't exist, rather than trusting the LLM's output blindly. A
  **deterministic Risk Governor** (`risk_governor.py`, plain Python, no LLM)
  goes further: it recomputes real max loss from the actual fact-checked
  contract data and hard-blocks execution if it disagrees with the LLM Risk
  Manager's own math — this caught a real ~10x risk-budget miscalculation
  during testing (see "Known issues found & fixed" below).
- Trades only ever go to Alpaca's **paper** environment unless you
  deliberately flip `ALPACA_PAPER_TRADE=false` in the MCP server's env.

## Setup

```bash
python3 -m venv venv
source venv/bin/activate       # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env           # then fill in your keys
```

You also need **`uv`/`uvx`** on PATH — the orchestrator launches Alpaca's MCP
server itself via `uvx alpaca-mcp-server`, the same way Claude Desktop or
Cursor would:
https://docs.astral.sh/uv/getting-started/installation/

You need:
- Alpaca **paper trading** keys from https://app.alpaca.markets/paper/dashboard/overview
- An LLM backend — either:
  - An [Anthropic API key](https://console.anthropic.com/) (best reasoning
    quality; use this for your actual submission/demo), **or**
  - A free [Groq API key](https://console.groq.com/) (no card required; good
    for testing while you sort out Anthropic billing — see "LLM provider"
    below)

## IMPORTANT: verify the tool schemas before you build on top of this

`alpaca-mcp-server` v2 generates its tools from Alpaca's OpenAPI specs and
its exact output shapes aren't in the tool schemas (those only cover
inputs). Run this first, with your real keys in `.env`:

```bash
python list_mcp_tools.py                    # lists every tool + its INPUT schema
python test_raw_call.py AAPL                 # shows REAL output shapes for a live symbol
```

`alpaca_tools.py` already unwraps the `{"data": ...}` envelope every response
comes in and matches the field names (`ap`/`bp`, `latestQuote`, etc.)
confirmed via `test_raw_call.py` — but if Alpaca changes their API surface,
these are the two scripts to re-run and cross-check against.

If Alpaca auth itself is failing (401 errors), `check_alpaca_keys.py` tests
your `.env` keys directly against Alpaca's REST API, bypassing MCP entirely
— useful for isolating whether a problem is your credentials or the MCP
layer.

## LLM provider

Set in `.env` via `LLM_PROVIDER`:

```bash
LLM_PROVIDER=anthropic    # default — best reasoning quality, use for the real submission
# or
LLM_PROVIDER=groq         # free tier, no card required — good for testing
```

Groq's free tier caps requests at **8,000 tokens total** (prompt + max
output) per call — `orchestrator.py`'s `_build_agent_payload()` trims what
gets sent to agents specifically to stay under this (raw daily bars and the
full option chain are still saved to the debug JSON file, just not sent to
the LLM). `llm_client.py` also auto-retries on rate limits with backoff.
Anthropic's limits are far higher and this trimming costs nothing there
either.

Groq also runs reasoning models (`openai/gpt-oss-120b`) that spend tokens on
internal chain-of-thought before writing their actual answer — `max_tokens`
is set generously (4000) to give room for that, and `llm_client._extract_json()`
robustly pulls the JSON object out of the response regardless of code
fences or reasoning preamble around it.

Run `python -c "from openai import OpenAI; import config; c = OpenAI(api_key=config.GROQ_API_KEY, base_url='https://api.groq.com/openai/v1'); [print(m.id) for m in c.models.list().data]"`
to see what models your Groq key currently has access to if `openai/gpt-oss-120b`
stops working — Groq's catalog changes.

## Run it

**Single symbol:**
```bash
python main.py AAPL                 # runs the debate, prints the transcript
python main.py AAPL --execute       # also submits the winning trade as a PAPER order
```

**Scan a watchlist and debate the top candidate:**
```bash
python main.py --scan                          # scans config.WATCHLIST (10 default tickers)
python main.py --scan --symbols AAPL,NVDA,AMD   # scan a custom list instead
python main.py --scan --execute                 # scan + debate + place the paper trade if approved
```

Try a few tickers with different setups (high IV vs low IV, earnings soon
vs. not) — the transcripts differ meaningfully, which is a good demo moment.

**⚠ Watch for the vol-outlier warning.** Realized vol is computed from a
30-day close-to-close lookback, which means a single large earnings-day gap
inside that window can dominate the whole number and make a stock look like
it has a much bigger IV-vs-realized-vol "edge" than it really does. Both
`--scan`'s table and the single-symbol debate print a warning
(`detect_vol_outlier_days()` in `alpaca_tools.py`) when this happens — don't
trust a top-ranked pick without checking whether that warning fired.

## Running continuously

`run_continuous.py` re-runs the scan+debate pipeline on a fixed interval
instead of you invoking `--scan` by hand each time — useful for catching
opportunities as prices move throughout a session, and for building up a
track record over the judging window.

```bash
python run_continuous.py                          # scan every 60 min, log only, no execution
python run_continuous.py --execute                 # also place real paper orders each run
python run_continuous.py --interval 30 --top-n 2   # tighter loop, fewer candidates per run
python run_continuous.py --market-hours-only        # skip scans when markets are closed
python run_continuous.py --max-runs 3               # good for testing the script itself
```

Every run appends a compact summary to a JSONL journal
(`trade_journal.jsonl` by default) — review it anytime with:

```bash
python view_journal.py            # all runs
python view_journal.py --last 10  # just the recent ones
```

**With `--execute` on, this runs unattended and places real (paper) orders.**
Two things worth knowing:

- With `RISK_CONFIG["max_risk_pct_of_equity"]` at 1% (lowered from 2% for
  exactly this use case), repeated runs across a session could otherwise
  stack risk into whatever symbol ranks #1 that hour. By default,
  `run_scan(skip_held_positions=True)` skips symbols you already hold a
  position in, so hourly runs diversify instead of piling in. Disable with
  `--no-skip-held` if you want the old behavior back.
- The position-skip check depends on `alpaca_tools.get_held_underlying_symbols()`,
  which — like `get_market_clock()` — hasn't been schema-verified against
  a real response the way the rest of `alpaca_tools.py` was (see
  `test_raw_call.py`). Both fail safe (an unparseable response is treated
  as "unknown"/"nothing held" rather than crashing the loop), but
  `run_continuous.py` prints what it detects as currently held at the start
  of every run specifically so you can sanity-check this is actually
  working before trusting a long unattended session.

## Improving expected P&L (not just correctness)

Short judging windows are dominated by variance, but a few things measurably
help the odds without gaming anything:

- **Outlier-flagged edges are now a HARD filter, not just a warning.**
  `scanner.get_eligible_candidates()` excludes any symbol whose realized vol
  is dominated by a single outlier day, regardless of how high its score
  ranks — trading a fake mispricing (an earnings-gap artifact, not a real
  edge) is worse for P&L than not trading at all.
- **Liquidity filter on option contracts.** `get_iv_edge()` only trusts
  near-ATM contracts with a bid-ask spread ≤ 20% of mid
  (`LIQUIDITY_CONFIG` in `config.py`) — a "cheap" option on a wide spread
  often isn't real profit once you cross the spread both ways.
- **Diversify across multiple candidates, not just #1.** `--scan` now runs
  a full debate for the top-N *eligible* candidates (default 3, tune with
  `--top-n`), each independently risk-checked, with a portfolio-level
  "📊 Batch Summary" panel at the end — one position over a short window is
  mostly variance regardless of edge quality.
- **A wider watchlist** (25 symbols by default: mega-cap tech, other liquid
  large caps, and a few broad ETFs) gives the eligible-candidate filter more
  to choose from after the outlier/liquidity exclusions.

None of this guarantees a good outcome over a one-week window — it reduces
the odds of trading on noise and concentrates risk more sensibly, which is
the honest ceiling of what's controllable here.

## Verifying agent output isn't hallucinated

Every run does two things automatically:

1. **Saves the full data the agents saw** to `debug_market_data_<SYMBOL>_<timestamp>.json`
   in the project folder — inspect this to independently confirm any strike,
   price, or IV an agent cites in its thesis text.
2. **Fact-checks the final decision** against the real option chain — a
   "🔍 Fact Check vs. Real Chain Data" panel prints after every decision,
   showing ✓ for contracts that genuinely exist (with their real
   strike/mid/IV alongside) and ⚠ for anything that doesn't — a real
   hallucination guardrail, not just a hope that the model got it right.

## Known issues found & fixed (worth knowing if you keep building on this)

Order placement took three real, sequential bugs to get right — each only
surfaced by actually trying against a live paper account, not by reading
docs or unit tests alone:

1. **Single-leg orders were being silently rejected.** Alpaca's API
   auto-infers `order_class: mleg` from the mere presence of a `legs` array
   — even with just one entry — and `mleg` orders require 2–4 legs. Every
   single-leg trade (e.g. a plain long call) failed with a 422 that the
   code didn't surface, returning `{order_id: None, status: None}` instead
   of an error. **Fixed**: single-leg orders now use the top-level
   `symbol`/`side` fields the schema actually wants for that case.
2. **Market orders on options are rejected outright when markets are
   closed** (a real, correct Alpaca rule, not a bug in their system).
   **Fixed** by switching to limit orders priced at the real mid
   (`compute_limit_price()`, using the same fact-checked chain data the
   Risk Governor already validated) — this also protects the Risk
   Governor's math from slippage a market order could otherwise cause.
3. **Limit prices need exactly 2 decimal places**; a raw bid/ask mid like
   `4.465` gets rejected. **Fixed** with a `round(..., 2)`.

Any order that still doesn't come back with a real order ID now raises
loudly with the full raw response, instead of failing silently like the
original version did.

**Verified end-to-end against a real paper account** after all three fixes:
order ID `7639d467-f129-4337-9766-5e5e5741cb2b`, `AAPL260902C00310000`,
limit $4.46, status `accepted` — confirmed independently on Alpaca's own
dashboard, not just this app's own printed output. `debug_place_order.py`
is the fastest way to re-verify this end-to-end if you change anything in
`alpaca_tools.py`.

Separately, **LLM risk math can't be trusted over real data.** See
`risk_governor.py`'s docstring and the write-up for the full story — an
agent's narrative text described a different trade than the actual
contract symbols it selected, and the LLM Risk Manager approved a trade
based on the narrative's (wrong) numbers rather than the real ones. The
deterministic Risk Governor exists specifically because of this.

**The Risk Manager also had the opposite failure mode: false vetoes from
date-math errors.** It once rejected a legitimate ~7-day trade, claiming
its expiry was "far beyond the max 60-day window" — a pure hallucination,
almost certainly because the LLM was never told what "today" actually is
and had to guess. Fixed in `agents/risk_manager.py`: it's now given
today's real date explicitly, plus a deterministically pre-computed
days-to-expiry for every leg (`_verified_expiry_facts()`, using the same
OCC-symbol parsing `black_scholes.py` already relies on) — the same
"stop asking the LLM to compute something we can just calculate correctly"
principle as the Risk Governor, applied one step earlier in the pipeline.

## Project layout

```
config.py             model choice, RISK_CONFIG, default WATCHLIST
mcp_client.py          opens the MCP session with Alpaca's official server (uvx alpaca-mcp-server)
alpaca_tools.py        agent-facing functions — MCP tool calls, local Black-Scholes IV/Greeks,
                        realized vol + outlier detection, near-ATM edge scoring, limit price computation
black_scholes.py       IV solver + Greeks calculator (server doesn't provide these for this account tier)
scanner.py             ranks a watchlist by IV-vs-realized-vol edge (--scan mode)
risk_governor.py        deterministic (non-LLM) real-max-loss recheck — hard execution gate
llm_client.py          Anthropic/Groq wrapper, JSON extraction, rate-limit retry with backoff
list_mcp_tools.py       dumps real MCP tool INPUT schemas from your server
test_raw_call.py        dumps real MCP tool OUTPUT shapes for a live symbol
check_alpaca_keys.py    tests your Alpaca keys directly via REST, bypassing MCP
check_recent_orders.py  looks up real recent orders directly — use when unsure if a trade went through
debug_place_order.py     places ONE real order via the actual order-placement function, to verify it end-to-end
run_continuous.py        re-runs scan+debate on a fixed interval, with journal logging
view_journal.py         reviews the JSONL journal run_continuous.py writes to
position_tracker.py     persists daily new-position count across process runs; enforces the daily cap
agents/
  bull.py              bullish analyst — one concrete proposal, JSON out
  bear.py              bearish analyst — one concrete proposal, JSON out
  risk_manager.py       reviews both against RISK_CONFIG, can veto/resize
  decision.py          picks a side, resizes per Risk Manager, or no-trade
orchestrator.py         wires the pipeline together (async), trims LLM payloads, saves debug data,
                        fact-checks decisions, runs the Risk Governor, pretty-prints via `rich`
main.py                CLI entry point — single-symbol and --scan modes
```

## Tuning the risk budget

Edit `RISK_CONFIG` in `config.py`:

```python
RISK_CONFIG = {
    "max_risk_pct_of_equity": 0.01,   # cap max-loss at 1% of equity per trade
    "max_contracts_per_leg": 10,
    "defined_risk_only": True,        # spreads only, no naked options
    "min_days_to_expiry": 7,
    "max_days_to_expiry": 60,
    "max_new_positions_per_day": 4,   # hard cap on NEW positions opened per day
}
```

`max_new_positions_per_day` and the "no new position in an already-held
symbol" rule are enforced together at the actual execution gate in
`orchestrator.py`, right before every order — not just at candidate
selection — so they apply no matter which entry point you use
(`main.py SYMBOL --execute`, `--scan --execute`, or `run_continuous.py
--execute`). The daily count is tracked in `position_tracker.py` and
persisted to `positions_opened_log.json`, since each process invocation is
otherwise a fresh start with no memory of earlier runs today. The
already-held check uses a LIVE call to your actual Alpaca positions
(`alpaca_tools.get_held_underlying_symbols`) rather than a cached list, so
it stays accurate even partway through a multi-candidate batch.

## Tuning the watchlist

Edit `WATCHLIST` in `config.py` (used by `--scan` when `--symbols` isn't
passed) — currently 25 symbols across mega-cap tech, other liquid large
caps, and broad ETFs.

## Tuning the liquidity filter

Edit `LIQUIDITY_CONFIG` in `config.py`:

```python
LIQUIDITY_CONFIG = {
    "max_spread_pct": 0.20,       # skip contracts with bid-ask spread > 20% of mid
    "min_liquid_contracts": 3,    # need at least this many after filtering to trust an edge
}
```

## Ideas if you have extra build time

- **Dashboard**: wrap `run_debate()`/`run_scan()` in a small Streamlit app so
  judges can watch the scan table and four-agent panels populate live
  (Streamlit supports running async code via `asyncio.run` inside a
  callback).
- **Show the MCP session itself**: log the raw tool calls/responses
  (`mcp_client.py` is the place to add this) and surface them in the demo —
  judges reviewing an MCP-themed hackathon will want to see the protocol
  working, not just trust that it is.
- **Backtest mode**: replay `run_debate` against historical option chains
  (Alpaca's historical option data) to show a track record, not just a
  single live call.
- **Position tracking**: after `--execute`, poll `alpaca_tools` positions
  and let a fifth "Portfolio Manager" agent decide when to close/roll, or
  factor existing exposure into new Risk Manager reviews.
- **More robust realized vol**: `detect_vol_outlier_days()` currently just
  flags when one day dominates — a trimmed or winsorized realized-vol
  estimator would fix the number itself instead of just warning about it.

## Disclaimers

This is a hackathon project, not investment advice. Options trading carries
substantial risk. Keep `ALPACA_PAPER=true` for the entire hackathon and demo
— there's no reason to touch real money to win this competition.
