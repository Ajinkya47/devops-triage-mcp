import asyncio
import json
import re
import time
from dataclasses import dataclass, field

import tool_bridge as tb
from actions import strip_control
from llm_client import (BadToolCall, LLMError, RateLimited, TransientError)

ACTION_ID = re.compile(r"^[0-9a-f]{8}$")
STUB = "[older tool output omitted to save tokens]"
WRAP_UP = ("You are out of steps or budget. Answer now using only what you already have. "
           "Say clearly what you could not check.")
NUDGE_EMPTY = "Your reply was empty. Give your answer now."
NUDGE_MALFORMED = ("Your last tool call was malformed. Call the tool again with valid JSON "
                   "arguments, or answer without tools.")


@dataclass(frozen=True)
class Limits:
    max_steps: int = 8                 # model calls, including the final answer
    max_tool_calls: int = 12
    max_calls_per_step: int = 4
    max_total_tokens: int = 30000      # prompt + completion, summed over the run
    max_seconds: float = 240.0
    max_result_chars: int = 4000
    prompt_budget_tokens: int = 5500   # keeps prompt + reply under an 8,000-token minute
    max_proposals: int = 3
    max_repeat_refusals: int = 3
    max_refused_calls: int = 6
    max_empty_retries: int = 1
    max_malformed_retries: int = 2
    max_transient_retries: int = 2
    max_rate_waits: int = 4
    max_wait_seconds: float = 65.0
    default_rate_wait: float = 20.0


@dataclass
class RunResult:
    answer: str = ""
    stop_reason: str = ""
    error: str = ""
    steps: int = 0
    tool_calls: list = field(default_factory=list)
    refused_calls: int = 0
    malformed_replies: int = 0
    empty_replies: int = 0
    rate_waits: int = 0
    pace_waits: int = 0
    seconds_waited: float = 0.0
    compacted: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    proposals: list = field(default_factory=list)
    injection_flags_seen: list = field(default_factory=list)
    unverified_claims: list = field(default_factory=list)
    elapsed: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class _Counters:
    def __init__(self):
        self.seen: set = set()
        self.executed = 0
        self.proposal_attempts = 0
        self.repeat_refusals = 0


# ------------------------------------------------------------------ helpers

def _clip(text, limit: int) -> str:
    return strip_control(str(text))[:limit]


def _clip_args(args: dict) -> dict:
    return {k: (_clip(v, 200) if isinstance(v, str) else v) for k, v in args.items()}


def _is_json_object(text, max_chars: int) -> bool:
    if not isinstance(text, str) or len(text) > max_chars:
        return False
    try:
        return isinstance(json.loads(text), dict)
    except ValueError:
        return False


def _extract_action_id(raw_text: str) -> str | None:
    try:
        data = json.loads(raw_text)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("status") != "pending_human_approval":
        return None
    action_id = data.get("action_id")
    return action_id if isinstance(action_id, str) and ACTION_ID.match(action_id) else None


def _message_tokens(message: dict) -> int:
    return tb.estimate_tokens(json.dumps(message, ensure_ascii=False))


def compact_history(messages: list, max_tokens: int, tools_tokens: int = 0,
                    keep_last: int = 2) -> int:
    """Swap the oldest tool outputs for a stub until the estimate fits. Returns how many."""
    total = tools_tokens + sum(_message_tokens(m) for m in messages)
    candidates = [i for i, m in enumerate(messages)
                  if m.get("role") == "tool" and m.get("content") != STUB]
    protected = set(candidates[-keep_last:]) if keep_last > 0 else set()
    replaced = 0
    for i in candidates:
        if total <= max_tokens:
            break
        if i in protected:
            continue
        before = _message_tokens(messages[i])
        messages[i]["content"] = STUB
        total -= max(0, before - _message_tokens(messages[i]))
        replaced += 1
    return replaced


# ------------------------------------------------------------ honesty check

_CLAIM = re.compile(
    r"\b(?:i|we)(?:'ve|\s+have|\s+had)?\s+(?:just\s+|now\s+|successfully\s+)*"
    r"(?:posted|commented|assigned|closed|approved|merged|executed|submitted)\b"
    r"|\b(?:has|have|had|was|were|is|are)\s+(?:now\s+|successfully\s+|already\s+)*(?:been\s+)?"
    r"(?:posted|assigned|approved|executed|submitted)\b"
    r"|\bsuccessfully\s+(?:posted|assigned|closed|approved|executed)\b",
    re.I)
