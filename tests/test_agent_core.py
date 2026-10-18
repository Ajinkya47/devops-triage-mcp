import asyncio
import json
import re
import time
from pathlib import Path

import pytest

import agent_core as core
import tool_bridge as tb
from llm_client import (BadToolCall, FatalLLMError, LLMReply, RateLimited,
                        ToolCallRequest, TransientError)
from tool_bridge import ToolSpec

REPO = "me/triage-test-repo"
LIST = {"repo": REPO, "limit": 5}


def _spec(name, props, required):
    return ToolSpec(name, f"{name} description",
                    {"type": "object", "properties": props, "required": required})


ALL_SPECS = [
    _spec("list_issues", {"repo": {"type": "string"}, "limit": {"type": "integer"}}, ["repo"]),
    _spec("assess_issue", {"repo": {"type": "string"}, "number": {"type": "integer"}},
          ["repo", "number"]),
    _spec("propose_comment", {"repo": {"type": "string"}, "number": {"type": "integer"},
                              "text": {"type": "string"}}, ["repo", "number", "text"]),
]


def assess(n):
    return {"repo": REPO, "number": n}

def propose(text, n=1):
    return {"repo": REPO, "number": n, "text": text}

PENDING = '{"action_id": "c1711f12", "status": "pending_human_approval"}'


def reply(content="", calls=(), pt=100, ct=20, remaining=None, reset=None):
    return LLMReply(content=content, tool_calls=list(calls), prompt_tokens=pt,
                    completion_tokens=ct, remaining_tokens=remaining, reset_seconds=reset)

def tc(name, args=None, id="c1"):
    return ToolCallRequest(id=id, name=name,
                           arguments=json.dumps(args if args is not None else {}))


class FakeLLM:
    max_completion_tokens = 500

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def chat(self, messages, tools=None):
        self.calls.append({"messages": json.loads(json.dumps(messages)), "tools": tools})
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class Tools:
    def __init__(self, outputs=None):
        self.calls = []
        self.outputs = outputs or {}

    async def __call__(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        out = self.outputs.get(name, '{"ok": true}')
        if isinstance(out, Exception):
            raise out
        if callable(out):
            out = out(arguments)
        if isinstance(out, tuple):
            return out
        return out, False


class FakeClock:
    def __init__(self):
        self.t = 0.0
    def __call__(self):
        return self.t
    def advance(self, seconds):
        self.t += seconds


def go(llm, tools, *, request="rank the issues", mode=tb.MODE_READ, limits=None,
       prior=(), clock=None, sleeps=None):
    exposed = tb.select_tools(ALL_SPECS, mode)
    log = sleeps if sleeps is not None else []

    async def fake_sleep(seconds):
        log.append(seconds)

    return asyncio.run(core.run_agent(
        request, llm=llm, call_tool=tools, exposed=exposed, system_prompt="SYS",
        limits=limits, prior_turns=prior, sleep=fake_sleep, now=clock or time.monotonic))


# ---------- Basic flow ----------

def test_direct_answer_without_tools():
    llm, tools = FakeLLM([reply("Top issue is #1", pt=500, ct=50)]), Tools()
    res = go(llm, tools)
    assert res.stop_reason == "answered" and res.answer == "Top issue is #1"
    assert res.steps == 1 and res.total_tokens == 550 and tools.calls == []

def test_system_prompt_first_and_request_last():
    llm = FakeLLM([reply("ok")])
    go(llm, Tools(), request="hello there")
    msgs = llm.calls[0]["messages"]
    assert msgs[0] == {"role": "system", "content": "SYS"}
    assert msgs[-1] == {"role": "user", "content": "hello there"}

def test_tool_call_then_answer_feeds_the_result_back():
    llm = FakeLLM([reply(calls=[tc("assess_issue", assess(1))]), reply("done")])
    tools = Tools({"assess_issue": '{"evidence_score": 70}'})
    res = go(llm, tools)
    assert tools.calls == [("assess_issue", assess(1))]
    second = llm.calls[1]["messages"]
    assert second[-1]["role"] == "tool" and "evidence_score" in second[-1]["content"]
    assert second[-2]["tool_calls"][0]["id"] == second[-1]["tool_call_id"]
    assert res.stop_reason == "answered" and res.tool_calls[0]["status"] == "ok"

def test_prior_turns_are_included_and_clipped():
    llm = FakeLLM([reply("ok")])
    go(llm, Tools(), prior=[("q1", "a" * 5000)])
    msgs = llm.calls[0]["messages"]
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "user"]
    assert len(msgs[2]["content"]) == 1500


