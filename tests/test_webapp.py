import re
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import actions
import agent_core as core
import webapp

TOKEN = "test-token-123"
H = {"X-UI-Token": TOKEN}
REPO = "me/triage-test-repo"


def make_result(**over):
    base = dict(answer="Top issue is #1.", stop_reason="answered", steps=2,
                prompt_tokens=1000, completion_tokens=200, elapsed=3.4, seconds_waited=1.0)
    base.update(over)
    return core.RunResult(**base)


class Recorder:
    def __init__(self, result=None, error=None):
        self.calls = []
        self.result = result or make_result()
        self.error = error

    def factory(self):
        rec = self

        @asynccontextmanager
        async def build():
            async def run(request, allow, repo, history):
                rec.calls.append({"request": request, "allow": allow, "repo": repo,
                                  "history": history})
                if rec.error:
                    raise rec.error
                return rec.result
            yield run, "fake-model"
        return build


def client_for(rec=None):
    rec = rec or Recorder()
    app = webapp.create_app(runner_factory=rec.factory(), token=TOKEN, default_repo=REPO,
                            allowed_hosts=("testserver",))
    return TestClient(app), rec


# ---------- Page and headers ----------

def test_page_and_assets_are_public_and_carry_no_token():
    client, _ = client_for()
    with client:
        page = client.get("/")
        assert page.status_code == 200 and "DevOps Triage" in page.text
        assert TOKEN not in page.text and "onclick" not in page.text.lower()
        csp = page.headers["content-security-policy"]
        assert "default-src 'none'" in csp and "'unsafe-inline'" not in csp
        assert page.headers["x-content-type-options"] == "nosniff"
        assert client.get("/app.js").status_code == 200
        assert client.get("/style.css").status_code == 200

def test_frontend_never_uses_dangerous_dom_calls():
    for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write",
                   "eval(", "new Function"):
        assert banned not in webapp.APP_JS

def test_api_responses_are_not_cached():
    client, _ = client_for()
    with client:
        assert client.get("/api/config", headers=H).headers["cache-control"] == "no-store"


# ---------- Who may call it ----------

@pytest.mark.parametrize("method,path", [("get", "/api/config"), ("get", "/api/proposals"),
                                         ("post", "/api/ask"), ("post", "/api/clear")])
def test_api_requires_the_token(method, path):
    client, _ = client_for()
    kwargs = {"json": {"request": "hi"}} if path == "/api/ask" else {}
    with client:
        call = getattr(client, method)
        assert call(path, **kwargs).status_code == 401
        assert call(path, headers={"X-UI-Token": "wrong"}, **kwargs).status_code == 401

def test_unknown_host_header_is_rejected():
    client, _ = client_for()
    with client:
        assert client.get("/", headers={"host": "evil.example"}).status_code == 400

def test_default_hosts_are_loopback_only():
    assert webapp.LOOPBACK_HOSTS == ("127.0.0.1", "localhost")
    app = webapp.create_app(runner_factory=Recorder().factory(), token=TOKEN)
    with TestClient(app) as client:
        assert client.get("/").status_code == 400          # "testserver" is not allowed

def test_a_random_token_is_generated_when_none_is_given():
    a = webapp.create_app(runner_factory=Recorder().factory())
    b = webapp.create_app(runner_factory=Recorder().factory())
    assert len(a.state.token) >= 24 and a.state.token != b.state.token

def test_config():
    client, _ = client_for()
    with client:
        assert client.get("/api/config", headers=H).json() == {
            "model": "fake-model", "default_repo": REPO}


# ---------- Asking ----------

def test_ask_passes_the_request_and_defaults_to_read_only():
    client, rec = client_for()
    with client:
        r = client.post("/api/ask", headers=H, json={"request": "rank them"})
    assert r.status_code == 200
    assert rec.calls == [{"request": "rank them", "allow": False, "repo": REPO, "history": []}]

def test_proposals_only_when_asked_and_repo_can_be_overridden():
    client, rec = client_for()
    with client:
        client.post("/api/ask", headers=H,
                    json={"request": "draft", "repo": "a/b", "allow_proposals": True})
        client.post("/api/ask", headers=H, json={"request": "x", "repo": "  "})
    assert rec.calls[0]["allow"] is True and rec.calls[0]["repo"] == "a/b"
    assert rec.calls[1]["repo"] == REPO

@pytest.mark.parametrize("body", [
    {}, {"request": ""}, {"request": "   "}, {"request": "x" * 2001},
    {"request": "hi", "repo": "not a repo"}, {"request": "hi", "repo": "me/../x"},
    {"request": "hi", "repo": "a/b/c"}, {"request": "hi", "allow_proposals": "maybe"},
])
def test_bad_bodies_are_rejected(body):
    client, rec = client_for()
    with client:
        assert client.post("/api/ask", headers=H, json=body).status_code == 422
    assert rec.calls == []

