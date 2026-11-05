"""Codex evidence becomes shared facts before export policy."""

from copy import deepcopy

import pytest
from codex_capture import TRANSCRIPT, exec_interactions

from session_log_extractors.codex_sources import CodexSource, load_entries
from session_log_extractors.extract_codex_session_log import interpret_source
from session_log_extractors.session_log_envelope import export_session


def _interpret(entries):
    session = interpret_source(CodexSource(TRANSCRIPT, entries))
    assert session is not None
    return session


def _facts(session, field):
    """Inspect facts on their canonical messages, calls, and unmatched results."""
    facts = []
    for message in session["events"]:
        facts.extend(message.get(field, []))
        for call in message.get("tool_calls") or []:
            facts.extend(call.get(field, []))
    for result in session.get("unmatched_tool_results", []):
        facts.extend(result.get(field, []))
    return facts


def test_native_proposals_and_approval_keep_unknown_version_ownership():
    entries = load_entries(TRANSCRIPT)
    session = _interpret(entries)

    versions = _facts(session, "plan_versions")
    assert len(versions) == 2, "Both native plan proposals are required"
    for version in versions:
        activity = version["activity"]
        message = session["events"][activity["message_i"]]
        assert isinstance(version["text"], str) and version["text"].strip()
        recorded_block = "<proposed_plan>" + version["text"] + "</proposed_plan>"
        assert recorded_block == message["text"].strip()
        assert version["kind"] == "proposed"
        assert "edited_from" not in version
        assert message["plan_versions"] == [version]
    (decision,) = _facts(session, "plan_decisions")
    assert decision["outcome"] == "approved"
    assert decision["version"] is None
    assert decision["presentation"] is None
    assert not _facts(session, "plan_presentations")


def test_native_settings_refusal_excludes_cancellations_and_assistant_reports():
    session = _interpret(load_entries(TRANSCRIPT))

    (refusal,) = _facts(session, "permission_refusals")
    assert refusal["source"] == "settings"
    assert "candidate_feedback" not in refusal
    activity = refusal["activity"]
    call = session["events"][activity["message_i"]]["tool_calls"][0]
    assert refusal["evidence"][0]["value"] == call["result_record"]["content"]
    assert refusal["action"]["tool_name"] == call["name"]
    assert call["permission_refusals"] == [refusal]


@pytest.mark.parametrize("limit", [0, 17, -1])
def test_native_facts_survive_caps_without_changing_conversation(limit):
    session = _interpret(load_entries(TRANSCRIPT))
    before = deepcopy(session)

    envelope = export_session(session, limit)

    assert session == before
    assert len(envelope["events"]) == len(session["events"])
    for version in _facts(session, "plan_versions"):
        message = envelope["events"][version["activity"]["message_i"]]
        assert version in message["plan_versions"]
    for refusal in _facts(session, "permission_refusals"):
        call = envelope["events"][refusal["activity"]["message_i"]]["tool_calls"][0]
        assert call["permission_refusals"] == [refusal]
    (decision,) = _facts(session, "plan_decisions")
    message = envelope["events"][decision["activity"]["message_i"]]
    assert message["plan_decisions"] == [decision]
    assert "permission_refusals" not in envelope
    assert "plan_versions" not in envelope
    assert "permission_refusals" not in session
    assert "plan_versions" not in session
    assert "plan_decisions" not in session


@pytest.mark.parametrize(
    ("output", "source", "feedback"),
    [
        (
            'exec_command failed: CreateProcess { message: "rejected by user" }',
            "candidate",
            None,
        ),
        (
            "The user doesn't want to proceed with this tool use.\nThe user said:\nUse café instead",
            "candidate",
            "Use café instead",
        ),
        ("Permission for this tool use was denied.", "unknown", None),
    ],
)
@pytest.mark.parametrize("matched", [True, False])
def test_controlled_legacy_denials_preserve_source_and_unmatched_evidence(
    output, source, feedback, matched
):
    # Older denial variants are absent from the selected native capture.
    entries = exec_interactions()
    entries[-1]["payload"]["output"] = output
    if not matched:
        entries[-1]["payload"]["call_id"] = "missing-call"
    session = _interpret(entries)

    (refusal,) = _facts(session, "permission_refusals")
    assert refusal["source"] == source
    assert refusal.get("candidate_feedback") == feedback
    assert (refusal["activity"] is not None) == matched
    assert (refusal["action"] is not None) == matched
    assert refusal["evidence"][0]["value"] == output


