"""
Streamlit dashboard for the Options Alpha Debate Desk.

This is a UI layer only — it contains no trading or risk logic of its own.
Every real decision (Bull/Bear proposals, Risk Manager review, the Desk
Head's final call, the deterministic Risk Governor's execution gate, and
order placement) is made by calling the exact same functions main.py's CLI
calls: agents/*.py, risk_governor.check_trade(), alpaca_tools.py, and the
shared orchestrator helpers. This file just renders each stage live as it
completes instead of printing a rich console transcript.

Run with:
    streamlit run app.py
"""
import asyncio
import json
import time
import traceback
from datetime import datetime, timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

import alpaca_tools
import config
import position_tracker
import risk_governor
import scanner
import trade_journal
from mcp_client import AlpacaMCPClient
from agents import bull, bear, risk_manager, decision
from orchestrator import (
    gather_market_data,
    _build_agent_payload,
    _build_chain_lookup,
    _save_market_data,
)

st.set_page_config(page_title="Options Alpha Debate Desk", page_icon="📈", layout="wide")

# Captured once, before any UI-driven mutation, so the "reset to defaults"
# button has something real to reset back to (config.py's own values).
_DEFAULT_RISK_CONFIG = dict(config.RISK_CONFIG)

# Continuous-run state — persists across reruns via st.session_state (a
# fresh script rerun redraws the whole page, so anything that must survive
# between runs, like "is continuous mode on" or run history, has to live
# here rather than in a plain module-level variable).
for _key, _default in {
    "continuous_active": False,
    "next_run_at": None,
    "continuous_run_count": 0,
    "continuous_history": [],
    # Last-known-good snapshot of everything on-screen, repainted on EVERY
    # rerun (not just the rerun where a run actually happened) — an
    # st.empty() placeholder goes blank on any rerun that doesn't write
    # into it again, and continuous mode's poll cycle (plus any ordinary
    # widget interaction) reruns the whole script every few seconds without
    # necessarily starting a new run. Without this, the account snapshot,
    # positions table, and last debate/execute outcome would all flicker
    # away within seconds of finishing instead of staying up until the next
    # run replaces them.
    "last_account_info": None,
    "last_positions": None,
    "last_result": None,
    "last_risk_caption": None,
    "last_error": None,
}.items():
    if _key not in st.session_state:
        st.session_state[_key] = _default


# ──────────────────────────────────────────────────────────────────────────
# Small rendering helpers (presentation only — no trading logic lives here)
# ──────────────────────────────────────────────────────────────────────────

def render_account(container, account_info: dict) -> None:
    with container.container():
        st.markdown("#### 💰 Account Snapshot")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Equity", f"${account_info['equity']:,.2f}")
        c2.metric("Cash", f"${account_info['cash']:,.2f}")
        c3.metric("Buying Power", f"${account_info['buying_power']:,.2f}")
        c4.metric("Options BP", f"${account_info['options_buying_power']:,.2f}")


def render_positions(container, positions: list[dict]) -> None:
    """Renders alpaca_tools.get_open_positions()'s output — live P&L on
    whatever's actually held right now, not just what this session traded."""
    with container.container():
        st.markdown("#### 📊 Open Positions (live)")
        if not positions:
            st.caption("No open positions.")
            return

        rows = []
        for p in positions:
            label = p["symbol"]
            if p["option_type"]:
                label = f"{p['underlying']} {p['strike']:.0f}{p['option_type'][0].upper()} {p['expiry']}"
            pl, plpc = p["unrealized_pl"], p["unrealized_plpc"]
            rows.append({
                "Position": label,
                "Symbol": p["symbol"],
                "Side": p["side"],
                "Qty": p["qty"],
                "Avg Entry": p["avg_entry_price"],
                "Current": p["current_price"],
                "Mkt Value": p["market_value"],
                "Unrealized P&L": f"{'+' if pl is not None and pl >= 0 else ''}{pl:,.2f}" if pl is not None else "—",
                "P&L %": f"{'+' if plpc is not None and plpc >= 0 else ''}{plpc:.1%}" if plpc is not None else "—",
                "DTE": p["days_to_expiry"] if p["days_to_expiry"] is not None else "—",
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

        known_pl = [p["unrealized_pl"] for p in positions if p["unrealized_pl"] is not None]
        if known_pl:
            total = sum(known_pl)
            sign = "+" if total >= 0 else ""
            st.caption(f"Total unrealized P&L across {len(positions)} position(s): {sign}${total:,.2f}")
        st.caption("Field names for this response are unverified against Alpaca's real schema — "
                   "run `check_held_positions.py` to cross-check if a number here looks wrong.")


async def fetch_and_render_positions(positions_ph) -> list[dict]:
    """Standalone position check — opens its own short-lived MCP connection,
    independent of running a debate. Safe to click any time."""
    async with AlpacaMCPClient() as client:
        positions = await alpaca_tools.get_open_positions(client)
        render_positions(positions_ph, positions)
        return positions


def _summarize_args(args: dict, max_len: int = 100) -> str:
    s = json.dumps(args, default=str)
    return s if len(s) <= max_len else s[:max_len] + "…"


def _summarize_mcp_value(v, max_len: int = 100) -> str:
    """Short one-line preview for the log table — the full untruncated
    value is kept in the entry itself and rides along in the downloadable
    debate JSON, so nothing is actually lost, just not printed inline."""
    if isinstance(v, dict):
        keys = list(v.keys())
        head = ", ".join(keys[:4])
        return f"dict[{len(keys)} keys: {head}{'…' if len(keys) > 4 else ''}]"
    if isinstance(v, list):
        return f"list[{len(v)} items]"
    s = str(v)
    return s if len(s) <= max_len else s[:max_len] + "…"


def instrument_mcp_client(client: AlpacaMCPClient, log: list) -> None:
    """
    Wraps client.call_tool so every real MCP call is recorded into `log`
    (still rides along, untruncated, in the downloadable debate JSON as
    `mcp_call_log`) — no longer rendered live on the page (removed per
    request to declutter the view), but the capture itself is cheap and
    stays for that audit trail.

    Presentation only: never changes the arguments sent or the response any
    caller receives, and mcp_client.py itself is untouched — this monkey-
    patches only THIS client instance's bound method, so main.py and
    run_continuous.py behave identically; they never see this wrapper.
    """
    original_call_tool = client.call_tool

    async def logged_call_tool(name: str, arguments: dict):
        entry = {
            "time": datetime.now().strftime("%H:%M:%S"),
            "tool": name,
            "args": arguments,
            "args_preview": _summarize_args(arguments),
            "status": "⏳",
            "elapsed_ms": None,
            "result": None,
            "result_preview": "",
        }
        log.append(entry)
        start = time.monotonic()
        try:
            result = await original_call_tool(name, arguments)
            entry["elapsed_ms"] = round((time.monotonic() - start) * 1000, 1)
            entry["result"] = result
            entry["result_preview"] = _summarize_mcp_value(result)
            entry["status"] = "✓"
            return result
        except Exception as exc:
            entry["elapsed_ms"] = round((time.monotonic() - start) * 1000, 1)
            entry["status"] = "✗"
            entry["result_preview"] = f"ERROR: {exc}"
            raise

    client.call_tool = logged_call_tool


# Categorical slots 1 (blue) and 2 (orange) from the validated default
# palette — the pair with the largest adjacent CVD/normal-vision separation
# in the fixed 8-hue order, so Calls vs. Puts is never a guess even for a
# colorblind viewer. Light/dark steps of the same two hues, not a re-hue.
_CALL_COLOR = {"light": "#2a78d6", "dark": "#3987e5"}
_PUT_COLOR = {"light": "#eb6834", "dark": "#d95926"}


def _chart_theme() -> dict:
    """Best-effort light/dark detection for chart chrome — Streamlit's own
    theme setting, when available. Falls back to light (this app's default
    config) rather than guessing."""
    base = st.get_option("theme.base") or "light"
    dark = base == "dark"
    return {
        "call": _CALL_COLOR["dark"] if dark else _CALL_COLOR["light"],
        "put": _PUT_COLOR["dark"] if dark else _PUT_COLOR["light"],
        "ink": "#ffffff" if dark else "#0b0b0b",
        "muted": "#898781",
        "grid": "#2c2c2a" if dark else "#e1e0d9",
    }


def render_iv_skew(market_data: dict, max_expiries: int = 6) -> None:
    """
    IV vs. strike ("smile"/skew), faceted one subplot per expiry — mixing
    expiries on one axis would blur the shape a skew chart exists to show.
    Only two series ever appear (Calls, Puts), so this stays within the
    palette's validated 2-series pair regardless of how many expiries are
    shown. Purely a view over market_data["option_chain_sample"], which
    already carries locally-computed IV/Greeks — no new data fetched.
    """
    chain = market_data.get("option_chain_sample") or []
    contracts = [c for c in chain if c.get("implied_volatility") and c.get("strike")]
    if not contracts:
        st.caption("No contracts with a solvable implied volatility to chart (thin/no quotes).")
        return

    q = market_data["quote"]
    underlying_price = q.get("mid") or q.get("ask") or q.get("bid")

    by_expiry: dict[str, list[dict]] = {}
    for c in contracts:
        by_expiry.setdefault(c["expiry"], []).append(c)
    expiries = sorted(by_expiry.keys())
    shown = expiries[:max_expiries]
    hidden = len(expiries) - len(shown)

    theme = _chart_theme()
    cols = 2 if len(shown) > 1 else 1
    rows = (len(shown) + cols - 1) // cols
    fig = make_subplots(
        rows=rows, cols=cols,
        subplot_titles=[f"{e} ({by_expiry[e][0]['days_to_expiry']}d)" for e in shown],
    )

    all_ivs = [c["implied_volatility"] for c in contracts]
    y_max = max(all_ivs) * 100 * 1.15 if all_ivs else 100

    for i, expiry in enumerate(shown):
        r, cno = i // cols + 1, i % cols + 1
        pts = sorted(by_expiry[expiry], key=lambda c: c["strike"])
        for opt_type, color, name in [("call", theme["call"], "Calls"), ("put", theme["put"], "Puts")]:
            subset = [c for c in pts if c["option_type"] == opt_type]
            if not subset:
                continue
            fig.add_trace(
                go.Scatter(
                    x=[c["strike"] for c in subset],
                    y=[c["implied_volatility"] * 100 for c in subset],
                    mode="lines+markers",
                    name=name,
                    legendgroup=name,
                    showlegend=(i == 0),
                    line=dict(color=color, width=2),
                    marker=dict(size=8, color=color),
                    customdata=[[c.get("mid") or 0, c.get("delta") or 0] for c in subset],
                    hovertemplate=(
                        f"<b>{name[:-1]}</b> strike $%{{x:.2f}}<br>IV %{{y:.1f}}%<br>"
                        "mid $%{customdata[0]:.2f} · delta %{customdata[1]:.2f}<extra></extra>"
                    ),
                ),
                row=r, col=cno,
            )
        if underlying_price:
            fig.add_vline(x=underlying_price, line_dash="dash", line_color=theme["muted"],
                          line_width=1, row=r, col=cno)

    fig.update_layout(
        height=300 * rows,
        margin=dict(l=40, r=20, t=44, b=36),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color=theme["ink"]),
        legend=dict(orientation="h", yanchor="bottom", y=1.06, xanchor="right", x=1),
        hovermode="closest",
    )
    fig.update_xaxes(title_text="Strike", gridcolor=theme["grid"], zeroline=False)
    fig.update_yaxes(title_text="IV %", gridcolor=theme["grid"], zeroline=False, range=[0, y_max])

    st.markdown("##### 😊 Implied Volatility Skew")
    caption = f"Spot ${underlying_price:.2f} (dashed line) · locally computed via Black-Scholes"
    if hidden > 0:
        caption += f" · showing nearest {len(shown)} of {len(expiries)} expiries in the sample"
    st.caption(caption)
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})


