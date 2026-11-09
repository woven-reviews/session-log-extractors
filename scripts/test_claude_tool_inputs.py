"""Input schema recognition preserves native payloads and unknown fields."""

from copy import deepcopy

import pytest
from claude_fixture import TRANSCRIPT

from session_log_extractors import claude_protocol
from session_log_extractors.claude_sources import read_source
from session_log_extractors.tool_inputs.claude import input_schema


def _recorded_tool_calls(tool_name):
    source = read_source(TRANSCRIPT)
    calls = []
    for entry in source.entries:
        for block in entry.get("message", {}).get("content", []):
            if (
                isinstance(block, dict)
                and block.get("type") == claude_protocol.TOOL_USE_BLOCK
                and block.get("name") == tool_name
            ):
                calls.append(block)
    assert calls, f"Missing native {tool_name} calls"
    return calls


@pytest.mark.parametrize(
    "tool_name",
    [
        claude_protocol.READ_TOOL,
        claude_protocol.BASH_TOOL,
        claude_protocol.ASK_USER_QUESTION,
        claude_protocol.EXIT_PLAN_MODE,
        claude_protocol.SKILL_TOOL,
        claude_protocol.AGENT_TOOL,
        claude_protocol.WRITE_TOOL,
        claude_protocol.EDIT_TOOL,
        claude_protocol.NOTEBOOK_EDIT_TOOL,
    ],
)
def test_captured_tool_inputs_have_explicit_schemas(tool_name):
    calls = _recorded_tool_calls(tool_name)
    assert calls
    for call in calls:
        original = deepcopy(call["input"])
        # An additional native field must not be removed by schema recognition.
        call["input"]["future_extension"] = {"items": [None, True, 1.5, {"x": "y"}]}
        actual_input_schema = input_schema(call["name"], call["input"])
        assert actual_input_schema == call["name"]
        assert call["input"] == {
            **original,
            "future_extension": {"items": [None, True, 1.5, {"x": "y"}]},
        }


@pytest.mark.parametrize("payload", [None, "raw", [], 42, True])
def test_non_object_input_uses_explicit_fallback(payload):
    actual_input_schema = input_schema("Read", payload)
    assert actual_input_schema == "unsupported"


def test_unknown_tool_uses_explicit_fallback():
    actual_input_schema = input_schema("plugin__DeployService", {"region": "east"})
    assert actual_input_schema == "unsupported"


def test_legacy_plan_name_has_the_same_input_schema():
    actual_input_schema = input_schema("exit_plan_mode", {"plan": "Build it"})
    assert actual_input_schema == "ExitPlanMode"


@pytest.mark.parametrize(
    "name",
    [
        "bash",
        "view",
        "edit",
        "create",
        "skill",
        "exec_command",
        "request_user_input",
        "update_plan",
        "apply_patch",
        "skills.read",
    ],
)
def test_claude_does_not_recognize_copilot_tool_names(name):
    actual_input_schema = input_schema(name, {"recorded": True})
    assert actual_input_schema == "unsupported"
