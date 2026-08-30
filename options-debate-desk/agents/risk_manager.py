import json
from datetime import date

from llm_client import call_agent
import config
import black_scholes as bs

SYSTEM_PROMPT = """You are the RISK MANAGER on an options trading desk.

You review the Bull and Bear cases against the account's actual risk budget
and hard-veto anything that violates it — no matter how compelling the case.
You do not pick a directional winner; that's the Decision agent's job. Your
job is purely: is this SAFE TO PROPOSE given account size and the risk config?

Risk config you must enforce:
{risk_config}

For EACH of the Bull and Bear proposals, evaluate:
- Estimated max loss of the proposed legs relative to account equity
- Whether it fits within max_risk_pct_of_equity
- Whether contract count fits max_contracts_per_leg
- Whether it's a defined-risk structure if defined_risk_only is true
- Whether days-to-expiry fits the configured window

IMPORTANT — days-to-expiry: you are given today's real date AND
deterministically pre-computed days-to-expiry for every leg. USE THOSE
EXACT NUMBERS. Do not recompute days-to-expiry yourself from the option
symbol or from any date you assume — a prior version of this system
incorrectly vetoed a valid ~7-day trade by miscalculating its own idea of
"today," and that whole class of error is why these numbers are handed to
you directly instead of left for you to infer.

If a proposal violates any hard limit, mark it vetoed=true and say why. If it
passes but you'd resize it, suggest a smaller qty and explain.

Respond ONLY in this JSON shape, no prose outside it:
{{
  "bull_review": {{"vetoed": true|false, "recommended_qty": 0, "notes": "..."}},
  "bear_review": {{"vetoed": true|false, "recommended_qty": 0, "notes": "..."}},
  "overall_notes": "..."
}}"""


def _verified_expiry_facts(case: dict) -> dict:
    """
    Deterministically computes real days-to-expiry for each leg symbol in a
    Bull/Bear proposal, rather than trusting the LLM to infer it correctly
    from the OCC symbol string. Exists because of a real observed bug: the
    Risk Manager once vetoed a legitimate ~7-day trade by claiming its
    expiry was "far beyond the max 60-day window" — a pure date-math
    hallucination, almost certainly from the model having no real sense of
    "today." Giving it the real number directly removes this error class.
    """
    facts = {}
    for leg in case.get("legs", []):
        symbol = leg.get("symbol", "")
        try:
            parsed = bs.parse_occ_symbol(symbol)
            facts[symbol] = {
                "expiry_date": str(parsed["expiry"]),
                "days_to_expiry_from_today": bs.days_to_expiry(parsed["expiry"]),
            }
        except ValueError:
            facts[symbol] = {"error": "could not parse OCC symbol — treat as unverifiable"}
    return facts


def run(bull_case: dict, bear_case: dict, account_info: dict, risk_config: dict) -> dict:
    system = SYSTEM_PROMPT.format(risk_config=json.dumps(risk_config, indent=2))
    bull_facts = _verified_expiry_facts(bull_case)
    bear_facts = _verified_expiry_facts(bear_case)

    user_prompt = (
        f"TODAY'S REAL DATE: {date.today().isoformat()}\n\n"
        f"Account info:\n{json.dumps(account_info, indent=2)}\n\n"
        f"Bull proposal:\n{json.dumps(bull_case, indent=2)}\n\n"
        f"VERIFIED expiry facts for Bull's legs (deterministically computed — "
        f"use these exact days-to-expiry numbers, do NOT recompute):\n"
        f"{json.dumps(bull_facts, indent=2)}\n\n"
        f"Bear proposal:\n{json.dumps(bear_case, indent=2)}\n\n"
        f"VERIFIED expiry facts for Bear's legs (deterministically computed — "
        f"use these exact days-to-expiry numbers, do NOT recompute):\n"
        f"{json.dumps(bear_facts, indent=2)}"
    )
    raw = call_agent(system, user_prompt, model=config.AGENT_MODELS["risk_manager"])
    return json.loads(raw)
