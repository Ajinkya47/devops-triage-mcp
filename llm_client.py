import re
from dataclasses import dataclass, field

import openai
from openai import OpenAI

DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"


class LLMError(Exception):
    """Base class. Messages are fixed strings, safe to print (no keys, no raw responses)."""


class RateLimited(LLMError):
    def __init__(self, message: str = "Rate limit reached", retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class BadToolCall(LLMError):
    """The model produced a malformed tool call."""


class TransientError(LLMError):
    """Timeout, connection problem, or server error. A short retry is reasonable."""


class FatalLLMError(LLMError):
    """Retrying will not help (bad key, unknown model, request too large, rejected options)."""


@dataclass
class ToolCallRequest:
    id: str
    name: str
    arguments: str


@dataclass
class LLMReply:
    content: str = ""
    tool_calls: list = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    finish_reason: str = ""
    remaining_tokens: int | None = None
    reset_seconds: float | None = None
    limit_tokens: int | None = None


def parse_duration(text) -> float | None:
    """'735ms' -> 0.735, '1m26.4s' -> 86.4, '30' -> 30.0. Anything else -> None."""
    if text is None:
        return None
    s = str(text).strip().lower()
    if re.fullmatch(r"\d+(?:\.\d+)?", s):
        return float(s)
    if not re.fullmatch(r"(?:\d+(?:\.\d+)?(?:ms|h|m|s))+", s):
        return None
    unit = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}
    return sum(float(n) * unit[u] for n, u in re.findall(r"(\d+(?:\.\d+)?)(ms|h|m|s)", s))


def _int(text) -> int | None:
    try:
        return int(str(text).strip())
    except (TypeError, ValueError):
        return None


def _error_code(exc) -> str:
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        inner = body.get("error", body)
        if isinstance(inner, dict):
            return str(inner.get("code") or "")
    return ""


def classify(exc: Exception) -> LLMError:
    """Map an SDK error to one of our four types. Never includes raw response text."""
    if isinstance(exc, openai.RateLimitError):
        headers = getattr(getattr(exc, "response", None), "headers", None) or {}
        retry = parse_duration(headers.get("retry-after"))
        if retry is None:
            retry = parse_duration(headers.get("x-ratelimit-reset-tokens"))
        return RateLimited("Rate limit reached", retry)
    if isinstance(exc, openai.AuthenticationError):
        return FatalLLMError("The API key was rejected (401). Create a new key and update .env.agent.")
    if isinstance(exc, openai.PermissionDeniedError):
        return FatalLLMError("Access denied (403) for this key or model.")
    if isinstance(exc, openai.NotFoundError):
        return FatalLLMError("Model not found (404). Check LLM_MODEL against the provider's models page.")
    if isinstance(exc, openai.BadRequestError):
        if "tool_use_failed" in _error_code(exc):
            return BadToolCall("The model produced a malformed tool call")
        return FatalLLMError("The provider rejected the request (400). "
                             "Check the model name and options such as LLM_REASONING_EFFORT.")
    if isinstance(exc, openai.APITimeoutError):
        return TransientError("The provider timed out")
    if isinstance(exc, openai.APIConnectionError):
        return TransientError("Could not connect to the provider")
    if isinstance(exc, openai.InternalServerError):
        return TransientError("The provider had a server error")
    if isinstance(exc, openai.APIStatusError):
        if exc.status_code == 413:
            return FatalLLMError("The request was too large for the model's limits (413). "
                                 "Shorten the prompt or tool results.")
        return FatalLLMError(f"The provider returned HTTP {exc.status_code}.")
    return FatalLLMError(f"Unexpected error ({type(exc).__name__}).")


class ChatClient:
    """One chat call at a time. No automatic retries: the agent loop decides what to do."""

    def __init__(self, api_key: str, model: str, base_url: str = DEFAULT_BASE_URL, *,
                 reasoning_effort: str = "", max_completion_tokens: int = 1200,
                 timeout: float = 45.0, http_client=None):
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.max_completion_tokens = max_completion_tokens
        self._client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout,
                              max_retries=0, http_client=http_client)

    def chat(self, messages: list, tools: list | None = None) -> LLMReply:
        kwargs = {
            "model": self.model,
            "messages": messages,
            "temperature": 0,
            "max_completion_tokens": self.max_completion_tokens,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        if self.reasoning_effort:
            kwargs["extra_body"] = {"reasoning_effort": self.reasoning_effort}

        try:
            raw = self._client.chat.completions.with_raw_response.create(**kwargs)
            response = raw.parse()
            headers = {k.lower(): v for k, v in raw.headers.items()}
        except openai.OpenAIError as exc:
            raise classify(exc) from None

        if not response.choices:
            raise TransientError("The provider returned no choices")
        choice = response.choices[0]
        message = choice.message

        calls = []
        for tc in (getattr(message, "tool_calls", None) or []):
            fn = getattr(tc, "function", None)
            if fn is None:
                continue
            calls.append(ToolCallRequest(
                id=str(getattr(tc, "id", "") or ""),
                name=str(getattr(fn, "name", "") or ""),
                arguments=str(getattr(fn, "arguments", "") or ""),
            ))

        usage = getattr(response, "usage", None)
        return LLMReply(
            content=message.content or "",
            tool_calls=calls,
            prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
            finish_reason=str(choice.finish_reason or ""),
            remaining_tokens=_int(headers.get("x-ratelimit-remaining-tokens")),
            reset_seconds=parse_duration(headers.get("x-ratelimit-reset-tokens")),
            limit_tokens=_int(headers.get("x-ratelimit-limit-tokens")),
        )