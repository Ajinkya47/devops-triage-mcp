import json

import httpx
import pytest

from llm_client import (BadToolCall, ChatClient, FatalLLMError, RateLimited,
                        TransientError, parse_duration)

MESSAGES = [{"role": "user", "content": "hi"}]
TOOLS = [{"type": "function", "function": {
    "name": "assess_issue", "parameters": {"type": "object", "properties": {}}}}]
SECRET = "KEY-SECRET-123"


def completion(content="hi", tool_calls=None, usage=(10, 5)):
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": "x", "object": "chat.completion", "created": 0, "model": "m",
        "choices": [{"index": 0, "message": message,
                     "finish_reason": "tool_calls" if tool_calls else "stop"}],
        "usage": {"prompt_tokens": usage[0], "completion_tokens": usage[1],
                  "total_tokens": usage[0] + usage[1]},
    }


def client_with(handler, **kwargs):
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return ChatClient(SECRET, "test-model", "https://example.test/v1",
                      http_client=http, **kwargs)


# ---------- Durations ----------

@pytest.mark.parametrize("text,expected", [
    ("735ms", 0.735), ("1m26.4s", 86.4), ("2s", 2.0), ("1h2m3s", 3723.0),
    ("7.66s", 7.66), ("30", 30.0), (None, None), ("soon", None), ("", None), ("5x", None),
])
def test_parse_duration(text, expected):
    result = parse_duration(text)
    assert result == (pytest.approx(expected) if expected is not None else None)


# ---------- Good replies ----------

def test_plain_reply_usage_and_rate_headers():
    def handler(request):
        return httpx.Response(200, json=completion("hello"), headers={
            "x-ratelimit-remaining-tokens": "7000", "x-ratelimit-reset-tokens": "1m26.4s"})
    reply = client_with(handler).chat(MESSAGES)
    assert reply.content == "hello" and reply.tool_calls == []
    assert (reply.prompt_tokens, reply.completion_tokens) == (10, 5)
    assert reply.remaining_tokens == 7000
    assert reply.reset_seconds == pytest.approx(86.4)

def test_tool_call_reply():
    calls = [{"id": "call_1", "type": "function",
              "function": {"name": "assess_issue", "arguments": "{\"number\": 1}"}}]
    def handler(request):
        return httpx.Response(200, json=completion(None, calls))
    reply = client_with(handler).chat(MESSAGES, tools=TOOLS)
    assert reply.content == ""
    assert (reply.tool_calls[0].id, reply.tool_calls[0].name) == ("call_1", "assess_issue")
    assert reply.tool_calls[0].arguments == '{"number": 1}'

def test_missing_rate_headers_are_fine():
    reply = client_with(lambda r: httpx.Response(200, json=completion())).chat(MESSAGES)
    assert reply.remaining_tokens is None and reply.reset_seconds is None


# ---------- What we send ----------

def test_request_shape_and_reasoning_option():
    seen = {}
    def handler(request):
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers["authorization"]
        return httpx.Response(200, json=completion())
    client_with(handler, reasoning_effort="low").chat(MESSAGES, tools=TOOLS)
    body = seen["body"]
    assert body["model"] == "test-model" and body["temperature"] == 0
    assert body["tool_choice"] == "auto" and "tools" in body
    assert body["reasoning_effort"] == "low"
    assert "max_completion_tokens" in body
    assert seen["auth"] == f"Bearer {SECRET}"

def test_no_tools_means_no_tool_fields_and_no_reasoning_by_default():
    seen = {}
    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=completion())
    client_with(handler).chat(MESSAGES, tools=None)
    assert "tools" not in seen["body"] and "tool_choice" not in seen["body"]
    assert "reasoning_effort" not in seen["body"]


# ---------- Errors ----------

@pytest.mark.parametrize("status,body,expected", [
    (401, {"error": {"message": f"bad {SECRET}", "code": "invalid_api_key"}}, FatalLLMError),
    (403, {"error": {"message": "no"}}, FatalLLMError),
    (404, {"error": {"message": "no model"}}, FatalLLMError),
    (413, {"error": {"message": "too big"}}, FatalLLMError),
    (422, {"error": {"message": "weird"}}, FatalLLMError),
    (400, {"error": {"message": "bad", "code": "invalid_request"}}, FatalLLMError),
    (400, {"error": {"message": "Failed to call a function", "code": "tool_use_failed",
                     "failed_generation": "<garbage>"}}, BadToolCall),
    (500, {"error": {"message": "oops"}}, TransientError),
    (503, {"error": {"message": "busy"}}, TransientError),
])
def test_error_mapping(status, body, expected):
    def handler(request):
        return httpx.Response(status, json=body)
    with pytest.raises(expected) as e:
        client_with(handler).chat(MESSAGES)
    assert SECRET not in str(e.value) and "garbage" not in str(e.value)

def test_too_large_message_mentions_size():
    with pytest.raises(FatalLLMError, match="too large"):
        client_with(lambda r: httpx.Response(413, json={"error": {"message": "x"}})).chat(MESSAGES)

def test_rate_limit_uses_retry_after_header():
    def handler(request):
        return httpx.Response(429, json={"error": {"message": "slow"}}, headers={"retry-after": "7"})
    with pytest.raises(RateLimited) as e:
        client_with(handler).chat(MESSAGES)
    assert e.value.retry_after == 7.0

def test_rate_limit_falls_back_to_token_reset_header():
    def handler(request):
        return httpx.Response(429, json={"error": {"message": "slow"}},
                              headers={"x-ratelimit-reset-tokens": "2.5s"})
    with pytest.raises(RateLimited) as e:
        client_with(handler).chat(MESSAGES)
    assert e.value.retry_after == pytest.approx(2.5)

def test_rate_limit_without_headers_has_no_hint():
    with pytest.raises(RateLimited) as e:
        client_with(lambda r: httpx.Response(429, json={"error": {"message": "x"}})).chat(MESSAGES)
    assert e.value.retry_after is None

def test_connection_error_is_transient():
    def handler(request):
        raise httpx.ConnectError("down", request=request)
    with pytest.raises(TransientError):
        client_with(handler).chat(MESSAGES)

def test_timeout_is_transient():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)
    with pytest.raises(TransientError):
        client_with(handler).chat(MESSAGES)

def test_no_choices_is_transient():
    body = {"id": "x", "object": "chat.completion", "created": 0, "model": "m", "choices": []}
    with pytest.raises(TransientError):
        client_with(lambda r: httpx.Response(200, json=body)).chat(MESSAGES)