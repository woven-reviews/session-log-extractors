"""Controlled malformed and legacy inputs supplement the interactive capture."""

from copy import deepcopy

import pytest
from copilot_fixture import native_source

from session_log_extractors.copilot_interactions import (
    apply_human_response,
    questions_from_schema,
    subagent_summaries,
)
from session_log_extractors.session_model import ToolCall


@pytest.mark.parametrize(
    "payload",
    [None, "raw", {}, {"requestedSchema": {}}, {"requestedSchema": {"properties": []}}],
)
def test_malformed_question_containers_have_no_invented_questions(payload):
    actual = questions_from_schema(payload)
    assert actual == []


def _controlled_question_call():
    payload = {
        "message": "Controlled choice?",
        "requestedSchema": {
            "properties": {
                "choice": {"type": "string", "enum": ["Leaf", "Leaflet", None]},
                "malformed": False,
            }
        },
    }
    return ToolCall(
        name="ask_user", input=payload, questions=questions_from_schema(payload)
    )


@pytest.mark.parametrize(
    "response", [None, False, {}, {"responseKind": "ask_user", "content": []}]
)
def test_missing_or_invalid_responses_do_not_invent_answers(response):
    call = _controlled_question_call()
    original = deepcopy(call)
    apply_human_response(call, {"response": response})
    assert call == original


@pytest.mark.parametrize("answer", [None, 17, ["Leaf", False]])
def test_invalid_answer_values_do_not_become_string_answers(answer):
    call = _controlled_question_call()
    apply_human_response(
        call, {"response": {"responseKind": "ask_user", "content": {"choice": answer}}}
    )
    (question,) = call["questions"]
    assert "answer_text" not in question
    assert "chosen_labels" not in question


def test_controlled_overlapping_labels_select_only_the_exact_answer():
    call = _controlled_question_call()
    apply_human_response(
        call,
        {"response": {"responseKind": "ask_user", "content": {"choice": "Leaflet"}}},
    )
    (question,) = call["questions"]
    assert question["chosen_labels"] == ["Leaflet"]
    assert question["recorded_answer"] == "Leaflet"


@pytest.mark.parametrize("approval", [None, "approved", 1])
def test_unknown_plan_approval_is_not_an_approval(approval):
    call = ToolCall(name="exit_plan_mode")
    apply_human_response(
        call, {"response": {"responseKind": "exit_plan_mode", "approved": approval}}
    )
    assert call["plan_decision"]["decision"] == "unknown"


def test_inline_child_summary_uses_only_the_recorded_agent_scope():
    source = native_source()
    starts = []
    for record in source.state_records:
        if record["type"] == "subagent.started":
            starts.append(record)
    assert len(starts) == 1, "Missing bounded subagent evidence"
    (start,) = starts
    agent_id = start["agentId"]
    results = []
    tool_names = []
    for record in source.state_records:
        if record.get("agentId") != agent_id:
            continue
        if record["type"] == "assistant.message" and record["data"].get("content"):
            results.append(record["data"]["content"])
        if record["type"] == "tool.execution_start":
            tool_names.append(record["data"]["toolName"])

    summaries = subagent_summaries(source.state_records)

    assert set(summaries) == {agent_id}
    summary = summaries[agent_id]
    assert summary["task"] == start["data"]["agentDescription"]
    assert summary["tool_count"] == len(tool_names)
    assert set(summary["tool_names"]) == set(tool_names)
    assert isinstance(summary["result"], str) and summary["result"]
    assert summary["result"] == results[-1]