def render_market_data(container, market_data: dict) -> None:
    with container.container():
        st.markdown(f"#### 📡 Market Data — {market_data['symbol']}")
        q = market_data["quote"]
        c1, c2, c3 = st.columns(3)
        c1.metric("Underlying Mid", f"${q['mid']:.2f}" if q["mid"] else "—")
        c2.metric("Realized Vol (ann.)", f"{market_data['realized_volatility_annualized']:.1%}")
        outlier = market_data["realized_vol_outlier_check"]
        c3.metric("Vol Outlier Risk", "⚠ Yes" if outlier["dominated_by_outlier"] else "✓ No")
        if outlier["dominated_by_outlier"]:
            st.warning(outlier["note"])

        render_iv_skew(market_data)

        chain = market_data["option_chain_sample"]
        if chain:
            df = pd.DataFrame(chain).sort_values(["expiry", "strike"]).reset_index(drop=True)
            cols = ["symbol", "option_type", "strike", "expiry", "days_to_expiry",
                     "bid", "ask", "mid", "implied_volatility", "delta", "gamma", "theta", "vega"]
            cols = [c for c in cols if c in df.columns]
            with st.expander(f"Option chain sample ({len(df)} contracts, locally computed IV/Greeks)"):
                st.dataframe(df[cols], use_container_width=True, hide_index=True)
        else:
            st.warning("No option chain contracts came back for this symbol/window.")


# ──────────────────────────────────────────────────────────────────────────
# Badges — small colored chips (icon + label always together, never color
# alone, per the dataviz status-palette rule) for confidence, veto/pass
# verdicts, and the final action. Fixed hex, deliberately NOT theme-switched
# (same posture as the skill's status palette: "fixed — never themed").
# ──────────────────────────────────────────────────────────────────────────

def _md_safe(text) -> str:
    """
    Escapes literal '$' before handing agent/governor free text to
    st.markdown/st.caption/st.info/etc. Streamlit's markdown renderer treats
    ANY pair of bare '$' in one string as inline LaTeX math delimiters — an
    LLM-written thesis or a governor note ("Real width=$15.00, real net
    debit=$1.80/share") very plausibly contains two or more real dollar
    amounts, which then silently render as garbled math instead of the
    actual text. Found live while testing the new payoff-diagram caption;
    applied everywhere agent/governor free text reaches the page.
    """
    return str(text).replace("$", "\\$")


def _badge_html(label: str, bg: str, fg: str, icon: str = "") -> str:
    return (
        f'<span style="display:inline-block;padding:1px 10px;border-radius:12px;'
        f'background:{bg};color:{fg};font-weight:600;font-size:0.82em;'
        f'white-space:nowrap;">{icon} {label}</span>'
    )


# Ordinal sequential-blue ramp (single hue, light→dark = low→high), bounded
# per the skill: no lighter than step 250, no darker than step 600.
_CONFIDENCE_BADGE = {
    "low":    {"bg": "#86b6ef", "fg": "#0b0b0b", "icon": "○"},
    "medium": {"bg": "#2a78d6", "fg": "#ffffff", "icon": "◐"},
    "high":   {"bg": "#184f95", "fg": "#ffffff", "icon": "●"},
}


def _confidence_badge(confidence: str) -> str:
    level = (confidence or "").strip().lower()
    spec = _CONFIDENCE_BADGE.get(level)
    if spec is None:
        return _badge_html(confidence or "unknown", "#898781", "#ffffff", "?")
    return _badge_html(level.upper(), spec["bg"], spec["fg"], spec["icon"])


# Status palette: good (passes) / critical (vetoed) — fixed, never themed.
def _verdict_badge(vetoed: bool) -> str:
    if vetoed:
        return _badge_html("VETOED", "#d03b3b", "#ffffff", "✗")
    return _badge_html("PASSES", "#0ca30c", "#ffffff", "✓")


# Reuses the same Bull=blue / Bear=orange identity already established by
# the IV skew chart's Calls/Puts colors, so the whole dashboard stays
# consistent on what "bullish" vs "bearish" looks like.
_ACTION_BADGE = {
    "execute_bull": {"bg": _CALL_COLOR["light"], "fg": "#ffffff", "icon": "🐂", "label": "EXECUTE BULL"},
    "execute_bear": {"bg": _PUT_COLOR["light"], "fg": "#ffffff", "icon": "🐻", "label": "EXECUTE BEAR"},
    "no_trade":     {"bg": "#898781", "fg": "#ffffff", "icon": "⏸", "label": "NO TRADE"},
}


def _action_badge(action: str) -> str:
    spec = _ACTION_BADGE.get(action, {"bg": "#898781", "fg": "#ffffff", "icon": "•", "label": action})
    return _badge_html(spec["label"], spec["bg"], spec["fg"], spec["icon"])