_NEGATION = re.compile(
    r"\b(?:not|never|nothing|no|yet|until|once|after|before|pending|awaiting|waiting|"
    r"will|would|cannot|unable|if|when|should|could|needs?|requires?|must)\b|n't\b",
    re.I)


def check_claims(answer: str) -> list:
    """Sentences that claim an action was done. The agent cannot do any, so these are suspect."""
    claims = []
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", answer or ""):
        s = sentence.strip()
        if s and _CLAIM.search(s) and not _NEGATION.search(s):
            claims.append(strip_control(s)[:160])
    return claims[:5]


# ------------------------------------------------------------------- pacing

class Pacer:
    """Waits for the token window to refill instead of running into rate-limit errors."""

    def __init__(self, safety_tokens: int = 300, max_wait: float = 65.0):
        self.remaining: int | None = None
        self.reset: float | None = None
        self.safety = safety_tokens
        self.max_wait = max_wait

    def note(self, reply) -> None:
        if reply.remaining_tokens is not None:
            self.remaining = reply.remaining_tokens
            self.reset = reply.reset_seconds

    def wait_needed(self, needed_tokens: int) -> float:
        if self.remaining is None or self.remaining >= needed_tokens + self.safety:
            return 0.0
        return min((self.reset if self.reset is not None else 5.0) + 0.5, self.max_wait)

    def waited(self) -> None:
        self.remaining = None


# ------------------------------------------------------------ one tool call

async def _handle_call(call, *, exposed, call_tool, limits, counters, result, flags_seen) -> str:
    entry = {"name": tb._clip_name(call.name), "status": "refused", "arguments": {},
             "chars": 0, "truncated": False, "clamped": [], "flags": [], "reason": ""}
    result.tool_calls.append(entry)

    def refuse(reason: str) -> str:
        entry["status"], entry["reason"] = "refused", reason
        result.refused_calls += 1
        return f"REFUSED: {reason}"

    try:
        prepared = tb.prepare_call(call.name, call.arguments, exposed)
    except tb.ToolRefused as exc:
        return refuse(str(exc))

    entry["name"] = prepared.name
    entry["arguments"] = _clip_args(prepared.arguments)
    entry["clamped"] = list(prepared.clamped)

    key = (prepared.name, json.dumps(prepared.arguments, sort_keys=True, default=str))
    if key in counters.seen:
        counters.repeat_refusals += 1
        return refuse("You already made this exact call. Use its earlier result or try something different.")
    if counters.executed >= limits.max_tool_calls:
        return refuse("Tool call limit reached. Answer with what you have.")
    is_proposal = prepared.name in tb.PROPOSE_TOOLS
    if is_proposal and counters.proposal_attempts >= limits.max_proposals:
        return refuse("Proposal limit reached for this run.")

    counters.seen.add(key)
    counters.executed += 1
    if is_proposal:
        counters.proposal_attempts += 1

    try:
        raw_text, is_error = await call_tool(prepared.name, prepared.arguments)
    except Exception as exc:                        # never leak exception text
        raw_text, is_error = f"tool call failed ({type(exc).__name__})", True
    if is_error:
        counters.seen.discard(key)                  # one retry after an error is allowed

    shaped = tb.shape_for_model(raw_text, is_error, limits.max_result_chars)
    entry.update(status="error" if is_error else "ok", chars=shaped["chars"],
                 truncated=shaped["truncated"], flags=shaped["injection_flags"])
    flags_seen.update(shaped["injection_flags"])

    if is_proposal and not is_error:
        action_id = _extract_action_id(raw_text)
        if action_id:
            result.proposals.append(action_id)

    text = shaped["text"]
    if prepared.clamped:
        text = f"[host note: {', '.join(prepared.clamped)} adjusted to save tokens]\n" + text
    return text


# ----------------------------------------------------------------- the loop

