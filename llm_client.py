"""
Thin wrapper so agents don't repeat API boilerplate. Supports three
backends, picked via config.LLM_PROVIDER:

- "anthropic" (default): Claude via the Messages API. Best reasoning
  quality — use this for the actual hackathon submission/demo.
- "groq": free tier (console.groq.com, no card required), OpenAI-compatible
  endpoint. Good for testing the pipeline's plumbing.
- "featherless": hackathon sponsor bonus (code ALPACA26 at featherless.ai,
  $25 free credit), also OpenAI-compatible, no model-size limit, up to
  256K context.

Both OpenAI-compatible providers share one code path (_get_openai_client /
the main branch in _call_one_model) parameterized by base URL and API key —
adding a FOURTH OpenAI-compatible provider later is a config.py entry plus
one line here, not a new branch. Anthropic stays separate since it's a
genuinely different SDK/API shape.

For Groq/Featherless specifically, call_agent() automatically falls through
config.MODEL_ROSTERS[provider] when a model's quota is exhausted — each
model has its own separate quota bucket, so trying the next one is a real,
working escape hatch rather than just retrying the same limit.

Every agent calls call_agent() the same way regardless of backend.
"""
import re
import time
import config

# Provider name -> (base_url, api_key). Only OpenAI-compatible providers
# belong here; Anthropic uses its own SDK/branch below.
OPENAI_COMPATIBLE_PROVIDERS = {
    "groq": ("https://api.groq.com/openai/v1", lambda: config.GROQ_API_KEY),
    "featherless": ("https://api.featherless.ai/v1", lambda: config.FEATHERLESS_API_KEY),
}


def _is_rate_limit_error(exc: Exception) -> bool:
    """Works for openai.RateLimitError and anthropic's equivalent without
    importing either SDK's exception classes at module load time."""
    return exc.__class__.__name__ in ("RateLimitError",) or "rate_limit" in str(exc).lower()


def _extract_suggested_wait_seconds(exc: Exception) -> float | None:
    """
    Rate-limit errors from these providers include a 'Please try again in
    Xm Ys' hint. Parses it so we can distinguish a short per-minute limit
    (worth retrying on the SAME model) from a long daily-quota exhaustion
    (worth falling through to the NEXT model in the roster instead).
    Returns None if no hint is found.
    """
    match = re.search(r"try again in (?:(\d+)m)?([\d.]+)s", str(exc))
    if not match:
        return None
    minutes = float(match.group(1)) if match.group(1) else 0
    seconds = float(match.group(2))
    return minutes * 60 + seconds


# Above this, don't bother retrying the SAME model automatically — either
# fall through to the next model in the roster, or (if there isn't one /
# it's Anthropic) fail fast with a clear message. This is what a "tokens
# per day" limit looks like, versus a short "tokens per minute" one.
MAX_AUTO_RETRY_WAIT_SECONDS = 90

_anthropic_client = None
_openai_compatible_clients = {}  # provider name -> client, lazily built


class _ModelUnavailable(Exception):
    """Internal signal that THIS model is rate-limited/quota-exhausted badly
    enough to give up on it — the roster loop catches this to try the next
    model, rather than it propagating as a generic failure."""
    pass


def _get_anthropic_client():
    global _anthropic_client
    if _anthropic_client is None:
        from anthropic import Anthropic
        _anthropic_client = Anthropic(api_key=config.ANTHROPIC_API_KEY)
    return _anthropic_client


def _get_openai_compatible_client(provider: str):
    """Lazily builds (and caches) a client for any provider listed in
    OPENAI_COMPATIBLE_PROVIDERS — same openai SDK, different base_url/key."""
    if provider not in _openai_compatible_clients:
        from openai import OpenAI
        base_url, get_key = OPENAI_COMPATIBLE_PROVIDERS[provider]
        _openai_compatible_clients[provider] = OpenAI(api_key=get_key(), base_url=base_url)
    return _openai_compatible_clients[provider]


def _extract_json(text: str) -> str:
    """
    Robustly extracts a JSON object from model output, regardless of
    whether it's wrapped in code fences, preceded by reasoning/chain-of-
    thought preamble, or followed by trailing commentary. Finds the first
    '{' and its matching closing '}' rather than assuming clean output —
    reasoning models (like gpt-oss on Groq) often don't return clean JSON
    even when instructed to.
    """
    text = text.strip()
    start = text.find("{")
    if start == -1:
        return text  # no JSON object found; let json.loads raise a clear error

    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return text[start:]  # unbalanced; return what we have and let json.loads raise


