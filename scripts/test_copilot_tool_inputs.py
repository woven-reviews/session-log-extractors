"""Copilot owns its tool vocabulary while keeping complete native inputs."""

import json
from copy import deepcopy

import pytest
from copilot_fixture import native_records

from session_log_extractors.tool_inputs.copilot import input_schema


def test_captured_tool_inputs_have_copilot_schemas():
    expected = {
        "bash": "CopilotBash",
        "skill": "CopilotSkill",
        "view": "CopilotView",
        "edit": "CopilotEdit",
        "create": "CopilotCreate",
        "ask_user": "CopilotAskUser",
        "exit_plan_mode": "CopilotExitPlan",
        "glob": "CopilotGlob",
        "grep": "CopilotGrep",
        "sql": "CopilotSql",
        "task": "CopilotTask",
        "task_complete": "CopilotTaskComplete",
        "read_bash": "CopilotReadBash",
    }
    observed = set()
    for record in native_records():
        if record["type"] != "tool.execution_start":
            continue
        data = record["data"]
        payload = data["arguments"]
        payload["future_extension"] = {"items": [None, True, 1.5, {"x": "y"}]}
        original = deepcopy(payload)
        actual = input_schema(data["toolName"], payload)
        assert actual == expected[data["toolName"]]
        assert payload == original
        assert json.loads(json.dumps(payload)) == original
        observed.add(data["toolName"])
    assert observed == set(expected)


@pytest.mark.parametrize(
    ("name", "payload", "schema"),
    [
        ("view", {"path": "/app.py", "view_range": [1, 10]}, "CopilotView"),
        ("edit", {"path": "/app.py", "old_str": "a", "new_str": "b"}, "CopilotEdit"),
        ("create", {"path": "/app.py", "file_text": "content"}, "CopilotCreate"),
    ],
)
def test_file_tools_keep_their_harness_specific_fields(name, payload, schema):
    # Controlled examples keep exact field/schema expectations independent of prose.
    original = deepcopy(payload)
    assert input_schema(name, payload) == schema
    assert payload == original


@pytest.mark.parametrize("payload", [None, "raw", [], 42, True])
def test_non_object_input_uses_explicit_fallback(payload):
    assert input_schema("bash", payload) == "unsupported"


@pytest.mark.parametrize(
    "name",
    [
        "Bash",
        "Read",
        "Skill",
        "AskUserQuestion",
        "ExitPlanMode",
        "plugin__DeployService",
    ],
)
def test_other_harness_or_unknown_names_are_unsupported(name):
    assert input_schema(name, {"recorded": True}) == "unsupported"


def test_recognition_is_distinct_from_runtime_validation():
    # Recognition identifies a supported object shape without validating fields.
    payload = {"command": ["unexpected type"], "extra": {"preserve": None}}
    original = deepcopy(payload)
    assert input_schema("bash", payload) == "CopilotBash"
    assert payload == original
