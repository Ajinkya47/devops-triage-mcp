import json
import sqlite3
import sys

from audit import AUDIT_DB

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main(limit: int = 20) -> None:
    if not AUDIT_DB.exists():
        print("No audit log yet. Call a tool first.")
        return

    conn = sqlite3.connect(f"{AUDIT_DB.as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    total = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    print(f"Total calls logged: {total}\n")

    print("By tool:")
    for r in conn.execute(
        "SELECT tool, COUNT(*) AS n, SUM(status = 'error') AS errors, "
        "AVG(duration_ms) AS avg_ms FROM audit_log GROUP BY tool ORDER BY n DESC"
    ):
        print(f"  {r['tool']:16} calls={r['n']:3}  errors={r['errors'] or 0:3}  "
              f"avg_ms={int(r['avg_ms'] or 0)}")

    print(f"\nLast {limit} calls:")
    for r in conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)):
        flags = json.loads(r["injection_flags"] or "[]")
        mark = f"  FLAGS={flags}" if flags else ""
        print(f"{r['ts']}  {r['tool']:16} {r['status']:5} {r['duration_ms']:5}ms  "
              f"{(r['args_json'] or '')[:70]}{mark}")
        if r["error_message"]:
            print(f"      error: {r['error_message']}")


main(int(sys.argv[1]) if len(sys.argv) > 1 else 20)