async def run_agent(request, *, llm, call_tool, exposed, system_prompt, limits=None,
                    prior_turns=(), sleep=asyncio.sleep, now=time.monotonic) -> RunResult:
    limits = limits or Limits()
    result = RunResult()
    started = now()

    tools = tb.to_openai_tools(exposed)
    tools_tokens = tb.estimate_tokens(json.dumps(tools))
    completion_cap = int(getattr(llm, "max_completion_tokens", 1200))
    pacer = Pacer(max_wait=limits.max_wait_seconds)
    counters = _Counters()
    flags_seen: set = set()

    messages = [{"role": "system", "content": system_prompt}]
    for user_text, answer_text in list(prior_turns)[-3:]:
        messages.append({"role": "user", "content": _clip(user_text, 1000)})
        messages.append({"role": "assistant", "content": _clip(answer_text, 1500)})
    messages.append({"role": "user", "content": _clip(request, 2000)})

    wrap_up = force_wrap = False
    empty_retries = malformed_retries = transient_retries = 0

    while True:
        elapsed = now() - started
        if elapsed > limits.max_seconds:
            result.stop_reason = "timeout"
            break
        if result.total_tokens >= limits.max_total_tokens:
            result.stop_reason = "token_budget"
            break
        if result.steps >= limits.max_steps:
            result.stop_reason = "max_steps"
            break

        if not wrap_up and (force_wrap
                            or result.steps >= limits.max_steps - 1
                            or result.total_tokens >= 0.75 * limits.max_total_tokens
                            or elapsed >= 0.75 * limits.max_seconds):
            wrap_up = True
            messages.append({"role": "user", "content": WRAP_UP})

        use_tools = not wrap_up
        result.compacted += compact_history(
            messages, limits.prompt_budget_tokens, tools_tokens if use_tools else 0)
        needed = (sum(_message_tokens(m) for m in messages)
                  + (tools_tokens if use_tools else 0) + completion_cap)
        wait = pacer.wait_needed(needed)
        if wait:
            result.pace_waits += 1
            result.seconds_waited += wait
            await sleep(wait)
            pacer.waited()

        try:
            reply = await asyncio.to_thread(llm.chat, messages, tools if use_tools else None)
        except RateLimited as exc:
            result.rate_waits += 1
            if result.rate_waits > limits.max_rate_waits:
                result.stop_reason, result.error = "rate_limited", str(exc)
                break
            hint = exc.retry_after if exc.retry_after is not None else limits.default_rate_wait
            pause = min(hint, limits.max_wait_seconds) + 0.5
            result.seconds_waited += pause
            await sleep(pause)
            continue
        except BadToolCall:
            result.malformed_replies += 1
            if malformed_retries >= limits.max_malformed_retries:
                result.stop_reason = "malformed_tool_calls"
                break
            malformed_retries += 1
            messages.append({"role": "user", "content": NUDGE_MALFORMED})
            continue
        except TransientError as exc:
            if transient_retries >= limits.max_transient_retries:
                result.stop_reason, result.error = "provider_error", str(exc)
                break
            transient_retries += 1
            pause = 2.0 * transient_retries
            result.seconds_waited += pause
            await sleep(pause)
            continue
        except LLMError as exc:
            result.stop_reason, result.error = "llm_error", str(exc)
            break

        transient_retries = 0
        result.steps += 1
        result.prompt_tokens += reply.prompt_tokens
        result.completion_tokens += reply.completion_tokens
        pacer.note(reply)

        if reply.tool_calls and use_tools:
            batch = reply.tool_calls[: limits.max_calls_per_step]
            result.refused_calls += len(reply.tool_calls) - len(batch)
            assistant_calls, tool_messages = [], []
            for index, call in enumerate(batch):
                call_id = (call.id or f"call_{result.steps}_{index}")[:64]
                args_text = call.arguments if _is_json_object(call.arguments, 2000) else "{}"
                assistant_calls.append({
                    "id": call_id, "type": "function",
                    "function": {"name": tb._clip_name(call.name) or "unknown",
                                 "arguments": args_text}})
                text = await _handle_call(call, exposed=exposed, call_tool=call_tool,
                                          limits=limits, counters=counters, result=result,
                                          flags_seen=flags_seen)
                tool_messages.append({"role": "tool", "tool_call_id": call_id, "content": text})
            messages.append({"role": "assistant", "content": reply.content or "",
                             "tool_calls": assistant_calls})
            messages.extend(tool_messages)

            if counters.repeat_refusals >= limits.max_repeat_refusals:
                result.stop_reason = "repeat_loop"
                break
            if result.refused_calls >= limits.max_refused_calls:
                result.stop_reason = "too_many_refusals"
                break
            if counters.executed >= limits.max_tool_calls:
                force_wrap = True
            continue

        content = (reply.content or "").strip()
        if not content:
            result.empty_replies += 1
            if empty_retries >= limits.max_empty_retries:
                result.stop_reason = "empty_answer"
                break
            empty_retries += 1
            messages.append({"role": "user", "content": NUDGE_EMPTY})
            continue

        result.answer = content
        result.stop_reason = "answered"
        break

    result.injection_flags_seen = sorted(flags_seen)
    result.unverified_claims = check_claims(result.answer)
    result.elapsed = now() - started
    return result