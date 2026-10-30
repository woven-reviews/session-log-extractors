"""Input schema recognition preserves native payloads and unknown fields."""

from copy import deepcopy

import pytest
from claude_fixture import native_entries

from session_log_extractors.tool_inputs.claude import input_schema


@pytest.mark.parametrize(
    "fixture",
    [
        "multibyte_result_over_10kb",
        "settings_driven_bash_denial",
        "multiple_questions_and_multiselect",
        "genuine_editor_edited_plan_approval",
        "repeated_skill_invocation",
        "one_read_only_subagent",
        "native_write_read_edit_read",
        "native_notebook_edit",
        "successful_shell",
    ],
)
def test_captured_tool_inputs_have_explicit_schemas(fixture):
    calls = [
        block
        for entry in native_entries(fixture)
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
