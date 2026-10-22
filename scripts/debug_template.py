"""Check that the eval template database gets filled. Uses no model tokens."""
import asyncio
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import agent  # noqa: E402
import tool_bridge as tb  # noqa: E402
from run_evals import env_for  # noqa: E402


async def main(repo: str) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="evaldebug_"))
    try:
        async with agent.open_session(env_for(tmp, repo)) as session:
            result = await session.call_tool(
                "sync_issues", {"repo": repo, "state": "all", "limit": 100})
            text, is_error = tb.result_to_text(result)
        print("sync result  :", text[:300])
        print("is_error     :", is_error)
        db = tmp / "bugs.db"
        print("db exists    :", db.exists(), db)
        if db.exists():
            conn = sqlite3.connect(db)
            print("rows         :", conn.execute("SELECT COUNT(*) FROM issues").fetchone()[0])
            print("repos stored :", conn.execute("SELECT DISTINCT repo FROM issues").fetchall())
            print("first titles :", [r[0] for r in conn.execute(
                "SELECT title FROM issues ORDER BY number LIMIT 3")])
            conn.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


asyncio.run(main(sys.argv[1]))