def render_bull(container, bull_case: dict) -> None:
    with container.container():
        st.markdown("##### 🐂 Bull Case")
        st.markdown(f"**Thesis:** {_md_safe(bull_case['thesis'])}")
        st.markdown(f"**Strategy:** {_md_safe(bull_case['strategy'])}")
        st.markdown(
            f"**Confidence:** {_confidence_badge(bull_case['confidence'])} — "
            f"{_md_safe(bull_case['confidence_reason'])}",
            unsafe_allow_html=True,
        )
        if bull_case.get("legs"):
            st.dataframe(pd.DataFrame(bull_case["legs"]), hide_index=True, use_container_width=True)
        st.caption(f"Target: {_md_safe(bull_case.get('target_price'))} · "
                   f"Invalidation: {_md_safe(bull_case.get('invalidation'))}")


def render_bear(container, bear_case: dict) -> None:
    with container.container():
        st.markdown("##### 🐻 Bear Case")
        st.markdown(f"**Thesis:** {_md_safe(bear_case['thesis'])}")
        st.markdown(f"**Strategy:** {_md_safe(bear_case['strategy'])}")
        st.markdown(
            f"**Confidence:** {_confidence_badge(bear_case['confidence'])} — "
            f"{_md_safe(bear_case['confidence_reason'])}",
            unsafe_allow_html=True,
        )
        if bear_case.get("legs"):
            st.dataframe(pd.DataFrame(bear_case["legs"]), hide_index=True, use_container_width=True)
        st.caption(f"Target: {_md_safe(bear_case.get('target_price'))} · "
                   f"Invalidation: {_md_safe(bear_case.get('invalidation'))}")


def render_risk_review(container, risk_review: dict) -> None:
    with container.container():
        st.markdown("##### 🛡️ Risk Manager")
        b, r = risk_review["bull_review"], risk_review["bear_review"]
        c1, c2 = st.columns(2)
        with c1:
            st.markdown(
                f"**Bull:** {_verdict_badge(b['vetoed'])} · qty={b['recommended_qty']}",
                unsafe_allow_html=True,
            )
            st.caption(_md_safe(b["notes"]))
        with c2:
            st.markdown(
                f"**Bear:** {_verdict_badge(r['vetoed'])} · qty={r['recommended_qty']}",
                unsafe_allow_html=True,
            )
            st.caption(_md_safe(r["notes"]))
        if risk_review.get("overall_notes"):
            st.info(_md_safe(risk_review["overall_notes"]))


def render_decision(container, final: dict) -> None:
    with container.container():
        st.markdown("##### ✅ Desk Head Decision")
        action = final["action"]
        st.markdown(f"**Action:** {_action_badge(action)}", unsafe_allow_html=True)
        st.markdown(f"**Qty:** {final.get('qty', 0)}")
        st.markdown(f"**Rationale:** {_md_safe(final['rationale'])}")
        if final.get("legs"):
            st.dataframe(pd.DataFrame(final["legs"]), hide_index=True, use_container_width=True)


# ──────────────────────────────────────────────────────────────────────────
# Payoff diagram — P&L-at-expiration for the Desk Head's chosen trade.
# Presentation-only, but deliberately reuses the EXACT SAME real-chain-data
# resolution and buy/sell sign convention as risk_governor.check_trade and
# alpaca_tools.compute_limit_price (mid price, ratio_qty, side — never the
# agent's own stated numbers), so the shape on screen can never disagree
# with the dollar figure the Governor prints above it.
# ──────────────────────────────────────────────────────────────────────────

def _resolve_payoff_legs(legs: list[dict], chain_by_symbol: dict) -> list[dict] | None:
    """Same resolution step as risk_governor.check_trade: every leg must be
    a real, priced contract or we refuse to chart it (fail-closed, matching
    the Governor's own posture rather than guessing)."""
    resolved = []
    for leg in legs:
        contract = chain_by_symbol.get(leg.get("symbol", ""))
        if contract is None or contract.get("mid") is None:
            return None
        resolved.append({
            "side": leg.get("side"),
            "ratio_qty": float(leg.get("ratio_qty", 1)),
            "strike": contract["strike"],
            "option_type": contract["option_type"],
            "mid": contract["mid"],
        })
    return resolved


def _intrinsic_value(spot: float, leg: dict) -> float:
    if leg["option_type"] == "call":
        return max(0.0, spot - leg["strike"])
    return max(0.0, leg["strike"] - spot)


def compute_payoff_curve(legs: list[dict], qty: int, chain_by_symbol: dict,
                          spot_price: float | None, n_points: int = 200) -> dict | None:
    """
    Returns {"xs", "ys", "net_cost", "max_gain", "max_loss", "breakevens"} or
    None if the trade can't be charted (unresolvable legs / no spot price).

    net_cost follows Alpaca's own sign convention (same as compute_limit_price):
    positive = debit paid, negative = credit received. ys is P&L in dollars
    at each simulated expiration spot price in xs, already net of that cost.
    """
    if not legs or not qty or spot_price is None:
        return None
    resolved = _resolve_payoff_legs(legs, chain_by_symbol)
    if not resolved:
        return None

    net_premium_per_share = sum(
        r["mid"] * r["ratio_qty"] * (1 if r["side"] == "buy" else -1) for r in resolved
    )
    net_cost = net_premium_per_share * 100 * qty

    strikes = [r["strike"] for r in resolved]
    lo = max(0.01, min(strikes + [spot_price]) * 0.7)
    hi = max(strikes + [spot_price]) * 1.3
    step = (hi - lo) / (n_points - 1)
    xs = [lo + i * step for i in range(n_points)]

    ys = []
    for x in xs:
        payoff = sum(
            (1 if r["side"] == "buy" else -1) * _intrinsic_value(x, r) * r["ratio_qty"]
            for r in resolved
        ) * 100 * qty
        ys.append(payoff - net_cost)

    breakevens = []
    for i in range(1, len(xs)):
        y0, y1 = ys[i - 1], ys[i]
        if (y0 <= 0 < y1) or (y0 > 0 >= y1):
            frac = -y0 / (y1 - y0) if y1 != y0 else 0.0
            breakevens.append(xs[i - 1] + frac * step)

    return {
        "xs": xs, "ys": ys, "net_cost": net_cost,
        "max_gain": max(ys), "max_loss": min(ys),
        "breakevens": breakevens,
    }


def render_payoff(container, final: dict, chain_by_symbol: dict, spot_price: float | None) -> None:
    legs = final.get("legs") or []
    qty = final.get("qty", 0) or 0
    with container.container():
        st.markdown("##### 📐 Payoff at Expiration")
        if not legs or not qty:
            st.caption("No trade to chart (no legs / zero quantity).")
            return
        curve = compute_payoff_curve(legs, qty, chain_by_symbol, spot_price)
        if curve is None:
            st.caption("Could not chart payoff — one or more legs aren't priced in the real chain "
                       "data (see Fact Check below), or no spot price is available.")
            return

        theme = _chart_theme()
        xs, ys = curve["xs"], curve["ys"]
        pos_ys = [max(0.0, y) for y in ys]
        neg_ys = [min(0.0, y) for y in ys]

        fig = go.Figure()
        fig.add_trace(go.Scatter(x=xs, y=pos_ys, mode="none", fill="tozeroy",
                                  fillcolor="rgba(12,163,12,0.25)", name="🟢 Profit",
                                  hoverinfo="skip"))
        fig.add_trace(go.Scatter(x=xs, y=neg_ys, mode="none", fill="tozeroy",
                                  fillcolor="rgba(208,59,59,0.25)", name="🔴 Loss",
                                  hoverinfo="skip"))
        fig.add_trace(go.Scatter(x=xs, y=ys, mode="lines", line=dict(color=theme["ink"], width=2),
                                  name="P&L", showlegend=False,
                                  hovertemplate="Spot $%{x:.2f} → P&L $%{y:,.2f}<extra></extra>"))
        fig.add_hline(y=0, line_color=theme["muted"], line_width=1)
        if spot_price:
            fig.add_vline(x=spot_price, line_dash="dash", line_color=theme["muted"], line_width=1,
                          annotation_text="spot", annotation_position="top")
        for be in curve["breakevens"]:
            fig.add_vline(x=be, line_dash="dot", line_color=theme["muted"], line_width=1,
                          annotation_text=f"BE ${be:.2f}", annotation_position="bottom")

        fig.update_layout(
            height=320,
            margin=dict(l=40, r=20, t=20, b=36),
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            font=dict(color=theme["ink"]),
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
            hovermode="x",
        )
        fig.update_xaxes(title_text="Underlying price at expiration", gridcolor=theme["grid"], zeroline=False)
        fig.update_yaxes(title_text="P&L ($)", gridcolor=theme["grid"], zeroline=False)
        st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})

        # NOTE: escaped as \$ — Streamlit's markdown renderer treats a pair of
        # bare "$" anywhere in the same string as inline LaTeX math delimiters,
        # and this caption has several independent dollar amounts in one
        # string, which silently mis-rendered numbers as math before this fix.
        cost_label = (f"\\${curve['net_cost']:,.2f} debit paid" if curve["net_cost"] >= 0
                      else f"\\${-curve['net_cost']:,.2f} credit received")
        gain_label = f"\\${curve['max_gain']:,.2f}"
        loss_label = f"\\${curve['max_loss']:,.2f}" if curve["max_loss"] >= 0 else f"-\\${-curve['max_loss']:,.2f}"
        right_slope = ys[-1] - ys[-2] if len(ys) > 1 else 0.0
        if right_slope > 0.01:
            gain_label += " (still rising at chart edge — uncapped upside)"
        elif right_slope < -0.01:
            loss_label += " (still falling at chart edge — undefined-risk downside)"
        be_label = ", ".join(f"\\${b:.2f}" for b in curve["breakevens"]) or "none in range"
        st.caption(
            f"Net {cost_label} · breakeven(s) {be_label} · shown max gain {gain_label} · "
            f"shown max loss {loss_label} · same real chain data and sign convention as the "
            f"Risk Governor below — no separate estimate."
        )


