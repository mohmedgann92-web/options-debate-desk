"""
Data/action functions the agents rely on. Every one of these calls Alpaca's
official MCP server (see mcp_client.py). Field names below are confirmed
against REAL responses (captured via test_raw_call.py), not guessed:

- Every tool response is wrapped: {"_alpaca_mcp_security": {...}, "data": {...}}.
  The actual payload is under "data" — _data() unwraps this everywhere.
- get_stock_latest_quote -> data.quotes.<SYMBOL> = {ap, bp, as, bs, t, ...}
  (ap=ask price, bp=bid price — Alpaca's compact field naming)
- get_stock_bars -> data.bars.<SYMBOL> = [{c, h, l, o, t, v, vw, n}, ...]
- get_option_chain -> data.snapshots.<OCC_SYMBOL> = {latestQuote, dailyBar,
  latestTrade, minuteBar, prevDailyBar} — camelCase keys. Confirmed this
  account/tier does NOT return greeks or impliedVolatility, so we compute
  both ourselves via Black-Scholes (see black_scholes.py) from the quote
  mid-price, underlying price, and strike/expiry parsed from the OCC symbol.
"""
from datetime import datetime, date, timedelta
import json
import math
import statistics

from mcp_client import AlpacaMCPClient
import black_scholes as bs
import config

RISK_FREE_RATE = 0.045  # rough T-bill proxy; fine for demo-grade IV solving


def _data(result: dict) -> dict:
    """Unwrap the {"_alpaca_mcp_security": ..., "data": ...} envelope every tool returns."""
    return result.get("data", result) if isinstance(result, dict) else result


async def get_account_info(client: AlpacaMCPClient) -> dict:
    """Equity, cash, buying power — the Risk Manager's sizing inputs."""
    data = _data(await client.call_tool("get_account_info", {}))
    return {
        "equity": float(data.get("equity") or data.get("portfolio_value") or 0),
        "cash": float(data.get("cash") or 0),
        "buying_power": float(data.get("buying_power") or 0),
        "options_buying_power": float(data.get("options_buying_power") or data.get("buying_power") or 0),
        "pattern_day_trader": data.get("pattern_day_trader", False),
    }


async def get_stock_quote(client: AlpacaMCPClient, symbol: str) -> dict:
    """Latest NBBO quote for the underlying."""
    data = _data(await client.call_tool("get_stock_latest_quote", {"symbols": symbol}))
    q = data.get("quotes", {}).get(symbol, {})
    bid, ask = float(q.get("bp", 0) or 0), float(q.get("ap", 0) or 0)
    return {
        "symbol": symbol,
        "bid": bid,
        "ask": ask,
        "mid": round((bid + ask) / 2, 4) if bid and ask else None,
        "timestamp": q.get("t"),
    }


async def get_historical_bars(client: AlpacaMCPClient, symbol: str, lookback_days: int = 30) -> list[dict]:
    """Daily bars used to compute realized volatility."""
    data = _data(await client.call_tool(
        "get_stock_bars",
        {"symbols": symbol, "timeframe": "1Day", "days": lookback_days + 10, "limit": lookback_days + 10},
    ))
    bars = data.get("bars", {}).get(symbol, [])
    bars = bars[-lookback_days:]
    return [{"date": b.get("t"), "close": float(b["c"])} for b in bars if "c" in b]


def compute_realized_vol(bars: list[dict]) -> float:
    """Annualized realized volatility (close-to-close) from daily bars. Pure/local — no API call."""
    closes = [b["close"] for b in bars]
    if len(closes) < 2:
        return 0.0
    log_returns = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    daily_stdev = statistics.pstdev(log_returns)
    return round(daily_stdev * math.sqrt(252), 4)


