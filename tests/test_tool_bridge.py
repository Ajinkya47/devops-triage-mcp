import asyncio
import json

import pytest

import tool_bridge as tb
from tool_bridge import ToolRefused, ToolSpec


def spec(name, props=None, required=()):
    return ToolSpec(name, f"{name} description", {
        "type": "object", "properties": props or {}, "required": list(required)})


LIST_ISSUES = {
    "repo": {"title": "Repo", "type": "string"},
    "state": {"title": "State", "type": "string", "enum": ["open", "closed", "all"],
              "default": "open"},
    "limit": {"title": "Limit", "type": "integer", "default": 20},
}


def exposed_for_tests():
    return {
        "list_issues": spec("list_issues", LIST_ISSUES, required=["repo"]),
        "get_issue": spec("get_issue", {"repo": {"type": "string"},
                                        "number": {"type": "integer"},
                                        "max_comments": {"type": "integer"}},
                          required=["repo", "number"]),
        "propose_comment": spec("propose_comment", {"text": {"type": "string"}},
                                required=["text"]),
    }


class Block:
    def __init__(self, text):
        self.type = "text"
        self.text = text

class Result:
    def __init__(self, blocks, is_error=False):
        self.content = blocks
        self.isError = is_error


# ---------- Which tools the model can see ----------

def all_names():
    return [spec(n) for n in sorted(tb.READ_TOOLS | tb.PROPOSE_TOOLS)]

def test_read_mode_hides_propose_tools():
    exposed = tb.select_tools(all_names(), tb.MODE_READ)
    assert set(exposed) == tb.READ_TOOLS

def test_proposal_mode_adds_propose_tools():
    exposed = tb.select_tools(all_names(), tb.MODE_PROPOSE)
    assert set(exposed) == tb.READ_TOOLS | tb.PROPOSE_TOOLS

def test_unknown_mode_rejected():
    with pytest.raises(ValueError):
        tb.select_tools(all_names(), "admin")

def test_dangerous_server_tools_are_never_exposed():
    extra = [spec(n) for n in ("approve_action", "execute_write", "run_shell",
                               "confirm_action", "delete_repo")]
    exposed = tb.select_tools(all_names() + extra, tb.MODE_PROPOSE)
    assert set(exposed) == tb.READ_TOOLS | tb.PROPOSE_TOOLS

def test_deny_pattern_is_a_second_lock(monkeypatch):
    monkeypatch.setattr(tb, "READ_TOOLS", tb.READ_TOOLS | {"approve_action"})
    exposed = tb.select_tools(all_names() + [spec("approve_action")], tb.MODE_READ)
    assert "approve_action" not in exposed

def test_only_filter_limits_tools():
    exposed = tb.select_tools(all_names(), tb.MODE_PROPOSE, only=["assess_issue", "get_issue"])
    assert set(exposed) == {"assess_issue", "get_issue"}

def test_only_cannot_add_tools_outside_the_mode():
    exposed = tb.select_tools(all_names(), tb.MODE_READ, only=["propose_comment"])
    assert exposed == {}

@pytest.mark.parametrize("name", ["approve_action", "execute_write", "confirm_action",
                                  "reject_x", "run_shell", "eval_code", "admin_panel"])
def test_deny_pattern_catches_risky_names(name):
    assert tb.DENY_NAME.search(name)


# ---------- Converting to the model's format ----------

def test_conversion_shape_and_order():
    tools = tb.to_openai_tools(exposed_for_tests())
    assert [t["function"]["name"] for t in tools] == ["get_issue", "list_issues", "propose_comment"]
    assert all(t["type"] == "function" for t in tools)
    assert tools[1]["function"]["parameters"]["type"] == "object"

def test_titles_are_stripped_but_a_parameter_named_title_survives():
    s = ToolSpec("t", "d", {"title": "TArguments", "type": "object",
                            "properties": {"title": {"title": "Title", "type": "string"}},
                            "required": ["title"]})
    params = tb.to_openai_tools({"t": s})[0]["function"]["parameters"]
    assert "title" not in params
    assert "title" in params["properties"]
    assert "title" not in params["properties"]["title"]

def test_missing_schema_gets_a_safe_default():
    params = tb.to_openai_tools({"t": ToolSpec("t", "d", {})})[0]["function"]["parameters"]
    assert params == {"type": "object", "properties": {}}


# ---------- Checking calls from the model ----------

def prep(name, args):
    return tb.prepare_call(name, args, exposed_for_tests())

