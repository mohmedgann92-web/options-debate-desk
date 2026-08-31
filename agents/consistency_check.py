"""
A self-consistency check that runs right after Bull/Bear produce a
proposal, before Risk Manager or Decision ever see it. Exists directly
because of the MSFT near-miss: an agent's thesis TEXT described a $10-wide
credit spread, but its actual `legs` field pointed at real contracts $50
wide on two different expirations. Both symbols were genuine, so nothing
that only checks "does this symbol exist" would catch it — the problem was
the STORY not matching the STRUCTURE, and nothing verified that until the
deterministic Risk Governor caught it much later, right before execution.

This closes that gap earlier and more cheaply: resolve the agent's legs
against real chain data (same lookup risk_governor.py uses), show the
agent its own thesis next to the REAL facts about what it actually picked,
and ask it to correct the narrative if the two don't match.

Deliberately narrow in what it's allowed to fix: the corrected response
must use the EXACT SAME legs as the original — this check can rewrite a
wrong story, never the underlying trade. If a "correction" comes back with
different legs, it's rejected outright and the original proposal is kept
unchanged, on the theory that an unconstrained edit here would just be a
second chance to introduce a NEW error rather than fix the real one. The
deterministic Risk Governor remains the actual safety gate regardless of
what happens here — this only improves what reaches it, never replaces it.
"""
import json

from llm_client import call_agent
import config

SYSTEM_PROMPT = """You are a fact-checker reviewing ONE analyst's options
trade proposal before it goes to a Risk Manager.

You will be given:
1. The analyst's own thesis, strategy description, and stated legs.
2. VERIFIED REAL data for those exact contracts — computed directly from
   live market data, not from the analyst. This is ground truth.

Your ONLY job: check whether the analyst's thesis/strategy TEXT accurately
describes the REAL contracts (correct strikes, correct expiry, correct
spread width, correct credit/debit direction). Analysts sometimes write a
narrative that doesn't match what they actually selected — you're catching
that specific class of error.

Hard rules:
- If the text is already accurate, return the SAME case completely
  unchanged (including the exact same "legs" list).
- If the text is wrong, return a corrected "thesis" and "strategy" that
  accurately describe the REAL data — but the "legs" field in your
  response MUST be character-for-character identical to the legs you were
  given. You are correcting the STORY, never the TRADE. Do not invent new
  strikes, do not "fix" the legs, do not change ratio_qty or side.
- If the structure is fundamentally invalid (e.g. mismatched expiries on a
  2-leg spread) such that no accurate story can describe it as a coherent
  strategy, say so plainly in the corrected thesis rather than papering
  over it — do not pretend a broken structure is a clean one.
- Do not change target_price, invalidation, confidence, or
  confidence_reason unless they directly contradict the real data.

Respond ONLY in this exact JSON shape (same schema as the input case):
{{
  "thesis": "...",
  "strategy": "...",
  "legs": [{{"symbol": "...", "side": "buy|sell", "ratio_qty": 1}}],
  "target_price": 0.0,
  "invalidation": "...",
  "confidence": "low|medium|high",
  "confidence_reason": "..."
}}"""


def _resolve_real_facts(legs: list[dict], chain_by_symbol: dict) -> dict | None:
    """Same leg-resolution approach as risk_governor.check_trade — returns
    None if any leg can't be resolved against real chain data (a
    hallucinated symbol). That case is left for the Risk Governor to catch
    later; this check has nothing useful to compare against without real
    underlying data."""
    resolved = []
    for leg in legs:
        contract = chain_by_symbol.get(leg.get("symbol", ""))
        if contract is None or contract.get("mid") is None:
            return None
        resolved.append({
            "symbol": leg["symbol"],
            "side": leg.get("side"),
            "ratio_qty": leg.get("ratio_qty", 1),
            "strike": contract["strike"],
            "option_type": contract["option_type"],
            "expiry": contract["expiry"],
            "real_mid": contract["mid"],
        })

    facts = {"legs": resolved}
    if len(resolved) == 2 and resolved[0]["option_type"] == resolved[1]["option_type"]:
        strikes = sorted(r["strike"] for r in resolved)
        facts["real_width"] = round(strikes[1] - strikes[0], 2)
        facts["same_expiry"] = resolved[0]["expiry"] == resolved[1]["expiry"]
        net = sum(r["real_mid"] * float(r["ratio_qty"]) * (1 if r["side"] == "sell" else -1) for r in resolved)
        facts["real_net_premium_per_share"] = round(net, 4)  # positive = credit, negative = debit
    return facts


def _legs_match(a: list[dict], b: list[dict]) -> bool:
    """Order-independent comparison of two leg lists by symbol/side/ratio_qty."""
    def normalize(legs):
        return sorted(
            (leg.get("symbol", ""), leg.get("side", ""), str(float(leg.get("ratio_qty", 1))))
            for leg in legs
        )
    return normalize(a) == normalize(b)


def verify(case: dict, chain_by_symbol: dict, agent_label: str) -> dict:
    """
    Returns a copy of `case`, with "thesis"/"strategy" possibly corrected,
    plus a new "consistency_check" field describing what happened:
    "ok" | "corrected" | "skipped_unresolved_legs" | "check_failed" |
    "correction_rejected_changed_legs".

    Never raises — any failure in this check falls back to the original
    case unchanged, consistent with the rest of this project's fail-open
    philosophy for non-safety-critical helpers (the Risk Governor, not
    this, is what actually gates execution).
    """
    if not case.get("legs"):
        return {**case, "consistency_check": "skipped_no_legs"}

    real_facts = _resolve_real_facts(case["legs"], chain_by_symbol)
    if real_facts is None:
        return {**case, "consistency_check": "skipped_unresolved_legs"}

    user_prompt = (
        f"Analyst's original proposal:\n{json.dumps(case, indent=2)}\n\n"
        f"VERIFIED REAL data for these exact legs (ground truth):\n"
        f"{json.dumps(real_facts, indent=2)}"
    )

    try:
        raw = call_agent(SYSTEM_PROMPT, user_prompt, model=config.MODEL)
        corrected = json.loads(raw)
    except Exception:
        return {**case, "consistency_check": "check_failed"}

    if not _legs_match(case["legs"], corrected.get("legs", [])):
        # The check tried to change the actual trade — reject the
        # correction outright and keep the original proposal. This
        # shouldn't happen given the system prompt's hard rule, but this
        # check is the enforcement backstop for that rule, not just a
        # request the model might ignore.
        return {**case, "consistency_check": "correction_rejected_changed_legs"}

    changed = (corrected.get("thesis") != case.get("thesis")
               or corrected.get("strategy") != case.get("strategy"))

    # Defensive merge rather than trusting the LLM's response dict to be
    # complete: start from the original case (guaranteed to have every
    # required key, since it came from bull.py/bear.py's own schema), and
    # only overlay the fields the correction actually touched. A response
    # missing "confidence_reason" or similar, despite the system prompt's
    # instructions, would otherwise KeyError in the rendering code later.
    result = {**case, **{k: v for k, v in corrected.items() if k in case}}
    result["legs"] = case["legs"]  # byte-identical to input, not just "equivalent"
    result["consistency_check"] = "corrected" if changed else "ok"
    return result
