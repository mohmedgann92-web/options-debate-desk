"""
Lists models available to your Featherless key, and does a tiny live test
call against config.MODEL to confirm it actually works — the same
"verify against the real thing before trusting a guess" pattern used for
Groq earlier in this project. Featherless's catalog has 37,000+ models and
changes, so FEATHERLESS_MODEL_ROSTER in config.py is a documented-but-
unverified guess until you run this.

Usage:
    python list_featherless_models.py
"""
import config

from openai import OpenAI

client = OpenAI(api_key=config.FEATHERLESS_API_KEY, base_url="https://api.featherless.ai/v1")

print("=== Attempting to list models (may not be supported — Featherless's")
print("    OpenAI-compat docs don't explicitly confirm a /models endpoint) ===")
try:
    models = client.models.list()
    names = [m.id for m in models.data]
    print(f"{len(names)} models available. First 20:")
    for n in names[:20]:
        print(" ", n)
except Exception as exc:
    print(f"Listing failed ({exc}) — falling back to a direct test call instead.")

print(f"\n=== Test call against config.MODEL ({config.MODEL}) ===")
try:
    response = client.chat.completions.create(
        model=config.MODEL,
        max_tokens=50,
        messages=[{"role": "user", "content": "Reply with exactly: OK"}],
    )
    print("SUCCESS:", response.choices[0].message.content)
except Exception as exc:
    print(f"FAILED: {exc}")
    print("\nIf this model doesn't exist in your account's catalog, browse "
          "https://featherless.ai/models for a real one and update "
          "config.MODEL / config.FEATHERLESS_MODEL_ROSTER.")

print(f"\n=== Test call against each model in FEATHERLESS_MODEL_ROSTER ===")
for model in config.FEATHERLESS_MODEL_ROSTER:
    try:
        response = client.chat.completions.create(
            model=model, max_tokens=20,
            messages=[{"role": "user", "content": "Reply with exactly: OK"}],
        )
        print(f"  {model}: OK — {response.choices[0].message.content!r}")
    except Exception as exc:
        print(f"  {model}: FAILED — {exc}")
