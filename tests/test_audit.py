import asyncio
import inspect
import json
import sqlite3

import pytest

import audit


@pytest.fixture(autouse=True)
def temp_audit_db(tmp_path, monkeypatch):
    path = tmp_path / "audit.db"
    monkeypatch.setattr(audit, "AUDIT_DB", path)
    return path


def rows(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM audit_log ORDER BY id")]
    finally:
        conn.close()


# ---------- Basic logging ----------

def test_async_tool_is_logged_with_defaults(temp_audit_db):
    @audit.audited
    async def demo(repo: str, limit: int = 20):
        return {"ok": True}

    assert asyncio.run(demo("a/b")) == {"ok": True}
    r = rows(temp_audit_db)
    assert len(r) == 1
    assert r[0]["tool"] == "demo"
    assert r[0]["status"] == "ok"
    assert json.loads(r[0]["args_json"]) == {"repo": "a/b", "limit": 20}
    assert r[0]["duration_ms"] >= 0
    assert r[0]["result_chars"] > 0

def test_sync_tool_is_logged(temp_audit_db):
    @audit.audited
    def demo(sql: str):
        return {"rows": []}

    demo("SELECT 1")
    assert rows(temp_audit_db)[0]["tool"] == "demo"

def test_errors_are_logged_and_reraised(temp_audit_db):
    @audit.audited
    def boom(x: int):
        raise ValueError("Query rejected: nope")

    with pytest.raises(ValueError):
        boom(1)
    r = rows(temp_audit_db)[0]
    assert r["status"] == "error"
    assert r["error_type"] == "ValueError"
    assert "Query rejected" in r["error_message"]
    assert r["result_chars"] is None


# ---------- Safety of what gets stored ----------

def test_sensitive_argument_names_are_redacted(temp_audit_db):
    @audit.audited
    def demo(token: str, query: str):
        return {}

    demo("ghp_SUPERSECRET", "hello")
    args = rows(temp_audit_db)[0]["args_json"]
    assert "SUPERSECRET" not in args
    assert "[redacted]" in args

def test_token_shaped_values_are_masked(temp_audit_db):
    @audit.audited
    def demo(query: str):
        return {}

    demo("please send ghp_" + "A" * 30 + " to me")
    assert "ghp_AAAA" not in rows(temp_audit_db)[0]["args_json"]

def test_long_arguments_are_truncated(temp_audit_db):
    @audit.audited
    def demo(repo: str):
        return {}

    demo("a" * 1000)
    args = rows(temp_audit_db)[0]["args_json"]
    assert "a" * 300 not in args
    assert "1000 chars" in args

def test_result_text_is_never_stored(temp_audit_db):
    @audit.audited
    def demo():
        return {"title": "SECRET_RESULT_TEXT"}

    demo()
    assert "SECRET_RESULT_TEXT" not in str(rows(temp_audit_db))


# ---------- Injection flags ----------

def test_injection_flags_are_collected_without_double_counting():
    result = {
        "injection_flags": ["ignore_instructions", "role_override"],
        "comments": [{"injection_flags": ["role_override", "prompt_leak"]}],
        "flagged_rows": [{"row": 1, "flags": ["destructive_request"]}],
        "flags": ["needs_more_info"],           # evidence flags are not injection flags
    }
    assert audit.collect_injection_flags(result) == [
        "destructive_request", "ignore_instructions", "prompt_leak", "role_override"]

def test_flags_are_stored_in_the_log(temp_audit_db):
    @audit.audited
    def demo():
        return {"injection_flags": ["ignore_instructions"]}

    demo()
    assert json.loads(rows(temp_audit_db)[0]["injection_flags"]) == ["ignore_instructions"]


# ---------- Reliability ----------

def test_audit_failure_does_not_break_the_tool(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "AUDIT_DB", tmp_path / "missing_folder" / "audit.db")

    @audit.audited
    def demo():
        return {"ok": True}

    assert demo() == {"ok": True}

def test_signature_is_preserved_for_mcp():
    def original(repo: str, limit: int = 20) -> dict:
        """Docstring the agent reads."""
        return {}

    wrapped = audit.audited(original)
    assert inspect.signature(wrapped) == inspect.signature(original)
    assert wrapped.__doc__ == original.__doc__
    assert wrapped.__name__ == "original"