"""
Runs the full Bull -> Bear -> Risk Manager -> Decision pipeline for one
underlying symbol, pulling all market data through Alpaca's official MCP
server, and prints a readable transcript.
"""
import json
from datetime import datetime

from rich.console import Console
from rich.panel import Panel
from rich.markdown import Markdown

import alpaca_tools
import config
import risk_governor
import position_tracker
import trade_journal
from mcp_client import AlpacaMCPClient
from agents import bull, bear, risk_manager, decision

console = Console()


async def gather_market_data(client: AlpacaMCPClient, symbol: str) -> dict:
    console.print(f"[dim]Pulling market data for {symbol} via Alpaca's MCP server...[/dim]")
    quote = await alpaca_tools.get_stock_quote(client, symbol)
    bars = await alpaca_tools.get_historical_bars(client, symbol, lookback_days=30)
    realized_vol = alpaca_tools.compute_realized_vol(bars)
    outlier_check = alpaca_tools.detect_vol_outlier_days(bars)
    if outlier_check["dominated_by_outlier"]:
        console.print(f"[bold yellow]⚠ {outlier_check['note']}[/bold yellow]")
    underlying_price = quote["mid"] or quote["ask"] or quote["bid"]
    chain = await alpaca_tools.get_option_chain(
        client,
        symbol,
        underlying_price=underlying_price,
        min_dte=config.RISK_CONFIG["min_days_to_expiry"],
        max_dte=config.RISK_CONFIG["max_days_to_expiry"],
    )
    return {
        "symbol": symbol,
        "quote": quote,
        "realized_volatility_annualized": realized_vol,
        "realized_vol_outlier_check": outlier_check,
        "daily_bars_used_for_realized_vol": bars,  # so agents (and you) can see WHY realized vol is what it is
        "option_chain_sample": chain[:40],  # cap payload size sent to the LLM
    }