# ---------- Refusals ----------

def test_unknown_tool_is_refused_and_the_model_recovers():
    llm = FakeLLM([reply(calls=[tc("delete_repo", {})]), reply("sorry")])
    tools = Tools()
    res = go(llm, tools)
    assert tools.calls == [] and res.refused_calls == 1
    assert llm.calls[1]["messages"][-1]["content"].startswith("REFUSED:")
    assert res.tool_calls[0]["status"] == "refused" and res.stop_reason == "answered"

def test_propose_tool_is_refused_in_read_only_mode():
    llm = FakeLLM([reply(calls=[tc("propose_comment", propose("hi"))]), reply("ok")])
    tools = Tools()
    res = go(llm, tools, mode=tb.MODE_READ)
    assert tools.calls == [] and res.refused_calls == 1 and res.proposals == []

def test_invalid_json_arguments_refused_and_not_replayed():
    bad = ToolCallRequest(id="c1", name="list_issues", arguments="{bad")
    llm = FakeLLM([reply(calls=[bad]), reply("ok")])
    res = go(llm, Tools())
    history = llm.calls[1]["messages"]
    assistant = next(m for m in history if m["role"] == "assistant")
    assert assistant["tool_calls"][0]["function"]["arguments"] == "{}"
    assert res.refused_calls == 1

def test_duplicate_call_is_refused():
    llm = FakeLLM([reply(calls=[tc("list_issues", LIST, "c1")]),
                   reply(calls=[tc("list_issues", LIST, "c2")]), reply("done")])
    tools = Tools()
    res = go(llm, tools)
    assert len(tools.calls) == 1 and res.refused_calls == 1
    assert "already made" in llm.calls[2]["messages"][-1]["content"]

def test_call_may_be_retried_once_after_a_tool_error():
    llm = FakeLLM([reply(calls=[tc("list_issues", LIST, "c1")]),
                   reply(calls=[tc("list_issues", LIST, "c2")]), reply("done")])
    outputs = iter([("boom", True), ('{"ok": true}', False)])
    tools = Tools({"list_issues": lambda a: next(outputs)})
    res = go(llm, tools)
    assert len(tools.calls) == 2 and res.refused_calls == 0

def test_repeated_duplicates_stop_the_run():
    script = [reply(calls=[tc("list_issues", LIST, f"c{i}")]) for i in range(4)]
    res = go(FakeLLM(script), Tools(), limits=core.Limits(max_steps=10))
    assert res.stop_reason == "repeat_loop"

def test_too_many_refusals_stop_the_run():
    script = [reply(calls=[tc("nope", {}, f"c{i}")]) for i in range(3)]
    res = go(FakeLLM(script), Tools(), limits=core.Limits(max_refused_calls=3, max_steps=10))
    assert res.stop_reason == "too_many_refusals"

def test_extra_calls_in_one_step_are_dropped():
    calls = [tc("assess_issue", assess(n), f"c{n}") for n in range(1, 5)]
    llm = FakeLLM([reply(calls=calls), reply("ok")])
    tools = Tools()
    res = go(llm, tools, limits=core.Limits(max_calls_per_step=2))
    assert len(tools.calls) == 2 and res.refused_calls == 2
    assistant = next(m for m in llm.calls[1]["messages"] if m["role"] == "assistant")
    assert len(assistant["tool_calls"]) == 2


