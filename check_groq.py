import json
import os
import sys
import time
from pathlib import Path

import openai
from dotenv import load_dotenv
from openai import OpenAI

HERE = Path(__file__).parent
load_dotenv(HERE / ".env.agent")

DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"

TOOLS = [{
    "type": "function",
    "function": {
        "name": "add_numbers",
        "description": "Add two integers and return the sum.",
        "parameters": {
            "type": "object",
            "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
            "required": ["a", "b"],
        },
    },
}]


def explain(exc: Exception) -> str:
    """Turn a provider error into a plain-English hint. Never echoes secrets."""
    if isinstance(exc, openai.AuthenticationError):
        return "401: the API key was rejected. Create a new key and update .env.agent."
    if isinstance(exc, openai.PermissionDeniedError):
        return "403: access denied for this key or model."
    if isinstance(exc, openai.NotFoundError):
        return "404: model not found. Check LLM_MODEL against the models page."
    if isinstance(exc, openai.RateLimitError):
        return "429: rate limit hit. Wait a minute, or try a model with higher free limits."
    if isinstance(exc, openai.BadRequestError):
        return ("400: request rejected. The model may not support tool calling, "
                "may be retired, or produced a malformed tool call. Try another model.")
    if isinstance(exc, openai.APITimeoutError):
        return "Timed out waiting for the provider."
    if isinstance(exc, openai.APIConnectionError):
        return "Could not connect. Check your internet connection and LLM_BASE_URL."
    if isinstance(exc, openai.APIStatusError):
        return f"HTTP {exc.status_code} from the provider."
    return f"Unexpected error: {type(exc).__name__}"


def call(client, **kwargs):
    """Return (response, rate_limit_info)."""
    completions = client.chat.completions
    if hasattr(completions, "with_raw_response"):
        raw = completions.with_raw_response.create(**kwargs)
        headers = {k.lower(): v for k, v in raw.headers.items()}
        info = {k: v for k, v in headers.items()
                if k.startswith("x-ratelimit") or k == "retry-after"}
        return raw.parse(), info
    return completions.create(**kwargs), {}


def tokens_of(resp) -> int:
    return resp.usage.total_tokens if getattr(resp, "usage", None) else 0


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    key = os.getenv("GROQ_API_KEY", "").strip()
    model = os.getenv("LLM_MODEL", "").strip()
    base_url = os.getenv("LLM_BASE_URL", DEFAULT_BASE_URL).strip()

    if not key:
        print("FAIL: GROQ_API_KEY is missing. Put it in .env.agent (no quotes).")
        return 1
    if not model:
        print("FAIL: LLM_MODEL is missing. Copy a model id from the provider's models page.")
        return 1

    print(f"Key loaded: yes ({len(key)} characters)")
    print(f"Model:      {model}")
    print(f"Endpoint:   {base_url}\n")

    # max_retries=0 so a rate limit shows up here instead of being hidden by auto-retries
    client = OpenAI(api_key=key, base_url=base_url, timeout=30, max_retries=0)
    total_tokens = 0
    started = time.monotonic()

    # ---- Check 1: plain chat ------------------------------------------------
    try:
        t = time.monotonic()
        resp, limits = call(
            client, model=model, temperature=0, max_completion_tokens=20,
            messages=[{"role": "user", "content": "Reply with exactly one word: ready"}])
    except Exception as exc:
        print(f"1. Plain chat:       FAIL - {explain(exc)}")
        return 1
    total_tokens += tokens_of(resp)
    reply = (resp.choices[0].message.content or "").strip()
    print(f"1. Plain chat:       PASS ({int((time.monotonic() - t) * 1000)} ms) "
          f"reply: {reply[:30]!r}")

    # ---- Check 2: the model asks for a tool ---------------------------------
    messages = [
        {"role": "system", "content": "Use the provided tool for any arithmetic."},
        {"role": "user", "content": "What is 17 plus 25? Use the tool."},
    ]
    try:
        resp, _ = call(client, model=model, temperature=0, max_completion_tokens=200,
                       messages=messages, tools=TOOLS, tool_choice="auto")
    except Exception as exc:
        print(f"2. Tool call:        FAIL - {explain(exc)}")
        return 1
    total_tokens += tokens_of(resp)
    msg = resp.choices[0].message
    calls = msg.tool_calls or []
    if not calls:
        print("2. Tool call:        FAIL - the model answered in text instead of calling the tool.")
        print("   Try a model that lists tool use support.")
        return 1
    first = calls[0]
    try:
        args = json.loads(first.function.arguments)
        a, b = args["a"], args["b"]
        if not (isinstance(a, int) and isinstance(b, int)):
            raise ValueError
    except (ValueError, KeyError, TypeError):
        print("2. Tool call:        FAIL - the tool arguments were not valid JSON integers.")
        return 1
    if first.function.name != "add_numbers" or a + b != 42:
        print("2. Tool call:        FAIL - wrong tool name or wrong arguments.")
        return 1
    print(f"2. Tool call:        PASS (asked for add_numbers with a={a}, b={b})")

    # ---- Check 3: full round trip (the loop we are about to build) ----------
    messages.append({
        "role": "assistant", "content": msg.content or "",
        "tool_calls": [{"id": first.id, "type": "function",
                        "function": {"name": first.function.name,
                                     "arguments": first.function.arguments}}],
    })
    messages.append({"role": "tool", "tool_call_id": first.id,
                     "content": json.dumps({"sum": a + b})})
    try:
        resp, _ = call(client, model=model, temperature=0, max_completion_tokens=100,
                       messages=messages, tools=TOOLS)
    except Exception as exc:
        print(f"3. Tool round trip:  FAIL - {explain(exc)}")
        return 1
    total_tokens += tokens_of(resp)
    final = resp.choices[0].message.content or ""
    if "42" not in final:
        print(f"3. Tool round trip:  FAIL - the final answer did not use the tool result: {final[:60]!r}")
        return 1
    print("3. Tool round trip:  PASS (the model used the tool result in its answer)")

    print(f"\nTotal: {total_tokens} tokens, {int((time.monotonic() - started) * 1000)} ms")
    if limits:
        print("Rate-limit info reported by the provider:")
        for name, value in sorted(limits.items()):
            print(f"  {name}: {value}")
    print("\nAll checks passed. This model can run the agent loop.")
    return 0


if __name__ == "__main__":
    sys.exit(main())