import re
import unicodedata

from evidence import clean_body

DUPLICATE_THRESHOLD = 0.75
RELATED_THRESHOLD = 0.40
MIN_BODY_TOKENS = 5          # shorter bodies are ignored (mostly template noise)
TITLE_WEIGHT, BODY_WEIGHT = 0.7, 0.3

STOPWORDS = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "to", "of", "in",
    "on", "at", "for", "with", "when", "after", "before", "over", "than", "that",
    "this", "it", "its", "and", "or", "but", "not", "no", "from", "by", "as", "if",
    "then", "into", "i", "we", "you", "my", "our", "can", "cannot", "does", "do",
    "should", "would", "could", "has", "have", "had", "still", "now", "bug", "issue",
}

_TOKEN = re.compile(r"[a-z]+|[0-9]+")     # splits "10mb" into "10" and "mb"
_HEADING = re.compile(r"^\s*#{1,6}\s.*$", re.MULTILINE)


def _stem(token: str) -> str:
    for suffix in ("ing", "ed", "es", "s"):
        if len(token) > len(suffix) + 3 and token.endswith(suffix):
            return token[: -len(suffix)]
    return token


def tokenize(text: str | None) -> list[str]:
    text = unicodedata.normalize("NFKC", text or "").lower()
    tokens = []
    for t in _TOKEN.findall(text):
        if t in STOPWORDS or (len(t) < 2 and not t.isdigit()):
            continue
        tokens.append(_stem(t))
    return tokens


def jaccard(a: list[str], b: list[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _body_tokens(issue: dict) -> list[str]:
    text = clean_body(issue.get("body_excerpt"))   # strips checklists and placeholders
    text = _HEADING.sub(" ", text)                 # strips template headings
    return tokenize(text)


def similarity(a: dict, b: dict) -> dict:
    title_sim = jaccard(tokenize(a.get("title")), tokenize(b.get("title")))
    body_a, body_b = _body_tokens(a), _body_tokens(b)

    if len(body_a) >= MIN_BODY_TOKENS and len(body_b) >= MIN_BODY_TOKENS:
        body_sim = jaccard(body_a, body_b)
        score = TITLE_WEIGHT * title_sim + BODY_WEIGHT * body_sim
        used_body = True
    else:
        body_sim, score, used_body = 0.0, title_sim, False

    if score >= DUPLICATE_THRESHOLD:
        verdict = "likely_duplicate"
    elif score >= RELATED_THRESHOLD:
        verdict = "possibly_related"
    else:
        verdict = "different"
    return {"score": score, "title_similarity": title_sim,
            "body_similarity": body_sim, "used_body": used_body, "verdict": verdict}


def find_similar(target: dict, candidates: list[dict], top_n: int = 5) -> list[dict]:
    results = []
    for c in candidates:
        if c.get("number") == target.get("number"):
            continue
        s = similarity(target, c)
        if s["verdict"] != "different":
            results.append({"number": c["number"], **s})
    results.sort(key=lambda r: r["score"], reverse=True)
    return results[:top_n]