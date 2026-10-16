import pytest

import actions
import approve
import audit

REPO = "me/triage-test-repo"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(actions, "ACTIONS_DB", tmp_path / "actions.db")
    monkeypatch.setattr(audit, "AUDIT_DB", tmp_path / "audit.db")
    return actions.Settings(allowed_repos=frozenset({REPO}),
                            allowed_assignees=frozenset({"me"}))


def test_dry_run_is_the_default(monkeypatch):
    monkeypatch.delenv("DRY_RUN", raising=False)
    assert approve.is_dry_run() is True

@pytest.mark.parametrize("value,expected", [
    ("0", False), ("false", False), ("No", False),
    ("1", True), ("yes", True), ("", True), ("garbage", True),
])
def test_dry_run_parsing(monkeypatch, value, expected):
    monkeypatch.setenv("DRY_RUN", value)
    assert approve.is_dry_run() is expected

def test_cli_has_no_flag_to_skip_confirmation():
    parser = approve.build_parser()
    for flag in ("--yes", "-y", "--force", "--no-confirm"):
        with pytest.raises(SystemExit):
            parser.parse_args(["approve", "abc12345", flag])

def test_cli_commands_parse():
    parser = approve.build_parser()
    assert parser.parse_args(["list"]).command == "list"
    assert parser.parse_args(["show", "abc12345"]).id == "abc12345"

def test_main_list_and_unknown_show(env, capsys):
    assert approve.main(["list"], env_file=None) == 0
    assert "No actions" in capsys.readouterr().out
    assert approve.main(["show", "deadbeef"], env_file=None) == 1
    assert "No such action" in capsys.readouterr().out

def test_main_show_and_reject(env, capsys):
    r = actions.propose_comment(REPO, 1, "Could you share the logs?", settings=env)
    assert approve.main(["show", r["action_id"]], env_file=None) == 0
    assert "Could you share the logs?" in capsys.readouterr().out
    assert approve.main(["reject", r["action_id"]], env_file=None) == 0
    assert actions.get_action(r["action_id"])["status"] == "rejected"
    assert approve.main(["reject", r["action_id"]], env_file=None) == 1