def detect_vol_outlier_days(bars: list[dict], dominance_threshold: float = 0.30) -> dict:
    """
    Flags when realized vol is being dominated by one or two outlier days
    (e.g. a single earnings gap) rather than reflecting typical day-to-day
    movement. This matters because a trailing realized-vol number skewed by
    one big historical jump can look like a real IV-vs-RV 'edge' when it's
    actually a measurement artifact, not a persistent mispricing.

    dominance_threshold: if the single largest squared log-return accounts
    for more than this fraction of the TOTAL sum of squared log-returns
    (which is what realized vol is derived from), flag it.
    """
    closes = [b["close"] for b in bars]
    dates = [b["date"] for b in bars]
    if len(closes) < 3:
        return {"dominated_by_outlier": False, "note": "not enough data to assess"}

    log_returns = [(dates[i], math.log(closes[i] / closes[i - 1])) for i in range(1, len(closes))]
    squared = [(d, r ** 2) for d, r in log_returns]
    total_sq = sum(sq for _, sq in squared)
    if total_sq == 0:
        return {"dominated_by_outlier": False, "note": "no price movement in window"}

    max_date, max_sq = max(squared, key=lambda x: x[1])
    share = max_sq / total_sq

    return {
        "dominated_by_outlier": share > dominance_threshold,
        "outlier_date": max_date if share > dominance_threshold else None,
        "outlier_share_of_total_variance": round(share, 3),
        "note": (
            f"{round(share * 100, 1)}% of the realized-vol calculation comes from "
            f"a single day ({max_date}) — treat the realized vol number with caution, "
            f"it may not reflect typical day-to-day movement."
        ) if share > dominance_threshold else "realized vol looks representative of typical movement",
    }


async def get_option_chain(
    client: AlpacaMCPClient, underlying_symbol: str, underlying_price: float,
    min_dte: int = 7, max_dte: int = 60, limit: int = 250,
    strike_price_gte: float = None, strike_price_lte: float = None,
) -> list[dict]:
    """
    Option chain with LOCALLY COMPUTED IV and Greeks (the server doesn't
    provide them for this account tier — see module docstring). Requires
    the current underlying price to solve Black-Scholes.

    strike_price_gte/lte let the caller narrow the chain server-side (e.g.
    to a near-ATM band for scanning many symbols cheaply) — confirmed
    supported by get_option_chain's real input schema.
    """
    today = date.today()
    args = {
        "underlying_symbol": underlying_symbol,
        "expiration_date_gte": str(today + timedelta(days=min_dte)),
        "expiration_date_lte": str(today + timedelta(days=max_dte)),
        "limit": limit,
    }
    if strike_price_gte is not None:
        args["strike_price_gte"] = strike_price_gte
    if strike_price_lte is not None:
        args["strike_price_lte"] = strike_price_lte

    result = await client.call_tool("get_option_chain", args)
    snapshots = _data(result).get("snapshots", {})

    contracts = []
    for occ_symbol, snap in snapshots.items():
        try:
            parsed = bs.parse_occ_symbol(occ_symbol)
        except ValueError:
            continue  # skip anything that doesn't parse as a standard OCC symbol

        quote = snap.get("latestQuote", {}) or {}
        bid = float(quote.get("bp", 0) or 0)
        ask = float(quote.get("ap", 0) or 0)
        mid = (bid + ask) / 2 if bid and ask else None

        if mid is None:
            daily = snap.get("dailyBar", {}) or {}
            mid = float(daily.get("c", 0) or 0) or None

        dte = bs.days_to_expiry(parsed["expiry"], today)
        T = dte / 365.0

        iv = None
        greeks = {"delta": None, "gamma": None, "theta": None, "vega": None}
        if mid and mid > 0 and T > 0 and underlying_price > 0:
            iv = bs.implied_vol(mid, underlying_price, parsed["strike"], T, RISK_FREE_RATE, parsed["option_type"])
            if iv:
                greeks = bs.bs_greeks(underlying_price, parsed["strike"], T, RISK_FREE_RATE, iv, parsed["option_type"])

        contracts.append({
            "symbol": occ_symbol,
            "option_type": parsed["option_type"],
            "strike": parsed["strike"],
            "expiry": str(parsed["expiry"]),
            "days_to_expiry": dte,
            "bid": bid or None,
            "ask": ask or None,
            "mid": round(mid, 4) if mid else None,
            "implied_volatility": iv,
            **greeks,
        })
    return contracts


