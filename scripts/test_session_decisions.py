"""Shared permission/plan records and canonical conversation export."""

import json
from copy import deepcopy

import pytest
from claude_fixture import ROOT as CLAUDE_ROOT
from claude_fixture import TRANSCRIPT as CLAUDE_TRANSCRIPT
from codex_fixture import TRANSCRIPT as CODEX_TRANSCRIPT
from copilot_fixture import native_records

from session_log_extractors import claude_protocol, codex_protocol, copilot_protocol
from session_log_extractors import extract_claude_session_log as claude
from session_log_extractors.claude_sources import read_source
from session_log_extractors.codex_sources import CodexSource, load_entries
from session_log_extractors.extract_codex_session_log import interpret_source
from session_log_extractors.session_log_envelope import export_session
from session_log_extractors.session_model import (
    DecisionActivityReference,
    DecisionEvidence,
    DeniedAction,
    ObservedPlanDecision,
    PermissionRefusal,
    PlanOutcome,
    PlanPresentation,
    PlanPresentationAssociation,
    PlanVersion,
    PlanVersionAssociation,
    PlanVersionKind,
    RefusalSource,
    Session,
    ToolCall,
)


@pytest.mark.parametrize("source", list(RefusalSource))
def test_denials_retain_native_variants_and_feedback_separately(source):
    # Controlled boundary: unfamiliar native variant and no proven call owner.
    refusal = PermissionRefusal(
        ref="d1",
        action=DeniedAction(description="Write the file", tool_name="write"),
        source=source,
        native_variant="future-denial-kind",
        evidence=[DecisionEvidence(field="native.kind", value="future-denial-kind")],
        activity=None,
        explanation="The harness reports a refusal",
    )
    if source == RefusalSource.CANDIDATE:
        refusal["candidate_feedback"] = "Please use another file"
    session = Session(
        harness="illustrative", session_id="s", events=[], permission_refusals=[refusal]
    )

    decoded = json.loads(json.dumps(session))

    (actual,) = decoded["permission_refusals"]
    assert actual["source"] == source.value
    assert actual["native_variant"] == "future-denial-kind"
    assert actual["action"] == {"description": "Write the file", "tool_name": "write"}
    assert actual["activity"] is None
    if source == RefusalSource.CANDIDATE:
        assert actual["candidate_feedback"] == "Please use another file"
        assert actual["candidate_feedback"] != actual["explanation"]
    else:
        assert "candidate_feedback" not in actual


def _illustrative_plan_session():
    # Explicitly controlled examples, including an unpresented proposal, an
    # undecided presentation, and a decision with unknown version/presentation.
    evidence = [
        DecisionEvidence(
            field="example.review",
            value={
                "id": "review-1",
                "proposal": {"id": "native-v1", "text": "Original café plan"},
                "presented_version": "native-v1",
                "edit": {
                    "id": "native-v2",
                    "original_id": "native-v1",
                    "text": "Edited 雪 plan",
                },
                "decisions": [
                    {"version_id": "native-v1", "approved": False},
                    {"version_id": "native-v2", "approved": True},
                ],
            },
        )
    ]
    versions = [
        PlanVersion(
            ref="v1",
            kind=PlanVersionKind.PROPOSED,
            text="Original café plan",
            evidence=evidence,
            activity=None,
        ),
        PlanVersion(
            ref="v2",
            kind=PlanVersionKind.EDITED,
            text="Edited 雪 plan",
            evidence=evidence,
            activity=None,
            edited_from=PlanVersionAssociation(version_ref="v1", evidence=evidence),
        ),
        PlanVersion(
            ref="v3",
            kind=PlanVersionKind.UNKNOWN,
            text="Unpresented plan",
            evidence=[
                DecisionEvidence(field="native.content", value="Unpresented plan")
            ],
            activity=None,
        ),
    ]
    presentation = PlanPresentation(
        ref="p1",
        version=PlanVersionAssociation(version_ref="v1", evidence=evidence),
        evidence=evidence,
        activity=None,
    )
    decisions = [
        ObservedPlanDecision(
            ref="r1",
            outcome=PlanOutcome.REJECTED,
            native_decision=False,
            version=PlanVersionAssociation(version_ref="v1", evidence=evidence),
            presentation=PlanPresentationAssociation(
                presentation_ref="p1", evidence=evidence
            ),
            evidence=evidence,
            activity=None,
            candidate_feedback="Revise the plan",
            explanation="Review returned a rejection",
        ),
        ObservedPlanDecision(
            ref="r2",
            outcome=PlanOutcome.APPROVED,
            native_decision=True,
            version=PlanVersionAssociation(version_ref="v2", evidence=evidence),
            presentation=PlanPresentationAssociation(
                presentation_ref="p1", evidence=evidence
            ),
            evidence=evidence,
            activity=None,
        ),
        ObservedPlanDecision(
            ref="r3",
            outcome=PlanOutcome.UNKNOWN,
            native_decision={"kind": "future"},
            version=None,
            presentation=None,
            evidence=[
                DecisionEvidence(
                    field="example.unknown_decision", value={"kind": "future"}
                )
            ],
            activity=None,
        ),
    ]
    return Session(
        harness="illustrative",
        session_id="s",
        events=[],
        plan_versions=versions,
        plan_presentations=[
            presentation,
            PlanPresentation(
                ref="p2",
                version=None,
                evidence=[
                    DecisionEvidence(field="example.presentation.id", value="review-2")
                ],
                activity=None,
            ),
        ],
        plan_decisions=decisions,
    )


