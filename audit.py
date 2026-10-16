import functools
import inspect
import json
import re
import sqlite3
import sys
import time
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

AUDIT_DB = Path(__file__).parent / "audit.db"
SESSION_ID = uuid.uuid4().hex[:12]
MAX_ARG_CHARS = 200
MAX_ERROR_CHARS = 200

_SENSITIVE_KEY = re.compile(
    r"token|secret|password|passwd|authorization|api[_-]?key|credential", re.I)
_SECRET_VALUE = re.compile(
    r"\b(?:ghp|gho|ghs|ghu|ghr)_[A-Za-z0-9]{20,}\b"
    r"|\bgithub_pat_[A-Za-z0-9_]{20,}\b"
    r"|\bgsk_[A-Za-z0-9]{20,}\b"
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,
    session_id      TEXT NOT NULL,
    tool            TEXT NOT NULL,
    args_json       TEXT,
    status          TEXT NOT NULL,
    error_type      TEXT,
    error_message   TEXT,
    duration_ms     INTEGER,
    result_chars    INTEGER,
    injection_flags TEXT
)
"""


def _clip(text: str, limit: int) -> str:
    """Mask known token shapes, then cut to a maximum length."""
    text = _SECRET_VALUE.sub("[redacted]", text)
    if len(text) > limit:
        return f"{text[:limit]}...[{len(text)} chars]"
    return text


def summarize_args(arguments: dict) -> dict:
    out = {}
    for key, value in arguments.items():
        if _SENSITIVE_KEY.search(key):
            out[key] = "[redacted]"
        elif isinstance(value, str):
            out[key] = _clip(value, MAX_ARG_CHARS)
        elif isinstance(value, (int, float, bool)) or value is None:
            out[key] = value
        else:
            out[key] = f"<{type(value).__name__}>"
    return out


def collect_injection_flags(result) -> list[str]:
    """Distinct injection flag names found anywhere in a tool result."""
    found: set[str] = set()

    def walk(obj, depth):
        if depth > 6:
            return
        if isinstance(obj, dict):
            for key, value in obj.items():
                if key == "injection_flags" and isinstance(value, list):
                    found.update(v for v in value if isinstance(v, str))
                elif key == "flagged_rows" and isinstance(value, list):
                    for item in value:
                        if isinstance(item, dict) and isinstance(item.get("flags"), list):
                            found.update(f for f in item["flags"] if isinstance(f, str))
                elif key == "risk_flags" and isinstance(value, list):
                    found.update(
                        v.split(":", 1)[1] for v in value
                        if isinstance(v, str) and v.startswith("injection:") and len(v) > 10
                    )
                else:
                    walk(value, depth + 1)
        elif isinstance(obj, list):
            for item in obj:
                walk(item, depth + 1)

    walk(result, 0)
    return sorted(found)


def _result_chars(result) -> int | None:
    try:
        return len(json.dumps(result, default=str))
    except Exception:
        return None


# def record(tool: str, arguments: dict, status: str, started: float,
#            result=None, error: Exception | None = None) -> None:
#     """Write one audit row. Never raises: logging must not break the tool."""
#     try:
#         row = (
#             datetime.now(timezone.utc).isoformat(timespec="seconds"),
#             SESSION_ID,
#             tool,
#             json.dumps(summarize_args(arguments), default=str),
#             status,
#             type(error).__name__ if error else None,
#             _clip(str(error), MAX_ERROR_CHARS) if error else None,
#             int((time.monotonic() - started) * 1000),
#             None if error else _result_chars(result),
#             "[]" if error else json.dumps(collect_injection_flags(result)),
#         )
#         with closing(sqlite3.connect(AUDIT_DB, timeout=3)) as conn:
#             conn.execute(SCHEMA)
#             conn.execute(
#                 "INSERT INTO audit_log (ts, session_id, tool, args_json, status, "
#                 "error_type, error_message, duration_ms, result_chars, injection_flags) "
#                 "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
#                 row,
#             )
#             conn.commit()
#     except Exception as exc:
#         print(f"[audit] failed to write log: {type(exc).__name__}", file=sys.stderr)


class AuditError(RuntimeError):
    """The audit row could not be written."""


def _build_row(tool, arguments, status, started, result=None, error=None):
    return (
        datetime.now(timezone.utc).isoformat(timespec="seconds"),
        SESSION_ID,
        tool,
        json.dumps(summarize_args(arguments), default=str),
        status,
        type(error).__name__ if error else None,
        _clip(str(error), MAX_ERROR_CHARS) if error else None,
        int((time.monotonic() - started) * 1000),
        None if error else _result_chars(result),
        "[]" if error else json.dumps(collect_injection_flags(result)),
    )


def _write_row(row) -> None:
    with closing(sqlite3.connect(AUDIT_DB, timeout=3)) as conn:
        conn.execute(SCHEMA)
        conn.execute(
            "INSERT INTO audit_log (ts, session_id, tool, args_json, status, "
            "error_type, error_message, duration_ms, result_chars, injection_flags) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            row,
        )
        conn.commit()


def record(tool, arguments, status, started, result=None, error=None) -> None:
    """Fail-open: used for read tools. Never raises."""
    try:
        _write_row(_build_row(tool, arguments, status, started, result, error))
    except Exception as exc:
        print(f"[audit] failed to write log: {type(exc).__name__}", file=sys.stderr)


def record_strict(tool, arguments, status, started, result=None, error=None) -> None:
    """Fail-closed: used before any write. Raises AuditError if the row cannot be saved."""
    try:
        _write_row(_build_row(tool, arguments, status, started, result, error))
    except Exception as exc:
        raise AuditError(
            f"audit log unavailable ({type(exc).__name__}); nothing was sent") from None









    

def audited(func):
    """Log every call to the wrapped tool (works for sync and async functions)."""
    name = func.__name__
    sig = inspect.signature(func)

    def bind(args, kwargs) -> dict:
        try:
            bound = sig.bind_partial(*args, **kwargs)
            bound.apply_defaults()
            return dict(bound.arguments)
        except TypeError:
            return dict(kwargs)

    if inspect.iscoroutinefunction(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            started = time.monotonic()
            arguments = bind(args, kwargs)
            try:
                result = await func(*args, **kwargs)
            except Exception as exc:
                record(name, arguments, "error", started, error=exc)
                raise
            record(name, arguments, "ok", started, result=result)
            return result
    else:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            started = time.monotonic()
            arguments = bind(args, kwargs)
            try:
                result = func(*args, **kwargs)
            except Exception as exc:
                record(name, arguments, "error", started, error=exc)
                raise
            record(name, arguments, "ok", started, result=result)
            return result

    return wrapper