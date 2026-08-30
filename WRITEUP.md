# Options Alpha Debate Desk
### Alpaca AI Trading Agents Hackathon — Options Alpha Agents Track

A multi-agent options trading desk that finds its own opportunities, debates
them adversarially, and enforces risk limits with a deterministic backstop
that doesn't trust the LLM's own arithmetic. Built on Alpaca's official MCP
server — every market data pull, quote, and order goes through the Model
Context Protocol, not a direct SDK call.

**Verified live**: order ID `7639d467-f129-4337-9766-5e5e5741cb2b`,
`AAPL260902C00310000`, limit $4.46, status `accepted` — the full pipeline
(scan → debate → risk governor → real paper order) has actually run end to
end against a live Alpaca account, confirmed independently on Alpaca's own
dashboard.

---

## AI Logic

Four LLM agents, each with a narrow, adversarial role, reasoning over the
same real market data:

- **🐂 Bull** and **🐻 Bear** independently build the single strongest case
  for a directional options trade, grounded in the underlying's quote,
  realized volatility, and near-term option chain. They're required to name
  a concrete contract and strategy — no vague "AAPL looks bullish."
- **🛡️ Risk Manager** (LLM) reviews both proposals against the account's
  risk budget and can veto either side outright.
- **✅ Decision Agent** picks a winner, resizes per the Risk Manager's
  recommendation, or explicitly returns **NO TRADE** — a first-class
  outcome, not a failure mode.

Ahead of the debate, an optional **scanner** ranks a configurable watchlist
by `|mean near-ATM implied vol − realized vol|`, so the desk can identify
its own candidate instead of being told what to look at — the closest thing
to autonomous opportunity discovery the time budget allowed.

Since Alpaca's MCP server doesn't return Greeks or implied volatility for
this account/data tier, **IV and Greeks are computed locally via
Black-Scholes** (Newton-Raphson with a bisection fallback) from each
contract's real bid/ask, the underlying price, and strike/expiry parsed
directly from the OCC symbol — real quant work, not a passthrough.

## Risk Gates — and why there are two layers, not one

Every agent's output is structured JSON, so the full debate is logged and
auditable. But logging isn't the same as safety, and testing surfaced a real
failure worth being upfront about: in one run, the Bear agent's *narrative*
described a $10-wide credit spread, while the *actual contract symbols* it
selected were $50 wide on two different expiration dates. Both symbols were
genuine, so a naive "does this symbol exist" check passed — but the LLM Risk
Manager computed its approval from the agent's stated numbers, not the real
ones, and approved a trade that was actually **~10x over its own risk
budget** (~$19,100 real max loss vs. a $2,001 limit).

That produced the system's second, more important risk layer: a
**deterministic Risk Governor** (`risk_governor.py`, plain Python, no LLM)
that recomputes real max loss directly from the fact-checked contract data
— real strikes, real mids, real expiries — and hard-blocks execution if the
recomputed numbers violate the budget or the legs don't form a valid
same-expiry structure, regardless of what any agent concluded. On a
follow-up run with a legitimate, correctly-priced trade, the same governor
approved it — confirming it discriminates correctly rather than just
rejecting everything.

Every decision also passes through a **fact-check step** that looks up each
chosen leg against the real option chain and flags any symbol that doesn't
exist (hallucination), and a **volatility-outlier detector** that flags when
a single day (e.g. an earnings gap) is dominating a realized-vol number,
since that can manufacture a fake "edge" that isn't a real mispricing.

Order execution surfaced two more real bugs, both only found by actually
submitting orders against a live paper account: single-leg trades (e.g. a
plain long call) were being silently rejected because passing a `legs`
array at all — even with one entry — auto-triggers Alpaca's multi-leg
validation, which requires 2–4 legs; and limit prices computed from a raw
bid/ask mid (e.g. `4.465`) were rejected for having 3 decimal places
instead of 2. Both are fixed, and the fix is the confirmed live order
above, not just a passing unit test.

## Alpaca Infrastructure

- **MCP Server**: `alpacahq/alpaca-mcp-server` v2, launched via `uvx` and
  driven over stdio through the official Model Context Protocol — the
  integration the hackathon page calls "the core of the hackathon theme."
  Every data pull (`get_account_info`, `get_stock_latest_quote`,
  `get_stock_bars`, `get_option_chain`) and order (`place_option_order`) is
  a real MCP tool call.
- **Paper trading only** throughout development — `ALPACA_PAPER_TRADE=true`
  end to end.

## What's Next

A Streamlit dashboard for a live visual demo; a lightweight Portfolio
Manager agent that checks existing positions before sizing new trades; a
more robust (outlier-trimmed) realized-vol estimator instead of just
flagging the problem.

---
*Paper trading only. Not investment advice.*