def test_plan_versions_presentations_and_decisions_are_independent():
    session = _illustrative_plan_session()

    decoded = json.loads(json.dumps(session))

    assert decoded["plan_versions"][0]["text"] == "Original café plan"
    assert decoded["plan_versions"][1]["text"] == "Edited 雪 plan"
    assert decoded["plan_versions"][1]["edited_from"]["version_ref"] == "v1"
    assert "edited_from" not in decoded["plan_versions"][2]
    outcomes = []
    decided_presentations = set()
    decided_versions = set()
    for decision in decoded["plan_decisions"]:
        outcomes.append(decision["outcome"])
        if decision["presentation"] is not None:
            decided_presentations.add(decision["presentation"]["presentation_ref"])
        if decision["version"] is not None:
            decided_versions.add(decision["version"]["version_ref"])
    assert outcomes == ["rejected", "approved", "unknown"]
    assert decided_presentations == {"p1"}
    assert decided_versions == {"v1", "v2"}
    assert decoded["plan_decisions"][2]["native_decision"] == {"kind": "future"}
    rejection = decoded["plan_decisions"][0]
    assert rejection["candidate_feedback"] != rejection["explanation"]


@pytest.mark.parametrize("limit", [0, 17, 4096, -1])
def test_structured_decision_facts_survive_raw_result_policy(limit):
    session = _illustrative_plan_session()
    call = ToolCall(name="review", input={"plan": "Complete input"}, result="雪" * 2000)
    session["events"] = [{"i": 7, "role": "assistant", "tool_calls": [call]}]
    session["permission_refusals"] = [
        PermissionRefusal(
            ref="d1",
            action=None,
            source=RefusalSource.UNKNOWN,
            native_variant=None,
            evidence=[DecisionEvidence(field="native.denied", value=True)],
            activity=None,
        )
    ]
    original = deepcopy(session)

    exported = export_session(session, limit)

    assert session == original
    assert json.loads(json.dumps(session)) == original
    for field in (
        "permission_refusals",
        "plan_versions",
        "plan_presentations",
        "plan_decisions",
    ):
        assert field not in exported
    (exported_call,) = exported["events"][0]["tool_calls"]
    assert exported_call["input"] == call["input"]
    assert exported_call["result_truncated"] is (limit >= 0)
    if limit >= 0:
        assert len(exported_call["result"].encode()) <= limit
    else:
        assert exported_call["result"] == call["result"]
    assert "permission_denial" not in exported_call
    assert "plan_decision" not in exported_call


