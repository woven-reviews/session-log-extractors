"""Native permission/plan adoption and controlled association boundaries."""

from copy import deepcopy

import pytest
from copilot_fixture import ASSETS, SESSION_ID, native_source

from session_log_extractors.copilot_decisions import interpret_decisions
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
    for conversation in (original, actual):
        for message in conversation["events"]:
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


def test_native_plan_proposals_use_full_session_id_and_preserve_call_ownership():
    source = native_source()
    session = interpret_source(source)
    assert session is not None
    assert session["session_id"] == SESSION_ID
    proposals = []
    for version in session["plan_versions"]:
        if version["kind"] == "proposed":
            proposals.append(version)
    assert len(proposals) == 2, "Capture must contain both native plan proposals"

    envelope = export_session(session, 0)

    for version in proposals:
        activity = version["activity"]
        assert activity is not None
        call = envelope["events"][activity["message_i"]]["tool_calls"][
            activity["tool_call_index"]
        ]
        assert call["name"] == "create"
        assert call["input"]["path"].endswith(f"/session-state/{SESSION_ID}/plan.md")
        assert version["text"] == call["input"]["file_text"]
        assert version["text"]
        assert call["plan_versions"] == [version]


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


def _controlled_session(calls):
    # Explicit malformed/ambiguous IDs and native variants supplement the capture.
    return {
        "harness": "copilot",
        "session_id": "s",
        "events": [{"i": 4, "role": "assistant", "tool_calls": calls}],
    }


def _permission_record(kind="denied-interactively-by-user"):
    return {
        "id": "denial",
        "type": "permission.completed",
        "data": {
            "requestId": "request",
            "toolCallId": "call",
            "decisionSource": "human_response",
            "result": {"kind": kind, "feedback": "Controlled candidate feedback"},
        },
    }


def _response_record():
    return {
        "type": "human_response.recorded",
        "data": {
            "toolCallId": "call",
            "actor": "human_response",
            "response": {
                "responseKind": "exit_plan_mode",
                "approved": True,
                "planContent": "Plan 雪",
                "feedback": "Controlled feedback",
            },
        },
    }


@pytest.mark.parametrize("kind", ["approved", "cancelled", "future", None])
def test_non_denial_permission_results_do_not_create_refusals(kind):
    session = _controlled_session([{"id": "call", "name": "bash"}])
    record = _permission_record(kind)
    interpret_decisions(session, [record], native_session_id="s")
    assert session["permission_refusals"] == []
    assert session["plan_decisions"] == []


@pytest.mark.parametrize("source", [None, "settings", "future"])
def test_denial_source_and_feedback_require_recorded_human_authorship(source):
    session = _controlled_session([{"id": "call", "name": "bash"}])
    record = _permission_record()
    record["data"]["decisionSource"] = source
    interpret_decisions(session, [record], native_session_id="s")
    (refusal,) = session["permission_refusals"]
    assert refusal["source"] == "unknown"
    assert "candidate_feedback" not in refusal
    assert refusal["native_variant"] == record["data"]["result"]["kind"]


@pytest.mark.parametrize(
    "calls",
    [
        [],
        [{"name": "bash"}],
        [{"name": "bash", "id": "call"}, {"name": "bash", "id": "call"}],
    ],
)
def test_missing_or_ambiguous_calls_leave_refusal_unassociated(calls):
    session = _controlled_session(calls)
    interpret_decisions(session, [_permission_record()], native_session_id="s")
    (refusal,) = session["permission_refusals"]
    assert refusal["activity"] is None
    assert refusal["action"] is None
    envelope = export_session(session, 0)
    for call in envelope["events"][0]["tool_calls"]:
        assert "permission_refusals" not in call


def test_call_and_request_associations_respect_agent_scope():
    session = _controlled_session([{"name": "bash", "id": "call", "agent_id": "child"}])
    request = {
        "type": "permission.requested",
        "agentId": "child",
        "data": {
            "requestId": "request",
            "permissionRequest": {"fullCommandText": "Child command"},
        },
    }
    record = _permission_record()
    interpret_decisions(session, [request, record], native_session_id="s")
    (refusal,) = session["permission_refusals"]
    assert refusal["activity"] is None
    assert refusal["action"] is None