# ---------- Proposals ----------

def test_proposal_id_is_captured():
    llm = FakeLLM([reply(calls=[tc("propose_comment", propose("Please add logs"))]), reply("ok")])
    res = go(llm, Tools({"propose_comment": PENDING}), mode=tb.MODE_PROPOSE)
    assert res.proposals == ["c1711f12"]

def test_malformed_action_ids_are_ignored():
    bad = '{"action_id": "../../x", "status": "pending_human_approval"}'
    llm = FakeLLM([reply(calls=[tc("propose_comment", propose("hi"))]), reply("ok")])
    res = go(llm, Tools({"propose_comment": bad}), mode=tb.MODE_PROPOSE)
    assert res.proposals == []

def test_failed_proposal_is_not_recorded():
    llm = FakeLLM([reply(calls=[tc("propose_comment", propose("hi"))]), reply("ok")])
    tools = Tools({"propose_comment": ("Proposal refused: nope", True)})
    res = go(llm, tools, mode=tb.MODE_PROPOSE)
    assert res.proposals == [] and res.tool_calls[0]["status"] == "error"

def test_proposal_limit():
    calls = [tc("propose_comment", propose(f"note {i}"), f"c{i}") for i in range(3)]
    llm = FakeLLM([reply(calls=calls), reply("ok")])
    tools = Tools({"propose_comment": PENDING})
    res = go(llm, tools, mode=tb.MODE_PROPOSE, limits=core.Limits(max_proposals=2))
    assert len(tools.calls) == 2 and res.refused_calls == 1


# ---------- Limits and wrap-up ----------

def test_last_step_removes_tools_and_asks_for_an_answer():
    llm = FakeLLM([reply(calls=[tc("assess_issue", assess(1))]), reply("final answer")])
    res = go(llm, Tools(), limits=core.Limits(max_steps=2))
    assert llm.calls[0]["tools"] is not None and llm.calls[1]["tools"] is None
    assert llm.calls[1]["messages"][-1]["content"] == core.WRAP_UP
    assert res.stop_reason == "answered" and res.answer == "final answer"

def test_tool_call_budget_triggers_wrap_up():
    llm = FakeLLM([reply(calls=[tc("assess_issue", assess(1))]), reply("final")])
    res = go(llm, Tools(), limits=core.Limits(max_tool_calls=1))
    assert llm.calls[1]["tools"] is None and res.stop_reason == "answered"

def test_tool_calls_in_the_final_step_are_ignored():
    llm = FakeLLM([reply(calls=[tc("assess_issue", assess(1))]),
                   reply(calls=[tc("assess_issue", assess(2), "c2")])])
    tools = Tools()
    res = go(llm, tools, limits=core.Limits(max_steps=2))
    assert len(tools.calls) == 1
    assert res.stop_reason == "max_steps" and res.answer == ""

def test_token_budget_stops_the_run():
    llm = FakeLLM([reply(calls=[tc("assess_issue", assess(1))], pt=900, ct=200)])
    res = go(llm, Tools(), limits=core.Limits(max_total_tokens=1000))
    assert res.stop_reason == "token_budget"

def test_most_of_the_token_budget_triggers_wrap_up():
    llm = FakeLLM([reply(calls=[tc("assess_issue", assess(1))], pt=700, ct=100), reply("final")])
    res = go(llm, Tools(), limits=core.Limits(max_total_tokens=1000))
    assert llm.calls[1]["tools"] is None and res.stop_reason == "answered"

def test_timeout():
    clock = FakeClock()
    tools = Tools({"assess_issue": lambda a: clock.advance(500) or '{"ok": true}'})
    llm = FakeLLM([reply(calls=[tc("assess_issue", assess(1))]), reply("never reached")])
    res = go(llm, tools, clock=clock, limits=core.Limits(max_seconds=100))
    assert res.stop_reason == "timeout" and len(llm.script) == 1


# ---------- Reply problems ----------

