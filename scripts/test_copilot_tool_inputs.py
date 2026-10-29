"""Copilot owns its tool vocabulary while keeping complete native inputs."""

import json
from copy import deepcopy
from pathlib import Path

import pytest

from session_log_extractors.copilot_sources import parse_state_records
from session_log_extractors.tool_inputs.copilot import input_schema

FIXTURES = Path(__file__).parent / "fixtures" / "copilot"


@pytest.mark.parametrize("case", ["tool_success", "tool_failure", "skill"])
def test_captured_tool_inputs_have_copilot_schemas(case):
    records = parse_state_records(
        (FIXTURES / case / "events.jsonl").read_text().splitlines()
    )
    requests = [
        request
        for record in records
        if record["type"] == "assistant.message"
        for request in record["data"].get("toolRequests", [])
    ]
    assert requests
    expected = {"bash": "CopilotBash", "skill": "CopilotSkill"}
    for request in requests:
        payload = request["arguments"]
        payload["future_extension"] = {"items": [None, True, 1.5, {"x": "y"}]}
        original = deepcopy(payload)
        assert input_schema(request["name"], payload) == expected[request["name"]]
        assert payload == original
        assert json.loads(json.dumps(payload)) == original


@pytest.mark.parametrize(
    ("name", "payload", "schema"),
    [
        ("view", {"path": "/app.py", "view_range": [1, 10]}, "CopilotView"),
        ("edit", {"path": "/app.py", "old_str": "a", "new_str": "b"}, "CopilotEdit"),
        ("create", {"path": "/app.py", "file_text": "content"}, "CopilotCreate"),
    ],
)
def test_file_tools_keep_their_harness_specific_fields(name, payload, schema):
    # Focused schema examples supplement captures without native file-tool calls.
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
        "exit_plan_mode",
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