def _claude_interactions():
    records = read_source(CLAUDE_TRANSCRIPT).entries
    calls = {}
    for record in records:
        if record.get("type") == claude_protocol.ASSISTANT_RECORD:
            for block in record["message"]["content"]:
                if block.get("type") == claude_protocol.TOOL_USE_BLOCK:
                    calls[block["id"]] = block
    interactions = []
    for record in records:
        if record.get("type") != claude_protocol.USER_RECORD:
            continue
        content = record["message"]["content"]
        if not isinstance(content, list):
            continue
        for block in content:
            if block.get("tool_use_id") in calls:
                interactions.append((calls[block["tool_use_id"]], record, block))
    return interactions


def test_claude_native_denial_variants_have_evidenced_call_owners():
    refusals = []
    events = []
    sources = {
        "user-rejected": RefusalSource.CANDIDATE,
        "permission-rule": RefusalSource.SETTINGS,
    }
    for call, record, result in _claude_interactions():
        # Question cancellation and plan rejection use this marker too. Select
        # the capture's actual permission-gated actions before interpreting it.
        if call["name"] not in (claude_protocol.WRITE_TOOL, claude_protocol.BASH_TOOL):
            continue
        variant = record.get("toolDenialKind")
        if variant not in sources:
            continue
        evidence = [
            DecisionEvidence(field="tool_use.id", value=call["id"]),
            DecisionEvidence(
                field="tool_result.tool_use_id", value=result["tool_use_id"]
            ),
        ]
        refusal = PermissionRefusal(
            ref=f"d{len(refusals)}",
            action=DeniedAction(description=call["name"], tool_name=call["name"]),
            source=sources[variant],
            native_variant=variant,
            evidence=[DecisionEvidence(field="toolDenialKind", value=variant)],
            activity=DecisionActivityReference(
                message_i=len(refusals), tool_call_index=0, evidence=evidence
            ),
            explanation=result["content"],
        )
        events.append(
            {
                "i": len(refusals),
                "role": "assistant",
                "tool_calls": [
                    ToolCall(
                        name=call["name"],
                        id=call["id"],
                        input=call["input"],
                        result=result["content"],
                    )
                ],
            }
        )
        refusals.append(refusal)

    session = Session(
        harness="claude", session_id="s", events=events, permission_refusals=refusals
    )
    assert refusals, "Capture must supply explicit denial variants"
    observed_sources = set()
    decoded = json.loads(json.dumps(session))
    for refusal in decoded["permission_refusals"]:
        observed_sources.add(refusal["source"])
        call_id, result_id = refusal["activity"]["evidence"]
        assert call_id["value"] == result_id["value"]
        activity = refusal["activity"]
        owner = decoded["events"][activity["message_i"]]
        canonical_call = owner["tool_calls"][activity["tool_call_index"]]
        assert canonical_call["id"] == result_id["value"]
        assert "candidate_feedback" not in refusal
    assert observed_sources == {"candidate", "settings"}


def test_claude_native_edited_approval_retains_both_plan_versions():
    edited_approvals = []
    for interaction in _claude_interactions():
        call, record, _ = interaction
        result = record.get("toolUseResult")
        if (
            call["name"] == claude_protocol.EXIT_PLAN_MODE
            and isinstance(result, dict)
            and result.get("planWasEdited") is True
        ):
            edited_approvals.append(interaction)
    assert len(edited_approvals) == 1, "Capture must contain real plan-editor approval"
    call, record, result_block = edited_approvals[0]
    native = record["toolUseResult"]
    evidence = [
        DecisionEvidence(
            field="toolUseResult.planWasEdited", value=native["planWasEdited"]
        )
    ]
    proposed = PlanVersion(
        ref="v1",
        kind=PlanVersionKind.PROPOSED,
        text=call["input"]["plan"],
        evidence=[
            DecisionEvidence(field="tool_use.input.plan", value=call["input"]["plan"])
        ],
        activity=None,
    )
    edited = PlanVersion(
        ref="v2",
        kind=PlanVersionKind.EDITED,
        text=native["plan"],
        evidence=[
            *evidence,
            DecisionEvidence(field="toolUseResult.plan", value=native["plan"]),
        ],
        activity=None,
        edited_from=PlanVersionAssociation(
            version_ref="v1",
            evidence=[
                DecisionEvidence(field="tool_use.id", value=call["id"]),
                DecisionEvidence(
                    field="tool_result.tool_use_id", value=result_block["tool_use_id"]
                ),
                *evidence,
            ],
        ),
    )

    decoded = json.loads(json.dumps([proposed, edited]))

    assert decoded[0]["text"] == call["input"]["plan"]
    assert decoded[1]["text"] == native["plan"]
    assert decoded[0]["text"] != decoded[1]["text"]
    assert call["id"] == result_block["tool_use_id"]
    original_asset = (CLAUDE_ROOT / "assets" / "plan-before-editor.md").read_text()
    edited_asset = (CLAUDE_ROOT / "assets" / "plan-after-editor.md").read_text()
    assert proposed["text"] == original_asset
    assert edited["text"] == edited_asset