def render_fact_check(container, final: dict, chain_by_symbol: dict) -> None:
    """Presentation-only re-render of the same lookup orchestrator._fact_check_legs
    does — checking every chosen leg symbol against the real chain data the
    agents were given, to catch hallucinated contracts. No risk math here."""
    rows = []
    for leg in final.get("legs", []):
        symbol = leg.get("symbol", "")
        contract = chain_by_symbol.get(symbol)
        if contract is None:
            rows.append({"symbol": symbol, "found": "⚠ NOT FOUND — possibly hallucinated",
                         "strike": None, "type": None, "mid": None, "iv": None, "dte": None})
        else:
            rows.append({
                "symbol": symbol, "found": "✓ real contract",
                "strike": contract["strike"], "type": contract["option_type"],
                "mid": contract["mid"], "iv": contract["implied_volatility"],
                "dte": contract["days_to_expiry"],
            })
    with container.container():
        st.markdown("##### 🔍 Fact Check vs. Real Chain Data")
        if rows:
            df = pd.DataFrame(rows)
            bad = df["found"].str.contains("NOT FOUND")
            if bad.any():
                st.error(f"{bad.sum()} leg(s) do not exist in the real option chain data given to the agents.")
            st.dataframe(df, hide_index=True, use_container_width=True)


def render_governor(container, governor_result: dict) -> None:
    with container.container():
        st.markdown("##### 🏛️ Deterministic Risk Governor (non-LLM)")
        if governor_result["approved"]:
            st.success(
                f"✓ APPROVED — real max loss ${governor_result['real_max_loss_total']:,.2f} "
                f"({governor_result['real_max_loss_pct_equity']:.2%} of equity), computed from actual chain data."
            )
        else:
            st.error("✗ EXECUTION BLOCKED — this trade does NOT pass deterministic verification, "
                      "regardless of agent approval:")
            for v in governor_result["violations"]:
                st.markdown(f"- {_md_safe(v)}")
        for n in governor_result.get("notes", []):
            st.caption(_md_safe(n))


def render_scan_table(container, results: list[dict]) -> pd.DataFrame:
    rows = []
    for r in results:
        if r.get("candidate_score") is None:
            rows.append({
                "Symbol": r["symbol"], "Price": None, "Realized Vol": None, "Mean ATM IV": None,
                "Edge": None, "Score": None, "Avg Spread": None,
                "Note": r.get("error") or r.get("note") or "no data",
            })
        else:
            direction = "rich" if r["edge"] > 0 else "cheap"
            note = "⚠ vol outlier — EXCLUDED" if r.get("vol_outlier_warning") else f"{r['contracts_used']} contracts"
            rows.append({
                "Symbol": r["symbol"],
                "Price": r["underlying_price"],
                "Realized Vol": r["realized_volatility"],
                "Mean ATM IV": r["mean_atm_iv"],
                "Edge": r["edge"],
                "Direction": direction,
                "Score": r["candidate_score"],
                "Avg Spread": r.get("avg_spread_pct"),
                "Note": note,
            })
    df = pd.DataFrame(rows)
    with container.container():
        st.markdown("#### 📋 Watchlist Scan — Ranked by |IV − Realized Vol| Edge")
        st.dataframe(df, hide_index=True, use_container_width=True)
        if any(r.get("vol_outlier_warning") for r in results):
            st.warning("Symbols marked 'vol outlier' are EXCLUDED from candidate selection — their realized "
                       "vol is dominated by a single outlier day (likely an earnings gap), which can "
                       "manufacture a fake edge rather than reflect genuine mispricing.")
    return df


def render_batch_summary(container, debates: list[dict]) -> None:
    if len(debates) <= 1:
        return
    lines = []
    total_real_risk = 0.0
    for d in debates:
        final = d["debate"]["final_decision"]
        gov = d["debate"]["governor_result"]
        if final["action"] == "no_trade":
            lines.append(f"- {d['symbol']}: no trade")
        elif not gov["approved"]:
            lines.append(f"- **{d['symbol']}: {final['action']} — BLOCKED by Risk Governor**")
        else:
            total_real_risk += gov.get("real_max_loss_total") or 0
            # Escaped \$ — this list is joined into ONE st.markdown call below,
            # and multiple bare "$" across separate lines in a single markdown
            # string get paired up as LaTeX math delimiters by Streamlit's
            # renderer, silently corrupting the numbers on screen.
            lines.append(
                f"- {d['symbol']}: {final['action']}, qty={final.get('qty', 0)}, "
                f"real max loss=\\${gov['real_max_loss_total']:,.2f} ({gov['real_max_loss_pct_equity']:.2%} equity)"
            )
    with container.container():
        st.markdown("#### 📊 Batch Summary")
        st.markdown("\n".join(lines))
        st.markdown(f"**Total real max loss across all approved trades this run: \\${total_real_risk:,.2f}**")


# ──────────────────────────────────────────────────────────────────────────
# Core debate logic — mirrors orchestrator._run_debate_with_client exactly,
# just with Streamlit placeholders instead of rich console panels.
# ──────────────────────────────────────────────────────────────────────────

