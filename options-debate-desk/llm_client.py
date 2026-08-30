"""
Thin wrapper so agents don't repeat API boilerplate. Supports two backends,
picked via config.LLM_PROVIDER:

- "anthropic" (default): Claude via the Messages API. Best reasoning
  quality — use this for the actual hackathon submission/demo.
- "groq": free tier (console.groq.com, no card required), OpenAI-compatible
  endpoint running open models like Llama 3.3 70B. Good enough for testing
  the pipeline's plumbing while you sort out Anthropic billing.

Every agent calls call_agent() the same way regardless of backend.
"""
import time
import config


def _is_rate_limit_error(exc: Exception) -> bool:
    """Works for both openai.RateLimitError and anthropic's equivalent without
    importing either SDK's exception classes at module load time."""
    return exc.__class__.__name__ in ("RateLimitError",) or "rate_limit" in str(exc).lower()

_anthropic_client = None
_groq_client = None


def _get_anthropic_client():
    global _anthropic_client
    if _anthropic_client is None:
        from anthropic import Anthropic
        _anthropic_client = Anthropic(api_key=config.ANTHROPIC_API_KEY)
    return _anthropic_client


def _get_groq_client():
    global _groq_client
    if _groq_client is None:
        from openai import OpenAI
        _groq_client = OpenAI(api_key=config.GROQ_API_KEY, base_url="https://api.groq.com/openai/v1")
    return _groq_client


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


def call_agent(system_prompt: str, user_prompt: str, model: str, max_tokens: int = 4000) -> str:
    """Single-turn call: system prompt defines the agent's role, user prompt carries the data.

    Retries on rate-limit errors — the Groq free tier's 8K TPM cap is easy
    to hit mid-debate since each agent's prompt includes the prior agents'
    full JSON output. A short wait and retry is the correct response, not
    a crash.
    """
    max_attempts = 5
    base_wait = 20  # seconds; Groq free tier's rate limit windows are short

    for attempt in range(1, max_attempts + 1):
        try:
            if config.LLM_PROVIDER == "groq":
                client = _get_groq_client()
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
                    raise RuntimeError(
                        f"Groq returned empty content (finish_reason={finish_reason}). "
                        f"If finish_reason is 'length', the model used its whole token "
                        f"budget on internal reasoning before writing output — try "
                        f"raising max_tokens further or switching models."
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
            if not _is_rate_limit_error(exc) or attempt == max_attempts:
                raise
            wait = base_wait * attempt
            print(f"  [rate limited — waiting {wait}s before retry {attempt}/{max_attempts - 1}]")
            time.sleep(wait)
