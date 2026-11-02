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
# or
LLM_PROVIDER=featherless  # hackathon sponsor bonus — $25 free credit, code ALPACA26 at featherless.ai
```

`llm_client.py` treats Groq and Featherless as interchangeable "OpenAI-
compatible" backends (same SDK, different base URL/key — see
`OPENAI_COMPATIBLE_PROVIDERS` in that file), so everything below about
rosters, retries, and payload trimming applies to both identically. Adding
a fourth OpenAI-compatible provider later is a one-line config change, not
new logic.

Both free-tier providers cap requests at **8,000 tokens total** (prompt +
max output) per call — `orchestrator.py`'s `_build_agent_payload()` trims
what gets sent to agents specifically to stay under this (raw daily bars
and the full option chain are still saved to the debug JSON file, just not
sent to the LLM). Featherless's docs advertise up to 256K context, so this
trimming is likely unnecessary there, but it's left in place since it costs
nothing and keeps both free-tier paths behaving identically. Anthropic's
limits are far higher and the trimming costs nothing there either.

**Both free-tier providers also have separate per-model DAILY quotas**
(confirmed for Groq: 200,000 tokens/day for `gpt-oss-120b`) — a full
testing session can burn through one model's quota. `config.MODEL_ROSTERS`
maps each provider to several genuinely different models, each with its
own separate quota bucket:

```python
GROQ_MODEL_ROSTER = [
    "openai/gpt-oss-120b", "openai/gpt-oss-20b",
    "qwen/qwen3.6-27b", "groq/compound-mini",
]
FEATHERLESS_MODEL_ROSTER = [
    "Qwen/Qwen2.5-7B-Instruct", "meta-llama/Meta-Llama-3.1-8B-Instruct",
    "deepseek-ai/DeepSeek-R1-0528",
]
```

`llm_client.call_agent()` tries the current model first and automatically
falls through to the next one in the active provider's roster if it hits a
rate limit too long to wait out (over `MAX_AUTO_RETRY_WAIT_SECONDS`,
currently 90s) — a short per-minute limit still retries on the same model
as before; only a long one (like a daily quota) triggers moving to the next
model. Only raises once every model in the roster has failed. Verified
against mocked scenarios covering partial fallback, full exhaustion, and
rotation starting at whichever model was actually requested, for BOTH
providers independently (confirming each uses its own roster, not the
other's) — see this conversation's history for the exact test transcripts.

Some models on either provider (e.g. Groq's `gpt-oss-120b`) spend tokens on
internal chain-of-thought before writing their actual answer — `max_tokens`
is set generously (4000) to give room for that, and `llm_client._extract_json()`
robustly pulls the JSON object out of the response regardless of code
fences or reasoning preamble around it. An earlier version of this only
treated this as a Groq-specific quirk and raised a plain, non-recoverable
error on it — a real bug, caught via a live dashboard failure — now fixed
so any provider's empty-content response falls through to the next model
in the roster the same way a rate limit does.

**FEATHERLESS_MODEL_ROSTER is taken from Featherless's own docs, not yet
independently verified against a real key** (unlike Groq's list, which was
confirmed live — see below). Run this before trusting it in a long
unattended session:

```bash
python list_featherless_models.py
```

To re-check Groq's current catalog:

```bash
python -c "from openai import OpenAI; import config; c = OpenAI(api_key=config.GROQ_API_KEY, base_url='https://api.groq.com/openai/v1'); [print(m.id) for m in c.models.list().data]"
```

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

## The trade journal

**Every debate that runs, from any entry point, logs one line to
`trade_journal.jsonl`** — `main.py SYMBOL`, `main.py --scan`, and
`run_continuous.py` all funnel through the same shared execution gate in
`orchestrator.py`, so there's exactly one place logging happens and no way
for an entry point to silently skip it. This was NOT always true: earlier,
only `run_continuous.py` logged, which meant real trades placed via
single-symbol or `--scan --execute` were invisible in the journal — found
by cross-checking a real Alpaca order history against the journal and
noticing a whole batch of trades just wasn't there.

Each line records the symbol, the debate's action/qty, whether execution
was even requested, and — critically — the specific `execution_outcome`:
`executed` (with the real order ID), or exactly which gate blocked it
(`blocked_governor`, `blocked_market_hours`, `blocked_already_held`,
`blocked_daily_limit`, `blocked_no_limit_price`), or `not_requested`/
`no_trade_decided` for research-only runs. Review it anytime:

```bash
python view_journal.py            # all entries
python view_journal.py --last 10  # just the recent ones
```

`view_journal.py` also still reads journal lines written before this fix
(the old format logged one line per BATCH with a nested results list,
rather than one line per symbol) — so nothing from earlier sessions is
lost, it just renders differently and is called out as such.

## Running continuously

`run_continuous.py` re-runs the scan+debate pipeline on a fixed interval
instead of you invoking `--scan` by hand each time — useful for catching
opportunities as prices move throughout a session, and for building up a
track record over the judging window.

```bash
python run_continuous.py                          # scan every 60 min (market hours only), log only, no execution
python run_continuous.py --execute                 # also place real paper orders each run
python run_continuous.py --interval 30 --top-n 2   # tighter loop, fewer candidates per run
python run_continuous.py --max-runs 3               # good for testing the script itself
```

Individual trade decisions log automatically via the shared mechanism
above. This script additionally logs its own run-level events (a whole run
skipped for closed markets, a hard crash) that happen outside any single
symbol's debate.

### Market-hours protection (on by default, everywhere)

After-hours option quotes are often stale or served from a degraded
"indicative" feed rather than live NBBO — this was confirmed live during
testing: an after-hours scan showed 70–120% implied vol on normally calm
mega-caps (AAPL, MSFT), which is a data artifact, not a real market
condition. Solving Black-Scholes IV against a stale option price produces
exactly this kind of distorted-but-plausible-looking number, and the whole
Risk Governor/position-sizing math downstream depends on IV being real.

Protection now exists at **two independent layers**, both on by default:

1. `run_continuous.py` checks the market clock before each scan and skips
   the whole run (saving LLM cost) when closed.
2. `orchestrator.py`'s execution gate independently re-checks the clock
   right before every real order, regardless of entry point — this closes
   a real gap: `main.py SYMBOL --execute` and `main.py --scan --execute`
   had **no** market-hours protection at all before this, only
   `run_continuous.py` did.

Pass `--ignore-market-hours` to override (fine for research/scanning
without `--execute`; not recommended with it). An unknown/unverifiable
clock status is treated as "proceed" rather than "block" — matching
`get_market_clock()`'s documented fail-open philosophy; an earlier draft of
this exact check would have incorrectly blocked trading on an *unknown*
status instead of only an explicitly-closed one, caught during review
before shipping.

**With `--execute` on, this runs unattended and places real (paper) orders.**
One more thing worth knowing:

- With `RISK_CONFIG["max_risk_pct_of_equity"]` at 1% (lowered from 2% for
  exactly this use case), repeated runs across a session could otherwise
  stack risk into whatever symbol ranks #1 that hour. By default,
  `run_scan(skip_held_positions=True)` skips symbols you already hold a
  position in, so hourly runs diversify instead of piling in. Disable with
  `--no-skip-held` if you want the old behavior back.
- The position-skip check depends on `alpaca_tools.get_held_underlying_symbols()`,
  which was schema-VERIFIED against a real account during testing (see
  `check_held_positions.py`) — confirmed correctly extracting the right
  underlying from real option leg positions.

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
- **A wide watchlist** (52 symbols by default: mega-cap tech, semiconductors,
  financials, healthcare, consumer discretionary, high-options-volume growth
  names, and several sector/asset-class ETFs) gives the eligible-candidate
  filter a genuinely diverse set to choose from after the outlier/liquidity
  exclusions.

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
list_featherless_models.py  verifies Featherless model availability against a real key
check_held_positions.py verifies get_held_underlying_symbols/get_market_clock against your real account
debug_place_order.py     places ONE real order via the actual order-placement function, to verify it end-to-end
run_continuous.py        re-runs scan+debate on a fixed interval
trade_journal.py         single shared journal writer — used by every entry point
view_journal.py         reviews the journal (handles old and new entry formats)
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
already-held check uses LIVE calls to your actual Alpaca account rather
than a cached list, so it stays accurate even partway through a
multi-candidate batch — and it checks **both** filled positions
(`get_held_underlying_symbols`) **and still-open, unfilled orders**
(`get_symbols_with_open_orders`). This second check exists because of a
real gap found in testing: two separate COST orders went out about an hour
apart, because the first was still sitting as an unfilled resting limit
order (not yet filled or expired) when a second debate independently
picked COST again — the held-positions check alone couldn't see it, since
no position had actually opened yet from either order. `main.py`'s
`--execute` marking something "executed" only ever meant "Alpaca accepted
the order," never "it filled" — worth remembering when reading the
journal, since an accepted order can still expire unfilled hours later
(check `check_recent_orders.py` or your Alpaca dashboard's order history
against actual open positions if you want to confirm what really filled).

## Tuning the watchlist

Edit `WATCHLIST` in `config.py` (used by `--scan` when `--symbols` isn't
passed) — currently 52 symbols across mega-cap tech, semiconductors,
financials, healthcare, consumer discretionary, high-options-volume growth
names, and sector/asset-class ETFs.

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

## VWAP bounce / break scanner (day-trade alerts)

`vwap_scanner.py` is a separate tool from the options debate. It watches a
list of tickers every minute and notifies you when one **breaks through** its
session VWAP (a reclaim or a loss, on volume) or **bounces off** it (a
pullback that holds and reverses). Each alert includes a reference entry,
stop, 1R/2R targets and the next VWAP σ band.

```bash
python vwap_scanner.py --symbols SPY,QQQ,NVDA,TSLA --notify ntfy
python vwap_scanner.py --replay tests/fixtures/spy_2026-11-02_tv.json   # see the rules on a real session
python -m unittest discover -s tests -v
```

- **Data:** Alpaca's 1-minute bars, using the same `ALPACA_API_KEY` /
  `ALPACA_SECRET_KEY` from `.env`. The free plan is IEX-only (`--feed iex`),
  so the VWAP comes from IEX volume and can sit a few cents off your chart's
  value. With a paid data plan, use `--feed sip`.
- **Phone push:** install the free [ntfy](https://ntfy.sh) app, subscribe to
  a hard-to-guess topic, and set `NTFY_TOPIC=<topic>` in `.env`. Telegram
  also works: set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. Check the
  setup with `--test-notify`.
- **Rules:** the exact rules and thresholds are documented in
  `vwap_signals.py`. A chop filter keeps the scanner silent when price keeps
  crossing VWAP back and forth.
- Alerts are logged to `vwap_signals.jsonl`. They're reference levels, not
  trade instructions.

### Real-time variant: TradingView connector + phone push (`tv_vwap_scanner.py`)

Alpaca's free bars are IEX-only and partly delayed, so this variant runs on
TradingView's streaming quotes, which Claude pulls through the TradingView
connector. Only Claude can call the connector, so the scanner runs as a
recurring Claude task in a Claude Code session. Each minute Claude makes one
`get_symbol_data_batch` call for all tickers, pipes the result into
`python3 tv_vwap_scanner.py tick`, and sends each `ALERT` line to your phone
as a push notification.

The quote's cumulative **consolidated** volume is differenced between
snapshots, so the VWAP covers the full tape, not one exchange. Per-minute
bars are rebuilt from the snapshots. Intra-minute wicks are only seen when
they set a new day high or low, so bounce alerts are a little stricter than
on a bar chart, while breaks are unaffected. In a replay of 2026-11-02 SPY,
the VWAP loss fired at 10:59 ET and the session VWAP matched 758.98 exactly.
If you start the scanner mid-session, seed each ticker first with today's
5-minute bars (`tv_vwap_scanner.py seed SPY < bars.json`). State is kept in
`.vwap_state/`.

Ticking every 5 minutes (`tick --interval 5`) uses a rescaled rule set:
each snapshot becomes one 5-minute bar, one 5-minute close beyond the zone
confirms a break, and the volume bar is 1.25x, the 1-minute 1.5x adjusted
for 5-bar averaging. A 5-minute replay of 2026-11-02 SPY fires the 10:16
VWAP bounce and the 11:01 VWAP loss, and nothing else.