async def run_debate_ui(client: AlpacaMCPClient, symbol: str, execute: bool, account_info: dict, ui: dict,
                         ignore_market_hours: bool = False) -> dict:
    ui["status"].info(f"📡 Pulling market data for {symbol} via Alpaca's MCP server...")
    market_data = await gather_market_data(client, symbol)
    try:
        saved_path = _save_market_data(symbol, market_data)
        ui["status"].caption(f"Raw market data saved to `{saved_path}` — inspect this to verify any number an agent cites.")
    except Exception:
        pass  # debug dump is a nice-to-have, never block the demo on a disk write failing
    agent_data = _build_agent_payload(market_data)
    render_market_data(ui["market"], market_data)

    ui["bull"].info("🐂 Bull agent thinking...")
    bull_case = bull.run(agent_data)
    render_bull(ui["bull"], bull_case)

    ui["bear"].info("🐻 Bear agent thinking...")
    bear_case = bear.run(agent_data)
    render_bear(ui["bear"], bear_case)

    ui["risk"].info("🛡️ Risk Manager reviewing...")
    risk_review = risk_manager.run(bull_case, bear_case, account_info, config.RISK_CONFIG)
    render_risk_review(ui["risk"], risk_review)

    ui["decision"].info("✅ Desk Head deciding...")
    final = decision.run(bull_case, bear_case, risk_review)
    render_decision(ui["decision"], final)

    ui["status"].empty()
    chain_by_symbol = _build_chain_lookup(market_data["option_chain_sample"])
    governor_result = {"approved": False, "violations": [], "notes": []}
    # Mirrors orchestrator._run_debate_with_client's execution_outcome
    # tracking exactly, so the journal entry logged below carries the same
    # specific reason (not just a generic pass/fail) regardless of whether
    # this debate ran from the CLI or this dashboard.
    execution_outcome = "not_requested"
    order_id = None
    order_result = None
    # (severity, message) for the execute-result banner below, stashed
    # alongside execution_outcome so this exact banner can be redrawn later
    # from stored data (continuous-mode polling ticks, or any other rerun)
    # without re-running the debate — st.empty() placeholders go blank on
    # any rerun that doesn't rewrite them, so without this the execute
    # outcome would disappear within a few seconds of a run finishing.
    execute_status = None

    if final.get("legs"):
        q = market_data["quote"]
        spot_price = q.get("mid") or q.get("ask") or q.get("bid")
        render_payoff(ui["payoff"], final, chain_by_symbol, spot_price)
        render_fact_check(ui["factcheck"], final, chain_by_symbol)
        governor_result = risk_governor.check_trade(
            final["legs"], final.get("qty", 0), chain_by_symbol, account_info, config.RISK_CONFIG
        )
        render_governor(ui["governor"], governor_result)

    if execute and final["action"] != "no_trade" and final.get("legs"):
        with ui["execute"].container():
            if not governor_result["approved"]:
                execution_outcome = "blocked_governor"
                execute_status = ("error", "Order NOT submitted — blocked by the Risk Governor above. "
                                            "This is a hard stop, not a suggestion.")
                st.error(execute_status[1])
            else:
                # Market-hours gate — checked right after the Governor and
                # before the held-symbol/daily-limit checks, mirroring
                # orchestrator._run_debate_with_client's order exactly.
                # Explicit False only — is_open=None (clock check failed/
                # unverified shape) means "unknown" and should NOT block,
                # matching get_market_clock's own fail-open philosophy.
                clock = await alpaca_tools.get_market_clock(client)
                market_closed = clock.get("is_open") is False

                if not ignore_market_hours and market_closed:
                    execution_outcome = "blocked_market_hours"
                    execute_status = ("error", "Order NOT submitted — market is currently closed. After-hours "
                             "option quotes are often stale or degraded, which can distort the risk math this "
                             "whole pipeline depends on. Check \"Ignore market hours\" in the sidebar "
                             "(or pass --ignore-market-hours on the CLI) if you really want to override "
                             "this.")
                    st.error(execute_status[1])
                else:
                    # Position/day limits — checked with LIVE data right before
                    # execution, not a cached snapshot, so this is accurate even
                    # partway through a multi-candidate scan run. Mirrors
                    # orchestrator._run_debate_with_client exactly.
                    held_symbols_now = await alpaca_tools.get_held_underlying_symbols(client)
                    max_per_day = config.RISK_CONFIG.get("max_new_positions_per_day")
                    opened_today = position_tracker.count_positions_opened_today()

                    if symbol in held_symbols_now:
                        execution_outcome = "blocked_already_held"
                        execute_status = ("error", f"Order NOT submitted — already holding a position in "
                                 f"{symbol}. No new position will be opened in this symbol until the "
                                 f"existing one is liquidated.")
                        st.error(execute_status[1])
                    elif max_per_day is not None and opened_today >= max_per_day:
                        execution_outcome = "blocked_daily_limit"
                        execute_status = ("error", f"Order NOT submitted — daily new-position limit "
                                 f"({max_per_day}) already reached today ({opened_today} opened so far). "
                                 f"This resets tomorrow.")
                        st.error(execute_status[1])
                    else:
                        limit_price = alpaca_tools.compute_limit_price(final["legs"], chain_by_symbol)
                        if limit_price is None:
                            execution_outcome = "blocked_no_limit_price"
                            execute_status = ("error", "Order NOT submitted — could not compute a real limit "
                                      "price from the fact-checked chain data. Refusing to fall back to a "
                                      "market order.")
                            st.error(execute_status[1])
                        else:
                            cap_str = str(max_per_day) if max_per_day is not None else "∞"
                            st.info(f"Submitting paper LIMIT order at ${limit_price} via Alpaca MCP server "
                                     f"({opened_today + 1}/{cap_str} new positions today)...")
                            result = await alpaca_tools.place_option_spread_order(
                                client, final["legs"], final["qty"], limit_price=limit_price
                            )
                            order_id = result.get("order_id")
                            order_result = result
                            execution_outcome = "executed"
                            execute_status = ("success", f"📮 Order submitted: {result}")
                            position_tracker.record_position_opened(symbol, order_id)
                            st.success(execute_status[1])
                            await _try_render_positions(client, st.empty())
    elif execute and final["action"] == "no_trade":
        execution_outcome = "no_trade_decided"

    # Single shared journal — same module, same field shape, same
    # execution_outcome values as orchestrator._run_debate_with_client, so
    # every debate started from this dashboard shows up in
    # trade_journal.jsonl / view_journal.py exactly like one started from
    # main.py or run_continuous.py. This is the same choke point fix as the
    # backend's, just mirrored here since app.py renders its own copy of the
    # execute flow rather than calling _run_debate_with_client directly.
    trade_journal.log_entry({
        "symbol": symbol,
        "action": final["action"],
        "qty": final.get("qty", 0),
        "execute_requested": execute,
        "execution_outcome": execution_outcome,
        "order_id": order_id,
        "governor_approved": governor_result["approved"],
        "real_max_loss_total": governor_result.get("real_max_loss_total"),
        "real_max_loss_pct_equity": governor_result.get("real_max_loss_pct_equity"),
    })

    return {
        "symbol": symbol,
        "bull_case": bull_case,
        "bear_case": bear_case,
        "risk_review": risk_review,
        "final_decision": final,
        "governor_result": governor_result,
        # Everything below is unused by the live rendering above (which
        # already drew directly into `ui`'s placeholders) — it's captured
        # purely so a later rerun can redraw this exact same debate output
        # from stored data via render_cached_debate(), without re-running
        # anything. See render_cached_debate() for the replay path.
        "market_data": market_data,
        "execution_outcome": execution_outcome,
        "order_id": order_id,
        "execute_status": execute_status,
    }


def make_debate_placeholders(container) -> dict:
    with container.container():
        status = st.empty()
        market = st.empty()
        c1, c2 = st.columns(2)
        bull_ph = c1.empty()
        bear_ph = c2.empty()
        risk_ph = st.empty()
        decision_ph = st.empty()
        payoff_ph = st.empty()
        factcheck_ph = st.empty()
        governor_ph = st.empty()
        execute_ph = st.empty()
    return {
        "status": status, "market": market, "bull": bull_ph, "bear": bear_ph,
        "risk": risk_ph, "decision": decision_ph, "payoff": payoff_ph,
        "factcheck": factcheck_ph, "governor": governor_ph, "execute": execute_ph,
    }


def render_cached_debate(container, result: dict) -> None:
    """Redraws ONE already-completed debate (a single-symbol run, or one
    candidate from a scan) purely from the dict run_debate_ui() returned —
    no network calls, nothing re-run. Reuses the exact same render_*
    functions the live path uses, just fed from stored data instead of a
    live async pipeline, so this can never visually drift from what the
    live run actually showed.

    This exists because st.empty() placeholders go blank on any script
    rerun that doesn't rewrite them — continuous mode's short poll cycle
    causes one every ~3s even when no new run fires, and so does any other
    widget interaction (Refresh Positions, dragging a slider). Without
    this, the last run's output would flicker away within seconds instead
    of staying on screen until the next run replaces it."""
    ui = make_debate_placeholders(container)
    market_data = result.get("market_data")
    if market_data:
        render_market_data(ui["market"], market_data)
    ui["status"].empty()
    render_bull(ui["bull"], result["bull_case"])
    render_bear(ui["bear"], result["bear_case"])
    render_risk_review(ui["risk"], result["risk_review"])
    final = result["final_decision"]
    render_decision(ui["decision"], final)
    if final.get("legs") and market_data:
        chain_by_symbol = _build_chain_lookup(market_data["option_chain_sample"])
        q = market_data["quote"]
        spot_price = q.get("mid") or q.get("ask") or q.get("bid")
        render_payoff(ui["payoff"], final, chain_by_symbol, spot_price)
        render_fact_check(ui["factcheck"], final, chain_by_symbol)
        render_governor(ui["governor"], result["governor_result"])
    execute_status = result.get("execute_status")
    if execute_status:
        severity, message = execute_status
        with ui["execute"].container():
            (st.error if severity == "error" else st.success)(message)


