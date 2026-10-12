import sqlglot
from sqlglot import exp

ALLOWED_TABLES = {"issues", "issue_labels"}
MAX_LIMIT = 100

BLOCKED_FUNCTIONS = {
    "load_extension", "readfile", "writefile",
    "randomblob", "zeroblob", "edit", "fts3_tokenizer",
}
SAFE_UNKNOWN_FUNCTIONS = {
    "strftime", "julianday", "date", "datetime",
    "ifnull", "instr", "group_concat",
}


class UnsafeSQLError(ValueError):
    pass


def validate_sql(sql: str) -> str:
    """Return a safe, row-limited SELECT query, or raise UnsafeSQLError."""
    if not sql or not sql.strip():
        raise UnsafeSQLError("Empty query")

    try:
        statements = sqlglot.parse(sql, read="sqlite")
    except sqlglot.errors.ParseError as e:
        raise UnsafeSQLError(f"Could not parse query: {e}")

    statements = [s for s in statements if s is not None]
    if len(statements) != 1:
        raise UnsafeSQLError("Exactly one statement is allowed")

    tree = statements[0]

    if not isinstance(tree, exp.Select):
        raise UnsafeSQLError("Only SELECT statements are allowed")

    # Block anything that is not plain reading (DML/DDL hidden in subqueries, etc.)
    forbidden = (exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create,
                 exp.Alter, exp.Command, exp.Pragma, exp.Attach)
    for node in tree.walk():
        if isinstance(node, forbidden):
            raise UnsafeSQLError(f"Forbidden operation: {type(node).__name__}")


    # Function checks: block dangerous names, and unknown functions not on the safe list
    for node in tree.walk():
        if isinstance(node, exp.Anonymous):
            name = node.name.lower()
            if name in BLOCKED_FUNCTIONS or name not in SAFE_UNKNOWN_FUNCTIONS:
                raise UnsafeSQLError(f"Function not allowed: {node.name}")
        elif isinstance(node, exp.Func):
            if node.sql_name().lower() in BLOCKED_FUNCTIONS:
                raise UnsafeSQLError(f"Function not allowed: {node.sql_name()}")

    # Only allow our own tables
    for table in tree.find_all(exp.Table):
        if table.name.lower() not in ALLOWED_TABLES:
            raise UnsafeSQLError(f"Table not allowed: {table.name}")

    # Enforce a row limit
    limit = tree.args.get("limit")
    if limit is None:
        tree = tree.limit(MAX_LIMIT)
    else:
        try:
            value = int(limit.expression.name)
            if value > MAX_LIMIT:
                tree = tree.limit(MAX_LIMIT)
        except (ValueError, AttributeError):
            tree = tree.limit(MAX_LIMIT)

    return tree.sql(dialect="sqlite")