async def get_iv_edge(client: AlpacaMCPClient, symbol: str, min_dte: int = 7, max_dte: int = 60) -> dict:
    """
    Cheap per-symbol scoring signal for the scanner: pulls quote + realized
    vol (as usual) plus a NARROW near-ATM slice of the option chain (±5% of
    spot, server-side filtered) rather than the full chain, so scanning a
    watchlist doesn't mean solving Black-Scholes for hundreds of contracts
    per symbol.

    Returns None fields (and candidate_score=None) if there wasn't enough
    liquid data to compute an edge — the scanner should skip those, not
    treat them as a zero edge.
    """
    quote = await get_stock_quote(client, symbol)
    underlying_price = quote["mid"] or quote["ask"] or quote["bid"]
    if not underlying_price:
        return {"symbol": symbol, "error": "no valid quote", "candidate_score": None}

    bars = await get_historical_bars(client, symbol, lookback_days=30)
    realized_vol = compute_realized_vol(bars)
    outlier_check = detect_vol_outlier_days(bars)

    near_atm = await get_option_chain(
        client, symbol, underlying_price,
        min_dte=min_dte, max_dte=max_dte, limit=30,
        strike_price_gte=round(underlying_price * 0.95, 2),
        strike_price_lte=round(underlying_price * 1.05, 2),
    )

    # Liquidity filter: a "cheap" option on a wide bid-ask spread often
    # isn't real profit, since crossing the spread both ways eats the edge.
    # Only trust contracts where the spread is reasonably tight.
    max_spread_pct = config.LIQUIDITY_CONFIG["max_spread_pct"]
    liquid = []
    for c in near_atm:
        if not c.get("implied_volatility") or not c.get("bid") or not c.get("ask") or not c.get("mid"):
            continue
        spread_pct = (c["ask"] - c["bid"]) / c["mid"] if c["mid"] else 1.0
        if spread_pct <= max_spread_pct:
            liquid.append({**c, "spread_pct": spread_pct})

    min_liquid = config.LIQUIDITY_CONFIG["min_liquid_contracts"]
    if len(liquid) < min_liquid:
        return {
            "symbol": symbol, "underlying_price": underlying_price,
            "realized_volatility": realized_vol, "mean_atm_iv": None,
            "edge": None, "candidate_score": None, "contracts_used": len(liquid),
            "note": f"only {len(liquid)} liquid near-ATM contracts (need {min_liquid}, "
                    f"spread <= {max_spread_pct:.0%}) — too thin to trust",
        }

    ivs = [c["implied_volatility"] for c in liquid]
    avg_spread_pct = round(statistics.mean(c["spread_pct"] for c in liquid), 4)

    mean_iv = round(statistics.mean(ivs), 4)
    edge = round(mean_iv - realized_vol, 4)  # positive = options rich, negative = options cheap
    return {
        "symbol": symbol,
        "underlying_price": underlying_price,
        "realized_volatility": realized_vol,
        "vol_outlier_warning": outlier_check["note"] if outlier_check["dominated_by_outlier"] else None,
        "mean_atm_iv": mean_iv,
        "edge": edge,
        "candidate_score": round(abs(edge) * 100, 2),  # bigger mispricing = more interesting
        "contracts_used": len(ivs),
        "avg_spread_pct": avg_spread_pct,
    }


def compute_limit_price(legs: list[dict], chain_by_symbol: dict) -> float | None:
    """
    Computes the limit price to submit, from the SAME real chain data the
    Risk Governor already validated — not a fresh guess. Single-leg: the
    contract's real mid. Multi-leg: net debit/credit per share using
    Alpaca's own sign convention (positive = debit/cost, negative =
    credit/proceeds), i.e. what you pay on buys minus what you receive on
    sells. Returns None if any leg's real mid price isn't available.
    """
    if len(legs) == 1:
        contract = chain_by_symbol.get(legs[0]["symbol"])
        return round(contract["mid"], 2) if contract and contract.get("mid") is not None else None

    net = 0.0
    for leg in legs:
        contract = chain_by_symbol.get(leg["symbol"])
        if not contract or contract.get("mid") is None:
            return None
        sign = 1 if leg["side"] == "buy" else -1
        net += contract["mid"] * float(leg.get("ratio_qty", 1)) * sign
    return round(net, 2)


