import json
import re
from dataclasses import dataclass

from audit import collect_injection_flags

MODE_READ = "read_only"
MODE_PROPOSE = "proposals"

READ_TOOLS = frozenset({
    "list_issues", "get_issue", "sync_issues",
    "query_bugs_db", "assess_issue", "assess_issues", "find_duplicates",
})
PROPOSE_TOOLS = frozenset({"propose_comment", "propose_assign"})

# Any tool whose name looks like approving, executing, or running code is never exposed
DENY_NAME = re.compile(
    r"approv|confirm|exec|decid|reject|apply|run_action|shell|eval|admin", re.I)

for _name in READ_TOOLS | PROPOSE_TOOLS:
    if DENY_NAME.search(_name):
        raise RuntimeError(f"allowlisted tool name looks dangerous: {_name}")

# Ceilings that keep results small. An argument that is missing gets the ceiling.
CLAMPS = {
    "list_issues": {"limit": 20},
    "get_issue": {"max_comments": 3},
    "sync_issues": {"limit": 50},
    "find_duplicates": {"top_n": 5},
}

MAX_RAW_ARGS_CHARS = 4000
MAX_STRING_ARG_CHARS = 2000
DEFAULT_RESULT_CHARS = 6000
_FLAG_NAME = re.compile(r"^[a-z_]{1,40}$")


class ToolRefused(Exception):
    """The call was refused. The message is safe to send back to the model."""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    schema: dict


@dataclass(frozen=True)
class PreparedCall:
    name: str
    arguments: dict
    clamped: tuple = ()      # arguments that were lowered or filled in with a ceiling
    coerced: tuple = ()      # arguments converted, e.g. "5" to 5


def spec_from_mcp(tool) -> ToolSpec:
    return ToolSpec(
        name=tool.name,
        description=tool.description or "",
        schema=tool.inputSchema or {"type": "object", "properties": {}},
    )


# ------------------------------------------------------- choosing the tools

def select_tools(available, mode: str, only=None) -> dict:
    """Return {name: ToolSpec} for the tools the model may see in this mode."""
    if mode == MODE_READ:
        allowed = READ_TOOLS
    elif mode == MODE_PROPOSE:
        allowed = READ_TOOLS | PROPOSE_TOOLS
    else:
        raise ValueError("Unknown mode")
    if only is not None:
        allowed = allowed & frozenset(only)
    return {
        spec.name: spec for spec in available
        if spec.name in allowed and not DENY_NAME.search(spec.name)
    }


def _strip_titles(node, in_properties=False):
    """Drop schema 'title' labels (wasted tokens) but keep a parameter named 'title'."""
    if isinstance(node, dict):
        out = {}
        for key, value in node.items():
            if not in_properties and key == "title" and isinstance(value, str):
                continue
            out[key] = _strip_titles(value, in_properties=(key == "properties" and not in_properties))
        return out
    if isinstance(node, list):
        return [_strip_titles(v) for v in node]
    return node


def to_openai_tools(exposed: dict) -> list:
    tools = []
    for name in sorted(exposed):
        spec = exposed[name]
        schema = _strip_titles(spec.schema) if spec.schema else {}
        schema.setdefault("type", "object")
        schema.setdefault("properties", {})
        tools.append({"type": "function", "function": {
            "name": spec.name,
            "description": spec.description.strip(),
            "parameters": schema,
        }})
    return tools


# ------------------------------------------------------ checking a tool call

def _clip_name(value) -> str:
    return re.sub(r"[^\w.\-]", "?", str(value))[:40]


def _type_ok(value, expected: str) -> bool:
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    python_type = {"string": str, "array": list, "object": dict}.get(expected)
    return python_type is None or isinstance(value, python_type)


def _coerce(value, expected: str):
    if expected == "integer":
        if isinstance(value, str) and re.fullmatch(r"-?\d{1,9}", value.strip()):
            return int(value.strip()), True
        if isinstance(value, float) and value.is_integer():
            return int(value), True
    return value, False


