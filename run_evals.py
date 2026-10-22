import argparse
import asyncio
import glob
import json
import shutil
import sqlite3
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

import agent
import agent_core as core
import agent_ui as ui
import tool_bridge as tb
from evals import report, scoring
from evals.seed_data import ISSUES
from evals.tasks import TASKS

HERE = Path(__file__).parent
RESULTS = HERE / "evals" / "results"


def read_facts(tmp: Path) -> dict:
    """What the run left behind in its temporary databases."""
    facts = {"proposal_rows": [], "executed_count": 0,
             "db": {"issues_table": False, "issue_rows": 0}}
    actions_db = tmp / "actions.db"
    if actions_db.exists():
        conn = sqlite3.connect(f"{actions_db.as_uri()}?mode=ro", uri=True)
        try:
            for row in conn.execute(
                    "SELECT id, tool, repo, number, status, payload_json FROM pending_actions"):
                try:
                    payload = json.loads(row[5])
                except ValueError:
                    payload = None
                facts["proposal_rows"].append({"id": row[0], "tool": row[1], "repo": row[2],
                                               "number": row[3], "status": row[4],
                                               "payload": payload})
            facts["executed_count"] = conn.execute(
                "SELECT COUNT(*) FROM pending_actions WHERE status = 'executed'").fetchone()[0]
        except sqlite3.Error:
            pass
        finally:
            conn.close()
    bugs_db = tmp / "bugs.db"
    if bugs_db.exists():
        conn = sqlite3.connect(f"{bugs_db.as_uri()}?mode=ro", uri=True)
        try:
            facts["db"]["issue_rows"] = conn.execute("SELECT COUNT(*) FROM issues").fetchone()[0]
            facts["db"]["issues_table"] = True
        except sqlite3.Error:
            pass
        finally:
            conn.close()
    return facts


def env_for(tmp: Path, repo: str) -> dict:
    return {"BUGS_DB_PATH": str(tmp / "bugs.db"),
            "ACTIONS_DB_PATH": str(tmp / "actions.db"),
            "AUDIT_DB_PATH": str(tmp / "audit.db"),
            "WRITE_ALLOWED_REPOS": repo,
            "ASSIGNEES_ALLOWED": repo.split("/")[0]}


async def build_template(repo: str, work: Path) -> Path:
    folder = work / "template"
    folder.mkdir()
    async with agent.open_session(env_for(folder, repo)) as session:
        result = await session.call_tool("sync_issues",
                                         {"repo": repo, "state": "all", "limit": 100})
        text, is_error = tb.result_to_text(result)
        if is_error:
            raise SystemExit(f"Could not sync the test repo: {ui.safe_text(text)[:200]}")
    return folder / "bugs.db"


def build_world(db_path: Path, repo: str) -> dict:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT number, title FROM issues WHERE lower(repo) = lower(?)",
                            (repo,)).fetchall()
    finally:
        conn.close()
    by_title = {title: number for number, title in rows}
    missing = [i.title for i in ISSUES if i.title not in by_title]
    if missing:
        print("These seed issues are missing from the repo:")
        for title in missing:
            print(f"  - {title}")
        print(f"\nCreate them with:\n  uv run python scripts/seed_issues.py --repo {repo} --create --sync-bodies")
        raise SystemExit(2)
    return {"num": {i.key: by_title[i.title] for i in ISSUES}, "total": len(rows),
            "owner": repo.split("/")[0], "repo": repo}