@pytest.mark.parametrize(
    "output", ["aborted by user after 1.2s", "command failed", "success"]
)
def test_controlled_non_denials_do_not_create_decisions(output):
    entries = exec_interactions()
    entries[-1]["payload"]["output"] = output
    entries[-1]["payload"]["is_error"] = True
    session = _interpret(entries)

    assert not _facts(session, "permission_refusals")
    assert not _facts(session, "plan_decisions")


def _controlled_user(text):
    # Keep native authorship evidence while controlling an explicit statement.
    entries = load_entries(TRANSCRIPT)
    for entry in entries:
        payload = entry["payload"]
        if entry["type"] != "response_item" or payload.get("role") != "user":
            continue
        metadata = payload.get("internal_chat_message_metadata_passthrough", {})
        if metadata.get("content_item_kinds") == ["user.text"]:
            payload["content"] = [{"type": "input_text", "text": text}]
            return entry
    raise AssertionError("Native candidate text required")


@pytest.mark.parametrize(
    ("text", "outcome"),
    [("Implement the plan.", "approved"), ("Reject the plan.", "rejected")],
)
def test_explicit_statements_do_not_require_a_previous_plan(text, outcome):
    session = _interpret([_controlled_user(text)])

    (decision,) = _facts(session, "plan_decisions")
    assert decision["outcome"] == outcome
    assert decision["native_decision"] == text
    assert decision["version"] is None and decision["presentation"] is None
    assert not _facts(session, "plan_versions")


@pytest.mark.parametrize(
    "text",
    [
        "",
        "Maybe",
        "Please revise it",
        "Implement the plan. quoted",
        "<proposed_plan>unfinished",
    ],
)
def test_ordinary_user_prose_does_not_invent_plan_facts(text):
    entries = [_controlled_user(text), *exec_interactions()[1:]]
    session = _interpret(entries)

    assert not _facts(session, "plan_versions")
    assert not _facts(session, "plan_decisions")


def test_unknown_authorship_does_not_create_candidate_approval():
    entry = _controlled_user("Implement the plan.")
    entry["payload"].pop("internal_chat_message_metadata_passthrough")
    session = _interpret([entry])

    assert not _facts(session, "plan_decisions")


@pytest.mark.parametrize("limit", [0, 17, -1])
def test_legacy_assistant_denial_has_no_invented_action_owner(limit):
    # This legacy harness message has no recorded tool reference.
    entry = _controlled_user("Permission for this tool use was denied.")
    entry["payload"]["role"] = "assistant"
    entry["payload"]["content"][0]["type"] = "output_text"
    session = _interpret([entry])

    (refusal,) = _facts(session, "permission_refusals")
    assert refusal["source"] == "unknown"
    assert refusal["action"] is None
    assert "tool_call_index" not in refusal["activity"]
    original = deepcopy(session)
    envelope = export_session(session, limit)
    assert envelope["events"][0]["permission_refusals"] == [refusal]
    assert session == original
    assert "permission_denial" not in session["events"][0]


def test_unmatched_refusal_stays_internal_instead_of_attaching_to_nearest_call():
    entries = exec_interactions()
    entries[-1]["payload"]["output"] = "Permission for this tool use was denied."
    entries[-1]["payload"]["call_id"] = "missing-call"
    session = _interpret(entries)

    envelope = export_session(session, 0)

    (unmatched_refusal,) = _facts(session, "permission_refusals")
    (unmatched_result,) = session["unmatched_tool_results"]
    assert unmatched_result["permission_refusals"] == [unmatched_refusal]
    assert unmatched_refusal["activity"] is None
    for message in envelope["events"]:
        assert "permission_refusals" not in message
        for call in message.get("tool_calls") or []:
            assert "permission_refusals" not in call