def render_cached_result(run_area_ph, result: dict, risk_caption: str | None = None) -> None:
    """Top-level replay entry point — redraws whatever `_run_once()` last
    produced (single-symbol OR scan, detected from the result's own shape)
    into `run_area_ph`. See render_cached_debate() for why this exists."""
    with run_area_ph.container():
        if risk_caption:
            st.caption(risk_caption)
        if "debates" in result:
            scan_table_ph = st.empty()
            render_scan_table(scan_table_ph, result.get("scan_results", []))
            debates_container = st.container()
            debates = result.get("debates") or []
            if not debates:
                with debates_container.container():
                    st.error("No ELIGIBLE candidates (enough liquid data, no vol-outlier flag, not "
                             "already held). No debate to run.")
            else:
                for i, d in enumerate(debates, 1):
                    debates_container.markdown(
                        f"### Candidate {i}/{len(debates)}: {d['symbol']} "
                        f"(score {d['candidate']['candidate_score']:.1f}, edge {d['candidate']['edge']:+.1%})"
                    )
                    render_cached_debate(debates_container, d["debate"])
                summary_ph = st.empty()
                render_batch_summary(summary_ph, debates)
        else:
            render_cached_debate(run_area_ph, result)


def _render_cached_error(err: dict) -> None:
    """Replays the last run's error message (see `_run_once`'s except
    branches) so a failed run's error banner also stays on screen across
    reruns instead of vanishing on the very next poll tick, same reasoning
    as render_cached_debate()."""
    st.error(err["message"])
    if err.get("trace"):
        st.code(err["trace"])


async def _try_render_positions(client: AlpacaMCPClient, positions_ph) -> list[dict] | None:
    """Best-effort positions refresh for inside a debate run — field parsing
    is unverified (see get_open_positions' docstring), so a hiccup here
    shows as a caption rather than failing the whole debate. Returns the
    fetched positions (or None on failure) so callers can cache them for
    replay on later reruns — see render_cached_result()."""
    try:
        positions = await alpaca_tools.get_open_positions(client)
        render_positions(positions_ph, positions)
        return positions
    except Exception as exc:
        with positions_ph.container():
            st.caption(f"⚠ Could not fetch positions: {exc}")
        return None


async def run_single_symbol(symbol: str, execute: bool, account_ph, positions_ph, debate_container,
                             ignore_market_hours: bool = False) -> dict:
    mcp_log: list = []
    async with AlpacaMCPClient() as client:
        instrument_mcp_client(client, mcp_log)
        tools = await client.list_tools()
        st.toast(f"Connected — {len(tools)} MCP tools available.")
        account_info = await alpaca_tools.get_account_info(client)
        render_account(account_ph, account_info)
        positions = await _try_render_positions(client, positions_ph)
        ui = make_debate_placeholders(debate_container)
        result = await run_debate_ui(client, symbol, execute, account_info, ui,
                                      ignore_market_hours=ignore_market_hours)
        result["mcp_call_log"] = mcp_log
        result["account_info"] = account_info
        result["positions"] = positions
        return result


async def run_scan(symbols: list[str], execute: bool, top_n: int, skip_held_positions: bool,
                    account_ph, positions_ph, scan_table_ph, debates_container, summary_ph,
                    ignore_market_hours: bool = False) -> dict:
    mcp_log: list = []
    async with AlpacaMCPClient() as client:
        instrument_mcp_client(client, mcp_log)
        tools = await client.list_tools()
        st.toast(f"Connected — {len(tools)} MCP tools available.")
        account_info = await alpaca_tools.get_account_info(client)
        render_account(account_ph, account_info)
        positions = await _try_render_positions(client, positions_ph)

        with scan_table_ph.container():
            st.info(f"Scanning {len(symbols)} symbols...")
        results = await scanner.scan_watchlist(
            client, symbols,
            min_dte=config.RISK_CONFIG["min_days_to_expiry"],
            max_dte=config.RISK_CONFIG["max_days_to_expiry"],
        )
        render_scan_table(scan_table_ph, results)

        held_symbols = set()
        if skip_held_positions:
            held_symbols = await alpaca_tools.get_held_underlying_symbols(client)
            with debates_container.container():
                st.caption(f"Currently held underlying symbols (skipped as new candidates): "
                           f"{sorted(held_symbols) if held_symbols else 'none detected'}")

        candidates = scanner.get_eligible_candidates(results, top_n, exclude_symbols=held_symbols)
        if not candidates:
            with debates_container.container():
                st.error("No ELIGIBLE candidates (enough liquid data, no vol-outlier flag, not "
                         "already held). No debate to run.")
            return {"scan_results": results, "debates": [], "mcp_call_log": mcp_log,
                     "account_info": account_info, "positions": positions}

        debates = []
        for i, candidate in enumerate(candidates, 1):
            debates_container.markdown(
                f"### Candidate {i}/{len(candidates)}: {candidate['symbol']} "
                f"(score {candidate['candidate_score']:.1f}, edge {candidate['edge']:+.1%})"
            )
            ui = make_debate_placeholders(debates_container)
            debate_result = await run_debate_ui(client, candidate["symbol"], execute, account_info, ui,
                                                 ignore_market_hours=ignore_market_hours)
            debates.append({"symbol": candidate["symbol"], "candidate": candidate, "debate": debate_result})

        render_batch_summary(summary_ph, debates)
        return {"scan_results": results, "debates": debates, "mcp_call_log": mcp_log,
                "account_info": account_info, "positions": positions}


# ──────────────────────────────────────────────────────────────────────────
# Sidebar — configuration
# ──────────────────────────────────────────────────────────────────────────

st.sidebar.title("⚙️ Desk Configuration")

paper_ok = config.ALPACA_PAPER
if paper_ok:
    st.sidebar.success("🧪 PAPER trading — safe to run")
else:
    st.sidebar.error("🔴 LIVE trading is enabled (ALPACA_PAPER=false) — real money at risk!")

st.sidebar.caption(f"LLM provider: **{config.LLM_PROVIDER}** · Model: `{config.MODEL}`")

mode = st.sidebar.radio("Mode", ["Single Symbol", "Scan Watchlist"])

if mode == "Single Symbol":
    symbol = st.sidebar.text_input("Underlying symbol", value="AAPL").strip().upper()
    symbols_to_scan = None
    top_n = None
    skip_held = False
else:
    symbol = None
    default_watchlist = ", ".join(config.WATCHLIST)
    symbols_text = st.sidebar.text_area("Watchlist (comma-separated)", value=default_watchlist, height=100)
    symbols_to_scan = [s.strip().upper() for s in symbols_text.split(",") if s.strip()]
    top_n = st.sidebar.number_input("Run full debate on top-N eligible candidates", min_value=1, max_value=10, value=3)
    skip_held = st.sidebar.checkbox(
        "Skip symbols I already hold a position in", value=True,
        help="Same as run_continuous.py's default — diversifies across runs instead of "
             "repeatedly stacking into whatever ranks #1.",
    )

execute = st.sidebar.checkbox("Submit paper order if approved (--execute)", value=False)
if execute:
    st.sidebar.warning("Will submit a real PAPER order via Alpaca's MCP server if the Risk Governor approves.")

ignore_market_hours = st.sidebar.checkbox(
    "Ignore market hours (not recommended)", value=False,
    help="Mirrors main.py's --ignore-market-hours flag. When off (default), execution is blocked "
         "while the market is closed, because after-hours option quotes are often stale/degraded and "
         "can distort the Black-Scholes risk math this whole pipeline depends on.",
)

st.sidebar.markdown("#### 🔁 Continuous Run")
run_continuously = st.sidebar.checkbox(
    "Run continuously", value=False, key="run_continuously_cb",
    help="Repeats this exact configuration (mode, symbol/watchlist, execute, risk budget) on a "
         "timer, inside this dashboard — same idea as run_continuous.py, but click-driven and "
         "visible here instead of a background process. Requires this browser tab to stay open.",
)
if run_continuously:
    interval_minutes = st.sidebar.number_input(
        "Interval (minutes)", min_value=1, max_value=1440, value=60, key="interval_minutes_input",
        help="How long to wait between runs. The first run fires immediately on Start.",
    )
