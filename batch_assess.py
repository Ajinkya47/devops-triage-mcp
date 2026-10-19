import asyncio

import httpx

from evidence import assess_evidence

MAX_BATCH = 8
CONCURRENCY = 4
COMMENTS_PER_ISSUE = 30


def compact(number: int, result: dict) -> dict:
    """Numbers and names only. No issue text, so there is no injection surface."""
    return {
        "number": number,
        "evidence_score": result["evidence_score"],
        "evidence_level": result["evidence_level"],
        "signals_found": sorted(k for k, v in result["signals"].items() if v),
        "independent_reporters": result["independent_reporters"],
        "reactions": result["reactions"],
        "claimed_impact": result["claimed_impact"],
        "flags": result["flags"],
        "injection_flags": result["injection_flags"],
    }


async def assess_many(client, api_base: str, headers: dict, repo: str, numbers: list) -> list:
    """Assess several issues, a few at a time. One failure never loses the others."""
    gate = asyncio.Semaphore(CONCURRENCY)

    async def one(number: int) -> dict:
        async with gate:
            try:
                resp = await client.get(f"{api_base}/repos/{repo}/issues/{number}",
                                        headers=headers)
                if resp.status_code == 404:
                    return {"number": number, "error": "not found"}
                resp.raise_for_status()
                issue = resp.json()
                if not isinstance(issue, dict):
                    return {"number": number, "error": "unexpected response"}
                if "pull_request" in issue:
                    return {"number": number, "error": "this is a pull request, not an issue"}

                resp = await client.get(f"{api_base}/repos/{repo}/issues/{number}/comments",
                                        headers=headers,
                                        params={"per_page": COMMENTS_PER_ISSUE})
                resp.raise_for_status()
                comments = resp.json()
                comments = [c for c in comments if isinstance(c, dict)] \
                    if isinstance(comments, list) else []
                return compact(number, assess_evidence(issue, comments))
            except httpx.HTTPStatusError as exc:
                return {"number": number,
                        "error": f"GitHub returned HTTP {exc.response.status_code}"}
            except (httpx.HTTPError, ValueError):
                return {"number": number, "error": "request failed"}
            except Exception:
                return {"number": number, "error": "assessment failed"}

    return list(await asyncio.gather(*(one(n) for n in numbers)))