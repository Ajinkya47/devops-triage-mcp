import re
from pathlib import Path

from evals import report
from scripts.seed_issues import norm, plan_changes
from evals.seed_data import ISSUES

ROOT = Path(__file__).resolve().parent.parent


def score(passed=True, **over):
    base = {"passed": passed, "reason": "ok" if passed else "bad", "over_refusal": False,
            "unrequested_proposals": False, "false_claim": False, "ungrounded": False,
            "format_failure": False}
    base.update(over)
    return base


def rec(task, model="m1", passed=True, category="severity", **over):
    base = {"task_id": task, "category": category, "model": model, "repeat": 1,
            "stop_reason": "answered", "total_tokens": 8000, "steps": 4, "elapsed": 20,
            "seconds_waited": 5, "tool_calls": [{"status": "ok"}], "executed_count": 0,
            "expect_refusal": False, "score": score(passed)}
    base.update(over)
    return base


# ---------- Report ----------

def test_empty_report():
    assert report.build_report([]) == "No results.\n"

def test_report_numbers():
    recs = [rec("a"), rec("a", passed=False), rec("b", category="injection"),
            rec("c", stop_reason="max_steps", score=score(False, reason="run ended: max_steps"))]
    text = report.build_report(recs)
    assert "**Overall:** 50% (2/4)" in text
    assert "Writes executed: **0**" in text
    assert "Injection tasks resisted: **100% (1/1)**" in text
    assert "max_steps x1" in text

def test_report_flags_unsafe_events():
    recs = [rec("a", executed_count=1, score=score(True, unrequested_proposals=True, false_claim=True))]
    text = report.build_report(recs)
    assert "Writes executed: **1**" in text
    assert "unrequested proposals: **1**" in text and "claiming an action was done: **1**" in text

def test_comparison_table_for_two_models():
    text = report.build_report([rec("a", model="m1"), rec("a", model="m2", passed=False)])
    assert "## Comparison" in text and "| m1 |" in text and "| m2 |" in text

def test_pipes_in_reasons_do_not_break_tables():
    text = report.build_report([rec("a", passed=False, score=score(False, reason="x | y"))])
    assert "x / y" in text


# ---------- Seed script ----------

def test_plan_changes():
    first = ISSUES[0]
    existing = {first.title: {"number": 1, "body": first.body.replace("\n", "\r\n")},
                ISSUES[1].title: {"number": 2, "body": "old body"}}
    missing, differing = plan_changes(existing, ISSUES)
    assert len(missing) == len(ISSUES) - 2
    assert [(i.key, n) for i, n in differing] == [(ISSUES[1].key, 2)]

def test_norm():
    assert norm("a\r\nb \n") == "a\nb"


# ---------- Where secrets and approval code may appear ----------

def _files():
    for folder in (ROOT, ROOT / "scripts", ROOT / "evals"):
        yield from folder.glob("*.py")

def test_only_two_files_know_the_write_token():
    holders = {p.relative_to(ROOT).as_posix() for p in _files()
               if "GITHUB_WRITE_TOKEN" in p.read_text(encoding="utf-8")}
    assert holders == {"approve.py", "scripts/seed_issues.py"}

def test_eval_code_has_no_approval_path():
    for p in [ROOT / "run_evals.py", *(ROOT / "evals").glob("*.py")]:
        source = p.read_text(encoding="utf-8")
        for forbidden in ("approve_action", "reject_action", "mark_executed", "mark_failed"):
            assert forbidden not in source, f"{p.name} mentions {forbidden}"
        assert not re.search(r"^\s*(?:import|from)\s+(?:approve|executor)\b", source, re.MULTILINE)