else:
    interval_minutes = None

st.sidebar.markdown("#### 🎚️ Risk Budget")
if st.sidebar.button("↺ Reset to config.py defaults", use_container_width=True):
    st.session_state["risk_pct_slider"] = _DEFAULT_RISK_CONFIG["max_risk_pct_of_equity"] * 100
    st.session_state["max_contracts_slider"] = _DEFAULT_RISK_CONFIG["max_contracts_per_leg"]
    st.session_state["defined_risk_only_cb"] = _DEFAULT_RISK_CONFIG["defined_risk_only"]
    st.session_state["dte_range_slider"] = (
        _DEFAULT_RISK_CONFIG["min_days_to_expiry"], _DEFAULT_RISK_CONFIG["max_days_to_expiry"]
    )
    st.session_state["max_new_positions_slider"] = _DEFAULT_RISK_CONFIG.get("max_new_positions_per_day", 4)
    st.rerun()

ui_max_risk_pct = st.sidebar.slider(
    "Max risk % of equity per trade", min_value=0.5, max_value=10.0,
    value=_DEFAULT_RISK_CONFIG["max_risk_pct_of_equity"] * 100, step=0.5,
    format="%.1f%%", key="risk_pct_slider",
) / 100
ui_max_contracts = st.sidebar.slider(
    "Max contracts per leg", min_value=1, max_value=50,
    value=_DEFAULT_RISK_CONFIG["max_contracts_per_leg"], key="max_contracts_slider",
)
ui_defined_risk_only = st.sidebar.checkbox(
    "Defined-risk only (spreads, no naked options)",
    value=_DEFAULT_RISK_CONFIG["defined_risk_only"], key="defined_risk_only_cb",
)
ui_dte_range = st.sidebar.slider(
    "Days-to-expiry window", min_value=0, max_value=120,
    value=(_DEFAULT_RISK_CONFIG["min_days_to_expiry"], _DEFAULT_RISK_CONFIG["max_days_to_expiry"]),
    key="dte_range_slider",
)
ui_max_new_positions = st.sidebar.slider(
    "Max new positions per day (all entry points)", min_value=1, max_value=20,
    value=_DEFAULT_RISK_CONFIG.get("max_new_positions_per_day", 4), key="max_new_positions_slider",
    help="Enforced via position_tracker.py's persisted daily count — holds across separate "
         "process runs (CLI, this dashboard, run_continuous.py), not just this session.",
)

ui_risk_config = {
    "max_risk_pct_of_equity": ui_max_risk_pct,
    "max_contracts_per_leg": ui_max_contracts,
    "defined_risk_only": ui_defined_risk_only,
    "min_days_to_expiry": ui_dte_range[0],
    "max_days_to_expiry": ui_dte_range[1],
    "max_new_positions_per_day": ui_max_new_positions,
}
_default_comparable = {k: _DEFAULT_RISK_CONFIG.get(k) for k in ui_risk_config}
if ui_risk_config != _default_comparable:
    st.sidebar.caption("⚠ Modified from config.py defaults — applies to the next Run.")

positions_today = position_tracker.count_positions_opened_today()
st.sidebar.caption(
    f"📒 Positions opened today: **{positions_today}/{ui_max_new_positions}** "
    f"(persisted in `positions_opened_log.json`)"
)

def _start_continuous() -> None:
    st.session_state["continuous_active"] = True
    st.session_state["next_run_at"] = datetime.now()  # fire immediately on Start
    st.session_state["continuous_run_count"] = 0
    st.session_state["continuous_history"] = []


def _stop_continuous() -> None:
    st.session_state["continuous_active"] = False
    st.session_state["next_run_at"] = None


if run_continuously:
    run_clicked = False  # continuous mode uses Start/Stop below instead of a one-shot Run
    if st.session_state["continuous_active"]:
        st.sidebar.button("⏹ Stop Continuous Run", type="primary", use_container_width=True,
                           on_click=_stop_continuous)
    else:
        st.sidebar.button(f"▶ Start Continuous Run (every {interval_minutes} min)", type="primary",
                           use_container_width=True, on_click=_start_continuous)
else:
    run_clicked = st.sidebar.button("▶ Run", type="primary", use_container_width=True)
    if st.session_state["continuous_active"]:
        # Continuous mode was on and the checkbox above just got unchecked —
        # stop it rather than leaving an orphaned timer with no way to see it.
        _stop_continuous()

st.sidebar.divider()
st.sidebar.caption(
    "Every agent's output is structured JSON, so the whole debate is logged "
    "and auditable. Trades only ever go to Alpaca's paper environment unless "
    "ALPACA_PAPER_TRADE is deliberately flipped in the MCP server's env."
)


# ──────────────────────────────────────────────────────────────────────────
# Main page
# ──────────────────────────────────────────────────────────────────────────

st.title("📈 Options Alpha Debate Desk")
st.caption(
    "Four agents — 🐂 Bull, 🐻 Bear, 🛡️ Risk Manager, ✅ Desk Head — argue out every "
    "options trade using real market data pulled through Alpaca's official MCP server, "
    "with a deterministic Risk Governor as the final, non-LLM gate before anything executes."
)

refresh_positions_clicked = st.button("🔄 Refresh Positions", help="Check current live positions/P&L "
                                       "any time — doesn't run a debate.")

account_ph = st.empty()
positions_ph = st.empty()
# Continuous run history lives here now (moved down from the bottom of the
# page, right under Open Positions) — see the render call further down.
history_ph = st.empty()
# Single placeholder wrapping EVERYTHING a run renders (risk-budget caption,
# the debate itself). Continuous mode reruns this whole script every few
# seconds even between actual runs (see the polling loop below) — without
# this, the debate content's own st.container()/st.caption() calls would
# only exist on script runs where a debate actually fires, so their
# position in the element tree would shift relative to reruns where it
# doesn't. Streamlit matches elements across reruns by position, so that
# shift left a stale, grayed-out duplicate of everything after it (the
# continuous-mode status banner) behind on screen instead of cleanly
# replacing it. Keeping this placeholder's OWN existence unconditional,
# with only its CONTENTS conditional, keeps every element after it at a
# stable position regardless of whether this tick actually ran a debate.
#
# account_ph/positions_ph/run_area_ph all go BLANK on any rerun that
# doesn't write into them again (confirmed empirically — an st.empty()
# slot clears if this script run doesn't rewrite it, even if a previous
# run left content there). Continuous mode's short poll cycle causes a
# rerun every ~3s even between actual runs, and so does any ordinary
# widget interaction (Refresh Positions, dragging a slider) — without the
# repaint-from-cache step further down, the account snapshot, positions
# table, and last debate/execute outcome would all flicker away within
# seconds of a run finishing instead of staying up until the next run
# replaces them.
run_area_ph = st.empty()

if refresh_positions_clicked:
    try:
        st.session_state["last_positions"] = asyncio.run(fetch_and_render_positions(positions_ph))
    except FileNotFoundError as exc:
        st.error(
            "Could not launch Alpaca's MCP server (`uvx alpaca-mcp-server`). Make sure "
            "`uv`/`uvx` is installed and on PATH: https://docs.astral.sh/uv/getting-started/installation/"
        )
        st.exception(exc)
    except Exception as exc:
        st.error(f"Could not fetch positions: {exc}")
        st.code(traceback.format_exc())

if (not run_clicked and not st.session_state["continuous_active"]
        and st.session_state.get("last_result") is None and st.session_state.get("last_error") is None):
    st.info("Configure a symbol or watchlist in the sidebar, then click **Run** (or enable "
            "**Run continuously** and click **Start Continuous Run**).")


def _risk_budget_caption(rc: dict) -> str:
    """Single source of truth for the risk-budget summary line shown above
    every run's output — used both live (_run_once) and on replay
    (render_cached_result, via the cached string) so the two can never say
    different things about the same stored run."""
    return (
        f"🎚️ Risk budget for this run: max **{rc['max_risk_pct_of_equity']:.1%}** equity/trade · "
        f"≤**{rc['max_contracts_per_leg']}** contracts/leg · "
        f"{'defined-risk only' if rc['defined_risk_only'] else '⚠ naked options allowed'} · "
        f"DTE **{rc['min_days_to_expiry']}–{rc['max_days_to_expiry']}** days"
    )


