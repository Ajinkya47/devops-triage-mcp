import re
import unicodedata

# Characters that are invisible but can hide words from a human reader
ZERO_WIDTH = re.compile(r"[\u200b\u200c\u200d\u2060\ufeff]")

# Each pattern runs on normalized, lowercase text with whitespace collapsed.
# [^.]{0,N} means "a few words later, within the same sentence".
INJECTION_PATTERNS = {
    "ignore_instructions": re.compile(
        r"\b(?:ignore|disregard|forget|override|bypass)\b[^.]{0,40}"
        r"\b(?:instructions?|rules?|prompts?|guidelines?|directives?)\b"
    ),
    "role_override": re.compile(
        r"\byou are now (?:a|an|the)\b"
        r"|\bpretend (?:to be|you are)\b"
        r"|\bfrom now on,? you\b"
        r"|\bnew instructions?:"
        r"|\byour new (?:role|task|instructions?)\b"
    ),
    "prompt_leak": re.compile(
        r"\b(?:reveal|show|print|repeat|output|leak)\b[^.]{0,30}"
        r"\b(?:system prompt|your instructions|hidden prompt|initial prompt)\b"
    ),
    "tool_invocation": re.compile(
        r"\b(?:call|run|execute|invoke|use)\b[^.]{0,30}"
        r"\b(?:add_issue_comment|assign_issue|create_triage_note|query_bugs_db|sync_issues)\b"
    ),
    "destructive_request": re.compile(
        r"\b(?:delete|drop|close|remove|wipe|erase)\s+(?:all|every|everything)\b[^.]{0,20}"
        r"\b(?:issues?|data|tables?|repos?|repositories|records?|database)\b"
        r"|\b(?:delete|wipe|erase)\s+everything\b"
        r"|\bdrop\s+table\b"
    ),
    "severity_manipulation": re.compile(
        r"\b(?:mark|rank|rate|set|label|classify|treat)\b[^.]{0,25}"
        r"\b(?:this|it|me)\b[^.]{0,25}"
        r"\b(?:critical|highest|top priority|p0|urgent)\b"
    ),
    "chat_template_tokens": re.compile(
        r"</?\s*(?:system|assistant|user|human|instructions?|untrusted_issue_text)\s*>"
        r"|\[/?(?:inst|system)\]"
        r"|<\|(?:im_start|im_end|system|endoftext)\|>"
    ),
    "secret_exfiltration": re.compile(
        r"\b(?:send|post|upload|email|exfiltrate|leak)\b[^.]{0,40}"
        r"\b(?:api[ _-]?keys?|tokens?|secrets?|credentials?|passwords?)\b"
    ),
}

_OUR_TAG = re.compile(r"<\s*/?\s*untrusted_issue_text[^>]*>", re.IGNORECASE)


def _normalize(text: str) -> str:
    """Make sneaky formatting (fullwidth letters, zero-width chars, caps) comparable."""
    text = unicodedata.normalize("NFKC", text)
    text = ZERO_WIDTH.sub("", text)
    text = text.lower()
    return re.sub(r"\s+", " ", text)


def detect_injection(text: str) -> list[str]:
    """Return the names of suspicious patterns found. Names only, never the matched text."""
    if not text:
        return []
    flags = []
    if ZERO_WIDTH.search(text):
        flags.append("hidden_characters")
    normalized = _normalize(text)
    for name, pattern in INJECTION_PATTERNS.items():
        if pattern.search(normalized):
            flags.append(name)
    return flags


def wrap_untrusted(text: str, source: str = "issue_body") -> str:
    """Wrap text in markers, and remove any fake copy of our markers from inside it."""
    if not re.fullmatch(r"[a-z_]{1,30}", source):
        raise ValueError("source must be lowercase letters and underscores")
    cleaned = unicodedata.normalize("NFKC", text)
    cleaned = ZERO_WIDTH.sub("", cleaned)
    cleaned = _OUR_TAG.sub("[tag removed]", cleaned)
    return f'<untrusted_issue_text source="{source}">\n{cleaned}\n</untrusted_issue_text>'


def sanitize_issue_text(text: str | None, source: str = "issue_body",
                        max_chars: int = 4000) -> dict:
    """Scan, cap, and wrap one piece of issue text."""
    text = text or ""
    flags = detect_injection(text)          # scan the FULL text before cutting it
    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars] + "\n[truncated]"
    return {
        "text": wrap_untrusted(text, source),
        "injection_flags": flags,
        "truncated": truncated,
    }