def _call_one_model(system_prompt: str, user_prompt: str, model: str, max_tokens: int) -> str:
    """
    Calls exactly ONE model, with short-rate-limit retry-with-backoff on
    that same model. Raises _ModelUnavailable (rather than the raw
    exception) when this model's limit needs a wait too long to retry
    automatically, or when short retries are exhausted — the signal for
    call_agent()'s roster loop to try the next model instead of failing.
    """
    max_attempts = 5
    base_wait = 20  # seconds; these providers' short-term rate limit windows are short

    for attempt in range(1, max_attempts + 1):
        try:
            if config.LLM_PROVIDER in OPENAI_COMPATIBLE_PROVIDERS:
                client = _get_openai_compatible_client(config.LLM_PROVIDER)
                response = client.chat.completions.create(
                    model=model,
                    max_tokens=max_tokens,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                )
                content = response.choices[0].message.content or ""
                if not content.strip():
                    finish_reason = response.choices[0].finish_reason
                    # This is a DIFFERENT failure mode from rate limiting —
                    # the model spent its whole token budget on internal
                    # reasoning before writing an answer. Retrying the exact
                    # same prompt on the SAME model tends to hit the same
                    # wall again, so this goes straight to _ModelUnavailable
                    # (skip same-model retry, let the roster try the next
                    # model) rather than a generic RuntimeError, which used
                    # to escape the roster logic entirely and crash the
                    # whole run — a real bug, caught via a live dashboard
                    # failure, not just theoretical.
                    raise _ModelUnavailable(
                        f"'{model}' returned empty content (finish_reason={finish_reason}) — likely "
                        f"exhausted its token budget on internal reasoning before writing output."
                    )
                return _extract_json(content)

            client = _get_anthropic_client()
            response = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
            )
            return _extract_json("".join(block.text for block in response.content if block.type == "text"))

        except Exception as exc:
            if not _is_rate_limit_error(exc):
                raise

            suggested_wait = _extract_suggested_wait_seconds(exc)
            if suggested_wait is not None and suggested_wait > MAX_AUTO_RETRY_WAIT_SECONDS:
                raise _ModelUnavailable(
                    f"'{model}' needs a {suggested_wait:.0f}s wait (likely a DAILY quota) — too long "
                    f"to retry on this model."
                ) from exc

            if attempt == max_attempts:
                raise _ModelUnavailable(
                    f"'{model}' still rate-limited after {max_attempts} retries."
                ) from exc

            wait = suggested_wait if suggested_wait is not None else base_wait * attempt
            print(f"  [rate limited on {model} — waiting {wait:.0f}s before retry {attempt}/{max_attempts - 1}]")
            time.sleep(wait)


def call_agent(system_prompt: str, user_prompt: str, model: str, max_tokens: int = 4000) -> str:
    """
    Single-turn call: system prompt defines the agent's role, user prompt
    carries the data.

    If the active provider (config.LLM_PROVIDER) has a roster configured in
    config.MODEL_ROSTERS: tries `model` first, and on quota exhaustion /
    persistent rate limiting, automatically falls through to the next model
    in that roster (starting the rotation at wherever `model` sits in the
    list). Only raises once every model in the roster has been tried and
    failed.

    For Anthropic, or any provider with no roster configured: single-model
    behavior, still with short-rate-limit retry, still failing fast on a
    long wait — just no fallback model to move to.
    """
    roster = config.MODEL_ROSTERS.get(config.LLM_PROVIDER)
    if not roster:
        try:
            return _call_one_model(system_prompt, user_prompt, model, max_tokens)
        except _ModelUnavailable as exc:
            raise RuntimeError(str(exc)) from exc

    start_idx = roster.index(model) if model in roster else 0
    ordered_models = roster[start_idx:] + roster[:start_idx]  # start at `model`, wrap around the rest

    last_exc = None
    for m in ordered_models:
        try:
            return _call_one_model(system_prompt, user_prompt, m, max_tokens)
        except _ModelUnavailable as exc:
            print(f"  [model '{m}' unavailable — trying next in {config.LLM_PROVIDER} roster]")
            last_exc = exc
            continue

    raise RuntimeError(
        f"Every model in the {config.LLM_PROVIDER} roster ({', '.join(ordered_models)}) is "
        f"rate-limited or quota-exhausted. Last error: {last_exc}"
    )
