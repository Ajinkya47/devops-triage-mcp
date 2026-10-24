import sys
import time

import httpx

import actions
import audit
from actions import ActionError

GITHUB_API = "https://api.github.com"
MAX_DISPLAY_CHARS = 3000


class ExecutionError(Exception):
    """A write could not be completed. The message is safe to show to a human."""

    def __init__(self, message: str, outcome_unknown: bool = False):
        super().__init__(message)
        self.outcome_unknown = outcome_unknown


# ------------------------------------------------ building the GitHub request

def build_request(action: dict) -> dict:
    """Turn a stored action into an HTTP request. Re-validates everything."""
    payload = action.get("payload")
    if not isinstance(payload, dict):
        raise ExecutionError("Action has no valid payload")

    repo, number = payload.get("repo"), payload.get("number")
    if (not isinstance(repo, str) or not actions.REPO_RE.match(repo)
            or any(part in (".", "..") for part in repo.split("/"))
            or isinstance(number, bool) or not isinstance(number, int) or number < 1):
        raise ExecutionError("Action target is not valid")

    base = f"{GITHUB_API}/repos/{repo}/issues/{number}"
    tool = action.get("tool")

    if tool == actions.TOOL_COMMENT:
        body = payload.get("body")
        if not isinstance(body, str) or not body.strip():
            raise ExecutionError("Comment body is not valid")
        return {"method": "POST", "url": f"{base}/comments", "json": {"body": body}}

    if tool == actions.TOOL_ASSIGN:
        assignee = payload.get("assignee")
        if not isinstance(assignee, str) or not actions.USER_RE.match(assignee):
            raise ExecutionError("Assignee is not valid")
        return {"method": "POST", "url": f"{base}/assignees",
                "json": {"assignees": [assignee]}}

    raise ExecutionError("Unknown action type")


def interpret_response(action: dict, response: dict) -> dict:
    """Decide whether GitHub really did what we asked."""
    status = response.get("status")
    data = response.get("json")
    data = data if isinstance(data, dict) else {}

    if status not in (200, 201):
        raise ExecutionError(
            f"GitHub returned HTTP {status}",
            outcome_unknown=isinstance(status, int) and status >= 500,
        )

    if action["tool"] == actions.TOOL_COMMENT:
        return {"comment_url": data.get("html_url"), "comment_id": data.get("id")}

    # GitHub can answer 201 and silently skip users who cannot be assigned
    wanted = action["payload"]["assignee"].lower()
    assigned = [str(a.get("login") or "").lower()
                for a in (data.get("assignees") or []) if isinstance(a, dict)]
    if wanted not in assigned:
        raise ExecutionError(
            "GitHub did not apply the assignment (the user may not be assignable)")
    return {"assignees": assigned}


