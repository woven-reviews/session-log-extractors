"""Input schema recognition preserves native payloads and unknown fields."""

from copy import deepcopy
from pathlib import Path

import pytest

from session_log_extractors.claude_sources import read_source
from session_log_extractors.tool_inputs.claude import input_schema

FIXTURES = Path(__file__).parent / "fixtures" / "claude"


@pytest.mark.parametrize(
    "fixture",
    [
        "continued.jsonl",
        "permission_denial.jsonl",
        "questions.jsonl",
        "plan_modes.jsonl",
        "skill_command.jsonl",
        "subagent.jsonl",
    ],
)
def test_captured_tool_inputs_have_explicit_schemas(fixture):
    calls = [
        block
        for entry in read_source(FIXTURES / fixture).entries
        for block in entry.get("message", {}).get("content", [])
        if isinstance(block, dict) and block.get("type") == "tool_use"
    ]
    assert calls
    for call in calls:
        original = deepcopy(call["input"])
        # An additional native field must not be removed by schema recognition.
        call["input"]["future_extension"] = {"items": [None, True, 1.5, {"x": "y"}]}
        assert input_schema(call["name"], call["input"]) == call["name"]
        assert call["input"] == {
            **original,
            "future_extension": {"items": [None, True, 1.5, {"x": "y"}]},
        }


@pytest.mark.parametrize("payload", [None, "raw", [], 42, True])
def test_non_object_input_uses_explicit_fallback(payload):
    assert input_schema("Read", payload) == "unsupported"


def test_unknown_tool_uses_explicit_fallback():
    assert input_schema("plugin__DeployService", {"region": "east"}) == "unsupported"


def test_legacy_plan_name_has_the_same_input_schema():
    assert input_schema("exit_plan_mode", {"plan": "Build it"}) == "ExitPlanMode"


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
    assert input_schema(name, {"recorded": True}) == "unsupported"
