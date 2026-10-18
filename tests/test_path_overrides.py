import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KEYS = ("BUGS_DB_PATH", "ACTIONS_DB_PATH", "AUDIT_DB_PATH")
CODE = ("import audit, actions, server; "
        "print('|'.join(map(str, [server.DB_PATH, actions.ACTIONS_DB, audit.AUDIT_DB])))")


def run(extra: dict) -> list[Path]:
    env = {k: v for k, v in os.environ.items() if k not in KEYS}
    env.update(extra)
    r = subprocess.run([sys.executable, "-c", CODE], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=90)
    assert r.returncode == 0, r.stderr
    return [Path(p) for p in r.stdout.strip().splitlines()[-1].split("|")]


def test_defaults_stay_next_to_the_code():
    bugs, acts, aud = run({})
    assert bugs == ROOT / "bugs.db"
    assert acts == ROOT / "actions.db"
    assert aud == ROOT / "audit.db"


def test_overrides_move_all_three_databases(tmp_path):
    wanted = {
        "BUGS_DB_PATH": str(tmp_path / "bugs.db"),
        "ACTIONS_DB_PATH": str(tmp_path / "actions.db"),
        "AUDIT_DB_PATH": str(tmp_path / "audit.db"),
    }
    bugs, acts, aud = run(wanted)
    assert bugs == (tmp_path / "bugs.db").resolve()
    assert acts == (tmp_path / "actions.db").resolve()
    assert aud == (tmp_path / "audit.db").resolve()
    assert (tmp_path / "bugs.db").exists()          # the server created its schema there


def test_empty_override_falls_back_to_default():
    bugs, _, _ = run({"BUGS_DB_PATH": ""})
    assert bugs == ROOT / "bugs.db"