def test_claude_native_plan_tool_error_does_not_establish_rejection():
    errors = []
    for call, record, result in _claude_interactions():
        content = result.get("content")
        if (
            call["name"] == claude_protocol.EXIT_PLAN_MODE
            and isinstance(content, str)
            and content.startswith("<tool_use_error>")
        ):
            errors.append((call, record, result))
    assert len(errors) == 1, "Capture must retain the failed plan presentation"
    call, record, result = errors[0]
    assistants = []
    for entry in read_source(CLAUDE_TRANSCRIPT).entries:
        if entry.get("type") != claude_protocol.ASSISTANT_RECORD:
            continue
        for block in claude._content_blocks(entry):
            if isinstance(block, dict) and block.get("id") == call["id"]:
                assistants.append(entry)
    (assistant,) = assistants
    session = claude.interpret_source(
        claude.ClaudeSource(CLAUDE_TRANSCRIPT, [assistant, record])
    )
    assert result["is_error"] is True
    assert "toolDenialKind" not in record
    (message,) = session["events"]
    (interpreted_call,) = message["tool_calls"]
    assert "plan_decisions" not in interpreted_call
    assert "permission_refusals" not in interpreted_call


def _copilot_plan_records(data, start):
    response = data["response"]
    assert response["approved"] is True or response["approved"] is False
    evidence = [
        DecisionEvidence(
            field="tool.execution_start.toolCallId", value=start["toolCallId"]
        ),
        DecisionEvidence(
            field="human_response.recorded.toolCallId", value=data["toolCallId"]
        ),
        DecisionEvidence(
            field="tool.execution_start.toolName", value=start["toolName"]
        ),
        DecisionEvidence(
            field="data.response.responseKind", value=response["responseKind"]
        ),
    ]
    # The returned content is directly contained in the decision record. This
    # does not prove it was identical to the content originally displayed.
    version = PlanVersion(
        ref="v1",
        kind=PlanVersionKind.UNKNOWN,
        text=response["planContent"],
        evidence=[
            DecisionEvidence(
                field="data.response.planContent", value=response["planContent"]
            )
        ],
        activity=None,
    )
    presentation = PlanPresentation(
        ref="p1", version=None, evidence=evidence, activity=None
    )
    decision = ObservedPlanDecision(
        ref="r1",
        outcome=PlanOutcome.APPROVED
        if response["approved"] is True
        else PlanOutcome.REJECTED,
        native_decision=response["approved"],
        version=PlanVersionAssociation(
            version_ref="v1",
            evidence=[
                DecisionEvidence(field="data.response", value=response),
            ],
        ),
        presentation=PlanPresentationAssociation(
            presentation_ref="p1", evidence=evidence
        ),
        evidence=[
            DecisionEvidence(field="data.response.approved", value=response["approved"])
        ],
        activity=None,
    )
    if "feedback" in response:
        decision["candidate_feedback"] = response["feedback"]
    return Session(
        harness="copilot",
        session_id="s",
        events=[],
        plan_versions=[version],
        plan_presentations=[presentation],
        plan_decisions=[decision],
    )