def test_empty_reply_is_retried_once():
    llm = FakeLLM([reply(""), reply("ok")])
    res = go(llm, Tools())
    assert res.stop_reason == "answered" and res.empty_replies == 1
    assert llm.calls[1]["messages"][-1]["content"] == core.NUDGE_EMPTY

def test_two_empty_replies_stop_the_run():
    res = go(FakeLLM([reply(""), reply("")]), Tools())
    assert res.stop_reason == "empty_answer" and res.empty_replies == 2 and res.answer == ""

def test_malformed_tool_call_is_retried():
    llm = FakeLLM([BadToolCall("x"), reply("ok")])
    res = go(llm, Tools())
    assert res.stop_reason == "answered" and res.malformed_replies == 1
    assert llm.calls[1]["messages"][-1]["content"] == core.NUDGE_MALFORMED

def test_too_many_malformed_calls_stop_the_run():
    res = go(FakeLLM([BadToolCall("x")] * 3), Tools())
    assert res.stop_reason == "malformed_tool_calls" and res.malformed_replies == 3


# ---------- Provider errors ----------

def test_rate_limit_waits_for_the_hint_then_continues():
    sleeps = []
    res = go(FakeLLM([RateLimited("x", 7.0), reply("ok")]), Tools(), sleeps=sleeps)
    assert sleeps == [7.5] and res.rate_waits == 1 and res.stop_reason == "answered"

def test_rate_limit_without_hint_uses_the_default():
    sleeps = []
    go(FakeLLM([RateLimited("x", None), reply("ok")]), Tools(), sleeps=sleeps)
    assert sleeps == [20.5]

def test_rate_limit_wait_is_capped():
    sleeps = []
    go(FakeLLM([RateLimited("x", 500.0), reply("ok")]), Tools(), sleeps=sleeps)
    assert sleeps == [65.5]

def test_repeated_rate_limits_stop_the_run():
    res = go(FakeLLM([RateLimited("x", 1.0)] * 3), Tools(), limits=core.Limits(max_rate_waits=2))
    assert res.stop_reason == "rate_limited"

def test_transient_error_is_retried_after_a_pause():
    sleeps = []
    res = go(FakeLLM([TransientError("t"), reply("ok")]), Tools(), sleeps=sleeps)
    assert sleeps == [2.0] and res.stop_reason == "answered"

def test_repeated_transient_errors_stop_the_run():
    res = go(FakeLLM([TransientError("t")] * 3), Tools(),
             limits=core.Limits(max_transient_retries=2))
    assert res.stop_reason == "provider_error"

def test_fatal_error_stops_with_a_message():
    res = go(FakeLLM([FatalLLMError("The API key was rejected (401).")]), Tools())
    assert res.stop_reason == "llm_error" and "401" in res.error


# ---------- Pacing and trimming ----------

def test_low_token_window_causes_a_wait():
    sleeps = []
    llm = FakeLLM([reply(calls=[tc("assess_issue", assess(1))], remaining=100, reset=3.0),
                   reply("ok")])
    res = go(llm, Tools(), sleeps=sleeps)
    assert sleeps == [3.5] and res.pace_waits == 1

def test_plenty_of_tokens_means_no_wait():
    sleeps = []
    llm = FakeLLM([reply(calls=[tc("assess_issue", assess(1))], remaining=7000, reset=1.0),
                   reply("ok")])
    go(llm, Tools(), sleeps=sleeps)
    assert sleeps == []

def test_compact_history_replaces_oldest_and_keeps_the_last():
    messages = [{"role": "system", "content": "s"}] + [
        {"role": "tool", "tool_call_id": f"c{i}", "content": "x" * 4000} for i in range(3)]
    replaced = core.compact_history(messages, max_tokens=1500, keep_last=1)
    assert replaced == 2
    assert messages[1]["content"] == core.STUB and messages[2]["content"] == core.STUB
    assert messages[3]["content"] == "x" * 4000
    assert messages[1]["tool_call_id"] == "c0"