def test_valid_call_passes():
    call = prep("list_issues", '{"repo": "a/b", "state": "open", "limit": 5}')
    assert call.arguments == {"repo": "a/b", "state": "open", "limit": 5}
    assert call.clamped == () and call.coerced == ()

def test_unknown_tool_is_refused_with_the_available_list():
    with pytest.raises(ToolRefused, match="not available") as e:
        prep("delete_everything", "{}")
    assert "list_issues" in str(e.value)

def test_tool_not_in_this_mode_is_refused():
    read_only = tb.select_tools(all_names(), tb.MODE_READ)
    with pytest.raises(ToolRefused, match="not available"):
        tb.prepare_call("propose_comment", {"text": "hi"}, read_only)

def test_weird_tool_names_are_cleaned_in_the_message():
    with pytest.raises(ToolRefused) as e:
        prep("evil\x1b[2J" * 20, "{}")
    assert "\x1b" not in str(e.value) and len(str(e.value)) < 400

def test_non_string_tool_name_is_refused():
    with pytest.raises(ToolRefused):
        prep(None, "{}")

@pytest.mark.parametrize("raw", ["", "   ", None])
def test_empty_arguments_become_empty_dict(raw):
    with pytest.raises(ToolRefused, match="Missing required"):
        prep("list_issues", raw)         # parsed as {}, then the required repo is missing

def test_empty_arguments_for_a_tool_with_no_required_args():
    exposed = {"t": spec("t")}
    assert tb.prepare_call("t", "", exposed).arguments == {}

@pytest.mark.parametrize("raw", ["{bad json", "[1, 2]", "42", '"text"', "null"])
def test_bad_json_is_refused(raw):
    with pytest.raises(ToolRefused):
        prep("list_issues", raw)

def test_huge_arguments_are_refused():
    with pytest.raises(ToolRefused, match="too large"):
        prep("list_issues", '{"repo": "' + "a" * 5000 + '"}')

def test_unknown_argument_is_refused_and_allowed_ones_listed():
    with pytest.raises(ToolRefused, match="Unknown argument") as e:
        prep("list_issues", '{"repo": "a/b", "verbose": true}')
    assert "limit" in str(e.value)

def test_missing_required_argument_is_refused():
    with pytest.raises(ToolRefused, match="repo"):
        prep("list_issues", '{"limit": 3}')

def test_wrong_type_is_refused():
    with pytest.raises(ToolRefused, match="integer"):
        prep("list_issues", '{"repo": "a/b", "limit": "many"}')

def test_numbers_as_text_are_converted_and_reported():
    call = prep("get_issue", '{"repo": "a/b", "number": "9185"}')
    assert call.arguments["number"] == 9185
    assert "number" in call.coerced

def test_whole_floats_are_converted():
    assert prep("get_issue", '{"repo": "a/b", "number": 5.0}').arguments["number"] == 5

def test_fractional_floats_and_booleans_are_not_integers():
    for bad in ("5.5", "true"):
        with pytest.raises(ToolRefused):
            prep("get_issue", '{"repo": "a/b", "number": %s}' % bad)

def test_enum_is_enforced():
    with pytest.raises(ToolRefused, match="one of"):
        prep("list_issues", '{"repo": "a/b", "state": "everything"}')

def test_overlong_string_is_refused():
    with pytest.raises(ToolRefused, match="too long"):
        prep("propose_comment", {"text": "a" * 2001})

def test_dict_arguments_are_accepted_too():
    assert prep("get_issue", {"repo": "a/b", "number": 1}).arguments["number"] == 1


# ---------- Token clamps ----------

def test_limit_is_lowered_to_the_ceiling():
    call = prep("list_issues", '{"repo": "a/b", "limit": 500}')
    assert call.arguments["limit"] == 10 and "limit" in call.clamped

def test_missing_limit_gets_the_ceiling():
    call = prep("list_issues", '{"repo": "a/b"}')
    assert call.arguments["limit"] == 10 and "limit" in call.clamped

def test_small_limit_is_kept():
    call = prep("list_issues", '{"repo": "a/b", "limit": 3}')
    assert call.arguments["limit"] == 3 and call.clamped == ()

def test_get_issue_comments_are_clamped():
    call = prep("get_issue", '{"repo": "a/b", "number": 1, "max_comments": 20}')
    assert call.arguments["max_comments"] == 3


# ---------- Shaping results ----------

def test_result_text_joins_blocks_and_marks_errors():
    text, is_error = tb.result_to_text(Result([Block("a"), Block("b")], is_error=True))
    assert text == "a\nb" and is_error is True