def _run_once() -> None:
    """
    Runs the currently-configured mode/symbol(s)/execute/risk-budget exactly
    once, into the placeholders already created above. Shared by the
    one-shot ▶ Run button and the continuous-run scheduler below so both
    paths are guaranteed to behave identically — no separate copy to drift.
    """
    st.session_state["last_result"] = None
    st.session_state["last_error"] = None
    with run_area_ph.container():
        # config.RISK_CONFIG is a plain module-level dict — every consumer (the
        # Risk Manager agent's prompt, risk_governor.check_trade, and
        # orchestrator.gather_market_data's DTE window) reads it by key at call
        # time via `config.RISK_CONFIG[...]`, not a value captured at import
        # time. Mutating it in place is enough to make the sidebar sliders
        # actually govern this run — no other file needs to change.
        config.RISK_CONFIG.update(ui_risk_config)
        risk_caption = _risk_budget_caption(config.RISK_CONFIG)
        st.session_state["last_risk_caption"] = risk_caption
        st.caption(risk_caption)
        try:
            if mode == "Single Symbol":
                if not symbol:
                    st.error("Enter a symbol first.")
                else:
                    debate_container = st.container()
                    result = asyncio.run(
                        run_single_symbol(symbol, execute, account_ph, positions_ph, debate_container,
                                           ignore_market_hours=ignore_market_hours)
                    )
                    st.session_state["last_result"] = result
            else:
                if not symbols_to_scan:
                    st.error("Enter at least one symbol in the watchlist.")
                else:
                    scan_table_ph = st.empty()
                    debates_container = st.container()
                    summary_ph = st.empty()
                    result = asyncio.run(
                        run_scan(symbols_to_scan, execute, top_n, skip_held, account_ph, positions_ph,
                                 scan_table_ph, debates_container, summary_ph,
                                 ignore_market_hours=ignore_market_hours)
                    )
                    st.session_state["last_result"] = result
        except FileNotFoundError as exc:
            msg = ("Could not launch Alpaca's MCP server (`uvx alpaca-mcp-server`). Make sure "
                   "`uv`/`uvx` is installed and on PATH: https://docs.astral.sh/uv/getting-started/installation/")
            st.error(msg)
            st.exception(exc)
            st.session_state["last_error"] = {"message": msg, "trace": traceback.format_exc()}
        except json.JSONDecodeError as exc:
            msg = ("An agent's LLM response wasn't valid JSON — this can happen occasionally, especially "
                   "on Groq's free-tier models. Try running again.")
            st.error(msg)
            st.exception(exc)
            st.session_state["last_error"] = {"message": msg, "trace": traceback.format_exc()}
        except Exception as exc:
            msg = f"Debate failed: {exc}"
            st.error(msg)
            st.code(traceback.format_exc())
            st.session_state["last_error"] = {"message": msg, "trace": traceback.format_exc()}

    # Cache what this run produced so it can be redrawn on later reruns
    # that don't fire a new run (see the placeholder comments above) —
    # only account_ph/positions_ph got a fresh live paint above; run_area_ph's
    # content is already covered by render_cached_result() reading
    # last_result directly.
    _result = st.session_state.get("last_result")
    if _result is not None:
        if _result.get("account_info") is not None:
            st.session_state["last_account_info"] = _result["account_info"]
        if _result.get("positions") is not None:
            st.session_state["last_positions"] = _result["positions"]


def _summarize_result_for_history(current_mode: str, result: dict | None) -> str:
    """One-line outcome summary for the continuous-run history table."""
    if not result:
        return "no result"
    if current_mode == "Single Symbol":
        final = result.get("final_decision") or {}
        gov = result.get("governor_result") or {}
        action = final.get("action", "?")
        sym = result.get("symbol", "?")
        if action == "no_trade":
            return f"{sym}: no trade"
        return f"{sym}: {action} ({'approved' if gov.get('approved') else 'BLOCKED'})"
    debates = result.get("debates") or []
    if not debates:
        return "scan: no eligible candidates"
    parts = []
    for d in debates:
        final = d["debate"]["final_decision"]
        gov = d["debate"]["governor_result"]
        action = final.get("action", "?")
        tag = "" if action == "no_trade" else f"({'ok' if gov.get('approved') else 'blocked'})"
        parts.append(f"{d['symbol']}:{action}{tag}")
    return "scan: " + ", ".join(parts)


ran_this_rerun = False

if run_clicked:
    _run_once()
    ran_this_rerun = True

if st.session_state["continuous_active"]:
    now = datetime.now()
    due = st.session_state["next_run_at"] is None or now >= st.session_state["next_run_at"]
    if due:
        _run_once()
        ran_this_rerun = True
        st.session_state["continuous_run_count"] += 1
        st.session_state["continuous_history"].insert(0, {
            "time": now.strftime("%H:%M:%S"),
            "run #": st.session_state["continuous_run_count"],
            "summary": _summarize_result_for_history(mode, st.session_state.get("last_result")),
        })
        st.session_state["continuous_history"] = st.session_state["continuous_history"][:50]
        st.session_state["next_run_at"] = datetime.now() + timedelta(minutes=interval_minutes)

    remaining = max(0.0, (st.session_state["next_run_at"] - datetime.now()).total_seconds())
    st.info(
        f"🔁 Continuous mode ON — {st.session_state['continuous_run_count']} run(s) so far · "
        f"next run in ~{int(remaining)}s (every {interval_minutes} min) · click **⏹ Stop Continuous "
        f"Run** in the sidebar to end. Keep this browser tab open — continuous mode runs through "
        f"this session and stops if the tab closes."
    )

# account_ph/positions_ph/run_area_ph only got a fresh paint above when
# `_run_once()` actually ran THIS tick. On every other tick — an idle
# continuous-mode poll, or any unrelated widget interaction (Refresh
# Positions, dragging a slider) — repaint them here from the last-known
# snapshot in session_state instead, so the account balance, positions
# table, and last debate/execute outcome (or last error) stay visible
# until the NEXT run replaces them, rather than blanking out within
# seconds. See the placeholder-creation comment near the top of this
# section for why they'd otherwise go empty.
if not ran_this_rerun:
    if st.session_state.get("last_account_info") is not None:
        render_account(account_ph, st.session_state["last_account_info"])
    if st.session_state.get("last_positions") is not None:
        render_positions(positions_ph, st.session_state["last_positions"])
    if st.session_state.get("last_result") is not None:
        render_cached_result(run_area_ph, st.session_state["last_result"],
                              st.session_state.get("last_risk_caption"))
    elif st.session_state.get("last_error"):
        with run_area_ph.container():
            _render_cached_error(st.session_state["last_error"])

# Continuous run history — moved here, right below Open Positions (was
# previously all the way at the bottom of the page), and always rendered
# (not just while continuous mode is on) so it keeps counting runs and
# stays visible even after Stop. Written into history_ph (created earlier,
# right after positions_ph) rather than emitted directly at this script
# position, so its on-screen location matches where it was created
# regardless of what else did or didn't render above it this tick.
if st.session_state["continuous_history"]:
    with history_ph.container():
        with st.expander(f"Continuous run history ({len(st.session_state['continuous_history'])})",
                          expanded=not ran_this_rerun):
            st.dataframe(pd.DataFrame(st.session_state["continuous_history"]), hide_index=True,
                         use_container_width=True)

if st.session_state["continuous_active"]:
    # Poll every 15s rather than every 3s — the original 3s interval made
    # the Stop button feel responsive, but its real cost was a FULL PAGE
    # rerun every 3 seconds while continuous mode ran, which re-rendered
    # everything on screen (including whatever results you were trying to
    # read) constantly. Found via a screen recording showing exactly this.
    # 15s worst-case Stop latency is a fine tradeoff for a bot that only
    # actually acts once an hour — lower this back toward 3.0 if you'd
    # rather prioritize Stop responsiveness over a calmer screen.
    remaining = max(0.0, (st.session_state["next_run_at"] - datetime.now()).total_seconds())
    time.sleep(min(15.0, remaining) if remaining > 0 else 0.5)
    st.rerun()

if st.session_state.get("last_result"):
    st.download_button(
        "⬇ Download full debate JSON",
        data=json.dumps(st.session_state["last_result"], indent=2, default=str),
        file_name=f"debate_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
        mime="application/json",
    )
