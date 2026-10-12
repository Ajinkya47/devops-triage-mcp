# Project Notes

## Phase 1-2: Server basics
- MCP SDK v2 renamed FastMCP to MCPServer, which broke my imports.
  Fix: pinned `mcp[cli]<2`. Plan: try migrating to v2 later as a separate commit.
- Got 401 Unauthorized from GitHub. Cause: the token in .env was invalid, and
  my code sent it anyway. Fix: only send the Authorization header if a token
  exists, and test tokens with curl before using them.

## Phase 3: SQLite and safe SQL
- Built sync_issues (GitHub to SQLite, upsert so re-running doesn't duplicate).
- Built sql_guard.py: parses SQL with sqlglot, allows exactly one SELECT,
  checks a table allowlist, blocks dangerous functions, forces LIMIT 100.
- Layers: validator, read-only connection (mode=ro), PRAGMA query_only,
  3-second timeout, row cap, cell length cap.
- pytest initially collected 0 tests because the test file was not in tests/.
  Fix: put it in tests/test_sql_guard.py. Also added pythonpath in pyproject.toml.
- Result: 30+ tests passing (blocked X malicious queries, allowed Y good ones).

## Lessons
- Never trust one layer of security; stack several.
- Issue text is untrusted input, because anyone can write an issue.
- Real data was messier than expected (e.g. most issues had 0 reactions).