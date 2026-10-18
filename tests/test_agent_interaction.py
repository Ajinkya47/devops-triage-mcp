import asyncio
import json
import sqlite3

import agent
import agent_core as core
import tool_bridge as tb
from llm_client import LLMReply, ToolCallRequest


class ScriptedLLM:
    max_completion_tokens = 500

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def chat(self, messages, tools=None):
        self.calls.append(json.loads(json.dumps(messages)))
        return self.script.pop(0)


def audit_rows(tmp_path):
    path = tmp_path / "audit.db"
    if not path.exists():
        return []
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT tool, status FROM audit_log").fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def run_with_real_server(tmp_path, llm, mode, request="how many issues are stored?"):
    extra = {"BUGS_DB_PATH": str(tmp_path / "bugs.db"),
             "ACTIONS_DB_PATH": str(tmp_path / "actions.db"),
             "AUDIT_DB_PATH": str(tmp_path / "audit.db")}

    async def main():
        async with agent.open_session(extra) as session:
            specs = await agent.load_specs(session)
            res = await core.run_agent(
                request, llm=llm, call_tool=agent.make_call_tool(session),
                exposed=tb.select_tools(specs, mode), system_prompt="SYS")
            return specs, res

    return asyncio.run(asyncio.wait_for(main(), 90))


def test_real_server_round_trip(tmp_path):
    llm = ScriptedLLM([
        LLMReply(tool_calls=[ToolCallRequest(
            "c1", "query_bugs_db", json.dumps({"sql": "SELECT COUNT(*) AS n FROM issues"}))],
            prompt_tokens=100, completion_tokens=10),
        LLMReply(content="The database has 0 issues.", prompt_tokens=200, completion_tokens=10),
    ])
    specs, res = run_with_real_server(tmp_path, llm, tb.MODE_READ)
    assert {s.name for s in specs} == tb.READ_TOOLS | tb.PROPOSE_TOOLS
    assert res.stop_reason == "answered"
    assert res.tool_calls[0]["status"] == "ok"
    assert '"rows"' in llm.calls[1][-1]["content"]                # the result reached the model
    assert ("query_bugs_db", "ok") in audit_rows(tmp_path)       # the real server logged the call


def test_bad_sql_is_rejected_by_the_real_server_and_logged(tmp_path):
    llm = ScriptedLLM([
        LLMReply(tool_calls=[ToolCallRequest("c1", "query_bugs_db",
                                             json.dumps({"sql": "DROP TABLE issues"}))]),
        LLMReply(content="The query was rejected."),
    ])
    _, res = run_with_real_server(tmp_path, llm, tb.MODE_READ)
    assert res.tool_calls[0]["status"] == "error"
    assert "rejected" in llm.calls[1][-1]["content"].lower()
    assert ("query_bugs_db", "error") in audit_rows(tmp_path)


def test_read_only_mode_blocks_proposals_before_the_server_sees_them(tmp_path):
    llm = ScriptedLLM([
        LLMReply(tool_calls=[ToolCallRequest("c1", "propose_comment", json.dumps(
            {"repo": "me/r", "number": 1, "text": "hello"}))]),
        LLMReply(content="I cannot post anything."),
    ])
    _, res = run_with_real_server(tmp_path, llm, tb.MODE_READ)
    assert res.refused_calls == 1 and res.proposals == []
    assert all(tool != "propose_comment" for tool, _ in audit_rows(tmp_path))