async def place_option_spread_order(
    client: AlpacaMCPClient, legs: list[dict], qty: int, limit_price: float = None
) -> dict:
    """
    Submit an options order through Alpaca's MCP `place_option_order` tool.

    legs: [{"symbol": "AAPL250117C00200000", "side": "buy"|"sell", "ratio_qty": 1}, ...]
    qty is the strategy multiplier — each leg's ratio_qty gets scaled by it
    (confirmed via the tool's real input schema).

    Uses a LIMIT order at `limit_price` (compute via compute_limit_price()
    from real chain data, not a fresh guess) rather than a market order.
    This matters for two real reasons found in testing: (1) options
    market orders are rejected outright when the market is closed, while
    limit orders queue for the next session instead; (2) a market order on
    a wide-spread option can fill far from the price the Risk Governor
    validated, silently invalidating its risk math. If limit_price isn't
    provided, falls back to a market order (not recommended).

    IMPORTANT: the real schema wants single-leg orders shaped differently
    from multi-leg ones — single-leg orders take `symbol`/`side` directly
    on the TOP LEVEL, not wrapped in a `legs` array. Sending a 1-item
    `legs` array for what should be a single-leg order was a real bug that
    caused a live order to be silently rejected (confirmed by checking
    Alpaca's own order history directly — the order never appeared at all).

    Runs against Alpaca's PAPER account as long as ALPACA_PAPER_TRADE=true
    in the MCP server's env (set via mcp_client.py / config.py).
    """
    order_type = "limit" if limit_price is not None else "market"

    if len(legs) == 1:
        leg = legs[0]
        args = {
            "qty": str(qty),
            "type": order_type,
            "time_in_force": "day",
            "symbol": leg["symbol"],
            "side": leg["side"],
        }
    else:
        mcp_legs = [
            {"symbol": leg["symbol"], "side": leg["side"], "ratio_qty": str(leg.get("ratio_qty", 1))}
            for leg in legs
        ]
        args = {
            "qty": str(qty),
            "type": order_type,
            "time_in_force": "day",
            "legs": mcp_legs,
            "order_class": "mleg",
        }

    if limit_price is not None:
        args["limit_price"] = str(limit_price)

    raw_result = await client.call_tool("place_option_order", args)
    result = _data(raw_result)

    order_id = result.get("id")
    if not order_id:
        # Fail LOUD, not silent. A previous version of this function
        # returned {"order_id": None, "status": None} here and moved on —
        # which meant a real order failure looked identical to a slow-but-
        # successful call in the transcript. Never again: if we can't
        # confirm a real order ID came back, raise with the full raw
        # response so the actual reason is visible immediately.
        raise RuntimeError(
            f"place_option_order did not return a valid order ID — the order "
            f"may NOT have been placed. Raw MCP response:\n{json.dumps(raw_result, indent=2, default=str)}"
        )

    return {
        "order_id": order_id,
        "status": result.get("status"),
    }


async def get_market_clock(client: AlpacaMCPClient) -> dict:
    """
    Returns Alpaca's market open/closed status. UNVERIFIED field names
    (unlike the tools above, this hasn't been cross-checked against a real
    response via test_raw_call.py) — defensive on purpose: if the response
    shape doesn't match what's expected, returns is_open=None rather than
    raising, so callers can treat "unknown" as "proceed anyway" instead of
    crashing a long-running loop over a guessed field name.
    """
    try:
        result = await client.call_tool("get_clock", {})
        data = _data(result)
        return {
            "is_open": data.get("is_open"),
            "next_open": data.get("next_open"),
            "next_close": data.get("next_close"),
        }
    except Exception as exc:
        return {"is_open": None, "error": str(exc)}


async def get_held_underlying_symbols(client: AlpacaMCPClient) -> set[str]:
    """
    Returns the set of underlying tickers you currently hold an options
    position in (e.g. {"NVDA", "AAPL"} if you're long an NVDA call and an
    AAPL spread) — used by continuous/auto-execute mode to avoid repeatedly
    piling into the same name every run instead of diversifying.

    UNVERIFIED field names, same caveat as get_market_clock: get_all_positions
    hasn't been cross-checked against a real response. FAILS OPEN on parse
    errors (returns an empty set, i.e. "assume nothing is held") rather than
    blocking all trading if this breaks — the position-skip check is a
    diversification nicety, not a safety-critical gate like the Risk
    Governor, so it shouldn't be able to halt the whole system if it's wrong.
    Run_continuous.py prints what this returns at startup specifically so
    you can sanity-check it's actually working before trusting an
    unattended run.
    """
    try:
        result = await client.call_tool("get_all_positions", {})
        data = _data(result)
        positions = data if isinstance(data, list) else data.get("positions", data.get("result", []))
        symbols = set()
        for p in positions:
            symbol = p.get("symbol", "")
            # Options position symbols are OCC-format (e.g. AAPL260902C00310000);
            # equity positions are just the ticker (e.g. AAPL). Extract the
            # root ticker either way.
            try:
                symbols.add(bs.parse_occ_symbol(symbol)["root"])
            except ValueError:
                symbols.add(symbol)  # wasn't OCC-format, assume it's already a plain ticker
        return symbols
    except Exception:
        return set()
