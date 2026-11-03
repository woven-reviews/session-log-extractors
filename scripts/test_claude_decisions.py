"""Structured refusal and plan facts from the committed interactive capture."""

from copy import deepcopy
from pathlib import Path

import pytest

from session_log_extractors.claude_sources import ClaudeSource, load_entries
from session_log_extractors.extract_claude_session_log import (
    interpret_source,
    parse_permission_denial,
    parse_plan_decision,
)
from session_log_extractors.session_log_envelope import export_session

ROOT = Path(__file__).parent / "fixtures/claude/2.1.312"
(TRANSCRIPT,) = (ROOT / "native").glob("*.jsonl")


def _interaction(name, marker="", input_marker=""):
    entries = load_entries(TRANSCRIPT)
    matches = []
    for entry in entries:
        if entry.get("type") != "assistant":
            continue
        for block in entry.get("message", {}).get("content", []):
            if block.get("name") != name or input_marker not in str(block.get("input")):
                continue
            for result in entries:
                if result.get("type") != "user":
                    continue
                for native in result.get("message", {}).get("content", []):
                    if not isinstance(native, dict):
                        continue
                    if native.get("tool_use_id") == block.get("id") and marker in str(
                        native.get("content")
                    ):
                        matches.append((entry, result))
    assert len(matches) == 1
    return matches[0]


def _session(entries):
    session = interpret_source(ClaudeSource(TRANSCRIPT, entries))
    assert session is not None
    return session


def _call(session):
    calls = []
    for event in session["events"]:
        calls.extend(event.get("tool_calls") or [])
    assert len(calls) == 1
    return calls[0]


@pytest.mark.parametrize(
    ("name", "marker", "source"),
    [
        ("Write", "The user doesn't", "candidate"),
        ("Bash", "CAPTURE_SETTINGS_REFUSAL", "settings"),
    ],
)
def test_native_refusal_survives_cap(name, marker, source):
    input_marker = "denied-feedback.txt" if name == "Write" else ""
    pair = list(_interaction(name, marker, input_marker))
    session = _session(pair)
    before = deepcopy(session)
    envelope = export_session(session, 1)
    (refusal,) = _call(envelope)["permission_refusals"]
    assert refusal["source"] == source
    assert refusal["native_variant"] == pair[1]["toolDenialKind"]
    assert refusal["action"]["tool_name"] == name
    assert refusal["activity"]["tool_call_index"] == 0
    assert refusal["evidence"]
    assert "candidate_feedback" not in refusal
    assert _call(envelope)["result_truncated"] is True
    assert session == before


def test_native_edited_plan_preserves_versions_and_links():
    pair = list(_interaction("ExitPlanMode", "## Approved Plan (edited by user):"))
    session = _session(pair)
    envelope = export_session(session, 1)
    call = _call(envelope)
    proposed, edited = call["plan_versions"]
    assert proposed["text"] == _plan_call_block(pair[0])["input"]["plan"]
    assert edited["text"] == (ROOT / "assets/plan-after-editor.md").read_text()
    assert edited["edited_from"]["version_ref"] == proposed["ref"]
    (presentation,) = call["plan_presentations"]
    assert presentation["version"]["version_ref"] == proposed["ref"]
    (decision,) = call["plan_decisions"]
    assert decision["outcome"] == "approved"
    assert decision["version"]["version_ref"] == edited["ref"]
    assert decision["presentation"]["presentation_ref"] == presentation["ref"]
    assert "candidate_feedback" not in decision


def test_rejection_preserves_explicit_feedback():
    pair = list(_interaction("ExitPlanMode", "CAPTURE_PLAN_REJECT_1:"))
    envelope = export_session(_session(pair), 1)
    call = _call(envelope)
    (decision,) = call["plan_decisions"]
    assert decision["outcome"] == "rejected"
    assert decision["candidate_feedback"].startswith("CAPTURE_PLAN_REJECT_1:")
    assert decision["candidate_feedback"] not in decision["explanation"]
    assert (
        call["permission_refusals"][0]["candidate_feedback"]
        == decision["candidate_feedback"]
    )


def test_silence_does_not_create_plan_decision():
    assistant, _ = _interaction("ExitPlanMode", "CAPTURE_PLAN_REJECT_1:")
    envelope = export_session(_session([assistant]), 1)
    call = _call(envelope)
    assert call["plan_presentations"]
    assert "plan_decisions" not in call


def _plan_call_block(entry):
    matches = []
    for block in entry["message"]["content"]:
        if block.get("name") == "ExitPlanMode":
            matches.append(block)
    assert len(matches) == 1
    return matches[0]


def _result_block(entry):
    blocks = []
    for block in entry["message"]["content"]:
        if block.get("type") == "tool_result":
            blocks.append(block)
    assert len(blocks) == 1
    return blocks[0]


