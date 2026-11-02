"""Native permission/plan adoption and controlled association boundaries."""

from copy import deepcopy

import pytest
from copilot_fixture import ASSETS, native_source

from session_log_extractors.extract_copilot_session_log import interpret_source
from session_log_extractors.session_log_envelope import export_session


def _plan_responses(source):
    responses = []
    for record in source.state_records:
        data = record.get("data", {})
        if record["type"] == "human_response.recorded":
            if data.get("response", {}).get("responseKind") == "exit_plan_mode":
                responses.append(record)
    assert responses, "Capture must contain plan responses"
    return responses


def _assert_conversation_preserved(session, envelope, limit):
    original = deepcopy(session)
    actual = deepcopy(envelope)
    fields = (
        "permission_refusals",
        "plan_versions",
        "plan_presentations",
        "plan_decisions",
    )
    for field in fields:
        original.pop(field)
    for message in actual["events"]:
        for call in message.get("tool_calls") or []:
            for field in fields:
                call.pop(field, None)
    expected = export_session(original, limit)
    assert actual == expected


@pytest.mark.parametrize("limit", [0, 17, -1])
def test_native_decision_facts_survive_export_without_mutation(limit):
    source = native_source()
    responses = _plan_responses(source)
    session = interpret_source(source)
    assert session is not None
    assert len(session["plan_decisions"]) == len(responses)
    original = deepcopy(session)

    envelope = export_session(session, limit)

    assert session == original
    _assert_conversation_preserved(session, envelope, limit)
    _assert_native_plan_content(session, responses, envelope, limit)
    _assert_native_refusals(session, envelope)


def _assert_native_plan_content(session, responses, envelope, limit):
    exported = {}
    for message in envelope["events"]:
        for call in message.get("tool_calls") or []:
            for decision in call.get("plan_decisions", []):
                exported[decision["ref"]] = call
    outcomes = set()
    texts = []
    versions = {}
    for version in session["plan_versions"]:
        versions[version["ref"]] = version
        texts.append(version["text"])
        assert "edited_from" not in version
    for decision, record in zip(session["plan_decisions"], responses, strict=True):
        response = record["data"]["response"]
        assert decision["native_decision"] is response["approved"]
        assert decision.get("candidate_feedback") == response.get("feedback")
        assert "explanation" not in decision
        version = versions[decision["version"]["version_ref"]]
        assert version["text"] == response["planContent"]
        call = exported[decision["ref"]]
        assert call["plan_decisions"] == [decision]
        assert version in call["plan_versions"]
        assert call["input"]
        if limit >= 0 and isinstance(call.get("result"), str):
            assert len(call["result"].encode()) <= limit
        outcomes.add(decision["outcome"])
    assert outcomes == {"approved", "rejected"}
    assert (ASSETS / "plan-before-editor.md").read_text() in texts
    assert (ASSETS / "plan-after-editor.md").read_text() in texts


def _assert_native_refusals(session, envelope):
    refusals = session["permission_refusals"]
    assert len(refusals) == 2
    by_source = {}
    for refusal in refusals:
        by_source[refusal["source"]] = refusal
        assert refusal["activity"] is not None
        assert refusal["action"]["tool_name"] == "bash"
        activity = refusal["activity"]
        call = envelope["events"][activity["message_i"]]["tool_calls"][
            activity["tool_call_index"]
        ]
        assert refusal in call["permission_refusals"]
    assert set(by_source) == {"candidate", "settings"}
    assert by_source["candidate"]["candidate_feedback"]
    assert (
        by_source["candidate"]["explanation"]
        != by_source["candidate"]["candidate_feedback"]
    )
    assert by_source["settings"]["explanation"]
    assert "candidate_feedback" not in by_source["settings"]
    for field in (
        "permission_refusals",
        "plan_versions",
        "plan_presentations",
        "plan_decisions",
    ):
        assert field not in envelope


@pytest.mark.parametrize("approved", [None, 1, "approved", {}, "missing"])
def test_unknown_or_missing_plan_decision_is_never_approved(approved):
    source = native_source()
    responses = _plan_responses(source)
    # Change only the native decision label; retain the captured content and IDs.
    for record in responses:
        response = record["data"]["response"]
        response["approved"] = approved
        if approved == "missing":
            del response["approved"]
    session = interpret_source(source)
    assert session is not None
    assert len(session["plan_decisions"]) == len(responses)
    for decision in session["plan_decisions"]:
        assert decision["outcome"] == "unknown"
        if approved == "missing":
            assert "native_decision" not in decision
        else:
            assert decision["native_decision"] == approved


def test_unmatched_plan_responses_remain_internal_without_guessed_owners():
    source = native_source()
    responses = _plan_responses(source)
    # Keep only real responses; neither database turns nor unrelated calls prove ownership.
    source.state_records = responses
    source.turns = []
    source.files = []
    session = interpret_source(source)
    assert session is not None
    assert len(session["plan_decisions"]) == len(responses)
    for decision in session["plan_decisions"]:
        assert decision["activity"] is None
        assert decision["presentation"] is None
        assert decision["version"] is not None
    assert session["plan_presentations"] == []
    envelope = export_session(session, 0)
    assert envelope["events"] == []
