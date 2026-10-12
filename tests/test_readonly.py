import sqlite3
import pytest

def test_readonly_connection_rejects_writes(tmp_path):
    db = tmp_path / "t.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE issues (x INTEGER)")
    c.commit()
    c.close()

    ro = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True)
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("INSERT INTO issues VALUES (1)")

def test_progress_handler_interrupts_long_query():
    conn = sqlite3.connect(":memory:")
    conn.set_progress_handler(lambda: 1, 1000)   # non-zero return aborts the query
    with pytest.raises(sqlite3.OperationalError):
        conn.execute(
            "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM r) "
            "SELECT COUNT(*) FROM r"
        ).fetchall()