@pytest.mark.parametrize("variant", ["missing-id", "wrong-id", "duplicate-call"])
def test_orphan_approval_keeps_content_without_association(variant):
    assistant, result = _interaction(
        "ExitPlanMode", "## Approved Plan (edited by user):"
    )
    # Alter only the call ownership evidence, retaining native editor data.
    entries = [assistant, result]
    if variant == "missing-id":
        _result_block(result).pop("tool_use_id")
    elif variant == "wrong-id":
        _result_block(result)["tool_use_id"] = "unrecorded"
    else:
        entries.insert(1, deepcopy(assistant))
    envelope = export_session(_session(entries), 1)
    orphan_results = []
    for event in envelope["events"]:
        for call in event.get("tool_calls") or []:
            assert "plan_decisions" not in call
        orphan_results.extend(event.get("tool_results", []))
    assert len(orphan_results) == 1
    (decision,) = orphan_results[0]["plan_decisions"]
    assert decision["outcome"] == "approved"
    assert decision["activity"] is None
    assert decision["presentation"] is None
    edited = orphan_results[0]["plan_versions"][0]
    assert edited["text"] == result["toolUseResult"]["plan"]
    assert "edited_from" not in edited
    assert decision["version"]["version_ref"] == edited["ref"]


def test_unmatched_refusal_never_borrows_a_nearby_call():
    assistant, result = _interaction("Bash", "CAPTURE_SETTINGS_REFUSAL")
    _result_block(result)["tool_use_id"] = "unrecorded"
    envelope = export_session(_session([assistant, result]), 1)
    assert "permission_refusals" not in _call(envelope)
    record = envelope["events"][-1]["tool_results"][0]
    (refusal,) = record["permission_refusals"]
    assert refusal["activity"] is None
    assert refusal["action"] is None
    assert refusal["source"] == "settings"


@pytest.mark.parametrize(
    "text", ["error: file not found", "User has approved your plan"]
)
def test_plan_tool_errors_are_not_candidate_decisions(text):
    assistant, result = _interaction("ExitPlanMode", "CAPTURE_PLAN_REJECT_1:")
    _result_block(result)["content"] = text
    # Retain is_error but remove stale refusal evidence from the original capture.
    result.pop("toolDenialKind")
    result.pop("toolUseResult")
    call = _call(export_session(_session([assistant, result]), 1))
    assert "plan_decisions" not in call
    assert "plan_presentations" not in call
    assert "permission_refusals" not in call


def test_settings_plan_refusal_does_not_imply_candidate_rejection():
    assistant, _ = _interaction("ExitPlanMode", "CAPTURE_PLAN_REJECT_1:")
    _, result = _interaction("Bash", "CAPTURE_SETTINGS_REFUSAL")
    _result_block(result)["tool_use_id"] = _plan_call_block(assistant)["id"]
    call = _call(export_session(_session([assistant, result]), 1))
    assert "plan_decisions" not in call
    assert call["permission_refusals"][0]["source"] == "settings"
    assert "plan_presentations" not in call


@pytest.mark.parametrize("text", ["", "Unrecognized result"])
def test_empty_or_unknown_plan_result_never_becomes_approval(text):
    assistant, result = _interaction("ExitPlanMode", "CAPTURE_PLAN_REJECT_1:")
    block = _result_block(result)
    block["content"] = text
    block["is_error"] = False
    result.pop("toolDenialKind")
    result.pop("toolUseResult")
    call = _call(export_session(_session([assistant, result]), 1))
    decisions = call.get("plan_decisions", [])
    if text:
        assert len(decisions) == 1
        assert decisions[0]["outcome"] == "unknown"
    else:
        assert decisions == []


def test_unfamiliar_kind_alone_does_not_establish_a_refusal():
    assistant, result = _interaction("Bash", "CAPTURE_SETTINGS_REFUSAL")
    result["toolDenialKind"] = "future-classifier-value"
    result["toolUseResult"] = "error: process failed"
    _result_block(result)["content"] = "error: process failed"
    call = _call(export_session(_session([assistant, result]), 1))
    assert "permission_refusals" not in call
    assert call["result_record"]["native_denial_kind"] == "future-classifier-value"


def test_legacy_candidate_denial_without_native_kind():
    assistant, result = _interaction("Bash", "CAPTURE_SETTINGS_REFUSAL")
    # Controlled legacy candidate refusal; source is the recognized native variant.
    result.pop("toolDenialKind")
    result.pop("toolUseResult")
    _result_block(result)["content"] = (
        "Permission for this tool use was denied. The user said:\nPlease stop"
    )
    (refusal,) = _call(export_session(_session([assistant, result]), 1))[
        "permission_refusals"
    ]
    assert refusal["source"] == "candidate"
    assert refusal["candidate_feedback"] == "Please stop"
    assert refusal["native_variant"] == "Permission for this tool use was denied."


def test_native_capture_has_no_unresolved_refusal_sources():
    session = _session(load_entries(TRANSCRIPT))
    refusals = []
    for event in session["events"]:
        for call in event.get("tool_calls") or []:
            refusals.extend(call.get("permission_refusals", []))
    assert refusals
    for refusal in refusals:
        assert refusal["source"] in {"candidate", "settings"}, refusal["evidence"]