@pytest.mark.parametrize(
    "permission_request,expected_description",
    [
        (None, '{"path": "x"}'),
        ({}, '{"path": "x"}'),
        ({"intention": "Change file"}, "Change file"),
        ({"fileName": "a.txt"}, "a.txt"),
        ({"kind": "write"}, "write"),
        ({"toolCallId": "other", "intention": "Wrong call"}, '{"path": "x"}'),
    ],
)
def test_denied_action_uses_only_a_unique_nonconflicting_request(
    permission_request, expected_description
):
    session = _controlled_session(
        [{"name": "write", "id": "call", "input": {"path": "x"}}]
    )
    request_record = {
        "type": "permission.requested",
        "data": {"requestId": "request", "permissionRequest": permission_request},
    }

    interpret_decisions(
        session, [request_record, _permission_record()], native_session_id="s"
    )

    (refusal,) = session["permission_refusals"]
    assert refusal["action"]["description"] == expected_description
    assert refusal["action"]["tool_name"] == "write"


def test_duplicate_requests_cannot_supply_action_information():
    session = _controlled_session([])
    request = {
        "type": "permission.requested",
        "data": {
            "requestId": "request",
            "permissionRequest": {"intention": "Unproven action"},
        },
    }
    interpret_decisions(
        session,
        [request, deepcopy(request), _permission_record()],
        native_session_id="s",
    )
    (refusal,) = session["permission_refusals"]
    assert refusal["action"] is None


@pytest.mark.parametrize(
    "result",
    [
        "Permission to run this tool was denied due to the following rules: rule",
        {
            "content": "Permission to run this tool was denied due to the following rules: rule"
        },
    ],
)
def test_settings_refusal_can_be_recorded_in_tool_result(result):
    session = _controlled_session([{"name": "bash", "id": "call"}])
    record = {
        "type": "tool.execution_complete",
        "data": {"toolCallId": "call", "result": result},
    }
    interpret_decisions(session, [record], native_session_id="s")
    (refusal,) = session["permission_refusals"]
    assert refusal["source"] == "settings"
    assert refusal["native_variant"] is None
    assert "candidate_feedback" not in refusal
    assert refusal["explanation"]


@pytest.mark.parametrize("code", ["denied", "tool_error"])
def test_unknown_tool_denial_is_distinct_from_candidate_feedback(code):
    session = _controlled_session([])
    record = {
        "type": "tool.execution_complete",
        "data": {
            "toolCallId": "missing",
            "error": {"code": code, "message": "Opaque harness error"},
        },
    }
    interpret_decisions(session, [record], native_session_id="s")
    if code == "tool_error":
        assert session["permission_refusals"] == []
    else:
        (refusal,) = session["permission_refusals"]
        assert refusal["source"] == "unknown"
        assert refusal["activity"] is None
        assert refusal["explanation"] == "Opaque harness error"
        assert "candidate_feedback" not in refusal


@pytest.mark.parametrize("actor", [None, "settings", "future"])
def test_nonhuman_plan_response_preserves_native_value_without_candidate_approval(
    actor,
):
    session = _controlled_session([])
    record = _response_record()
    record["data"]["actor"] = actor
    interpret_decisions(session, [record], native_session_id="s")
    (decision,) = session["plan_decisions"]
    assert decision["outcome"] == "unknown"
    assert decision["native_decision"] is True
    assert "candidate_feedback" not in decision


@pytest.mark.parametrize("missing", ["toolCallId", "planContent"])
def test_missing_plan_content_or_id_stays_explicit(missing):
    session = _controlled_session([])
    record = _response_record()
    if missing == "toolCallId":
        del record["data"][missing]
    else:
        del record["data"]["response"][missing]
    interpret_decisions(session, [record], native_session_id="s")
    (decision,) = session["plan_decisions"]
    assert decision["activity"] is None
    assert decision["presentation"] is None
    if missing == "planContent":
        assert decision["version"] is None
        assert session["plan_versions"] == []


def test_conflicting_call_name_cannot_own_a_plan_response():
    session = _controlled_session([{"name": "bash", "id": "call"}])
    interpret_decisions(session, [_response_record()], native_session_id="s")
    (decision,) = session["plan_decisions"]
    assert decision["activity"] is None


def test_repeated_presentations_have_no_inferred_decision_link():
    session = _controlled_session([{"name": "exit_plan_mode", "id": "call"}])
    start = {
        "type": "tool.execution_start",
        "data": {"toolCallId": "call", "toolName": "exit_plan_mode"},
    }
    interpret_decisions(
        session, [start, deepcopy(start), _response_record()], native_session_id="s"
    )
    assert len(session["plan_presentations"]) == 2
    (decision,) = session["plan_decisions"]
    assert decision["presentation"] is None