def prepare_call(name, raw_arguments, exposed: dict) -> PreparedCall:
    """Validate one tool call from the model. Raises ToolRefused with a safe message."""
    if not isinstance(name, str) or name not in exposed:
        raise ToolRefused(
            f"Tool '{_clip_name(name)}' is not available. "
            f"Available tools: {', '.join(sorted(exposed))}.")
    spec = exposed[name]

    # ---- arguments must become a dict
    if raw_arguments is None or (isinstance(raw_arguments, str) and not raw_arguments.strip()):
        args = {}
    elif isinstance(raw_arguments, dict):
        args = dict(raw_arguments)
    elif isinstance(raw_arguments, str):
        if len(raw_arguments) > MAX_RAW_ARGS_CHARS:
            raise ToolRefused("Arguments are too large.")
        try:
            args = json.loads(raw_arguments)
        except ValueError:
            raise ToolRefused("Arguments must be a valid JSON object.") from None
    else:
        raise ToolRefused("Arguments must be a JSON object.")
    if not isinstance(args, dict):
        raise ToolRefused("Arguments must be a JSON object.")

    # ---- names
    schema = spec.schema or {}
    props = schema.get("properties") or {}
    unknown = sorted(map(str, set(args) - set(props)))
    if unknown:
        raise ToolRefused(
            f"Unknown argument(s) for {name}: {', '.join(_clip_name(k) for k in unknown)}. "
            f"Allowed: {', '.join(sorted(props)) or 'none'}.")
    missing = [r for r in (schema.get("required") or []) if r not in args]
    if missing:
        raise ToolRefused(f"Missing required argument(s) for {name}: {', '.join(missing)}.")

    # ---- types, enums, sizes
    coerced = []
    for key in list(args):
        prop = props[key] if isinstance(props.get(key), dict) else {}
        expected = prop.get("type")
        if isinstance(expected, str) and not _type_ok(args[key], expected):
            new_value, converted = _coerce(args[key], expected)
            if not (converted and _type_ok(new_value, expected)):
                raise ToolRefused(f"Argument '{key}' must be of type {expected}.")
            args[key] = new_value
            coerced.append(key)
        enum = prop.get("enum")
        if isinstance(enum, list) and args[key] not in enum:
            raise ToolRefused(f"Argument '{key}' must be one of: {', '.join(map(str, enum))}.")
        if isinstance(args[key], str) and len(args[key]) > MAX_STRING_ARG_CHARS:
            raise ToolRefused(f"Argument '{key}' is too long.")

    # ---- keep results small
    clamped = []
    for key, ceiling in CLAMPS.get(name, {}).items():
        if key not in props:
            continue
        current = args.get(key)
        if current is None or (isinstance(current, int) and current > ceiling):
            args[key] = ceiling
            clamped.append(key)

    return PreparedCall(name, args, tuple(clamped), tuple(coerced))


# ------------------------------------------------------- shaping the result

def result_to_text(result) -> tuple:
    """Return (text, is_error) from an MCP tool result."""
    parts = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        parts.append(text if isinstance(text, str) else "[non-text content omitted]")
    return ("\n".join(parts) or "[empty result]"), bool(getattr(result, "isError", False))

def parse_json_blocks(text: str) -> list:
    """Parse any number of JSON documents written back to back, in any formatting."""
    if not isinstance(text, str) or not text.strip():
        return []
    decoder = json.JSONDecoder()
    docs, i, n = [], 0, len(text)
    while i < n:
        while i < n and text[i].isspace():
            i += 1
        if i >= n:
            break
        try:
            doc, end = decoder.raw_decode(text, i)
        except ValueError:
            break
        docs.append(doc)
        i = end
    return docs


def shape_for_model(text: str, is_error: bool = False,
                    max_chars: int = DEFAULT_RESULT_CHARS) -> dict:
    """Cap the size of a result. Injection flags are always kept, even when text is cut."""
    flags = [f for f in collect_injection_flags(parse_json_blocks(text))
             if isinstance(f, str) and _FLAG_NAME.match(f)]

    full_len = len(text)
    truncated = full_len > max_chars
    out = text[:max_chars] if truncated else text
    if truncated:
        out += (f"\n[host note: output cut at {max_chars} of {full_len} characters. "
                f"injection_flags in the full output: {flags}. "
                "Ask a narrower question if you need the rest.]")
    if is_error:
        out = "TOOL ERROR: " + out
    return {"text": out, "truncated": truncated,
            "injection_flags": flags, "chars": full_len}


# ------------------------------------------------------------ token budget

def estimate_tokens(text: str) -> int:
    """Rough guess: about 4 characters per token. Real counts come from the provider."""
    return max(1, len(text) // 4)


def tool_budget(openai_tools: list) -> dict:
    per_tool = {t["function"]["name"]: estimate_tokens(json.dumps(t, separators=(",", ":")))
                for t in openai_tools}
    return {"per_tool": per_tool, "total": sum(per_tool.values())}