def test_text_blocks_preserve_plan_approval_and_legacy_edited_version():
    assistant, result = _interaction(
        "ExitPlanMode", "## Approved Plan (edited by user):"
    )
    result.pop("toolUseResult")
    original = _result_block(result)["content"]
    _result_block(result)["content"] = [
        {"type": "text", "text": original},
        {"type": "image", "source": {}},
    ]
    call = _call(export_session(_session([assistant, result]), 1))
    assert call["plan_decisions"][0]["outcome"] == "approved"
    assert (
        call["plan_versions"][-1]["text"]
        == (ROOT / "assets/plan-after-editor.md").read_text()
    )


def test_plan_presentation_without_content_preserves_unknown_version():
    assistant, _ = _interaction("ExitPlanMode", "CAPTURE_PLAN_REJECT_1:")
    _plan_call_block(assistant)["input"].pop("plan")
    call = _call(export_session(_session([assistant]), 1))
    assert "plan_versions" not in call
    assert call["plan_presentations"][0]["version"] is None


def test_permission_denial_with_message():
    # Legacy 2.1.292 inline-feedback wording; current capture records it separately.
    c = (
        "Permission for this tool use was denied. The tool use was rejected (eg. "
        "if it was a file edit, the new_string was NOT written to the file). The "
        "user said:\nWe need a new branch for this"
    )
    assert parse_permission_denial(c) == "We need a new branch for this"


def test_permission_denial_ignores_normal_result():
    assert parse_permission_denial("42 files changed") is None
    assert parse_permission_denial("error: file not found") is None


def test_permission_denial_ignores_isc_license_text():
    assert (
        parse_permission_denial("Permission to use, copy, modify, and/or distribute")
        is None
    )


def test_plan_decision_unknown_for_unrecognized_content():
    assert parse_plan_decision("just some other result text") == ("unknown", "", "")
    assert parse_plan_decision(None) == ("unknown", "", "")


def test_legacy_settings_variant_retains_source_without_native_kind():
    assistant, result = _interaction("Bash", "CAPTURE_SETTINGS_REFUSAL")
    result.pop("toolDenialKind")
    result.pop("toolUseResult")
    call = _call(export_session(_session([assistant, result]), 1))
    (refusal,) = call["permission_refusals"]
    assert refusal["source"] == "settings"
    assert refusal["native_variant"] == _result_block(result)["content"].removesuffix(
        "."
    )


def test_alternate_rejection_establishes_refusal_when_text_is_unavailable():
    assistant, result = _interaction(
        "ExitPlanMode", "The user doesn't", "CAPTURE_PLAN_SECOND"
    )
    _result_block(result)["content"] = None
    result.pop("toolDenialKind")
    call = _call(export_session(_session([assistant, result]), 1))
    assert call["permission_refusals"][0]["source"] == "candidate"
    assert call["permission_refusals"][0]["native_variant"] is None


@pytest.mark.parametrize("edited", [False, True])
def test_ambiguous_alternate_plan_keeps_content_without_inventing_approval(edited):
    assistant, result = _interaction(
        "ExitPlanMode", "## Approved Plan (edited by user):"
    )
    # A second result makes the entry-level payload's result owner ambiguous.
    second = deepcopy(_result_block(result))
    second["tool_use_id"] = "unrecorded"
    second["content"] = "Other tool completed"
    result["message"]["content"].append(second)
    result["toolUseResult"]["planWasEdited"] = edited
    envelope = export_session(_session([assistant, result]), 1)
    unmatched = envelope["events"][-1]["tool_results"]
    payload_records = []
    for record in unmatched:
        if isinstance(record.get("native_result"), dict):
            payload_records.append(record)
    assert len(payload_records) == 1
    record = payload_records[0]
    (version,) = record["plan_versions"]
    assert version["text"] == result["toolUseResult"]["plan"]
    assert version["kind"] == ("edited" if edited else "unknown")
    assert version["activity"] is None
    assert "edited_from" not in version
    assert "plan_decisions" not in record
    assert "plan_presentations" not in record


def test_native_as_is_approval_has_one_complete_proposal():
    assistant, result = _interaction("ExitPlanMode", "## Approved Plan:")
    call = _call(export_session(_session([assistant, result]), 1))
    (version,) = call["plan_versions"]
    assert isinstance(version["text"], str) and version["text"]
    assert version["text"] == result["toolUseResult"]["plan"]
    assert call["plan_decisions"][0]["outcome"] == "approved"
    assert call["plan_decisions"][0]["version"]["version_ref"] == version["ref"]


def test_orphan_as_is_approval_retains_complete_returned_plan():
    _, result = _interaction("ExitPlanMode", "## Approved Plan:")
    record = export_session(_session([result]), 1)["events"][0]["tool_results"][0]
    (version,) = record["plan_versions"]
    assert version["kind"] == "unknown"
    assert version["text"] == result["toolUseResult"]["plan"]
    (decision,) = record["plan_decisions"]
    assert decision["version"]["version_ref"] == version["ref"]
    assert decision["presentation"] is None
