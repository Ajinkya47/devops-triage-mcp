import pytest
from sql_guard import validate_sql, UnsafeSQLError

# ---------- Queries that must be ALLOWED ----------

GOOD_QUERIES = [
    "SELECT * FROM issues",
    "SELECT label, COUNT(*) FROM issue_labels GROUP BY label",
    "SELECT number, title FROM issues WHERE state = 'open' ORDER BY comments DESC",
    "SELECT i.number, i.title FROM issues i JOIN issue_labels l "
    "ON i.repo = l.repo AND i.number = l.number WHERE l.label = 'bug'",
    "select COUNT(*) from issues",
    "SELECT * FROM issues LIMIT 5",
]

@pytest.mark.parametrize("sql", GOOD_QUERIES)
def test_good_queries_allowed(sql):
    result = validate_sql(sql)
    assert result.upper().startswith("SELECT")
    assert "LIMIT" in result.upper()


# ---------- Queries that must be BLOCKED ----------

BAD_QUERIES = [
    "DROP TABLE issues",
    "DELETE FROM issues",
    "UPDATE issues SET title = 'hacked'",
    "INSERT INTO issues (repo, number, title, state) VALUES ('a/b', 1, 'x', 'open')",
    "CREATE TABLE evil (id INTEGER)",
    "ALTER TABLE issues ADD COLUMN x TEXT",
    "SELECT * FROM issues; DELETE FROM issues",
    "SELECT * FROM issues; DROP TABLE issues",
    "dRoP tAbLe issues",
    "PRAGMA table_info(issues)",
    "ATTACH DATABASE 'evil.db' AS evil",
    "SELECT * FROM sqlite_master",
    "SELECT * FROM main.sqlite_master",
    "SELECT * FROM issues UNION SELECT * FROM sqlite_master",
    "SELECT * FROM issues, sqlite_master",
    "SELECT * FROM secrets",
    "SELECT * FROM (SELECT * FROM sqlite_master)",
    "SELECT * FROM issues WHERE number IN (SELECT rootpage FROM sqlite_master)",
    "",
    "   ",
    "SELEC * FROM issues",
]

@pytest.mark.parametrize("sql", BAD_QUERIES)
def test_bad_queries_blocked(sql):
    with pytest.raises(UnsafeSQLError):
        validate_sql(sql)


# ---------- Row limit behavior ----------

def test_limit_added_when_missing():
    assert "LIMIT 100" in validate_sql("SELECT * FROM issues").upper()

def test_large_limit_is_capped():
    out = validate_sql("SELECT * FROM issues LIMIT 100000").upper()
    assert "LIMIT 100" in out
    assert "100000" not in out

def test_small_limit_is_kept():
    assert "LIMIT 5" in validate_sql("SELECT * FROM issues LIMIT 5").upper()