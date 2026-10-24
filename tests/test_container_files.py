import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXCLUDED_MODULES = ("approve", "executor", "run_evals")


def ignore_entries() -> set:
    text = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    return {line.strip() for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")}


def test_dockerignore_keeps_secrets_data_and_approval_code_out():
    entries = ignore_entries()
    for needed in (".git", ".venv", ".env", ".env.*", "**/*.db", "data", "tests", "evals",
                   "scripts", "approve.py", "executor.py", "run_evals.py"):
        assert needed in entries, needed


def test_dockerfile_is_clean():
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    for forbidden in ("GITHUB_WRITE_TOKEN", "GROQ_API_KEY", "GITHUB_TOKEN", "approve.py"):
        assert forbidden not in text
    assert re.search(r"^USER\s+\S+", text, re.MULTILINE)           # not root
    assert "--locked" in text
    assert re.search(r'^ENTRYPOINT\s+\["python",\s*"agent.py"\]', text, re.MULTILINE)


def test_files_in_the_image_never_import_excluded_code():
    entries = ignore_entries()
    checked = 0
    for path in ROOT.glob("*.py"):
        if path.name in entries:
            continue
        checked += 1
        source = path.read_text(encoding="utf-8")
        for module in EXCLUDED_MODULES:
            assert not re.search(rf"^\s*(?:import|from)\s+{module}\b", source, re.MULTILINE), \
                f"{path.name} imports {module}"
    assert checked >= 10


def test_files_the_agent_needs_are_not_ignored():
    entries = ignore_entries()
    for name in ("agent.py", "server.py", "agent_core.py", "tool_bridge.py", "sql_guard.py"):
        assert (ROOT / name).exists() and name not in entries