def test_unmatched_presentation_and_malformed_records_are_retained_or_ignored_honestly():
    session = _controlled_session([])
    start = {"type": "tool.execution_start", "data": {"toolName": "exit_plan_mode"}}
    interpret_decisions(
        session, [{"type": "malformed", "data": None}, start], native_session_id="s"
    )
    (presentation,) = session["plan_presentations"]
    assert presentation["activity"] is None
    assert presentation["version"] is None
    assert session["plan_decisions"] == []


def test_plan_tool_failure_without_human_response_never_creates_a_decision():
    source = native_source()
    # Remove human response evidence; raw plan tool completions alone prove no decision.
    records = []
    for record in source.state_records:
        if record["type"] != "human_response.recorded":
            records.append(record)
    source.state_records = records
    session = interpret_source(source)
    assert session is not None
    assert session["plan_presentations"]
    assert session["plan_decisions"] == []


def test_request_id_can_bridge_to_its_recorded_tool_call():
    session = _controlled_session([{"name": "bash", "id": "call"}])
    request = {
        "type": "permission.requested",
        "data": {
            "requestId": "request",
            "permissionRequest": {"toolCallId": "call", "fullCommandText": "Run test"},
        },
    }
    completion = _permission_record()
    del completion["data"]["toolCallId"]
    interpret_decisions(session, [completion, request], native_session_id="s")
    (refusal,) = session["permission_refusals"]
    assert refusal["activity"]["message_i"] == 4
    assert refusal["activity"]["tool_call_index"] == 0
    assert refusal["action"] == {"description": "Run test", "tool_name": "bash"}
    assert refusal["source"] == "candidate"


@pytest.mark.parametrize(
    "parent_id,call_id,agent",
    [
        (None, "call", None),
        ("other", "call", None),
        ("denial", "other", None),
        ("denial", "call", "child"),
        ("denial", None, None),
    ],
)
def test_tool_denial_merges_only_through_same_agent_parent_and_call(
    parent_id, call_id, agent
):
    session = _controlled_session([])
    permission = _permission_record()
    result = {
        "type": "tool.execution_complete",
        "parentId": parent_id,
        "agentId": agent,
        "data": {"toolCallId": call_id, "error": {"code": "denied"}},
    }
    interpret_decisions(session, [permission, result], native_session_id="s")
    assert len(session["permission_refusals"]) == 2
    candidate, unknown = session["permission_refusals"]
    assert candidate["source"] == "candidate"
    assert unknown["source"] == "unknown"
    assert "explanation" not in unknown
    assert "candidate_feedback" not in unknown


@pytest.mark.parametrize(
    "payload", [None, {}, {"path": "/other/plan.md", "file_text": "Unrelated"}]
)
def test_malformed_or_unrelated_file_creation_is_not_a_plan(payload):
    session = _controlled_session([{"name": "create", "id": "call", "input": payload}])
    interpret_decisions(session, [], native_session_id="s")
    assert session["plan_versions"] == []


@pytest.mark.parametrize("native_session_id", ["s", "other", None, ""])
def test_direct_plan_proposal_needs_matching_native_session_id(native_session_id):
    session = _controlled_session(
        [
            {
                "name": "create",
                "input": {
                    "path": "/home/x/.copilot/session-state/s/plan.md",
                    "file_text": "Complete proposal",
                },
            }
        ]
    )
    interpret_decisions(session, [], native_session_id=native_session_id)
    if native_session_id != "s":
        assert session["plan_versions"] == []
        return
    (version,) = session["plan_versions"]
    assert version["kind"] == "proposed"
    assert version["text"] == "Complete proposal"
    assert version["activity"]["message_i"] == 4
    assert session["plan_presentations"] == []
    assert session["plan_decisions"] == []


def test_native_plan_and_denial_tests_tolerate_reworded_generated_prose():
    source = native_source()
    for record in source.state_records:
        data = record.get("data", {})
        if record["type"] == "assistant.message":
            data["content"] = "Different assistant prose"
        if record["type"] == "human_response.recorded":
            response = data["response"]
            if response.get("responseKind") == "exit_plan_mode":
                response["planContent"] = "Reworded complete plan 雪"
                response["summary"] = "Different summary"
                if "feedback" in response:
                    response["feedback"] = "Different recorded feedback"
    session = interpret_source(source)
    assert session is not None
    responses = _plan_responses(source)
    assert len(session["plan_decisions"]) == len(responses)
    versions = {}
    for version in session["plan_versions"]:
        versions[version["ref"]] = version
    for decision, record in zip(session["plan_decisions"], responses, strict=True):
        response = record["data"]["response"]
        version = versions[decision["version"]["version_ref"]]
        assert version["text"] == response["planContent"]
        assert decision.get("candidate_feedback") == response.get("feedback")
        assert decision["activity"] is not None