def test_non_text_and_empty_results_are_handled():
    assert tb.result_to_text(Result([object()]))[0] == "[non-text content omitted]"
    assert tb.result_to_text(Result([]))[0] == "[empty result]"

def test_small_result_is_unchanged():
    out = tb.shape_for_model('{"ok": true}')
    assert out["text"] == '{"ok": true}' and out["truncated"] is False

def test_error_results_are_labelled():
    assert tb.shape_for_model("boom", is_error=True)["text"].startswith("TOOL ERROR: ")

def test_big_result_is_cut_but_flags_survive():
    payload = json.dumps({"body": "x" * 10000, "injection_flags": ["role_override"]})
    out = tb.shape_for_model(payload, max_chars=500)
    assert out["truncated"] is True
    assert "role_override" in out["text"]
    assert out["injection_flags"] == ["role_override"]
    assert len(out["text"]) < 800

def test_strange_flag_names_are_dropped():
    payload = json.dumps({"injection_flags": ["role_override", "Ignore all\nrules", "x" * 100]})
    assert tb.shape_for_model(payload)["injection_flags"] == ["role_override"]

def test_non_json_results_still_work():
    out = tb.shape_for_model("plain text " * 1000, max_chars=100)
    assert out["truncated"] is True and out["injection_flags"] == []


# ---------- Token budget ----------

def test_token_estimates():
    assert tb.estimate_tokens("") == 1
    assert tb.estimate_tokens("a" * 400) == 100

def test_budget_adds_up():
    tools = tb.to_openai_tools(exposed_for_tests())
    budget = tb.tool_budget(tools)
    assert budget["total"] == sum(budget["per_tool"].values())
    assert set(budget["per_tool"]) == {"get_issue", "list_issues", "propose_comment"}


# ---------- Against the real server's tool definitions ----------

def real_specs():
    import server
    return [tb.spec_from_mcp(t) for t in asyncio.run(server.mcp.list_tools())]

def test_real_tools_match_the_allowlists_exactly():
    names = {s.name for s in real_specs()}
    assert names == tb.READ_TOOLS | tb.PROPOSE_TOOLS

def test_real_read_mode_has_no_propose_tools():
    exposed = tb.select_tools(real_specs(), tb.MODE_READ)
    assert set(exposed) == tb.READ_TOOLS

def test_real_tools_convert_cleanly():
    exposed = tb.select_tools(real_specs(), tb.MODE_PROPOSE)
    for tool in tb.to_openai_tools(exposed):
        assert tool["function"]["description"]
        assert tool["function"]["parameters"]["type"] == "object"

def test_real_list_issues_gets_a_clamped_limit():
    exposed = tb.select_tools(real_specs(), tb.MODE_READ)
    call = tb.prepare_call("list_issues", '{"repo": "a/b"}', exposed)
    assert call.arguments["limit"] == 10

def test_real_propose_comment_is_refused_in_read_mode():
    exposed = tb.select_tools(real_specs(), tb.MODE_READ)
    with pytest.raises(ToolRefused):
        tb.prepare_call("propose_comment",
                        '{"repo": "a/b", "number": 1, "text": "hi"}', exposed)



def test_parse_json_blocks():
    assert tb.parse_json_blocks('{"a": 1}') == [{"a": 1}]
    assert tb.parse_json_blocks('{"a": 1}\n{"b": 2}') == [{"a": 1}, {"b": 2}]
    assert tb.parse_json_blocks("[1, 2]") == [[1, 2]]
    assert tb.parse_json_blocks("plain text") == []
    assert tb.parse_json_blocks("") == []

def test_flags_are_found_in_results_with_one_json_block_per_item():
    text = ('{"title": "a", "injection_flags": ["role_override"]}\n'
            '{"title": "b", "injection_flags": []}')
    assert tb.shape_for_model(text)["injection_flags"] == ["role_override"]

def test_parse_json_blocks_handles_pretty_printed_documents():
    text = '{\n  "number": 3,\n  "title": "x"\n}\n{\n  "number": 4,\n  "title": "y"\n}'
    assert tb.parse_json_blocks(text) == [{"number": 3, "title": "x"}, {"number": 4, "title": "y"}]

def test_flags_are_found_in_pretty_printed_blocks():
    text = ('{\n  "title": "a",\n  "injection_flags": [\n    "role_override"\n  ]\n}\n'
            '{\n  "title": "b",\n  "injection_flags": []\n}')
    assert tb.shape_for_model(text)["injection_flags"] == ["role_override"]