def test_explicit_rejection_keeps_candidate_feedback_without_linking_by_proximity():
    text = "Reject the plan.\nUse the café design instead.\nKeep the tests."
    session = _interpret([_controlled_user(text)])

    (decision,) = _facts(session, "plan_decisions")
    assert decision["outcome"] == "rejected"
    assert (
        decision["candidate_feedback"]
        == "Use the café design instead.\nKeep the tests."
    )
    assert "explanation" not in decision
    assert decision["native_decision"] == text
    envelope = export_session(session, 0)
    assert envelope["events"][0]["plan_decisions"] == [decision]


@pytest.mark.parametrize("value", [None, 123, {"unsupported": "shape"}])
def test_nontext_outputs_do_not_invent_refusals(value):
    entries = exec_interactions()
    entries[-1]["payload"]["output"] = value
    session = _interpret(entries)

    assert not _facts(session, "permission_refusals")


def test_reversed_results_attach_denial_only_to_its_recorded_call():
    entries = exec_interactions()
    denied_call_id = entries[1]["payload"]["call_id"]
    entries[3]["payload"]["output"] = (
        "The user doesn't want to proceed with this tool use."
    )
    first_result = entries.pop(3)
    entries.append(first_result)
    session = _interpret(entries)

    (refusal,) = _facts(session, "permission_refusals")
    activity = refusal["activity"]
    call = session["events"][activity["message_i"]]["tool_calls"][
        activity["tool_call_index"]
    ]
    assert call["id"] == denied_call_id
    assert activity["evidence"][0]["value"] == denied_call_id


@pytest.mark.parametrize("limit", [0, 17, -1])
def test_full_candidate_feedback_survives_textual_result_cap(limit):
    entries = exec_interactions()
    feedback = "Use café instead. " * 200
    output = (
        "The user doesn't want to proceed with this tool use.\nThe user said:\n"
        + feedback
    )
    entries[-1]["payload"]["output"] = output
    session = _interpret(entries)
    original = deepcopy(session)

    envelope = export_session(session, limit)

    (refusal,) = _facts(session, "permission_refusals")
    activity = refusal["activity"]
    call = envelope["events"][activity["message_i"]]["tool_calls"][0]
    assert call["permission_refusals"][0]["candidate_feedback"] == feedback.strip()
    assert call["permission_refusals"][0]["evidence"][0]["value"] == output
    assert session == original
    if limit >= 0:
        assert len(call["result"].encode("utf-8")) <= limit
        assert call["result_truncated"] is True


def test_native_generated_plan_wording_can_change_without_changing_ownership():
    entries = load_entries(TRANSCRIPT)
    expected_texts = []
    for entry in entries:
        payload = entry["payload"]
        if entry["type"] != "response_item" or payload.get("role") != "assistant":
            continue
        for block in payload.get("content", []):
            text = block.get("text", "")
            if text.lstrip().startswith("<proposed_plan>"):
                replacement = (
                    "\n# Reworded café proposal\n" + str(len(expected_texts)) + "\n"
                )
                block["text"] = "<proposed_plan>" + replacement + "</proposed_plan>"
                expected_texts.append(replacement)
    assert len(expected_texts) == 2, "Both native proposals required"
    session = _interpret(entries)

    actual_texts = []
    for version in _facts(session, "plan_versions"):
        actual_texts.append(version["text"])
    assert actual_texts == expected_texts
    (decision,) = _facts(session, "plan_decisions")
    assert decision["version"] is None


def test_settings_createprocess_failure_is_not_a_candidate_denial():
    # The settings explanation contains the older user-rejection substring.
    entries = exec_interactions()
    entries[-1]["payload"]["output"] = (
        'exec_command failed: CreateProcess { message: "rejected by user approval settings" }'
    )
    session = _interpret(entries)

    (refusal,) = _facts(session, "permission_refusals")
    assert refusal["source"] == "settings"
    for message in session["events"]:
        for call in message.get("tool_calls") or []:
            assert "permission_denial" not in call