def test_copilot_native_plan_decisions_keep_content_and_feedback():
    starts = {}
    records = native_records()
    for record in records:
        if (
            record["type"] == copilot_protocol.TOOL_EXECUTION_START
            and record["data"].get("toolName") == copilot_protocol.EXIT_PLAN_MODE
        ):
            starts[record["data"]["toolCallId"]] = record["data"]
    outcomes = set()
    feedback = []
    for record in records:
        if record["type"] != copilot_protocol.HUMAN_RESPONSE_RECORDED:
            continue
        data = record["data"]
        response = data["response"]
        if response.get("responseKind") != copilot_protocol.EXIT_PLAN_MODE:
            continue
        start = starts[data["toolCallId"]]
        session = _copilot_plan_records(data, start)

        decoded = json.loads(json.dumps(session))

        (version,) = decoded["plan_versions"]
        (decision,) = decoded["plan_decisions"]
        (presentation,) = decoded["plan_presentations"]
        assert version["text"] == response["planContent"]
        assert isinstance(version["text"], str) and version["text"]
        assert decision["version"]["version_ref"] == version["ref"]
        assert decision["presentation"]["presentation_ref"] == presentation["ref"]
        assert presentation["version"] is None
        assert start["toolCallId"] == data["toolCallId"]
        assert decision["native_decision"] is response["approved"]
        assert decision.get("candidate_feedback") == response.get("feedback")
        outcomes.add(decision["outcome"])
        if "candidate_feedback" in decision:
            feedback.append(decision["candidate_feedback"])
    assert outcomes == {"approved", "rejected"}, "Both plan decisions are required"
    assert feedback, "Capture must retain rejection feedback"


def test_copilot_native_permission_feedback_is_not_a_settings_explanation():
    refusals = []
    for record in native_records():
        data = record["data"]
        if (
            record["type"] == copilot_protocol.PERMISSION_COMPLETED
            and data.get("result", {}).get("kind") == copilot_protocol.DENIED_BY_USER
        ):
            assert data["decisionSource"] == "human_response"
            refusals.append(
                PermissionRefusal(
                    ref="candidate",
                    native_id=data["requestId"],
                    action=None,
                    source=RefusalSource.CANDIDATE,
                    native_variant=data["result"]["kind"],
                    candidate_feedback=data["result"]["feedback"],
                    activity=None,
                    evidence=[
                        DecisionEvidence(field="permission.completed.data", value=data)
                    ],
                )
            )
        if record["type"] == copilot_protocol.TOOL_EXECUTION_COMPLETE:
            error = data.get("error", {})
            if error.get("code") == copilot_protocol.DENIED_ERROR and error.get(
                "message", ""
            ).startswith(copilot_protocol.SETTINGS_REFUSAL_PREFIX):
                refusals.append(
                    PermissionRefusal(
                        ref="settings",
                        action=None,
                        source=RefusalSource.SETTINGS,
                        native_variant=error["code"],
                        explanation=error["message"],
                        activity=None,
                        evidence=[
                            DecisionEvidence(
                                field="tool.execution_complete.data", value=data
                            )
                        ],
                    )
                )

    decoded = json.loads(json.dumps(refusals))

    assert len(decoded) == 2, "Human denial and configured refusal both required"
    by_source = {}
    for refusal in decoded:
        by_source[refusal["source"]] = refusal
    assert set(by_source) == {"candidate", "settings"}
    candidate = by_source["candidate"]
    settings = by_source["settings"]
    assert (
        isinstance(candidate["candidate_feedback"], str)
        and candidate["candidate_feedback"]
    )
    assert "explanation" not in candidate
    assert "candidate_feedback" not in settings
    assert (
        settings["explanation"] == settings["evidence"][0]["value"]["error"]["message"]
    )


def _codex_native_refusal_evidence(entries):
    calls = {}
    results = []
    for entry in entries:
        payload = entry["payload"]
        if entry["type"] != codex_protocol.RESPONSE_ITEM:
            continue
        if payload.get("type") in (
            codex_protocol.FUNCTION_CALL,
            codex_protocol.CUSTOM_TOOL_CALL,
        ):
            calls[payload["call_id"]] = payload
        if payload.get("type") in (
            codex_protocol.FUNCTION_CALL_OUTPUT,
            codex_protocol.CUSTOM_TOOL_CALL_OUTPUT,
        ):
            results.append(payload)
    cancellations = []
    settings = []
    for result in results:
        output = result["output"]
        if isinstance(output, str) and output.startswith("aborted by user"):
            cancellations.append(result)
        if isinstance(output, list) and "read-only sandbox" in json.dumps(output):
            settings.append(result)
    return calls, cancellations, settings


