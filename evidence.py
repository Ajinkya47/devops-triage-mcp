import re
from sanitize import detect_injection

WEIGHTS = {
    "repro_steps": 20,
    "code_example": 10,
    "stack_trace": 20,
    "version_info": 10,
    "expected_vs_actual": 10,
    "independent_reporters": 20,   # 10 for one reporter, 20 for two or more
    "reactions": 10,               # 5 for 3+, 10 for 10+
}
INJECTION_SCORE_CAP = 40

# ---- Template noise removal: checklists and placeholders must not count as evidence ----
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_CHECKLIST_LINE = re.compile(r"^\s*[-*]\s*\[[ xX]\].*$", re.MULTILINE)
_PLACEHOLDER = re.compile(r"_No response_")

# ---- Evidence signals ----
_NUMBERED_STEP = re.compile(r"^\s*\d+[.)]\s+\S", re.MULTILINE)
_REPRO_PHRASE = re.compile(
    r"\b(?:steps? to reproduce|to reproduce|reproduction|repro|minimal example)\b", re.I)
_CODE_BLOCK = re.compile(r"```.+?```", re.DOTALL)
_STACK_TRACE = re.compile(
    r"Traceback \(most recent call last\)"
    r"|^\s*File \".+\", line \d+"
    r"|^\s+at [\w.$<>]+\(.+:\d+(?::\d+)?\)"
    r"|\bpanic:"
    r"|\bsegmentation fault\b"
    r"|\bUnhandled(?:Exception| exception)\b"
    r"|\bException in thread\b",
    re.MULTILINE | re.I,
)
_VERSION = re.compile(
    r"\b(?:version|python|node(?:js)?|java|ubuntu|windows|macos)\b[^\n]{0,30}?\d+\.\d+"
    r"|==\s*\d+\.\d+"
    r"|\b[\w-]+@\d+\.\d+",
    re.I,
)
_EXPECTED = re.compile(r"\bexpected\b", re.I)
_ACTUAL = re.compile(r"\b(?:actual|instead|but (?:got|get|received)|observed)\b", re.I)

_CONFIRM = re.compile(
    r"\b(?:i|we)\s+(?:also\s+|independently\s+)?"
    r"(?:reproduced|can reproduce|could reproduce|can confirm|confirm|"
    r"hit this|see this|saw this|am seeing|are seeing|encountered this|"
    r"experienced this|have (?:the same|this))\b"
    r"|\bsame (?:here|issue|problem|error|bug|behaviou?r)\b"
    r"|\bme too\b"
    r"|\b(?:also|still) (?:seeing|affected|happens|happening|occurs|occurring)\b",
    re.I,
)

# ---- Claims (not evidence) ----
_URGENCY = re.compile(
    r"\b(?:urgent|critical|asap|blocker|emergency|immediately|showstopper)\b|!{3,}", re.I)
_IMPACT = {
    "data_loss": r"\b(?:data loss|lost data|loses data|corrupt\w*|overwrit\w+)\b",
    "security": r"\b(?:vulnerab\w+|cve-\d+|security|exploit\w*|leak\w*|exposed|credentials?|rce|xss)\b",
    "crash_or_hang": r"\b(?:crash\w*|segfault|panic|deadlock\w*|hangs?|freez\w+|infinite loop)\b",
    "money": r"\b(?:payments?|charg(?:e|ed|es|ing)|billing|refund\w*|invoice\w*)\b",
    "cosmetic_or_docs": r"\b(?:typo|spelling|docs?|documentation|readme|cosmetic)\b",
}
_IMPACT = {k: re.compile(v, re.I) for k, v in _IMPACT.items()}


def clean_body(body: str | None) -> str:
    text = body or ""
    text = _HTML_COMMENT.sub(" ", text)
    text = _CHECKLIST_LINE.sub(" ", text)
    return _PLACEHOLDER.sub(" ", text)


def count_independent_reporters(issue: dict, comments: list[dict]) -> int:
    """Distinct people, other than the author, who say they hit the same problem."""
    author = ((issue.get("user") or {}).get("login") or "").lower()
    seen = set()
    for c in comments:
        user = c.get("user") or {}
        login = (user.get("login") or "").lower()
        if not login or login == author:
            continue
        if user.get("type") == "Bot" or login.endswith("[bot]"):
            continue
        if _CONFIRM.search(c.get("body") or ""):
            seen.add(login)
    return len(seen)


def _level(score: int) -> str:
    if score >= 60:
        return "strong"
    if score >= 35:
        return "moderate"
    if score >= 15:
        return "weak"
    return "insufficient"


def assess_evidence(issue: dict, comments: list[dict]) -> dict:
    title = issue.get("title") or ""
    raw_body = issue.get("body") or ""
    body = clean_body(raw_body)

    # --- signals from the issue text ---
    has_code = bool(_CODE_BLOCK.search(body))
    numbered = len(_NUMBERED_STEP.findall(body))
    signals = {
        "repro_steps": numbered >= 2 or (bool(_REPRO_PHRASE.search(body)) and has_code),
        "code_example": has_code,
        "stack_trace": bool(_STACK_TRACE.search(body)),
        "version_info": bool(_VERSION.search(body)),
        "expected_vs_actual": bool(_EXPECTED.search(body) and _ACTUAL.search(body)),
    }

    score = sum(WEIGHTS[name] for name, found in signals.items() if found)

    # --- signals from other people ---
    reporters = count_independent_reporters(issue, comments)
    score += 0 if reporters == 0 else (10 if reporters == 1 else 20)

    reactions = (issue.get("reactions") or {}).get("total_count", 0)
    score += 10 if reactions >= 10 else (5 if reactions >= 3 else 0)

    # --- injection: flagged text may be trying to inflate its own score ---
    scanned = [title, raw_body] + [c.get("body") or "" for c in comments]
    injection_flags = sorted({f for t in scanned for f in detect_injection(t)})
    flags = []
    if injection_flags and score > INJECTION_SCORE_CAP:
        score = INJECTION_SCORE_CAP
        flags.append("score_capped_injection")

    score = max(0, min(score, 100))
    level = _level(score)

    # --- claims, kept separate from evidence ---
    claim_text = f"{title}\n{body}"
    claimed_impact = [name for name, rx in _IMPACT.items() if rx.search(claim_text)]
    if _URGENCY.search(claim_text) and score < 35:
        flags.append("unverified_urgency")
    if level == "insufficient":
        flags.append("needs_more_info")

    # --- plain-language reasons (fixed strings, never issue text) ---
    labels = {
        "repro_steps": "reproduction steps",
        "code_example": "a code example",
        "stack_trace": "a stack trace or error trace",
        "version_info": "version or environment details",
        "expected_vs_actual": "expected vs actual behaviour",
    }
    reasons = [f"Has {labels[k]}" if v else f"No {labels[k]} found" for k, v in signals.items()]
    reasons.append(f"{reporters} independent reporter(s) confirmed it")
    reasons.append(f"{reactions} reaction(s)")
    if "score_capped_injection" in flags:
        reasons.append("Score capped because the text contains injection-like content")
    if "unverified_urgency" in flags:
        reasons.append("Urgency words used without supporting evidence")

    return {
        "evidence_score": score,
        "evidence_level": level,
        "signals": signals,
        "independent_reporters": reporters,
        "reactions": reactions,
        "claimed_impact": claimed_impact,
        "injection_flags": injection_flags,
        "flags": flags,
        "reasons": reasons,
        "comments_scanned": len(comments),
    }