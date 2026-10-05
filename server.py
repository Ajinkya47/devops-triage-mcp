import os
import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

import sys

load_dotenv()
mcp = FastMCP("devops-triage")

GITHUB_API = "https://api.github.com"

token = os.getenv("GITHUB_TOKEN")
print("TOKEN LOADED:", bool(token), file=sys.stderr)   # temporary debug line

HEADERS = {"Accept": "application/vnd.github+json"}
if token:
    HEADERS["Authorization"] = f"Bearer {token}"


@mcp.tool()
async def list_issues(
    repo: str,
    state: str = "open",
    labels: str = "",
    since: str = "",
    limit: int = 20,
) -> list[dict]:
    """List issues for a GitHub repo (read-only).

    Args:
        repo: Repository in 'owner/name' format, e.g. 'modelcontextprotocol/python-sdk'
        state: 'open', 'closed', or 'all'
        labels: Comma-separated labels to filter by, e.g. 'bug' or 'bug,security'
        since: Only issues updated after this ISO date, e.g. '2026-09-28T00:00:00Z'
        limit: Max issues to return (1-50)
    """
    if state not in ("open", "closed", "all"):
        raise ValueError("state must be 'open', 'closed', or 'all'")
    limit = max(1, min(limit, 50))

    params = {"state": state, "per_page": limit}
    if labels:
        params["labels"] = labels
    if since:
        params["since"] = since

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            f"{GITHUB_API}/repos/{repo}/issues", headers=HEADERS, params=params
        )
        r.raise_for_status()

    return [
        {
            "number": i["number"],
            "title": i["title"],
            "labels": [l["name"] for l in i["labels"]],
            "reactions": i["reactions"]["total_count"],
            "comments": i["comments"],
            "created_at": i["created_at"],
            "url": i["html_url"],
        }
        for i in r.json()
        if "pull_request" not in i  # the issues endpoint also returns PRs
    ]

@mcp.tool()
async def get_issue(repo: str, number: int, max_comments: int = 10) -> dict:
    """Get one GitHub issue with its body and recent comments (read-only).

    Args:
        repo: Repository in 'owner/name' format
        number: Issue number, e.g. 9185
        max_comments: Max comments to include (1-20)
    """
    max_comments = max(1, min(max_comments, 20))
    async with httpx.AsyncClient(timeout=15) as client:
        issue = await client.get(
            f"{GITHUB_API}/repos/{repo}/issues/{number}", headers=HEADERS
        )
        issue.raise_for_status()
        comments = await client.get(
            f"{GITHUB_API}/repos/{repo}/issues/{number}/comments",
            headers=HEADERS,
            params={"per_page": max_comments},
        )
        comments.raise_for_status()

    i = issue.json()
    return {
        "number": i["number"],
        "title": i["title"],
        "state": i["state"],
        "labels": [l["name"] for l in i["labels"]],
        "author": i["user"]["login"],
        "body": (i["body"] or "")[:4000],   # cap size to protect tokens
        "comments": [
            {"author": c["user"]["login"], "body": c["body"][:1000]}
            for c in comments.json()
        ],
        "url": i["html_url"],
    }

if __name__ == "__main__":
    mcp.run()