def test_result_is_shaped_and_cleaned():
    res = make_result(answer="Hello \x1b[2J world")
    res.tool_calls = [{"name": "list_issues", "status": "ok", "chars": 120, "truncated": False,
                       "clamped": [], "flags": [], "reason": ""}]
    res.proposals = ["c1711f12"]
    res.injection_flags_seen = ["role_override"]
    res.unverified_claims = ["I posted it \x1b[31m"]
    res.ungrounded_refs = [123]
    client, _ = client_for(Recorder(res))
    with client:
        data = client.post("/api/ask", headers=H, json={"request": "go"}).json()
    assert "\x1b" not in data["answer"] and "\x1b" not in str(data["unverified_claims"])
    assert data["proposals"] == [{"id": "c1711f12",
                                  "command": "uv run python approve.py show c1711f12"}]
    assert data["tokens"] == 1200 and data["steps"] == 2
    assert data["injection_flags"] == ["role_override"] and data["ungrounded_refs"] == [123]
    assert data["tool_calls"][0]["name"] == "list_issues"


# ---------- History, errors, and one run at a time ----------

def test_history_is_kept_capped_and_clearable():
    client, rec = client_for()
    with client:
        for i in range(5):
            client.post("/api/ask", headers=H, json={"request": f"q{i}"})
        assert [h[0] for h in rec.calls[-1]["history"]] == ["q1", "q2", "q3"]
        assert client.post("/api/clear", headers=H).json() == {"ok": True}
        client.post("/api/ask", headers=H, json={"request": "fresh"})
        assert rec.calls[-1]["history"] == []

def test_unanswered_runs_are_not_remembered():
    client, rec = client_for(Recorder(make_result(stop_reason="max_steps", answer="")))
    with client:
        client.post("/api/ask", headers=H, json={"request": "a"})
        client.post("/api/ask", headers=H, json={"request": "b"})
    assert rec.calls[-1]["history"] == []

def test_a_failing_run_returns_a_generic_error_and_recovers():
    rec = Recorder(error=RuntimeError("secret ghp_" + "A" * 30))
    client, _ = client_for(rec)
    with client:
        r = client.post("/api/ask", headers=H, json={"request": "go"})
        assert r.status_code == 500
        assert "ghp_" not in r.text and "secret" not in r.text and "RuntimeError" in r.text
        rec.error = None
        assert client.post("/api/ask", headers=H, json={"request": "again"}).status_code == 200

def test_a_second_run_is_refused_while_one_is_in_progress():
    client, rec = client_for()
    with client:
        client.app.state.busy = True
        assert client.post("/api/ask", headers=H, json={"request": "go"}).status_code == 409
        assert client.post("/api/clear", headers=H).status_code == 409
    assert rec.calls == []


# ---------- Proposals list ----------

def test_proposals_list_is_read_only_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr(actions, "ACTIONS_DB", tmp_path / "actions.db")
    settings = actions.Settings(allowed_repos=frozenset({REPO}),
                                allowed_assignees=frozenset({"me"}))
    fresh = actions.propose_comment(REPO, 1, "Could you share the logs?", settings=settings)
    old = actions.propose_comment(REPO, 2, "Another note", settings=settings, now=1000.0)
    client, _ = client_for()
    with client:
        rows = {r["id"]: r for r in
                client.get("/api/proposals", headers=H).json()["proposals"]}
    mine, stale = rows[fresh["action_id"]], rows[old["action_id"]]
    assert mine["status"] == "pending" and mine["expires_in"] > 0
    assert mine["command"] == f"uv run python approve.py show {fresh['action_id']}"
    assert stale["status"] == "expired" and stale["expires_in"] == 0
    assert not {"payload", "text", "body"} & set(mine)
    assert actions.get_action(old["action_id"])["status"] == "pending"   # listing changed nothing


# ---------- No approval path ----------

def test_web_app_has_no_approval_path():
    source = Path(webapp.__file__).read_text(encoding="utf-8")
    for forbidden in ("approve_action", "reject_action", "mark_executed", "mark_failed",
                      "GITHUB_WRITE_TOKEN"):
        assert forbidden not in source
    assert not re.search(r"^\s*(?:import|from)\s+(?:approve|executor|run_evals)\b",
                         source, re.MULTILINE)

def test_no_approval_route_exists():
    client, _ = client_for()
    paths = {getattr(r, "path", "") for r in client.app.routes}
    assert not any(re.search(r"approv|confirm|execut|reject", p, re.I) for p in paths)