def test_codex_native_cancellations_stay_distinct_from_settings_refusal():
    entries = load_entries(CODEX_TRANSCRIPT)
    calls, cancellations, settings = _codex_native_refusal_evidence(entries)
    assert len(cancellations) == 2, "Permission and question cancellations required"
    assert len(settings) == 1, "Settings refusal must remain distinct"
    for result in cancellations + settings:
        assert result["call_id"] in calls
    session = interpret_source(CodexSource(CODEX_TRANSCRIPT, entries))
    (settings_result,) = settings
    assert session is not None
    decoded = json.loads(json.dumps(session))
    interpreted_calls = {}
    for event in decoded["events"]:
        for call in event.get("tool_calls", []):
            interpreted_calls[call["id"]] = call
    settings_call = interpreted_calls[settings_result["call_id"]]
    (refusal,) = settings_call["permission_refusals"]
    assert refusal["source"] == "settings"
    assert refusal["evidence"][0]["value"] == settings_result["output"]
    assert "candidate_feedback" not in refusal
    for result in cancellations:
        canonical_call = interpreted_calls[result["call_id"]]
        assert canonical_call["result_record"]["content"] == result["output"]
        assert "permission_denial" not in canonical_call
        assert "plan_decision" not in canonical_call


@pytest.mark.parametrize("on_call", [False, True])
@pytest.mark.parametrize("limit", [0, 17, -1])
def test_shared_export_preserves_facts_on_their_canonical_activity(on_call, limit):
    session = _illustrative_plan_session()
    session["events"] = [
        {
            "i": 7,
            "role": "assistant",
            "text": "Conversation before review",
            "tool_calls": [
                {"name": "review", "input": {"complete": True}, "result": "雪" * 50}
            ],
        },
        {"i": 9, "role": "user", "text": "Unrelated conversation"},
    ]
    evidence = [DecisionEvidence(field="controlled.review_id", value="review-1")]
    activity = DecisionActivityReference(message_i=7, evidence=evidence)
    if on_call:
        activity["tool_call_index"] = 0
    for collection in (
        session["plan_versions"],
        session["plan_presentations"],
        session["plan_decisions"],
    ):
        for record in collection:
            record["activity"] = deepcopy(activity)
    target = session["events"][0]
    if on_call:
        target = target["tool_calls"][0]
    target["plan_versions"] = session["plan_versions"]
    target["plan_presentations"] = session["plan_presentations"]
    target["plan_decisions"] = session["plan_decisions"]
    original = deepcopy(session)

    envelope = export_session(session, limit)

    assert session == original
    assert envelope["events"][0]["text"] == session["events"][0]["text"]
    assert envelope["events"][1] == session["events"][1]
    target = envelope["events"][0]
    if on_call:
        target = target["tool_calls"][0]
    assert target["plan_versions"] == session["plan_versions"]
    assert target["plan_presentations"] == session["plan_presentations"]
    assert target["plan_decisions"] == session["plan_decisions"]
    target["plan_versions"][0]["text"] = "Edited export"
    assert session == original


@pytest.mark.parametrize(
    "activity",
    [
        None,
        {"message_i": 7, "evidence": [{"field": "id", "value": "valid"}]},
        {"message_i": 7, "evidence": []},
        {"message_i": 8, "evidence": [{"field": "id", "value": "missing"}]},
        {
            "message_i": 7,
            "tool_call_index": -1,
            "evidence": [{"field": "id", "value": "missing"}],
        },
        {
            "message_i": 7,
            "tool_call_index": 2,
            "evidence": [{"field": "id", "value": "missing"}],
        },
    ],
)
def test_session_decision_collections_are_not_projected_during_export(activity):
    session = _illustrative_plan_session()
    session["events"] = [
        {
            "i": 7,
            "role": "assistant",
            "text": "Existing",
            "tool_calls": [{"name": "review"}],
        }
    ]
    for collection in (
        session["plan_versions"],
        session["plan_presentations"],
        session["plan_decisions"],
    ):
        for record in collection:
            record["activity"] = activity

    envelope = export_session(session, 0)

    assert envelope["events"] == session["events"]
