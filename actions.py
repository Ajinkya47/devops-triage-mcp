import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from sanitize import detect_injection

ACTIONS_DB = Path(__file__).parent / "actions.db"

STATUSES = {"pending", "approved", "executed", "failed", "rejected", "expired"}
TOOL_COMMENT = "add_issue_comment"
TOOL_ASSIGN = "assign_issue"

FOOTER = "\n\n_Drafted by an AI triage assistant and approved by a human._"
MAX_ISSUE_NUMBER = 10_000_000

REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
USER_RE = re.compile(r"^(?!-)(?!.*--)(?!.*-$)[A-Za-z0-9-]{1,39}$")
ID_RE = re.compile(r"^[0-9a-f]{8}$")

# Control, invisible, and direction-changing characters (hide text or fake terminal screens)
CONTROL_CHARS = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f"
    r"\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]"
)
MENTION = re.compile(r"(?<![\w`@])@([A-Za-z0-9][A-Za-z0-9_/-]*)")
URL_PATTERN = re.compile(r"https?://|www\.", re.I)
SECRET_PATTERN = re.compile(
    r"\b(?:ghp|gho|ghs|ghu|ghr)_[A-Za-z0-9]{20,}\b"
    r"|\bgithub_pat_[A-Za-z0-9_]{20,}\b"
    r"|\bgsk_[A-Za-z0-9]{20,}\b"
    r"|\bsk-[A-Za-z0-9_-]{20,}"
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS pending_actions (
    id            TEXT PRIMARY KEY,
    created_at    REAL NOT NULL,
    expires_at    REAL NOT NULL,
    tool          TEXT NOT NULL,
    repo          TEXT NOT NULL,
    number        INTEGER NOT NULL,
    payload_json  TEXT NOT NULL,
    payload_hash  TEXT NOT NULL,
    risk_flags    TEXT NOT NULL,
    status        TEXT NOT NULL,
    decided_at    REAL,
    decided_by    TEXT,
    result_json   TEXT
)
"""


class ActionError(ValueError):
    """A write request was refused. The message is safe to show to the agent."""


@dataclass(frozen=True)
class Settings:
    allowed_repos: frozenset
    allowed_assignees: frozenset
    ttl_seconds: int = 600
    max_proposals_per_hour: int = 10
    max_pending: int = 20
    max_comment_chars: int = 1000


def _csv_set(name: str) -> frozenset:
    raw = os.getenv(name, "")
    return frozenset(x.strip().lower() for x in raw.split(",") if x.strip())


def load_settings() -> Settings:
    """Read the allowlists from the environment. Empty means disabled."""
    return Settings(
        allowed_repos=_csv_set("WRITE_ALLOWED_REPOS"),
        allowed_assignees=_csv_set("ASSIGNEES_ALLOWED"),
    )


# ---------------------------------------------------------------- helpers

def strip_control(text: str) -> str:
    return CONTROL_CHARS.sub("", text)


def _clean_text(text: str) -> str:
    text = strip_control(text).replace("\r\n", "\n").replace("\r", "\n")
    return text.strip()


def neutralize_mentions(text: str) -> tuple[str, bool]:
    """Wrap @mentions in backticks so GitHub does not notify anyone."""
    new_text, count = MENTION.subn(r"`@\1`", text)
    return new_text, count > 0


def _hash(tool: str, payload) -> str:
    blob = json.dumps({"tool": tool, "payload": payload},
                      sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _loads(text, default):
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def _check_id(action_id) -> str:
    if not isinstance(action_id, str) or not ID_RE.match(action_id):
        raise ActionError("Invalid action id")
    return action_id


def _who(name) -> str:
    return strip_control(str(name))[:40]


@contextmanager
def _db(path):
    conn = sqlite3.connect(path, timeout=5, isolation_level=None)   # autocommit
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(SCHEMA)
        yield conn
    finally:
        conn.close()


@contextmanager
def _tx(conn):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def _sweep(conn, now: float) -> int:
    cur = conn.execute(
        "UPDATE pending_actions SET status = 'expired' "
        "WHERE status = 'pending' AND expires_at <= ?", (now,))
    return cur.rowcount


def _row_to_dict(row) -> dict:
    d = dict(row)
    d["payload"] = _loads(d.pop("payload_json"), None)
    d["risk_flags"] = _loads(d["risk_flags"], [])
    result = d.pop("result_json")
    d["result"] = _loads(result, None) if result else None
    return d


def _proposal_view(row, now: float, duplicate: bool = False) -> dict:
    view = {
        "action_id": row["id"],
        "status": "pending_human_approval",
        "tool": row["tool"],
        "repo": row["repo"],
        "number": row["number"],
        "preview": _loads(row["payload_json"], None),
        "risk_flags": _loads(row["risk_flags"], []),
        "expires_in_seconds": max(0, int(row["expires_at"] - now)),
        "message": ("NOT posted. A human must approve this in a separate step. "
                    "You cannot approve it."),
    }
    if duplicate:
        view["duplicate_of_pending"] = True
    return view


# ------------------------------------------------------------- validation

def _validate_repo(repo, settings: Settings) -> str:
    if not isinstance(repo, str) or not REPO_RE.match(repo):
        raise ActionError("repo must look like 'owner/name'")
    if not settings.allowed_repos:
        raise ActionError("Writes are disabled: no repositories are on the write allowlist")
    if repo.lower() not in settings.allowed_repos:
        raise ActionError("Repository is not on the write allowlist")
    return repo


def _validate_number(number) -> int:
    if isinstance(number, bool) or not isinstance(number, int) \
            or not 1 <= number <= MAX_ISSUE_NUMBER:
        raise ActionError("Issue number must be a positive integer")
    return number


def _validate_assignee(assignee, settings: Settings) -> str:
    if not isinstance(assignee, str) or not USER_RE.match(assignee):
        raise ActionError("assignee is not a valid GitHub username")
    if not settings.allowed_assignees:
        raise ActionError("Assigning is disabled: no assignees are on the allowlist")
    if assignee.lower() not in settings.allowed_assignees:
        raise ActionError("Assignee is not on the allowlist")
    return assignee


# -------------------------------------------------------------- proposing

def _create(tool, repo, number, payload, risk_flags, settings, db_path, now) -> dict:
    digest = _hash(tool, payload)
    with _db(db_path or ACTIONS_DB) as conn:
        _sweep(conn, now)
        with _tx(conn):
            row = conn.execute(
                "SELECT * FROM pending_actions WHERE payload_hash = ? AND status = 'pending'",
                (digest,)).fetchone()
            if row is not None:                      # same request already waiting
                return _proposal_view(row, now, duplicate=True)

            recent = conn.execute(
                "SELECT COUNT(*) FROM pending_actions WHERE created_at > ?",
                (now - 3600,)).fetchone()[0]
            if recent >= settings.max_proposals_per_hour:
                raise ActionError("Proposal limit reached for this hour. "
                                  "Ask a human to review the pending actions.")
            waiting = conn.execute(
                "SELECT COUNT(*) FROM pending_actions WHERE status = 'pending'").fetchone()[0]
            if waiting >= settings.max_pending:
                raise ActionError("Too many pending actions. A human must review them first.")

            for _ in range(5):
                action_id = secrets.token_hex(4)
                try:
                    conn.execute(
                        "INSERT INTO pending_actions (id, created_at, expires_at, tool, repo, "
                        "number, payload_json, payload_hash, risk_flags, status) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')",
                        (action_id, now, now + settings.ttl_seconds, tool, repo, number,
                         json.dumps(payload), digest, json.dumps(risk_flags)))
                    break
                except sqlite3.IntegrityError:
                    continue
            else:
                raise ActionError("Could not allocate an action id")

            row = conn.execute("SELECT * FROM pending_actions WHERE id = ?",
                               (action_id,)).fetchone()
            return _proposal_view(row, now)


def propose_comment(repo, number, text, *, settings=None, db_path=None, now=None) -> dict:
    settings = settings or load_settings()
    now = time.time() if now is None else now
    repo = _validate_repo(repo, settings)
    number = _validate_number(number)
    if not isinstance(text, str):
        raise ActionError("text must be a string")

    body = _clean_text(text)
    if not body:
        raise ActionError("Comment text is empty")
    if len(body) > settings.max_comment_chars:
        raise ActionError(f"Comment is too long (max {settings.max_comment_chars} characters)")
    if SECRET_PATTERN.search(body):
        raise ActionError("Comment looks like it contains a secret and was refused")

    flags = [f"injection:{f}" for f in detect_injection(body)]
    if URL_PATTERN.search(body):
        flags.append("contains_link")
    body, mentioned = neutralize_mentions(body)
    if mentioned:
        flags.append("mentions_neutralized")

    payload = {"repo": repo, "number": number, "body": body + FOOTER}
    return _create(TOOL_COMMENT, repo, number, payload, sorted(flags), settings, db_path, now)


def propose_assign(repo, number, assignee, *, settings=None, db_path=None, now=None) -> dict:
    settings = settings or load_settings()
    now = time.time() if now is None else now
    repo = _validate_repo(repo, settings)
    number = _validate_number(number)
    assignee = _validate_assignee(assignee, settings)

    payload = {"repo": repo, "number": number, "assignee": assignee}
    return _create(TOOL_ASSIGN, repo, number, payload, [], settings, db_path, now)


# --------------------------------------------------------------- deciding

def approve_action(action_id, decided_by, *, settings=None, db_path=None, now=None) -> dict:
    """Move pending -> approved after re-checking everything. Does NOT post anything."""
    settings = settings or load_settings()
    now = time.time() if now is None else now
    _check_id(action_id)

    with _db(db_path or ACTIONS_DB) as conn:
        _sweep(conn, now)
        tampered = False
        with _tx(conn):
            row = conn.execute("SELECT * FROM pending_actions WHERE id = ?",
                               (action_id,)).fetchone()
            if row is None:
                raise ActionError("No such action")
            if row["status"] != "pending":
                raise ActionError(f"Action is {row['status']}, not pending")

            payload = _loads(row["payload_json"], None)
            if _hash(row["tool"], payload) != row["payload_hash"]:
                conn.execute(
                    "UPDATE pending_actions SET status = 'rejected', decided_at = ?, "
                    "decided_by = 'system', result_json = ? WHERE id = ?",
                    (now, json.dumps({"reason": "payload_hash_mismatch"}), action_id))
                tampered = True
            else:
                _validate_repo(payload.get("repo"), settings)
                if row["tool"] == TOOL_ASSIGN:
                    _validate_assignee(payload.get("assignee"), settings)
                cur = conn.execute(
                    "UPDATE pending_actions SET status = 'approved', decided_at = ?, "
                    "decided_by = ? WHERE id = ? AND status = 'pending' AND expires_at > ?",
                    (now, _who(decided_by), action_id, now))
                if cur.rowcount != 1:
                    raise ActionError("Action could not be approved (it may have just expired)")

        if tampered:
            raise ActionError("Payload does not match its recorded hash; action refused")
        return _row_to_dict(conn.execute("SELECT * FROM pending_actions WHERE id = ?",
                                         (action_id,)).fetchone())


def reject_action(action_id, decided_by, *, db_path=None, now=None) -> None:
    now = time.time() if now is None else now
    _check_id(action_id)
    with _db(db_path or ACTIONS_DB) as conn:
        _sweep(conn, now)
        cur = conn.execute(
            "UPDATE pending_actions SET status = 'rejected', decided_at = ?, decided_by = ? "
            "WHERE id = ? AND status = 'pending'", (now, _who(decided_by), action_id))
        if cur.rowcount != 1:
            row = conn.execute("SELECT status FROM pending_actions WHERE id = ?",
                               (action_id,)).fetchone()
            if row is None:
                raise ActionError("No such action")
            raise ActionError(f"Action is {row['status']}, not pending")


def _finish(action_id, status: str, result: dict, db_path) -> None:
    _check_id(action_id)
    blob = json.dumps(result, default=str)
    if len(blob) > 2000:
        blob = json.dumps({"note": "result too large to store"})
    with _db(db_path or ACTIONS_DB) as conn:
        cur = conn.execute(
            "UPDATE pending_actions SET status = ?, result_json = ? "
            "WHERE id = ? AND status = 'approved'", (status, blob, action_id))
        if cur.rowcount != 1:
            raise ActionError("Only an approved action can be marked finished")


def mark_executed(action_id, result: dict, *, db_path=None) -> None:
    _finish(action_id, "executed", result, db_path)


def mark_failed(action_id, error: str, *, db_path=None) -> None:
    _finish(action_id, "failed", {"error": strip_control(str(error))[:200]}, db_path)


# ---------------------------------------------------------------- reading

def expire_stale(*, db_path=None, now=None) -> int:
    now = time.time() if now is None else now
    with _db(db_path or ACTIONS_DB) as conn:
        return _sweep(conn, now)


def get_action(action_id, *, db_path=None) -> dict | None:
    if not isinstance(action_id, str) or not ID_RE.match(action_id):
        return None
    with _db(db_path or ACTIONS_DB) as conn:
        row = conn.execute("SELECT * FROM pending_actions WHERE id = ?",
                           (action_id,)).fetchone()
    return _row_to_dict(row) if row else None


def list_actions(status: str | None = None, limit: int = 50, *, db_path=None) -> list[dict]:
    if status is not None and status not in STATUSES:
        raise ActionError("Unknown status")
    limit = max(1, min(limit, 200))
    query = "SELECT * FROM pending_actions"
    params: list = []
    if status:
        query += " WHERE status = ?"
        params.append(status)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    with _db(db_path or ACTIONS_DB) as conn:
        return [_row_to_dict(r) for r in conn.execute(query, params).fetchall()]