import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from llm_client import DEFAULT_BASE_URL, ChatClient, LLMError

load_dotenv(Path(__file__).parent / ".env.agent")

client = ChatClient(
    os.getenv("GROQ_API_KEY", "").strip(),
    os.getenv("LLM_MODEL", "").strip(),
    os.getenv("LLM_BASE_URL", DEFAULT_BASE_URL).strip(),
    reasoning_effort=os.getenv("LLM_REASONING_EFFORT", "").strip(),
)
tools = [{"type": "function", "function": {
    "name": "add_numbers", "description": "Add two integers.",
    "parameters": {"type": "object",
                   "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
                   "required": ["a", "b"]}}}]
try:
    reply = client.chat([{"role": "system", "content": "Use tools for arithmetic."},
                         {"role": "user", "content": "What is 17 plus 25?"}], tools)
except LLMError as exc:
    print("FAIL:", exc)
    sys.exit(1)

print("tool calls :", [(c.name, c.arguments) for c in reply.tool_calls])
print("text       :", repr(reply.content[:80]))
print("tokens     :", reply.prompt_tokens, "prompt +", reply.completion_tokens, "completion")
print("remaining  :", reply.remaining_tokens, "tokens, reset in", reply.reset_seconds, "s")