async def run_one(task, repeat, *, llm, repo, template_db, world, limits) -> dict:
    tmp = Path(tempfile.mkdtemp(prefix="evalrun_"))
    request = task.request.replace("{owner}", world["owner"])
    res, crash = None, ""
    try:
        shutil.copy(template_db, tmp / "bugs.db")
        try:
            async with agent.open_session(env_for(tmp, repo)) as session:
                specs = await agent.load_specs(session)
                res = await core.run_agent(
                    request, llm=llm, call_tool=agent.make_call_tool(session),
                    exposed=tb.select_tools(specs, task.mode),
                    system_prompt=ui.build_system_prompt(repo, task.mode == tb.MODE_PROPOSE),
                    limits=limits)
        except Exception as exc:                       # a crash is a result, not a reason to stop
            crash = type(exc).__name__
        facts = read_facts(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    r = res or core.RunResult(stop_reason="crashed")
    rec = {
        "task_id": task.id, "category": task.category, "repeat": repeat, "model": llm.model,
        "mode": task.mode, "request": request, "expect_refusal": task.expect_refusal,
        "ts": datetime.now().isoformat(timespec="seconds"), "crash": crash,
        "stop_reason": r.stop_reason, "error": r.error, "answer": (r.answer or "")[:3000],
        "steps": r.steps, "total_tokens": r.total_tokens, "prompt_tokens": r.prompt_tokens,
        "completion_tokens": r.completion_tokens, "elapsed": round(r.elapsed, 1),
        "seconds_waited": round(r.seconds_waited, 1), "refused_calls": r.refused_calls,
        "malformed_replies": r.malformed_replies, "empty_replies": r.empty_replies,
        "rate_waits": r.rate_waits, "pace_waits": r.pace_waits, "compacted": r.compacted,
        "tool_calls": r.tool_calls, "proposals": r.proposals,
        "injection_flags_seen": r.injection_flags_seen,
        "unverified_claims": r.unverified_claims, "ungrounded_refs": r.ungrounded_refs,
        **facts,
    }
    rec["score"] = scoring.score_run(rec, task, world)
    return rec


def load_records(paths) -> list:
    records = []
    for path in paths:
        try:
            lines = Path(path).read_text(encoding="utf-8").splitlines()
        except OSError:
            print(f"Could not read {path}", file=sys.stderr)
            continue
        for line in lines:
            try:
                records.append(json.loads(line))
            except ValueError:
                continue
    return records


def report_command(patterns, out) -> int:
    files = []
    for pattern in patterns:
        files += sorted(glob.glob(pattern)) or [pattern]
    text = report.build_report(load_records(files))
    print(text)
    if out:
        Path(out).write_text(text, encoding="utf-8")
        print(f"Saved to {out}")
    return 0


def select_tasks(args) -> list:
    chosen = list(TASKS)
    if args.tasks:
        wanted = [t.strip() for t in args.tasks.split(",") if t.strip()]
        unknown = sorted(set(wanted) - {t.id for t in TASKS})
        if unknown:
            raise SystemExit("Unknown task id(s): " + ", ".join(unknown))
        chosen = [t for t in TASKS if t.id in wanted]
    if args.category:
        chosen = [t for t in chosen if t.category == args.category]
    return chosen


async def amain(args) -> int:
    load_dotenv(HERE / ".env.agent")
    repo = (args.repo or __import__("os").getenv("DEFAULT_REPO") or "").strip()
    if not repo:
        raise SystemExit("Give --repo owner/name (or set DEFAULT_REPO in .env.agent).")
    llm = agent.make_llm(args.model)
    tasks = select_tasks(args)

    RESULTS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = Path(args.out) if args.out else RESULTS / f"{llm.model.replace('/', '-')}_{stamp}.jsonl"
    done = set()
    if args.resume and out.exists():
        done = {(r["task_id"], r["repeat"]) for r in load_records([out])}

    work = Path(tempfile.mkdtemp(prefix="evalwork_"))
    used = 0
    try:
        print(f"Model {llm.model}   repo {repo}   results -> {out}")
        template_db = await build_template(repo, work)
        world = build_world(template_db, repo)
        limits = core.Limits()
        plan = [(t, r) for r in range(1, args.repeats + 1) for t in tasks
                if (t.id, r) not in done]
        print(f"{len(plan)} run(s) planned, {len(done)} already done\n")

        for index, (task, repeat) in enumerate(plan, 1):
            if args.max_tokens and used >= args.max_tokens:
                print(f"\nToken budget reached ({used:,}). Run again with --resume --out {out}")
                break
            rec = await run_one(task, repeat, llm=llm, repo=repo, template_db=template_db,
                                world=world, limits=limits)
            with open(out, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(rec, default=str) + "\n")
            used += rec["total_tokens"]
            s = rec["score"]
            note = "" if s["passed"] else f"  <- {s['reason']}"
            print(f"[{index}/{len(plan)}] {task.id:<20} {'PASS' if s['passed'] else 'FAIL'}  "
                  f"{rec['total_tokens']:>6,} tok  {rec['elapsed']:>4.0f}s{note}")
            if index < len(plan):
                await asyncio.sleep(args.cooldown)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    if out.exists():
        mine = [r for r in load_records([out]) if r.get("model") == llm.model]
        print("\n" + report.build_report(mine))
    return 0


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    p = argparse.ArgumentParser(description="Run the evaluation suite against your test repo.")
    p.add_argument("--repo", help="owner/name of the seeded test repo")
    p.add_argument("--model", help="override LLM_MODEL")
    p.add_argument("--tasks", help="comma-separated task ids")
    p.add_argument("--category", help="only this category")
    p.add_argument("--repeats", type=int, default=1)
    p.add_argument("--max-tokens", type=int, default=0, help="stop after about this many tokens")
    p.add_argument("--cooldown", type=float, default=20.0, help="seconds to rest between runs")
    p.add_argument("--out", help="results file (.jsonl)")
    p.add_argument("--resume", action="store_true", help="skip runs already in --out")
    p.add_argument("--list", action="store_true", help="list the tasks and exit")
    p.add_argument("--report", nargs="+", metavar="FILE", help="print a report from result files")
    p.add_argument("--report-out", help="save the report here")
    args = p.parse_args(argv)

    if args.list:
        for t in TASKS:
            print(f"{t.id:<20} {t.category:<11} {t.mode:<10} {t.request[:70]}")
        return 0
    if args.report:
        return report_command(args.report, args.report_out)
    try:
        return asyncio.run(amain(args))
    except KeyboardInterrupt:
        print("\nInterrupted. Finished runs are saved; use --resume to continue.")
        return 130


if __name__ == "__main__":
    sys.exit(main())