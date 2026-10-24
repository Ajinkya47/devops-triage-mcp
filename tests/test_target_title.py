import time

import httpx
import pytest

import actions
import approve
import audit
import executor
from actions import ActionError

REPO = "me/triage-test-repo"
NOW = 1000.0


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(actions, "ACTIONS_DB", tmp_path / "actions.db")
    monkeypatch.setattr(audit, "AUDIT_DB", tmp_path / "audit.db")
    return actions.Settings(allowed_repos=frozenset({REPO}),
                            allowed_assignees=frozenset({"me"}))


def client_for(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def issue_json(**over):
    base = {"title": "Payment is charged twice", "state": "open"}
    base.update(over)
    return base


def pending(env, text="Could you share the logs?", now=NOW):
    r = actions.propose_comment(REPO, 14, text, settings=env, now=now)
    return actions.get_action(r["action_id"])


OK = {"status": "ok", "title": "Payment is charged twice", "state": "open",
      "is_pull_request": False}


# ---------- lookup_issue ----------

def test_lookup_ok_uses_get_and_the_token():
    seen = {}
    def handler(request):
        seen.update(method=request.method, url=str(request.url),
                    auth=request.headers.get("authorization"))
        return httpx.Response(200, json=issue_json())
    out = executor.lookup_issue(REPO, 14, token="READTOKEN", client=client_for(handler))
    assert out == OK
    assert seen["method"] == "GET" and seen["url"].endswith("/repos/me/triage-test-repo/issues/14")
    assert seen["auth"] == "Bearer READTOKEN"

def test_lookup_without_a_token_sends_no_auth_header():
    seen = {}
    def handler(request):
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=issue_json())
    executor.lookup_issue(REPO, 14, client=client_for(handler))
    assert seen["auth"] is None

def test_lookup_flags_pull_requests():
    handler = lambda r: httpx.Response(200, json=issue_json(pull_request={"url": "x"}))
    assert executor.lookup_issue(REPO, 3, client=client_for(handler))["is_pull_request"] is True

def test_lookup_not_found():
    out = executor.lookup_issue(REPO, 999, client=client_for(lambda r: httpx.Response(404, json={})))
    assert out == {"status": "not_found"}

@pytest.mark.parametrize("response", [
    lambda r: httpx.Response(500, json={}),
    lambda r: httpx.Response(403, json={}),
    lambda r: httpx.Response(302, headers={"location": "https://evil.example"}),
    lambda r: httpx.Response(200, text="not json"),
    lambda r: httpx.Response(200, json=[1, 2]),
])
def test_lookup_other_answers_are_unavailable(response):
    assert executor.lookup_issue(REPO, 1, client=client_for(response)) == {"status": "unavailable"}

def test_lookup_network_error_is_unavailable():
    def handler(request):
        raise httpx.ConnectError("down", request=request)
    assert executor.lookup_issue(REPO, 1, client=client_for(handler)) == {"status": "unavailable"}

def test_lookup_missing_title_becomes_empty_text():
    handler = lambda r: httpx.Response(200, json={"title": None, "state": "open"})
    assert executor.lookup_issue(REPO, 1, client=client_for(handler))["title"] == ""

@pytest.mark.parametrize("repo,number", [
    ("bad", 1), (REPO, 0), (REPO, True), (REPO, "5"), ("me/../x", 1), (None, 1), (REPO, None)])
def test_lookup_rejects_bad_input_without_any_request(repo, number):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=issue_json())
    out = executor.lookup_issue(repo, number, client=client_for(handler))
    assert out == {"status": "unavailable"} and calls == []


# ---------- The screen ----------

def test_screen_without_a_target_has_no_title_line(env):
    assert "Title" not in executor.render_action(pending(env), False, NOW)

def test_screen_shows_title_and_state(env):
    out = executor.render_action(pending(env), False, NOW, OK)
    assert " Title  : Payment is charged twice  (open issue)" in out
    assert out.index("Target") < out.index("Title") < out.index("Status")