def _save_market_data(symbol: str, market_data: dict) -> str:
    """Dumps the exact data the agents saw to a timestamped JSON file, so
    you can inspect it directly and cross-check any number an agent cites."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = f"debug_market_data_{symbol}_{ts}.json"
    with open(path, "w") as f:
        json.dump(market_data, f, indent=2, default=str)
    return path


def _build_chain_lookup(option_chain_sample: list[dict]) -> dict:
    return {c["symbol"]: c for c in option_chain_sample}


def _fact_check_legs(final_decision: dict, chain_by_symbol: dict) -> list[str]:
    """
    Looks up every leg symbol the Decision agent picked against the actual
    option chain data it was given. Flags symbols that don't exist in the
    source data (a real hallucination) and, for ones that do, shows the
    actual strike/mid price alongside so you can compare against what the
    agent claimed in its thesis text.

    NOTE: this only verifies the symbol is real. It does NOT verify that
    the agent's narrative (stated width, credit, strike) matches what that
    symbol actually is — see risk_governor.py for that check, which is
    what actually gates execution.
    """
    lines = []
    for leg in final_decision.get("legs", []):
        symbol = leg.get("symbol", "")
        contract = chain_by_symbol.get(symbol)
        if contract is None:
            lines.append(f"[bold red]⚠ {symbol}[/bold red] — NOT FOUND in the option chain data given "
                          f"to the agents. This symbol may be hallucinated.")
        else:
            lines.append(
                f"[green]✓ {symbol}[/green] — real contract: strike=${contract['strike']}, "
                f"type={contract['option_type']}, mid=${contract['mid']}, "
                f"IV={contract['implied_volatility']}, dte={contract['days_to_expiry']}"
            )
    return lines


def _build_agent_payload(market_data: dict, max_chain_contracts: int = 15) -> dict:
    """
    Trims market_data down before it goes into an LLM prompt. The full
    version (saved to disk via _save_market_data) keeps everything for
    your own inspection; this version drops the raw daily bars (agents get
    the derived realized_vol number and outlier warning instead, which is
    what they actually need) and caps the option chain tighter.

    This exists because Groq's free tier caps total request size (prompt +
    max_tokens) at 8,000 tokens — the full payload with 30 raw bars and 40
    option contracts blew past that. Anthropic has far higher limits, but
    trimming costs nothing there either.
    """
    trimmed = {k: v for k, v in market_data.items() if k != "daily_bars_used_for_realized_vol"}
    trimmed["option_chain_sample"] = trimmed["option_chain_sample"][:max_chain_contracts]
    return trimmed


async def _run_debate_with_client(client: AlpacaMCPClient, symbol: str, execute: bool = False,
                                    ignore_market_hours: bool = False) -> dict:
    """Core debate logic given an ALREADY-OPEN MCP client — reused by both
    run_debate() (single symbol) and run_scan() (reuses the same connection
    for the top-ranked candidate instead of reconnecting)."""
    # Checked ALWAYS, not just when execute=True — market-hours protection
    # started as an execution-only gate, but the real underlying problem
    # (after-hours option quotes are often stale/degraded, distorting the
    # Black-Scholes IV math) affects pure RESEARCH runs just as much as
    # real orders. A --scan with no --execute run before the market opens
    # could show a "great opportunity" that's actually just bad data, with
    # nothing telling you that — this closes that gap. Doesn't block
    # research (there's no capital risk in looking), just labels it clearly.
    clock = await alpaca_tools.get_market_clock(client)
    market_was_closed = clock.get("is_open") is False
    if market_was_closed:
        console.print(f"[bold yellow]⚠ Market is currently closed — this debate is running on "
                      f"quotes that may be stale or from a degraded feed. Treat any IV/edge "
                      f"numbers below with extra caution.[/bold yellow]")

    account_info = await alpaca_tools.get_account_info(client)
    market_data = await gather_market_data(client, symbol)
    saved_path = _save_market_data(symbol, market_data)
    console.print(f"[dim]Raw market data saved to {saved_path} — inspect this to verify any "
                  f"number an agent cites.[/dim]")
    agent_data = _build_agent_payload(market_data)

    console.print(Panel(f"[bold]Options Alpha Debate Desk[/bold] — {symbol}", style="cyan"))

    console.print("[bold green]BULL agent thinking...[/bold green]")
    bull_case = bull.run(agent_data)
    console.print(Panel(Markdown(f"**Thesis:** {bull_case['thesis']}\n\n"
                                  f"**Strategy:** {bull_case['strategy']}\n\n"
                                  f"**Confidence:** {bull_case['confidence']} — {bull_case['confidence_reason']}"),
                         title="🐂 Bull Case", style="green"))

    console.print("[bold red]BEAR agent thinking...[/bold red]")
    bear_case = bear.run(agent_data)
    console.print(Panel(Markdown(f"**Thesis:** {bear_case['thesis']}\n\n"
                                  f"**Strategy:** {bear_case['strategy']}\n\n"
                                  f"**Confidence:** {bear_case['confidence']} — {bear_case['confidence_reason']}"),
                         title="🐻 Bear Case", style="red"))

    console.print("[bold yellow]RISK MANAGER reviewing...[/bold yellow]")
    risk_review = risk_manager.run(bull_case, bear_case, account_info, config.RISK_CONFIG)
    console.print(Panel(Markdown(f"**Bull:** vetoed={risk_review['bull_review']['vetoed']}, "
                                  f"qty={risk_review['bull_review']['recommended_qty']} — "
                                  f"{risk_review['bull_review']['notes']}\n\n"
                                  f"**Bear:** vetoed={risk_review['bear_review']['vetoed']}, "
                                  f"qty={risk_review['bear_review']['recommended_qty']} — "
                                  f"{risk_review['bear_review']['notes']}"),
                         title="🛡️ Risk Manager", style="yellow"))

    console.print("[bold cyan]DESK HEAD deciding...[/bold cyan]")
    final = decision.run(bull_case, bear_case, risk_review)
    console.print(Panel(Markdown(f"**Action:** {final['action']}\n\n"
                                  f"**Qty:** {final.get('qty', 0)}\n\n"
                                  f"**Rationale:** {final['rationale']}"),
                         title="✅ Final Decision", style="bold cyan"))

    governor_result = {"approved": False, "violations": [], "notes": []}
    chain_by_symbol = _build_chain_lookup(market_data["option_chain_sample"])

    if final.get("legs"):
        fact_check_lines = _fact_check_legs(final, chain_by_symbol)
        if fact_check_lines:
            console.print(Panel("\n".join(fact_check_lines), title="🔍 Fact Check vs. Real Chain Data",
                                 style="blue"))

        governor_result = risk_governor.check_trade(
            final["legs"], final.get("qty", 0), chain_by_symbol, account_info, config.RISK_CONFIG
        )
        gov_lines = []
        if governor_result["approved"]:
            gov_lines.append(f"[bold green]✓ APPROVED[/bold green] — real max loss "
                              f"${governor_result['real_max_loss_total']:,.2f} "
                              f"({governor_result['real_max_loss_pct_equity']:.2%} of equity), "
                              f"computed from actual chain data.")
        else:
            gov_lines.append("[bold red]✗ EXECUTION BLOCKED[/bold red] — this trade does NOT pass "
                              "deterministic verification, regardless of agent approval:")
            for v in governor_result["violations"]:
                gov_lines.append(f"  [bold red]•[/bold red] {v}")
        for n in governor_result.get("notes", []):
            gov_lines.append(f"  [dim]{n}[/dim]")
        console.print(Panel("\n".join(gov_lines), title="🏛️ Deterministic Risk Governor (non-LLM)",
                             style="green" if governor_result["approved"] else "bold red"))

    execution_outcome = "not_requested"
    order_id = None

    if execute and final["action"] != "no_trade" and final.get("legs"):
        if not governor_result["approved"]:
            execution_outcome = "blocked_governor"
            console.print("[bold red]Order NOT submitted — blocked by the Risk Governor above. "
                          "This is a hard stop, not a suggestion.[/bold red]")
        else:
            # Reuse the market status already fetched at the top of this
            # function (market_was_closed) instead of calling get_market_clock
            # a second time — same clock, no reason to ask twice.
            if not ignore_market_hours and market_was_closed:
                # Checked directly here (not just relied on by
                # run_continuous.py's pre-scan skip) so this protects EVERY
                # entry point — including main.py's single-symbol and
                # --scan --execute, which had no market-hours protection at
                # all before this. After-hours option quotes are often
                # stale/degraded (indicative feed, not live NBBO), which can
                # produce wildly distorted implied vol and therefore a
                # distorted risk picture — confirmed happening live during
                # testing (an entire watchlist showing 70-120% IV on calm
                # mega-caps at midnight). Defaults to blocking; pass
                # ignore_market_hours=True to override.
                execution_outcome = "blocked_market_hours"
                console.print("[bold red]Order NOT submitted — market is currently closed. After-hours "
                              "option quotes are often stale or degraded, which can distort the risk "
                              "math this whole pipeline depends on. Pass ignore_market_hours=True (or "
                              "--ignore-market-hours on the CLI) if you really want to override "
                              "this.[/bold red]")
            else:
                # Position/day limits — checked with LIVE data right before
                # execution, not a cached snapshot, so this is accurate even
                # partway through a multi-candidate batch run. Checks BOTH
                # filled positions AND still-open (unfilled/pending) orders
                # — a real gap found in testing: two separate COST orders
                # went out an hour apart because the first was still an
                # unfilled resting limit order (not yet expired) when the
                # second debate ran. get_held_underlying_symbols alone only
                # sees filled positions, so it missed this entirely.
                held_symbols_now = await alpaca_tools.get_held_underlying_symbols(client)
                pending_symbols_now = await alpaca_tools.get_symbols_with_open_orders(client)
                blocked_symbols_now = held_symbols_now | pending_symbols_now
                max_per_day = config.RISK_CONFIG.get("max_new_positions_per_day")
                opened_today = position_tracker.count_positions_opened_today()

                if symbol in blocked_symbols_now:
                    execution_outcome = "blocked_already_held"
                    reason = "already holding a position in" if symbol in held_symbols_now else \
                             "already have a pending (unfilled) order on"
                    console.print(f"[bold red]Order NOT submitted — {reason} "
                                  f"{symbol}. No new position will be opened in this symbol until the "
                                  f"existing one is liquidated or the pending order fills/expires.[/bold red]")
                elif max_per_day is not None and opened_today >= max_per_day:
                    execution_outcome = "blocked_daily_limit"
                    console.print(f"[bold red]Order NOT submitted — daily new-position limit "
                                  f"({max_per_day}) already reached today ({opened_today} opened "
                                  f"so far). This resets tomorrow.[/bold red]")
                else:
                    limit_price = alpaca_tools.compute_limit_price(final["legs"], chain_by_symbol)
                    if limit_price is None:
                        execution_outcome = "blocked_no_limit_price"
                        console.print("[bold red]Order NOT submitted — could not compute a real limit "
                                      "price from the fact-checked chain data. Refusing to fall back to "
                                      "a market order.[/bold red]")
                    else:
                        console.print(f"[bold magenta]Submitting paper LIMIT order at ${limit_price} "
                                      f"via Alpaca MCP server ({opened_today + 1}/{max_per_day} new "
                                      f"positions today)...[/bold magenta]")
                        result = await alpaca_tools.place_option_spread_order(
                            client, final["legs"], final["qty"], limit_price=limit_price
                        )
                        console.print(Panel(str(result), title="📮 Order Result", style="magenta"))
                        order_id = result.get("order_id")
                        execution_outcome = "executed"
                        position_tracker.record_position_opened(symbol, order_id)
    elif execute and final["action"] == "no_trade":
        execution_outcome = "no_trade_decided"

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
        "market_was_closed": market_was_closed,
    })

    return {
        "bull_case": bull_case,
        "bear_case": bear_case,
        "risk_review": risk_review,
        "final_decision": final,
        "governor_result": governor_result,
    }


async def run_debate(symbol: str, execute: bool = False, ignore_market_hours: bool = False) -> dict:
    async with AlpacaMCPClient() as client:
        console.print(f"[dim]Connected to Alpaca MCP server. Tools available: "
                      f"{len(await client.list_tools())}[/dim]")
        return await _run_debate_with_client(client, symbol, execute=execute,
                                              ignore_market_hours=ignore_market_hours)


async def run_scan(symbols: list[str], execute: bool = False, top_n: int = 3,
                    skip_held_positions: bool = True, ignore_market_hours: bool = False) -> dict:
    """Scans a watchlist, prints a ranked table, then runs a FULL DEBATE for
    EACH of the top-N ELIGIBLE candidates (not just #1) — diversifying
    across a few names instead of betting everything on one pick, which is
    mostly variance over a short window. 'Eligible' excludes anything the
    outlier/liquidity filters flagged as an unreliable edge — see
    scanner.get_eligible_candidates(). Reuses one open MCP connection for
    the whole scan + all debates.

    skip_held_positions=True also excludes symbols you already hold a
    position in (see alpaca_tools.get_held_underlying_symbols) — meant for
    continuous/auto-execute mode, so it diversifies across runs instead of
    repeatedly stacking into whatever ranks #1 this hour."""
    import scanner  # imported here to avoid a circular import at module load

    async with AlpacaMCPClient() as client:
        console.print(f"[dim]Connected to Alpaca MCP server. Tools available: "
                      f"{len(await client.list_tools())}[/dim]")

        held_symbols = set()
        if skip_held_positions:
            held_symbols = await alpaca_tools.get_held_underlying_symbols(client)
            pending_symbols = await alpaca_tools.get_symbols_with_open_orders(client)
            held_symbols |= pending_symbols
            console.print(f"[dim]Currently held or pending underlying symbols (will be skipped as "
                          f"new candidates): {sorted(held_symbols) if held_symbols else 'none detected'}[/dim]")

        console.print(Panel(f"[bold]Scanning {len(symbols)} symbols[/bold]", style="cyan"))
        results = await scanner.scan_watchlist(
            client, symbols,
            min_dte=config.RISK_CONFIG["min_days_to_expiry"],
            max_dte=config.RISK_CONFIG["max_days_to_expiry"],
        )
        scanner.print_scan_table(results)

        candidates = scanner.get_eligible_candidates(results, top_n, exclude_symbols=held_symbols)
        if not candidates:
            console.print("[bold red]No ELIGIBLE candidates (enough liquid data, no vol-outlier flag, "
                           "not already held). No debate to run.[/bold red]")
            return {"scan_results": results, "debates": []}

        console.print(f"\n[bold]Running full debates for {len(candidates)} eligible candidate(s): "
                       f"{', '.join(c['symbol'] for c in candidates)}[/bold]\n")

        debates = []
        for i, candidate in enumerate(candidates, 1):
            console.print(Panel(f"[bold]Candidate {i}/{len(candidates)}: {candidate['symbol']}[/bold] "
                                 f"(score {candidate['candidate_score']:.1f}, edge {candidate['edge']:+.1%})",
                                 style="bold blue"))
            debate_result = await _run_debate_with_client(client, candidate["symbol"], execute=execute,
                                                            ignore_market_hours=ignore_market_hours)
            debates.append({"symbol": candidate["symbol"], "candidate": candidate, "debate": debate_result})

        _print_batch_summary(debates)
        return {"scan_results": results, "debates": debates}


def _print_batch_summary(debates: list[dict]) -> None:
    """Portfolio-level summary across all candidates debated in this run —
    useful once you're running more than one, so you're not scrolling back
    through the whole transcript to see the overall picture."""
    if len(debates) <= 1:
        return  # a single-candidate summary would just repeat what's already visible above

    lines = []
    total_real_risk = 0.0
    for d in debates:
        final = d["debate"]["final_decision"]
        gov = d["debate"]["governor_result"]
        if final["action"] == "no_trade":
            lines.append(f"[dim]{d['symbol']}: no trade[/dim]")
        elif not gov["approved"]:
            lines.append(f"[bold red]{d['symbol']}: {final['action']} — BLOCKED by Risk Governor[/bold red]")
        else:
            total_real_risk += gov.get("real_max_loss_total") or 0
            lines.append(f"[green]{d['symbol']}: {final['action']}, qty={final.get('qty', 0)}, "
                          f"real max loss=${gov['real_max_loss_total']:,.2f} "
                          f"({gov['real_max_loss_pct_equity']:.2%} equity)[/green]")

    lines.append(f"\n[bold]Total real max loss across all approved trades this run: "
                 f"${total_real_risk:,.2f}[/bold]")
    console.print(Panel("\n".join(lines), title="📊 Batch Summary", style="bold cyan"))