def make_github_sender(token: str, client: httpx.Client | None = None):
    """Return a function that sends one request. It never retries."""
    def send(request: dict) -> dict:
        own = client is None
        http = httpx.Client(timeout=15, follow_redirects=False) if own else client
        try:
            r = http.request(
                request["method"], request["url"], json=request["json"],
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
        except (httpx.ConnectError, httpx.ConnectTimeout):
            raise ExecutionError("Could not connect to GitHub",
                                 outcome_unknown=False) from None
        except httpx.HTTPError:
            raise ExecutionError("The request failed after it was sent",
                                 outcome_unknown=True) from None
        finally:
            if own:
                http.close()
        try:
            body = r.json()
        except ValueError:
            body = None
        return {"status": r.status_code, "json": body}
    return send


# ------------------------------------------------------ what the human sees

def _safe(text, limit: int = MAX_DISPLAY_CHARS) -> str:
    text = actions.strip_control(str(text)).replace("\r", "\n")
    if len(text) > limit:
        return text[:limit] + f"\n[... {len(text) - limit} more characters not shown]"
    return text


def _describe_flag(flag) -> str:
    flag = actions.strip_control(str(flag))[:60]
    if flag.startswith("injection:"):
        return (f"contains injection-like text ({flag.split(':', 1)[1]}). "
                "The agent may have been manipulated by an issue.")
    if flag == "contains_link":
        return "contains a link. Check where it goes."
    if flag == "mentions_neutralized":
        return "had @mentions, now wrapped so nobody is notified."
    return f"flag: {flag}"

# ----------------------------------------------- checking where it will land

def lookup_issue(repo, number, token=None, client=None) -> dict:
    """Read-only look at the target issue, for the human's screen. Never raises."""
    unavailable = {"status": "unavailable"}
    if (not isinstance(repo, str) or not actions.REPO_RE.match(repo)
            or any(part in (".", "..") for part in repo.split("/"))
            or isinstance(number, bool) or not isinstance(number, int) or number < 1):
        return unavailable

    headers = {"Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    own = client is None
    http = httpx.Client(timeout=10, follow_redirects=False) if own else client
    try:
        r = http.get(f"{GITHUB_API}/repos/{repo}/issues/{number}", headers=headers)
    except httpx.HTTPError:
        return unavailable
    finally:
        if own:
            http.close()

    if r.status_code == 404:
        return {"status": "not_found"}
    if r.status_code != 200:
        return unavailable
    try:
        data = r.json()
    except ValueError:
        return unavailable
    if not isinstance(data, dict):
        return unavailable
    return {"status": "ok", "title": str(data.get("title") or ""),
            "state": str(data.get("state") or ""),
            "is_pull_request": "pull_request" in data}


def _target_lines(target) -> list:
    if not isinstance(target, dict):
        return []
    status = target.get("status")
    if status == "ok":
        title = _safe(target.get("title", ""), 120).replace("\n", " ")
        state = _safe(target.get("state", ""), 20)
        pr = bool(target.get("is_pull_request"))
        lines = [f" Title  : {title}  ({state} {'pull request' if pr else 'issue'})"]
        if pr:
            lines.append("  ! This target is a pull request, not an issue.")
        return lines
    if status == "not_found":
        return [" Title  : (not found)",
                "  ! No such issue was found. It may not exist, or your read token cannot see it."]
    return [" Title  : (could not be checked)"]

def render_action(action: dict, dry_run: bool, now: float, target=None) -> str:
    payload = action.get("payload") or {}
    secs = int(action.get("expires_at", 0) - now)
    expires = f"{secs // 60}m{secs % 60:02d}s left" if secs > 0 else "expired"
    mode = ("DRY RUN (nothing will be sent)" if dry_run
            else "LIVE (this WILL change GitHub)")
    line = "=" * 60

    out = [
        line,
        f" ACTION {_safe(action.get('id'))}  |  {_safe(action.get('tool'))}",
        line,
        f" Target : {_safe(action.get('repo'))}  issue #{_safe(action.get('number'))}",
        *_target_lines(target),
        f" Status : {_safe(action.get('status'))} ({expires})",
        f" Mode   : {mode}",
    ]

    flags = action.get("risk_flags") or []
    if flags:
        out += ["", " WARNING"]
        out += [f"  ! {_describe_flag(f)}" for f in flags]

    if action.get("tool") == actions.TOOL_COMMENT:
        out += ["", " EXACT TEXT THAT WILL BE POSTED", " " + "-" * 58,
                _safe(payload.get("body", "")), " " + "-" * 58]
    else:
        out += ["", f" WILL BE ASSIGNED TO: @{_safe(payload.get('assignee', ''), 60)}"]

    out += ["", " This text came from an AI agent. If you did not expect it, reject it."]
    return "\n".join(out)

def confirm_by_typing(screen: str, action_id: str, *, input_fn=input,
                      isatty=None, out=print) -> bool:
    """Show the screen and require the human to type the action id."""
    if isatty is None:
        isatty = sys.stdin.isatty
    if not isatty():
        raise ExecutionError(
            "Approval needs an interactive terminal; it cannot be piped or scripted")
    out(screen)
    answer = input_fn(f"\nType the action id ({action_id}) to approve, "
                      "or press Enter to cancel: ")
    return answer.strip() == action_id


# ---------------------------------------------------- the whole approval flow

def _audit_args(action: dict, mode: str) -> dict:
    return {"action_id": action.get("id"), "action_tool": action.get("tool"),
            "repo": action.get("repo"), "number": action.get("number"), "mode": mode}


def process_approval(action_id, *, decided_by, dry_run, sender, confirm,
                settings=None, db_path=None, now=None, lookup=None) -> dict:
    started = time.monotonic()
    now = time.time() if now is None else now

    actions.expire_stale(db_path=db_path, now=now)
    action = actions.get_action(action_id, db_path=db_path)
    if action is None:
        raise ActionError("No such action")
    if action["status"] != "pending":
        raise ActionError(f"Action is {action['status']}, not pending")

    request = build_request(action)              # fails here, before any state change
    payload = action.get("payload") or {}
    try:
        target = lookup(payload.get("repo"), payload.get("number")) if lookup else None
    except Exception:
        target = {"status": "unavailable"}
    screen = render_action(action, dry_run, now, target)

    if dry_run:
        audit.record("human_dry_run", _audit_args(action, "dry_run"), "ok", started)
        return {"outcome": "dry_run", "screen": screen, "would_send": request}

    if sender is None:
        raise ExecutionError("No sender is configured for live mode")

    if not confirm(screen, action["id"]):
        audit.record("human_cancel", _audit_args(action, "live"), "ok", started)
        return {"outcome": "cancelled"}

    approved = actions.approve_action(action["id"], decided_by,
                                      settings=settings, db_path=db_path, now=now)

    # Fail-closed: no audit row, no write
    try:
        audit.record_strict("human_approve", _audit_args(approved, "live"), "ok", started)
    except audit.AuditError:
        actions.mark_failed(action["id"], "audit log unavailable; nothing was sent",
                            db_path=db_path)
        raise

    try:
        result = interpret_response(approved, sender(request))
    except Exception as exc:
        err = exc if isinstance(exc, ExecutionError) else ExecutionError(
            f"unexpected error ({type(exc).__name__})", outcome_unknown=True)
        note = str(err) + (" OUTCOME UNKNOWN: check GitHub before proposing again"
                           if err.outcome_unknown else "")
        actions.mark_failed(action["id"], note, db_path=db_path)
        audit.record("human_execute", _audit_args(approved, "live"), "error",
                     started, error=err)
        return {"outcome": "failed", "error": note,
                "outcome_unknown": err.outcome_unknown}

    actions.mark_executed(action["id"], result, db_path=db_path)
    audit.record("human_execute", _audit_args(approved, "live"), "ok",
                 started, result=result)
    return {"outcome": "executed", "result": result}