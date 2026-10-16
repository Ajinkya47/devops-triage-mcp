import argparse
import getpass
import json
import os
import sys
import time
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

import actions
import audit
import executor
from actions import ActionError

HERE = Path(__file__).parent


def is_dry_run() -> bool:
    """Anything unclear counts as dry run. Only 0, false, or no turns it off."""
    return os.getenv("DRY_RUN", "1").strip().lower() not in ("0", "false", "no")


def read_write_token() -> str:
    """The write token is read only here, from .env.write, and only in live mode."""
    token = (dotenv_values(HERE / ".env.write").get("GITHUB_WRITE_TOKEN") or "").strip()
    if not token:
        raise executor.ExecutionError("No GITHUB_WRITE_TOKEN found in .env.write")
    return token


def _time_left(expires_at: float, now: float) -> str:
    secs = int(expires_at - now)
    return "expired" if secs <= 0 else f"{secs // 60}m{secs % 60:02d}s"


def cmd_list(args) -> int:
    actions.expire_stale()
    rows = actions.list_actions(status=args.status, limit=args.limit)
    if not rows:
        print("No actions.")
        return 0
    now = time.time()
    clean = actions.strip_control
    print(f"{'ID':10}{'STATUS':10}{'TYPE':19}{'TARGET':34}{'EXPIRES':10}FLAGS")
    for a in rows:
        target = f"{a['repo']}#{a['number']}"
        left = _time_left(a["expires_at"], now) if a["status"] == "pending" else "-"
        flag = "!" if a["risk_flags"] else ""
        print(f"{clean(a['id']):10}{clean(a['status']):10}{clean(a['tool']):19}"
              f"{clean(target):34}{left:10}{flag}")
    return 0


def cmd_show(args) -> int:
    action = actions.get_action(args.id)
    if action is None:
        raise ActionError("No such action")
    print(executor.render_action(action, is_dry_run(), time.time()))
    return 0


def cmd_approve(args) -> int:
    dry = is_dry_run()
    sender = None if dry else executor.make_github_sender(read_write_token())
    result = executor.process_approval(
        args.id, decided_by=f"human:{getpass.getuser()}", dry_run=dry,
        sender=sender, confirm=executor.confirm_by_typing)

    outcome = result["outcome"]
    if outcome == "dry_run":
        req = result["would_send"]
        print(result["screen"])
        print("\nDRY RUN: this is what would be sent. Nothing was sent.")
        print(f"  {req['method']} {req['url']}")
        print(json.dumps(req["json"], indent=2))
        print("\nThe action is still pending. Set DRY_RUN=0 in .env to go live.")
        return 0
    if outcome == "cancelled":
        print("Cancelled. Nothing was changed.")
        return 0
    if outcome == "executed":
        print("Done.")
        for key, value in result["result"].items():
            print(f"  {key}: {actions.strip_control(str(value))}")
        return 0
    print(f"FAILED: {actions.strip_control(result['error'])}")
    return 2


def cmd_reject(args) -> int:
    started = time.monotonic()
    actions.reject_action(args.id, f"human:{getpass.getuser()}")
    audit.record("human_reject", {"action_id": args.id}, "ok", started)
    print(f"Rejected {args.id}.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Human approval for actions proposed by the triage agent.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("list", help="List actions")
    p.add_argument("--status", choices=sorted(actions.STATUSES))
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_list)

    for name, func, helptext in (("show", cmd_show, "Show one action in full"),
                                 ("approve", cmd_approve, "Approve and execute one action"),
                                 ("reject", cmd_reject, "Reject one action")):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("id")
        p.set_defaults(func=func)
    return parser


def main(argv=None, env_file=HERE / ".env") -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if env_file:
        load_dotenv(env_file)
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ActionError, executor.ExecutionError, audit.AuditError) as e:
        print(f"Error: {actions.strip_control(str(e))}")
        return 1


if __name__ == "__main__":
    sys.exit(main())