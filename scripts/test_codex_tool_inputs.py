"""Codex schema recognition retains payloads and stays within its harness."""

import json
from copy import deepcopy

import pytest
from codex_capture import TRANSCRIPT, question_interaction

from session_log_extractors.codex_sources import read_source
from session_log_extractors.tool_inputs.codex import input_schema


def test_native_question_schema_retains_unknown_fields():
    source = read_source(TRANSCRIPT)
    source.entries = question_interaction()
    question_calls = []
    for entry in source.entries:
        if entry["payload"].get("type") == "function_call":
            question_calls.append(entry["payload"])
    (call,) = question_calls
    payload = json.loads(call["arguments"])
    # Add an opaque extension to a captured native request.
    payload["future_extension"] = {"items": [None, True, {"nested": 3}]}
    original = deepcopy(payload)
    assert input_schema(call["name"], payload) == "request_user_input"
    assert payload == original


@pytest.mark.parametrize(
    ("name", "schema", "payload"),
    [
        ("functions.exec_command", "exec_command", {"cmd": "pwd"}),
        ("functions.request_user_input", "request_user_input", {"questions": []}),
        ("functions.update_plan", "update_plan", {"plan": []}),
        ("functions.apply_patch", "apply_patch", {"input": "*** Add File: f.py"}),
        ("mcp__skills__read", "skills.read", {"package": "pdf:pdf"}),
    ],
)
def test_uncaptured_namespaced_schemas_keep_additional_fields(name, schema, payload):
    # Native captures do not contain these namespaced tool variants.
    payload["future_extension"] = [None, True, {"nested": 3}]
    original = deepcopy(payload)
    assert input_schema(name, payload) == schema
    assert payload == original


@pytest.mark.parametrize("payload", [None, "raw patch", [], 42, True])
def test_non_object_input_is_explicitly_unsupported(payload):
    assert input_schema("functions.apply_patch", payload) == "unsupported"


@pytest.mark.parametrize(
    "name", ["Bash", "Read", "AskUserQuestion", "view", "edit", "plugin__DeployService"]
)
def test_other_harnesses_and_unknown_tools_are_unsupported(name):
    assert input_schema(name, {"recorded": True}) == "unsupported"
