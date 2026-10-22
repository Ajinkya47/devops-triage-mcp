"""Create the evaluation issues in YOUR test repo. Run by you, never by the agent.

Dry run by default: it only prints what it would do. Add --create to really do it.
"""
import argparse
import re
import sys
import time
from pathlib import Path

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from evals.seed_data import ISSUES  # noqa: E402

API = "https://api.github.com"
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
PAUSE_SECONDS = 3.0


def norm(text) -> str:
    return (text or "").replace("\r\n", "\n").strip()


def plan_changes(existing: dict, issues) -> tuple:
    """existing maps title -> {'number': n, 'body': text}. Returns (missing, differing)."""
    missing = [i for i in issues if i.title not in existing]
    differing = [(i, existing[i.title]["number"]) for i in issues
                 if i.title in existing and norm(existing[i.title]["body"]) != norm(i.body)]
    return missing, differing


def fetch_existing(client, repo, headers) -> dict:
    existing = {}
    for page in range(1, 6):
        r = client.get(f"{API}/repos/{repo}/issues", headers=headers,
                       params={"state": "all", "per_page": 100, "page": page})
        r.raise_for_status()
        batch = r.json()
        for item in batch:
            if isinstance(item, dict) and "pull_request" not in item:
                existing.setdefault(item["title"], {"number": item["number"],
                                                    "body": item.get("body")})
        if len(batch) < 100:
            break
    return existing


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Create the eval issues in your test repo.")
    parser.add_argument("--repo", help="owner/name (default: DEFAULT_REPO from .env.agent)")
    parser.add_argument("--create", action="store_true", help="really create issues")
    parser.add_argument("--sync-bodies", action="store_true",
                        help="with --create: also update existing issues whose body differs")
    args = parser.parse_args(argv)

    agent_env = dotenv_values(ROOT / ".env.agent")
    main_env = dotenv_values(ROOT / ".env")
    token = (dotenv_values(ROOT / ".env.write").get("GITHUB_WRITE_TOKEN") or "").strip()

    repo = (args.repo or agent_env.get("DEFAULT_REPO") or "").strip()
    if not REPO_RE.match(repo):
        print("Give the repo as --repo owner/name (or set DEFAULT_REPO in .env.agent).")
        return 2
    allowed = {x.strip().lower()
               for x in (main_env.get("WRITE_ALLOWED_REPOS") or "").split(",") if x.strip()}
    if repo.lower() not in allowed:
        print("That repo is not in WRITE_ALLOWED_REPOS in .env. Refusing to write to it.")
        return 2
    if not token:
        print("No GITHUB_WRITE_TOKEN found in .env.write.")
        return 2

    headers = {"Authorization": f"Bearer {token}",
               "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}

    with httpx.Client(timeout=20, follow_redirects=False) as client:
        try:
            existing = fetch_existing(client, repo, headers)
        except httpx.HTTPError as exc:
            print(f"Could not read the repository ({type(exc).__name__}).")
            return 1

        missing, differing = plan_changes(existing, ISSUES)
        print(f"Repo: {repo}   issues found: {len(existing)}   seed issues: {len(ISSUES)}")
        print(f"\nTo create ({len(missing)}):")
        for i in missing:
            print(f"  + {i.title}")
        print(f"\nExisting issues whose body differs from the seed ({len(differing)}):")
        for i, number in differing:
            print(f"  ~ #{number} {i.title}")

        if not args.create:
            print("\nDry run. Nothing was changed. Add --create to apply "
                  "(and --sync-bodies to update the differing ones).")
            return 0

        created = 0
        for i in missing:
            r = client.post(f"{API}/repos/{repo}/issues", headers=headers,
                            json={"title": i.title, "body": i.body})
            if r.status_code != 201:
                print(f"GitHub returned HTTP {r.status_code}. Stopped after creating {created}.")
                return 1
            created += 1
            print(f"  created #{r.json().get('number')} {i.title}")
            time.sleep(PAUSE_SECONDS)

        if args.sync_bodies:
            for i, number in differing:
                r = client.patch(f"{API}/repos/{repo}/issues/{number}", headers=headers,
                                 json={"body": i.body})
                if r.status_code != 200:
                    print(f"GitHub returned HTTP {r.status_code} while updating #{number}.")
                    return 1
                print(f"  updated #{number}")
                time.sleep(PAUSE_SECONDS)
    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())