def test_screen_cleans_and_shortens_hostile_titles(env):
    target = dict(OK, title="x" * 500 + "\x1b[2J\u202e line\nbreak")
    out = executor.render_action(pending(env), False, NOW, target)
    assert "\x1b" not in out and "\u202e" not in out
    assert "x" * 200 not in out and "not shown" in out

def test_screen_joins_multiline_titles(env):
    out = executor.render_action(pending(env), False, NOW, dict(OK, title="a\nb"))
    assert "Title  : a b" in out

def test_screen_warns_when_the_issue_is_not_found(env):
    out = executor.render_action(pending(env), False, NOW, {"status": "not_found"})
    assert "(not found)" in out and "No such issue" in out

def test_screen_says_when_it_could_not_check(env):
    out = executor.render_action(pending(env), False, NOW, {"status": "unavailable"})
    assert "could not be checked" in out

def test_screen_warns_about_pull_requests(env):
    out = executor.render_action(pending(env), False, NOW, dict(OK, is_pull_request=True))
    assert "pull request" in out and "not an issue" in out


# ---------- The approval flow ----------

def approval(env, action_id, **kw):
    return executor.process_approval(action_id, decided_by="human:t", dry_run=True, sender=None,
                                     confirm=lambda s, i: True, settings=env, now=NOW + 10, **kw)

def test_flow_passes_repo_and_number_to_the_lookup(env):
    action = pending(env)
    calls = []
    def spy(repo, number):
        calls.append((repo, number))
        return OK
    out = approval(env, action["id"], lookup=spy)
    assert calls == [(REPO, 14)] and "Payment is charged twice" in out["screen"]

def test_flow_survives_a_failing_lookup(env):
    action = pending(env)
    def broken(repo, number):
        raise RuntimeError("boom")
    out = approval(env, action["id"], lookup=broken)
    assert out["outcome"] == "dry_run" and "could not be checked" in out["screen"]

def test_flow_without_a_lookup_is_unchanged(env):
    out = approval(env, pending(env)["id"])
    assert out["outcome"] == "dry_run" and "Title" not in out["screen"]

def test_unknown_actions_never_trigger_a_lookup(env):
    calls = []
    with pytest.raises(ActionError):
        approval(env, "deadbeef", lookup=lambda r, n: calls.append((r, n)))
    assert calls == []


# ---------- The command line ----------

def test_show_prints_the_title(env, monkeypatch, capsys):
    action = pending(env, now=time.time())
    seen = []
    def fake(repo, number):
        seen.append((repo, number))
        return OK
    monkeypatch.setattr(approve, "lookup_target", fake)
    assert approve.main(["show", action["id"]], env_file=None) == 0
    assert seen == [(REPO, 14)]
    assert "Payment is charged twice" in capsys.readouterr().out

def test_dry_run_approve_prints_the_title_too(env, monkeypatch, capsys):
    action = pending(env, now=time.time())
    monkeypatch.setenv("DRY_RUN", "1")
    monkeypatch.setattr(approve, "lookup_target", lambda repo, number: OK)
    assert approve.main(["approve", action["id"]], env_file=None) == 0
    out = capsys.readouterr().out
    assert "Payment is charged twice" in out and "DRY RUN" in out

def test_lookup_target_passes_only_the_read_token(monkeypatch):
    seen = {}
    monkeypatch.setenv("GITHUB_TOKEN", "READ")
    monkeypatch.setenv("GITHUB_WRITE_TOKEN", "WRITE")
    def fake(repo, number, token=None, client=None):
        seen["token"] = token
        return {"status": "unavailable"}
    monkeypatch.setattr(executor, "lookup_issue", fake)
    import importlib
    importlib.reload(approve)                 # restore the real lookup_target after conftest
    try:
        approve.lookup_target(REPO, 1)
    finally:
        importlib.reload(approve)
    assert seen["token"] == "READ"