def test_compact_history_does_nothing_when_small():
    messages = [{"role": "tool", "tool_call_id": "c", "content": "short"}]
    assert core.compact_history(messages, max_tokens=1500) == 0

def test_long_runs_get_older_outputs_replaced():
    script = [reply(calls=[tc("assess_issue", assess(n), f"c{n}")]) for n in (1, 2, 3)]
    script.append(reply("ok"))
    tools = Tools({"assess_issue": "x" * 3500})
    res = go(FakeLLM(script), tools, limits=core.Limits(prompt_budget_tokens=1200, max_steps=10))
    assert res.compacted >= 1


# ---------- Tool results and errors ----------

def test_tool_exception_text_never_reaches_the_model():
    llm = FakeLLM([reply(calls=[tc("list_issues", LIST)]), reply("ok")])
    tools = Tools({"list_issues": RuntimeError("secret ghp_" + "A" * 30)})
    res = go(llm, tools)
    content = llm.calls[1]["messages"][-1]["content"]
    assert "RuntimeError" in content and "ghp_" not in content
    assert res.tool_calls[0]["status"] == "error"

def test_big_results_are_cut_and_flags_are_recorded():
    big = json.dumps({"body": "x" * 9000, "injection_flags": ["role_override"]})
    llm = FakeLLM([reply(calls=[tc("list_issues", LIST)]), reply("ok")])
    res = go(llm, Tools({"list_issues": big}))
    assert res.injection_flags_seen == ["role_override"]
    assert res.tool_calls[0]["truncated"] is True
    assert "role_override" in llm.calls[1]["messages"][-1]["content"]

def test_argument_clamps_are_applied_and_reported():
    llm = FakeLLM([reply(calls=[tc("list_issues", {"repo": REPO})]), reply("ok")])
    tools = Tools()
    res = go(llm, tools)
    assert tools.calls[0][1]["limit"] == 10
    assert res.tool_calls[0]["clamped"] == ["limit"]
    assert "host note" in llm.calls[1]["messages"][-1]["content"]


# ---------- Honesty check ----------

CLAIMS = [
    "I posted the comment on issue 3.",
    "Done! I have assigned the issue to alice.",
    "The comment has been posted successfully.",
    "I've successfully assigned it.",
    "Comment was successfully posted.",
]
NOT_CLAIMS = [
    "Nothing has been posted; it is waiting for your approval (id c1711f12).",
    "The comment has not been posted yet.",
    "I can't approve or post anything.",
    "Once you approve it, the comment will be posted.",
    "A proposal was created and is waiting for approval.",
    "I drafted a comment for your approval.",
    "Issue 5 is open and has 3 comments.",
]

@pytest.mark.parametrize("text", CLAIMS)
def test_action_claims_are_flagged(text):
    assert core.check_claims(text) != []

@pytest.mark.parametrize("text", NOT_CLAIMS)
def test_honest_statements_are_not_flagged(text):
    assert core.check_claims(text) == []

def test_run_reports_unverified_claims():
    res = go(FakeLLM([reply("I posted the comment on issue 3.")]), Tools())
    assert len(res.unverified_claims) == 1

def test_run_with_an_honest_answer_has_no_claims():
    res = go(FakeLLM([reply("Nothing was posted. It is waiting for your approval.")]), Tools())
    assert res.unverified_claims == []


# ---------- No approval path in agent code ----------

def test_agent_code_has_no_approval_path():
    root = Path(core.__file__).parent
    files = set(root.glob("agent*.py")) | {root / "llm_client.py"}
    assert files
    for path in files:
        source = path.read_text(encoding="utf-8")
        for forbidden in ("approve_action", "reject_action", "mark_executed",
                          "mark_failed", "GITHUB_WRITE_TOKEN"):
            assert forbidden not in source, f"{path.name} mentions {forbidden}"
        assert not re.search(r"^\s*(?:import|from)\s+(?:approve|executor)\b",
                             source, re.MULTILINE), f"{path.name} imports the approval code"