@pytest.mark.parametrize(
    "kind,expected_source",
    [
        ("denied-by-rules", "settings"),
        ("denied-by-content-exclusion-policy", "settings"),
        ("denied-no-approval-rule-and-could-not-request-from-user", "unknown"),
    ],
)
def test_native_noninteractive_denial_variants_never_claim_candidate_feedback(
    kind, expected_source
):
    # Controlled variants from Copilot's shipped session-events schema, absent
    # in this capture. Deliberately conflicting feedback/source must not win.
    session = _controlled_session([])
    record = _permission_record(kind)
    record["data"]["result"]["message"] = "Harness policy explanation"
    interpret_decisions(session, [record], native_session_id="s")
    (refusal,) = session["permission_refusals"]
    assert refusal["native_variant"] == kind
    assert refusal["source"] == expected_source
    assert refusal["explanation"] == "Harness policy explanation"
    assert "candidate_feedback" not in refusal


@pytest.mark.parametrize("result", [None, "denied", {}, {"kind": "denied-future"}])
def test_unsupported_permission_payload_does_not_invent_a_refusal(result):
    session = _controlled_session([])
    record = _permission_record()
    record["data"]["result"] = result
    interpret_decisions(session, [record], native_session_id="s")
    assert session["permission_refusals"] == []


@pytest.mark.parametrize(
    "activity",
    [
        None,
        {"message_i": 0, "evidence": [{"field": "id", "value": "call"}]},
        {"message_i": 0, "tool_call_index": 0, "evidence": []},
        {
            "message_i": -1,
            "tool_call_index": 0,
            "evidence": [{"field": "id", "value": "call"}],
        },
        {
            "message_i": 0,
            "tool_call_index": -1,
            "evidence": [{"field": "id", "value": "call"}],
        },
    ],
)
def test_export_serializes_established_ownership_without_resolving_addresses(activity):
    session = _controlled_session([{"name": "bash", "id": "call"}])
    interpret_decisions(session, [_permission_record()], native_session_id="s")
    (refusal,) = session["permission_refusals"]
    refusal["activity"] = activity
    envelope = export_session(session, 0)
    (call,) = envelope["events"][0]["tool_calls"]
    assert call["permission_refusals"] == [refusal]
    assert envelope["events"][0]["i"] == 4


def test_conflicting_native_tool_name_cannot_own_plan_presentation():
    session = _controlled_session([{"name": "bash", "id": "call"}])
    start = {
        "type": "tool.execution_start",
        "data": {"toolName": "exit_plan_mode", "toolCallId": "call"},
    }
    interpret_decisions(session, [start], native_session_id="s")
    (presentation,) = session["plan_presentations"]
    assert presentation["activity"] is None
    envelope = export_session(session, 0)
    (call,) = envelope["events"][0]["tool_calls"]
    assert "plan_presentations" not in call


@pytest.mark.parametrize(
    "field",
    ["permission_refusals", "plan_versions", "plan_presentations", "plan_decisions"],
)
def test_native_decision_ownership_is_established_before_export(field):
    session = interpret_source(native_source())
    assert session is not None
    canonical = {}
    for message in session["events"]:
        for call in message.get("tool_calls") or []:
            for fact in call.get(field, []):
                canonical[fact["ref"]] = fact
    assert canonical, f"Native capture must contain canonical {field}"
    for fact in session[field]:
        if fact["activity"] is not None:
            assert canonical[fact["ref"]] is fact

    # Internal indexes are secondary views; serialization uses canonical calls.
    expected = export_session(session, 0)
    del session[field]
    actual = export_session(session, 0)

    assert actual == expected


def test_session_only_decision_index_never_establishes_call_ownership():
    session = _controlled_session([{"name": "bash", "id": "call"}])
    interpret_decisions(session, [_permission_record()], native_session_id="s")
    (call,) = session["events"][0]["tool_calls"]
    del call["permission_refusals"]

    envelope = export_session(session, 0)

    (exported_call,) = envelope["events"][0]["tool_calls"]
    assert session["permission_refusals"]
    assert "permission_refusals" not in exported_call
