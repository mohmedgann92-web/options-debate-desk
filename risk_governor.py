"""
A deterministic backstop that sits BELOW the LLM Risk Manager, not instead
of it. This exists because of a real failure observed in testing: an agent's
narrative text said one strike ($495) while its actual `legs` field used a
different, real strike ($535) on a different expiration entirely — both
symbols were genuine contracts (so the symbol-existence fact-check passed),
but the LLM Risk Manager computed "max loss" from the agent's STATED numbers
rather than the real ones, approving a trade that was actually ~10x over
its own risk budget.

This module never trusts agent-reported width, credit, or max loss. It
recomputes everything from the real contract data (strike, mid price,
expiry) that _fact_check_legs() already resolved, and refuses to authorize
execution if the real numbers violate the risk budget — regardless of what
any LLM concluded.
"""


def check_trade(legs: list[dict], qty: int, chain_by_symbol: dict, account_info: dict, risk_config: dict) -> dict:
    """
    Returns:
        {
            "approved": bool,
            "violations": [str, ...],   # empty if approved
            "notes": [str, ...],        # informational, doesn't block
            "real_max_loss_per_contract": float | None,
            "real_max_loss_total": float | None,
            "real_max_loss_pct_equity": float | None,
        }
    approved=False means execution must be blocked, full stop, regardless
    of --execute or what any agent said.
    """
    violations = []
    notes = []

    resolved = []
    for leg in legs:
        symbol = leg.get("symbol", "")
        contract = chain_by_symbol.get(symbol)
        if contract is None:
            violations.append(f"Leg {symbol} does not exist in the real chain data — cannot verify risk. BLOCKED.")
            continue
        if contract.get("mid") is None:
            violations.append(f"Leg {symbol} has no real mid price available — cannot compute real risk. BLOCKED.")
            continue
        resolved.append({
            "symbol": symbol,
            "side": leg.get("side"),
            "ratio_qty": float(leg.get("ratio_qty", 1)),
            "strike": contract["strike"],
            "option_type": contract["option_type"],
            "expiry": contract["expiry"],
            "mid": contract["mid"],
        })

    if violations:
        return {
            "approved": False, "violations": violations, "notes": notes,
            "real_max_loss_per_contract": None, "real_max_loss_total": None,
            "real_max_loss_pct_equity": None,
        }

    if not resolved:
        return {
            "approved": True, "violations": [], "notes": ["no legs to check (no_trade)"],
            "real_max_loss_per_contract": 0.0, "real_max_loss_total": 0.0, "real_max_loss_pct_equity": 0.0,
        }

    # Structural checks — these are hard requirements for defined_risk_only,
    # independent of any single-symbol lookup.
    expiries = {r["expiry"] for r in resolved}
    types = {r["option_type"] for r in resolved}

    if risk_config.get("defined_risk_only") and len(resolved) > 1 and len(expiries) > 1:
        violations.append(
            f"Legs have MISMATCHED EXPIRIES ({', '.join(sorted(expiries))}) — this is not a valid "
            f"single-expiry defined-risk vertical spread, whatever the agent's narrative claimed. BLOCKED."
        )

    if len(resolved) > 1 and len(types) > 1:
        notes.append(f"Legs mix option types ({', '.join(sorted(types))}) — not a standard vertical spread "
                      f"(could be a straddle/strangle). Real max-loss math below assumes a simple vertical "
                      f"and may not apply cleanly to this structure — verify manually.")

    if violations:
        return {
            "approved": False, "violations": violations, "notes": notes,
            "real_max_loss_per_contract": None, "real_max_loss_total": None,
            "real_max_loss_pct_equity": None,
        }

    # Real max-loss computation from actual market data, not agent claims.
    if len(resolved) == 1:
        leg = resolved[0]
        if leg["side"] == "sell" and leg["option_type"] == "call":
            violations.append("Single-leg NAKED CALL SALE has undefined risk — violates defined_risk_only. BLOCKED.")
            max_loss_per_contract = None
        elif leg["side"] == "sell" and leg["option_type"] == "put":
            # Cash-secured put: worst case is assignment at strike, offset by premium received.
            max_loss_per_contract = (leg["strike"] - leg["mid"]) * 100 * leg["ratio_qty"]
            notes.append("Single-leg cash-secured put — max loss assumes full assignment at strike.")
        else:  # buying a single option: risk is capped at premium paid
            max_loss_per_contract = leg["mid"] * 100 * leg["ratio_qty"]
    elif len(resolved) == 2 and len(types) == 1:
        # Standard vertical spread: real width from real strikes, real net
        # premium from real mids and sides — not from what the agent said.
        strikes = sorted(r["strike"] for r in resolved)
        width = strikes[1] - strikes[0]
        net_premium_per_share = sum(
            r["mid"] * r["ratio_qty"] * (1 if r["side"] == "sell" else -1) for r in resolved
        )
        if net_premium_per_share > 0:
            # credit spread: max loss is width minus credit received (floor at 0)
            max_loss_per_contract = max(0.0, width - net_premium_per_share) * 100
            notes.append(f"Real width=${width:.2f}, real net credit=${net_premium_per_share:.2f}/share "
                         f"(computed from actual chain data, not agent's stated numbers).")
        else:
            # debit spread: max loss is the debit paid
            debit = abs(net_premium_per_share)
            max_loss_per_contract = debit * 100
            notes.append(f"Real width=${width:.2f}, real net debit=${debit:.2f}/share "
                         f"(computed from actual chain data, not agent's stated numbers).")
    else:
        notes.append(f"{len(resolved)}-leg structure — real max-loss auto-calculation isn't implemented "
                      f"for this shape. Verify manually before trusting any agent's risk claim.")
        max_loss_per_contract = None

    if violations:
        return {
            "approved": False, "violations": violations, "notes": notes,
            "real_max_loss_per_contract": None, "real_max_loss_total": None,
            "real_max_loss_pct_equity": None,
        }

    if max_loss_per_contract is None:
        # Couldn't compute a number but no hard violation either — don't
        # silently approve something we can't actually verify.
        violations.append("Could not compute a verified real max-loss figure for this structure. BLOCKED "
                          "pending manual review — this is a fail-closed default, not a data problem.")
        return {
            "approved": False, "violations": violations, "notes": notes,
            "real_max_loss_per_contract": None, "real_max_loss_total": None,
            "real_max_loss_pct_equity": None,
        }

    real_max_loss_total = max_loss_per_contract * qty
    equity = account_info.get("equity", 0)
    real_pct = (real_max_loss_total / equity) if equity else None

    if real_pct is not None and real_pct > risk_config.get("max_risk_pct_of_equity", 0.02):
        violations.append(
            f"Real max loss is ${real_max_loss_total:,.2f} ({real_pct:.2%} of equity), which exceeds the "
            f"{risk_config.get('max_risk_pct_of_equity', 0.02):.0%} budget — regardless of what any agent "
            f"claimed the risk was. BLOCKED."
        )

    return {
        "approved": len(violations) == 0,
        "violations": violations,
        "notes": notes,
        "real_max_loss_per_contract": round(max_loss_per_contract, 2),
        "real_max_loss_total": round(real_max_loss_total, 2),
        "real_max_loss_pct_equity": round(real_pct, 4) if real_pct is not None else None,
    }
