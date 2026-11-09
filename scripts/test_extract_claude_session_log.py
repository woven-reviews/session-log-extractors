#!/usr/bin/env python3
"""Claude extraction and boundary checks backed by the current native capture.

Run: uv run --locked pytest scripts/test_extract_claude_session_log.py
"""

from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import re
import shutil
import tempfile
from copy import deepcopy
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest
from claude_fixture import CHILD_TRANSCRIPT as SUBAGENT
from claude_fixture import ROOT, TRANSCRIPT

from session_log_extractors import claude_protocol, session_log_envelope
from session_log_extractors import extract_claude_session_log as eclog
from session_log_extractors.claude_decisions import parse_permission_denial
from session_log_extractors.claude_sources import (
    ClaudeSource,
    read_source,
    read_subagent_sources,
)
from session_log_extractors.extract_claude_session_log import (
    _candidate_project_dirs,
    _content_blocks,
    _extract_all,
    _human_text,
    _is_tool_result_entry,
    _jsonl_files,
    _result_block_text,
    build_events,
    build_raw_envelope,
    clean_user_text,
    encode_project_path,
    extract_file_path,
    extract_transcript,
    first_timestamp,
    load_entries,
    load_subagents,
    main,
    resolve_project_dir,
    select_transcript,
    session_cwd,
    session_models,
    summarize_subagent,
    tool_descriptor,
)
from session_log_extractors.session_log_envelope import (
    SIZE_WARN_BYTES,
    build_envelope,
    write_envelope,
)
from session_log_extractors.session_model import Session


def _recorded_session_entries():
    return load_entries(TRANSCRIPT)


def _capture_session_id():
    recorded_ids = set()
    for entry in _recorded_session_entries():
        if isinstance(entry.get("sessionId"), str) and entry["sessionId"]:
            recorded_ids.add(entry["sessionId"])
    return _require_single_match(recorded_ids, "recorded session ID")


def _require_single_match(matches, description):
    assert len(matches) == 1, f"Expected one {description}, found {len(matches)}"
    (match,) = matches
    return match


def _content_block(entry, kind):
    matches = []
    for block in _content_blocks(entry):
        if not isinstance(block, dict):
            continue
        if block.get("type") == kind:
            matches.append(block)

    return _require_single_match(matches, f"{kind} block")


def _recorded_user_message(marker):
    matches = []
    for entry in _recorded_session_entries():
        if entry.get("type") != claude_protocol.USER_RECORD:
            continue
        if entry.get("isMeta") or entry.get("isCompactSummary"):
            continue
        if marker in _human_text(entry):
            matches.append(entry)

    return _require_single_match(matches, f"user message containing {marker!r}")


def _recorded_result_for_call(entries, call):
    matches = []
    for entry in entries:
        if entry.get("type") != claude_protocol.USER_RECORD:
            continue

        for block in _content_blocks(entry):
            if not isinstance(block, dict):
                continue
            if block.get("type") != claude_protocol.TOOL_RESULT_BLOCK:
                continue
            if block.get("tool_use_id") == call["id"]:
                matches.append(entry)
                break

    return _require_single_match(
        matches, f"result for {call['name']} {call['input']!r}"
    )


def _recorded_tool_interactions(name, input_contains="", result_contains=""):
    """Return native (assistant entry, result entry) pairs matching the filters."""
    entries = _recorded_session_entries()
    matches = []
    for assistant in entries:
        if assistant.get("type") != claude_protocol.ASSISTANT_RECORD:
            continue

        for call in _content_blocks(assistant):
            if not isinstance(call, dict):
                continue
            if call.get("type") != claude_protocol.TOOL_USE_BLOCK:
                continue
            if call.get("name") != name:
                continue
            input_text = json.dumps(call.get("input"), ensure_ascii=False)
            if input_contains not in input_text:
                continue

            result = _recorded_result_for_call(entries, call)
            result_block = _content_block(result, claude_protocol.TOOL_RESULT_BLOCK)
            result_text = _result_block_text(result_block)
            if result_contains in result_text:
                matches.append((assistant, result))

    assert matches, (
        f"Missing {name} activity matching {input_contains!r} / {result_contains!r}"
    )
    return matches


def _recorded_tool_interaction(name, input_contains="", result_contains=""):
    matches = _recorded_tool_interactions(name, input_contains, result_contains)
    return _require_single_match(
        matches,
        f"{name} activity matching {input_contains!r} / {result_contains!r}",
    )


def _single_tool_call_in(events):
    calls = []
    for event in events:
        calls.extend(event.get("tool_calls", []))
    return _require_single_match(calls, "tool call")


def _assert_assistant_text(events):
    responses = []
    for event in events:
        if event["role"] == "assistant":
            responses.append(event["text"])
    assert responses
    nonempty_responses = []
    for text in responses:
        assert isinstance(text, str)
        if text.strip():
            nonempty_responses.append(text)
    assert nonempty_responses


def _native_question_in(tool_input, *, multi_select):
    matches = []
    for question in tool_input["questions"]:
        if bool(question.get("multiSelect")) == multi_select:
            matches.append(question)
    return _require_single_match(matches, f"question with multiSelect={multi_select}")


def _response_to_question(call, *, multi_select):
    native_question = _native_question_in(call["input"], multi_select=multi_select)
    matches = []
    for question in call["interaction_questions"]:
        if question["text"] == native_question["question"]:
            matches.append(question)
    question = _require_single_match(matches, "question matching its native input")
    responses = []
    for response in call["question_responses"]:
        association = response["question"]
        if association is not None and association["question_ref"] == question["ref"]:
            responses.append(response)
    return _require_single_match(responses, "response to the recorded question")


def _tool_calls_in(session):
    calls = []
    for event in session["events"]:
        calls.extend(event.get("tool_calls", []))
    return calls


def _unmatched_results_in(session):
    results = []
    for message in session["events"]:
        results.extend(message.get("tool_results", []))
    return results


def _plan_decisions_in(session):
    decisions = []
    for call in _tool_calls_in(session):
        decisions.extend(call.get("plan_decisions", []))
    return decisions


def _recorded_multiselect_interaction():
    matches = []
    for assistant, result in _recorded_tool_interactions(
        claude_protocol.ASK_USER_QUESTION
    ):
        call = _content_block(assistant, claude_protocol.TOOL_USE_BLOCK)
        questions = call["input"]["questions"]
        for question in questions:
            if question.get("multiSelect"):
                matches.append((assistant, result))
                break
    return _require_single_match(matches, "multiselect activity")


def _recorded_overlapping_options_interaction():
    matches = []
    for assistant, result in _recorded_tool_interactions(
        claude_protocol.ASK_USER_QUESTION
    ):
        call = _content_block(assistant, claude_protocol.TOOL_USE_BLOCK)
        questions = call["input"]["questions"]
        for question in questions:
            if _has_overlapping_labels(question["options"]):
                matches.append((assistant, result))
                break
    return _require_single_match(matches, "overlapping-option activity")


def _has_overlapping_labels(options):
    labels = []
    for option in options:
        labels.append(option["label"])
    for shorter in labels:
        for longer in labels:
            if longer != shorter and longer.startswith(shorter):
                return True
    return False


def _assert_answer_uses_distinct_recorded_options(response, call):
    assert response["outcome"] == "answered"
    labels = []
    association = response["question"]
    assert association is not None
    options_by_ref = {}
    for question in call["interaction_questions"]:
        if question["ref"] != association["question_ref"]:
            continue
        for option in question["options"]:
            options_by_ref[option["ref"]] = option
    for selection in response.get("selections", []):
        label = selection["label"]
        assert isinstance(label, str) and label
        labels.append(label)
        assert options_by_ref[selection["option_ref"]]["label"] == label
    assert labels
    assert len(labels) == len(set(labels))


def test_native_skill_and_command_invocations_are_retained():
    entries = _recorded_session_entries()
    session = eclog.interpret_source(ClaudeSource(Path("session.jsonl"), entries))
    assert session["skills_used"] == ["capture-leaf"]
    calls = []
    for call in _tool_calls_in(session):
        if call["name"] == claude_protocol.SKILL_TOOL:
            calls.append(call)
    assert len(calls) >= 2
    skills = set()
    for call in calls:
        skills.add(call["skill"])
    assert skills == {"capture-leaf"}
    commands = []
    for event in session["events"]:
        if event.get("command") == "/capture-echo":
            commands.append(event)
    command = _require_single_match(commands, "capture-echo command")
    assert command["command_args"] == 'CAPTURE_ARGS hello, "café 雪" — two words!'
    command_events = []
    for event in build_events(entries):
        if "/capture-echo" in event["text"]:
            command_events.append(event)
    assert command_events


def test_native_multiselect_answer():
    entries = _recorded_multiselect_interaction()

    session = eclog.interpret_source(ClaudeSource(Path("session.jsonl"), entries))

    call = _single_tool_call_in(session["events"])
    answer = _response_to_question(call, multi_select=True)
    assert len(answer["selections"]) >= 2
    _assert_answer_uses_distinct_recorded_options(answer, call)


def test_permission_denial_bare():
    _, result = _recorded_tool_interaction(
        claude_protocol.WRITE_TOOL, "denied-feedback.txt"
    )
    actual_parse_permission_denial = parse_permission_denial(
        _content_block(result, "tool_result")["content"]
    )
    assert actual_parse_permission_denial == ""


def test_native_settings_denial_is_recorded():
    entries = _recorded_tool_interaction(
        claude_protocol.BASH_TOOL, "CAPTURE_SETTINGS_REFUSAL"
    )
    _, result_entry = entries
    result = _content_block(result_entry, claude_protocol.TOOL_RESULT_BLOCK)["content"]
    assert isinstance(result, str)
    assert result.strip()
    actual_parse_permission_denial = parse_permission_denial(result)
    assert actual_parse_permission_denial == ""
    interpreted_session = eclog.interpret_source(
        ClaudeSource(Path("session.jsonl"), entries)
    )
    denials = []
    for call in _tool_calls_in(interpreted_session):
        denials.extend(call.get("permission_refusals", []))
    denial = _require_single_match(denials, "settings denial")
    assert denial["action"]["tool_name"] == "Bash"
    assert denial["source"] == "settings"


def test_custom_answer_matches_no_option():
    entries = _recorded_tool_interaction(
        claude_protocol.ASK_USER_QUESTION, result_contains="CAPTURE_CUSTOM_PLAIN_MOSS"
    )

    session = eclog.interpret_source(ClaudeSource(Path("session.jsonl"), entries))

    call = _single_tool_call_in(session["events"])
    response = _response_to_question(call, multi_select=False)
    assert response["free_text"] == "CAPTURE_CUSTOM_PLAIN_MOSS"
    assert not response.get("selections")


def test_multi_question_anchors_on_its_own_text():
    entries = _recorded_multiselect_interaction()
    interpreted_session = eclog.interpret_source(
        ClaudeSource(Path("session.jsonl"), entries)
    )
    call = _single_tool_call_in(interpreted_session["events"])
    answer = _response_to_question(call, multi_select=True)
    _assert_answer_uses_distinct_recorded_options(answer, call)
    assert len(answer["selections"]) >= 2
    answer = _response_to_question(call, multi_select=False)
    _assert_answer_uses_distinct_recorded_options(answer, call)
    assert len(answer["selections"]) == 1
    assert "free_text" not in answer


def test_task_notification_is_stripped_to_empty():
    entry = _recorded_user_message("<task-notification>")
    actual_human_text = _human_text(entry)
    actual_clean_user_text = clean_user_text(actual_human_text)
    assert actual_clean_user_text == ""


def test_bash_output_is_stripped_to_empty():
    entry = _recorded_user_message("<bash-stdout>CAPTURE_USER_STDOUT_1")
    actual_clean_user_text = clean_user_text(entry["message"]["content"])
    assert actual_clean_user_text == ""


def test_bash_input_survives_cleaning_for_detection():
    entry = _recorded_user_message("<bash-input>printf")
    text = entry["message"]["content"]
    actual_clean_user_text = clean_user_text(text)
    assert actual_clean_user_text == text
    assert "CAPTURE_USER_STDOUT_1" in text


def test_strict_candidate_dirs_skip_parents():
    cwd = Path("/Users/me/work/app/sub")
    loose = list(_candidate_project_dirs(cwd, strict=False))
    strict = list(_candidate_project_dirs(cwd, strict=True))
    assert len(strict) == 1  # only the cwd itself
    assert len(loose) > 1  # cwd + parents
    assert strict[0] == loose[0]


def test_encode_project_path_does_not_collapse_runs():
    # Windows paths have adjacent non-alnums (":" then "\") -- one dash each.
    actual_encode_project_path = encode_project_path(
        PureWindowsPath("C:\\Users\\me\\proj")
    )
    assert actual_encode_project_path == "C--Users-me-proj"
    actual_encode_project_path = encode_project_path(PurePosixPath("/Users/me/proj"))
    assert actual_encode_project_path == "-Users-me-proj"


def test_build_events_inlines_pasted_images_as_base64():
    entries = []
    for marker in (
        "CAPTURE_IMAGES_SAME:",
        "CAPTURE_IMAGES_LATER_GIF:",
        "CAPTURE_IMAGES_LATER_WEBP:",
    ):
        entries.append(_recorded_user_message(marker))
    recorded_image_data = []
    for entry in entries:
        for block in entry["message"]["content"]:
            if block["type"] == claude_protocol.IMAGE_BLOCK:
                recorded_image_data.append(block["source"]["data"])

    events = build_events(entries)

    roles = []
    image_counts = []
    marked_image_events = []
    extracted_image_data = []
    extracted_media_types = []
    for event in events:
        roles.append(event["role"])
        image_counts.append(len(event["images"]))
        if "CAPTURE_IMAGES_SAME" in event["text"]:
            marked_image_events.append(event)
        for image in event["images"]:
            extracted_image_data.append(image["data"])
            extracted_media_types.append(image["media_type"])
    assert roles == ["user"] * 3
    assert image_counts == [2, 1, 1]
    assert marked_image_events
    # Claude 2.1.312/314 converts all four source formats to PNG on attachment.
    assert extracted_media_types == ["image/png"] * 4
    assert extracted_image_data == recorded_image_data


def test_build_events_attaches_tool_results_to_their_call():
    events = build_events(
        _recorded_tool_interaction(claude_protocol.READ_TOOL, "multibyte.txt"),
        tool_result_max_bytes=100,
    )
    call = _single_tool_call_in(events)
    assert call["name"] == "Read"
    assert call["input"]["file_path"] == "/tmp/CAPTURE_ROOT/workspace/multibyte.txt"
    assert call["result_bytes"] > 10_000
    assert call["result_truncated"] is True
    assert 0 < len(call["result"].encode()) <= 100


def test_large_native_result_reaches_envelope_size_warning():
    events = build_events(
        _recorded_tool_interaction(claude_protocol.READ_TOOL, "multibyte.txt"),
        tool_result_max_bytes=100_000,
    )
    # Repeated native Read results simulate a long session without checking in 8 MB.
    copies = SIZE_WARN_BYTES // len(json.dumps(events, ensure_ascii=False).encode()) + 2
    envelope = build_envelope(
        Session(harness="claude", session_id="large-session", events=events * copies)
    )
    out = Path(tempfile.mkdtemp()) / "large.json"
    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
        size = write_envelope(out, envelope)
    assert size > SIZE_WARN_BYTES
    assert "session-log uploads are capped" in stderr.getvalue()


def test_build_events_preserves_long_file_paths():
    entries = _recorded_session_entries()
    events = build_events(entries)
    paths = set()
    for event in events:
        for call in event.get("tool_calls", []):
            path = call["input"].get("file_path", "")
            if "nested space 雪" in path:
                paths.add(path)

    long_path = _require_single_match(paths, "generated long path")
    assert len(long_path) > 100
    assert long_path.endswith("greeting.py")


def test_build_events_preserves_large_write_inputs():
    entries = _recorded_session_entries()
    recorded_write_inputs = []
    for entry in entries:
        if entry.get("type") != claude_protocol.ASSISTANT_RECORD:
            continue
        for block in _content_blocks(entry):
            if (
                not isinstance(block, dict)
                or block.get("type") != claude_protocol.TOOL_USE_BLOCK
                or block.get("name") != claude_protocol.WRITE_TOOL
            ):
                continue
            file_path = Path(block["input"]["file_path"])
            if file_path.name == "large-input.txt":
                recorded_write_inputs.append(block["input"])

    assert recorded_write_inputs, "Capture must include a Write to large-input.txt"
    for payload in recorded_write_inputs:
        assert len(payload["content"].encode()) > 4096

    events = build_events(entries)

    extracted_write_inputs = []
    for event in events:
        for call in event.get("tool_calls", []):
            if (
                call["name"] == claude_protocol.WRITE_TOOL
                and Path(call["input"]["file_path"]).name == "large-input.txt"
            ):
                extracted_write_inputs.append(call["input"])

    assert extracted_write_inputs == recorded_write_inputs


def test_messages_skip_sidechain_and_retain_meta():
    entries = _recorded_session_entries()
    sidechain = load_entries(SUBAGENT)
    meta_entries = []
    for entry in entries:
        if entry.get("isMeta"):
            meta_entries.append(entry)
    assert meta_entries
    sidechain_entries = []
    for entry in sidechain:
        if entry.get("isSidechain"):
            sidechain_entries.append(entry)
    assert sidechain_entries
    expected = build_events(entries)
    session = eclog.interpret_source(
        ClaudeSource(Path("session.jsonl"), entries + sidechain)
    )
    actual_build_events = build_events(entries + sidechain)
    assert actual_build_events == expected
    expected_meta_ids = []
    actual_meta_ids = []
    for entry in meta_entries:
        expected_meta_ids.append(entry["uuid"])
    for message in session["events"]:
        if message["id"] in expected_meta_ids:
            actual_meta_ids.append(message["id"])
    assert actual_meta_ids == expected_meta_ids
    assert session["subagent_count"] == 1
    assert "interactions" not in session


def test_build_events_surfaces_slash_command_invocations():
    entry = _recorded_user_message("<command-name>/capture-echo</command-name>")
    event = _require_single_match(build_events([entry]), "command event")
    assert event["text"] == entry["message"]["content"]
    assert event["role"] == "user"
    interpreted = eclog.interpret_source(ClaudeSource(TRANSCRIPT, [entry]))
    message = _require_single_match(interpreted["events"], "command message")
    assert message["command"] == "/capture-echo"
    assert message["command_args"] == 'CAPTURE_ARGS hello, "café 雪" — two words!'


def test_plan_approved_is_captured():
    entries = _recorded_tool_interaction(
        claude_protocol.EXIT_PLAN_MODE, result_contains="## Approved Plan:\n"
    )
    interpreted_session = eclog.interpret_source(
        ClaudeSource(Path("session.jsonl"), entries)
    )
    (plan,) = _plan_decisions_in(interpreted_session)
    (call,) = _tool_calls_in(interpreted_session)
    assert call["plan_versions"][0]["text"] == call["input"]["plan"]
    assert plan["outcome"] == "approved"
    assert len(call["plan_versions"]) == 1


def test_plan_approved_with_edit_keeps_final_version():
    entries = _recorded_tool_interaction(
        claude_protocol.EXIT_PLAN_MODE, result_contains="(edited by user):"
    )
    interpreted_session = eclog.interpret_source(
        ClaudeSource(Path("session.jsonl"), entries)
    )
    (plan,) = _plan_decisions_in(interpreted_session)
    assert plan["outcome"] == "approved"
    (call,) = _tool_calls_in(interpreted_session)
    assert (
        call["plan_versions"][0]["text"]
        == (ROOT / "assets/plan-before-editor.md").read_text()
    )
    assert (
        call["plan_versions"][1]["text"]
        == (ROOT / "assets/plan-after-editor.md").read_text()
    )


def test_plan_rejected_keeps_steering_message():
    entries = _recorded_tool_interaction(
        claude_protocol.EXIT_PLAN_MODE, result_contains="CAPTURE_PLAN_REJECT_1:"
    )
    interpreted_session = eclog.interpret_source(
        ClaudeSource(Path("session.jsonl"), entries)
    )
    (call,) = _tool_calls_in(interpreted_session)
    (plan,) = call["plan_decisions"]
    assert plan["outcome"] == "rejected"
    assert (
        plan["candidate_feedback"]
        == "CAPTURE_PLAN_REJECT_1: print CAPTURE_PLAN_REVISED instead of CAPTURE_PLAN_HELLO. Keep everything else tiny and local."
    )
    assert call["permission_refusals"][0]["source"] == "candidate"


def test_plan_rejection_reason_is_captured():
    # The wording the harness actually uses when a plan is sent back in plan mode.
    entries = _plan_result_variant(
        "The user doesn't want to proceed with this tool use. The tool use "
        "was rejected (eg. if it was a file edit, the new_string was NOT "
        "written to the file). STOP what you are doing and wait for the user "
        "to tell you how to proceed. The user provided the following reason "
        "for the rejection:\nwrong table, use line_items\nand keep it in one "
        "migration\n\nNote: The user's next message may contain a correction."
    )
    interpreted_session = eclog.interpret_source(
        ClaudeSource(Path("session.jsonl"), entries)
    )
    plans = _plan_decisions_in(interpreted_session)
    (plan,) = plans
    assert plan["outcome"] == "rejected"
    assert (
        plan["candidate_feedback"]
        == "wrong table, use line_items\nand keep it in one migration"
    )


def test_plan_without_result_is_marked_undecided():
    assistant, _ = _recorded_tool_interaction(
        claude_protocol.EXIT_PLAN_MODE, result_contains="## Approved Plan:\n"
    )
    interpreted_session = eclog.interpret_source(
        ClaudeSource(Path("session.jsonl"), [assistant])
    )
    assert _plan_decisions_in(interpreted_session) == []
    (call,) = _tool_calls_in(interpreted_session)
    assert call["plan_presentations"]
    assert call["plan_versions"]


# --------------------------------------------------------------------------- #
# Transcript discovery
# --------------------------------------------------------------------------- #


class _patched_projects_root:
    """Point eclog.PROJECTS_ROOT at a fresh empty temp dir for the duration."""

    def __enter__(self):
        self._original = eclog.PROJECTS_ROOT
        self.tmp = Path(tempfile.mkdtemp())
        eclog.PROJECTS_ROOT = self.tmp
        return self.tmp

    def __exit__(self, *exc_info):
        eclog.PROJECTS_ROOT = self._original


def test_resolve_project_dir_explicit_wins():
    actual_resolve_project_dir = resolve_project_dir("/some/explicit", Path("/cwd"))
    assert actual_resolve_project_dir == Path("/some/explicit")


def test_resolve_project_dir_matches_parent_unless_strict():
    with _patched_projects_root() as root:
        cwd = Path("/Users/me/work/app")
        parent_encoded = encode_project_path(Path("/Users/me/work"))
        (root / parent_encoded).mkdir(parents=True)
        actual_resolve_project_dir = resolve_project_dir(None, cwd, strict=False)
        assert actual_resolve_project_dir == root / parent_encoded
        actual_resolve_project_dir = resolve_project_dir(None, cwd, strict=True)
        assert actual_resolve_project_dir is None


def test_jsonl_files_missing_dir_is_empty():
    actual_jsonl_files = _jsonl_files(Path(tempfile.mkdtemp()) / "nope")
    assert actual_jsonl_files == []


def test_select_transcript_explicit_transcript_wins():
    f = Path(tempfile.mkdtemp()) / "a.jsonl"
    f.write_bytes(TRANSCRIPT.read_bytes())
    actual_select_transcript = select_transcript(None, str(f), None)
    assert actual_select_transcript == f


def test_select_transcript_explicit_transcript_missing_raises():
    with pytest.raises(FileNotFoundError):
        select_transcript(None, "/no/such/file.jsonl", None)


def test_select_transcript_selector_as_file_path():
    f = Path(tempfile.mkdtemp()) / "sess.jsonl"
    f.write_bytes(TRANSCRIPT.read_bytes())
    actual_select_transcript = select_transcript(str(f), None, None)
    assert actual_select_transcript == f


def _copy_with_session_id(path, session_id):
    # Keep the native records, changing only the top-level recorded session ID.
    entries = _recorded_session_entries()
    for entry in entries:
        if "sessionId" in entry:
            entry["sessionId"] = session_id
    _write_jsonl(path, entries)


def test_select_transcript_by_recorded_session_id(tmp_path):
    transcript = tmp_path / "renamed.jsonl"
    _copy_with_session_id(transcript, "abc123")
    selected = select_transcript("abc123", None, str(tmp_path))
    assert selected == transcript


def test_select_transcript_prefix_match_unique_and_ambiguous(tmp_path):
    first = tmp_path / "first.jsonl"
    _copy_with_session_id(first, "sess-aaa")
    selected = select_transcript("sess-a", None, str(tmp_path))
    assert selected == first

    _copy_with_session_id(tmp_path / "second.jsonl", "sess-aab")
    with pytest.raises(FileNotFoundError, match="ambiguous"):
        select_transcript("sess-aa", None, str(tmp_path))


def test_select_transcript_selector_not_found_in_project_dir(tmp_path):
    # A populated project must not fall back to its newest transcript.
    shutil.copyfile(TRANSCRIPT, tmp_path / "newest.jsonl")
    with pytest.raises(FileNotFoundError) as exc_info:
        select_transcript("missing-id", None, str(tmp_path))

    assert "missing-id" in str(exc_info.value) and str(tmp_path) in str(exc_info.value)


def test_select_transcript_selector_not_found_without_project_dir():
    with _patched_projects_root():
        with pytest.raises(FileNotFoundError) as exc_info:
            select_transcript("some-id", None, None, strict=True)

        assert str(exc_info.value) == "no transcript matching 'some-id'"


def test_select_transcript_no_selector_picks_newest_in_project_dir():
    proj = Path(tempfile.mkdtemp())
    a, b = proj / "a.jsonl", proj / "b.jsonl"
    a.write_bytes(TRANSCRIPT.read_bytes())
    b.write_bytes(TRANSCRIPT.read_bytes())
    os.utime(a, (1, 1))
    os.utime(b, (1000, 1000))
    actual_select_transcript = select_transcript(None, None, str(proj))
    assert actual_select_transcript == b


def test_select_transcript_strict_no_match_mentions_strict():
    with _patched_projects_root():
        with pytest.raises(FileNotFoundError) as exc_info:
            select_transcript(None, None, None, strict=True)

        assert "--strict" in str(exc_info.value)


def test_select_transcript_no_match_anywhere_raises():
    with _patched_projects_root():
        with pytest.raises(FileNotFoundError) as exc_info:
            select_transcript(None, None, None, strict=False)

        assert "could not locate" in str(exc_info.value)


def test_select_transcript_global_fallback_picks_newest_anywhere():
    with _patched_projects_root() as root:
        unrelated = root / "-some-other-project"
        unrelated.mkdir()
        f = unrelated / "s1.jsonl"
        f.write_bytes(TRANSCRIPT.read_bytes())
        actual_select_transcript = select_transcript(None, None, None, strict=False)
        assert actual_select_transcript == f


# --------------------------------------------------------------------------- #
# Content-block / descriptor / result-note helpers
# --------------------------------------------------------------------------- #


def test_content_blocks_handles_none_and_non_list_content():
    entry = _recorded_user_message("Remember **CAPTURE_CONTEXT_CEDAR_73**")
    actual_content_blocks = _content_blocks(entry)
    assert actual_content_blocks == [
        {"type": "text", "text": entry["message"]["content"]}
    ]
    # Malformed content containers cannot be obtained from the native capture.
    entry["message"]["content"] = None
    actual_content_blocks = _content_blocks(entry)
    assert actual_content_blocks == []
    entry["message"]["content"] = {"weird": True}
    actual_content_blocks = _content_blocks(entry)
    assert actual_content_blocks == []


def test_tool_descriptor_shell_search_and_fallback():
    assistant, _ = _recorded_tool_interaction(
        claude_protocol.BASH_TOOL, "CAPTURE_BASH_OK"
    )
    call = _content_block(assistant, claude_protocol.TOOL_USE_BLOCK)
    descriptor = tool_descriptor(call["name"], call["input"])
    assert isinstance(descriptor, str)
    assert descriptor.strip()
    # The current CLI has no Grep tool; retain its legacy descriptor boundary.
    actual_tool_descriptor = tool_descriptor("Grep", {"pattern": "foo"})
    assert actual_tool_descriptor == "foo"
    actual_tool_descriptor = tool_descriptor("X", {"odd": 1})
    assert actual_tool_descriptor == '{"odd": 1}'
    actual_tool_descriptor = tool_descriptor("X", None)
    assert actual_tool_descriptor == ""
    actual_tool_descriptor = tool_descriptor("X", "raw string input")
    assert actual_tool_descriptor == "raw string input"


def test_extract_file_path_variants():
    assistant, _ = _recorded_tool_interaction(
        claude_protocol.READ_TOOL, "multibyte.txt"
    )
    read_input = _content_block(assistant, claude_protocol.TOOL_USE_BLOCK)["input"]
    actual_extract_file_path = extract_file_path(read_input)
    assert actual_extract_file_path == "/tmp/CAPTURE_ROOT/workspace/multibyte.txt"
    assistant, _ = _recorded_tool_interaction(
        claude_protocol.NOTEBOOK_EDIT_TOOL, "sample.ipynb"
    )
    notebook_input = _content_block(assistant, claude_protocol.TOOL_USE_BLOCK)["input"]
    actual_extract_file_path = extract_file_path(notebook_input)
    assert actual_extract_file_path == "/tmp/CAPTURE_ROOT/workspace/sample.ipynb"
    actual_extract_file_path = extract_file_path({})
    assert actual_extract_file_path is None
    actual_extract_file_path = extract_file_path("nope")
    assert actual_extract_file_path is None


def test_is_tool_result_entry_true_for_bare_tool_use_result():
    _, entry = _recorded_tool_interaction(claude_protocol.BASH_TOOL, "CAPTURE_BASH_OK")
    # Legacy top-level result shape: remove only the result block carrying its ID.
    entry["message"]["content"] = []
    assert entry["toolUseResult"]
    actual_is_tool_result_entry = _is_tool_result_entry(entry)
    assert actual_is_tool_result_entry is True


def test_human_text_includes_plain_string_blocks():
    entry = _recorded_user_message("Remember **CAPTURE_CONTEXT_CEDAR_73**")
    text = entry["message"]["content"]
    # Legacy content accepts raw strings inside a block list.
    entry["message"]["content"] = [text]
    actual_human_text = _human_text(entry)
    assert actual_human_text == text


def test_clean_user_text_empty_input_is_empty():
    actual_clean_user_text = clean_user_text("")
    assert actual_clean_user_text == ""
    actual_clean_user_text = clean_user_text(None)
    assert actual_clean_user_text == ""


def test_result_block_text_flattens_list_content():
    _, result = _recorded_tool_interaction(claude_protocol.AGENT_TOOL)
    block = _content_block(result, claude_protocol.TOOL_RESULT_BLOCK)
    recorded_texts = []
    for part in block["content"]:
        if part.get("type") == claude_protocol.TEXT_BLOCK:
            recorded_texts.append(part["text"])
    original = _require_single_match(recorded_texts, "result text")
    actual_result_block_text = _result_block_text(block)
    assert actual_result_block_text == original
    # Repeat the captured text block to exercise multi-block flattening.
    block["content"].append(
        deepcopy(_require_single_match(block["content"], "result content block"))
    )
    actual_result_block_text = _result_block_text(block)
    assert actual_result_block_text == original + " " + original


def test_native_session_keeps_commands_shell_subagents_and_files():
    entries = _recorded_session_entries() + load_entries(SUBAGENT)
    session = eclog.interpret_source(ClaudeSource(Path("session.jsonl"), entries))
    commands = []
    shell_commands = []
    for event in session["events"]:
        commands.append(event.get("command"))
        shell_commands.append(event.get("shell_command", ""))
    assert "/capture-echo" in commands
    marked_shell_commands = []
    for command in shell_commands:
        if "CAPTURE_USER_STDOUT_1" in command:
            marked_shell_commands.append(command)
    assert marked_shell_commands
    assert session["subagent_count"] > 0
    greeting_paths = []
    for path in session["files_changed"]:
        if path.endswith("greeting.py"):
            greeting_paths.append(path)
    assert greeting_paths


def test_native_session_keeps_multiselect_answers():
    entries = _recorded_session_entries() + load_entries(SUBAGENT)
    session = eclog.interpret_source(ClaudeSource(Path("session.jsonl"), entries))

    multiselect_calls = []
    for call in _tool_calls_in(session):
        if call["name"] != claude_protocol.ASK_USER_QUESTION:
            continue
        for question in call["input"]["questions"]:
            if question.get("multiSelect"):
                multiselect_calls.append(call)
                break
    question_call = _require_single_match(multiselect_calls, "multiselect call")
    answer = _response_to_question(question_call, multi_select=True)
    _assert_answer_uses_distinct_recorded_options(answer, question_call)
    assert len(answer["selections"]) >= 2


def test_native_session_keeps_user_write_denial():
    entries = _recorded_session_entries() + load_entries(SUBAGENT)
    session = eclog.interpret_source(ClaudeSource(Path("session.jsonl"), entries))

    write_calls = []
    for call in _tool_calls_in(session):
        if call["name"] == claude_protocol.WRITE_TOOL:
            write_calls.append(call)
    assert write_calls
    denied_writes = []
    for call in write_calls:
        if "denied-feedback.txt" in json.dumps(call["input"]):
            denied_writes.append(call)
    denied_write = _require_single_match(denied_writes, "denied Write call")
    (denial,) = denied_write["permission_refusals"]
    assert denial["source"] == "candidate"
    assert "candidate_feedback" not in denial


def test_build_events_retains_unmatched_results_and_skips_invalid_blocks():
    entries = [
        {"type": "summary"},
        {
            "type": claude_protocol.ASSISTANT_RECORD,
            "timestamp": None,
            "message": {"content": ["x"]},  # non-dict block, skipped
        },
        {
            "type": claude_protocol.USER_RECORD,
            "timestamp": None,
            "message": {
                "content": [
                    {
                        "type": claude_protocol.TOOL_RESULT_BLOCK,
                        "tool_use_id": "unknown",
                        "content": "x",
                    }
                ]
            },
        },
    ]
    # The malformed assistant block is skipped; the orphan result remains in
    # its own native user-message event without inventing a tool-call owner.
    (message,) = build_events(entries)
    assert message["i"] == 0 and message["role"] == "user"
    (result,) = message["tool_results"]
    assert result["tool_call_id"] == "unknown"
    assert result["content"] == "x"


# --------------------------------------------------------------------------- #
# Envelope metadata
# --------------------------------------------------------------------------- #


def test_first_timestamp_comes_from_the_native_capture():
    entries = _recorded_session_entries()
    recorded_timestamps = set()
    for entry in entries:
        if entry.get("timestamp"):
            recorded_timestamps.add(entry["timestamp"])
    actual_first_timestamp = first_timestamp(entries)
    assert actual_first_timestamp in recorded_timestamps


def test_session_cwd_comes_from_the_native_capture():
    actual_session_cwd = session_cwd(_recorded_session_entries())
    assert actual_session_cwd == "/tmp/CAPTURE_ROOT/workspace"


def test_synthetic_model_is_excluded():
    assistant, _ = _recorded_tool_interaction(
        claude_protocol.READ_TOOL, "multibyte.txt"
    )
    assistant["message"]["model"] = "<synthetic>"
    actual_session_models = session_models([assistant])
    assert actual_session_models == []


def test_metadata_is_absent_when_not_recorded():
    assistant, _ = _recorded_tool_interaction(
        claude_protocol.READ_TOOL, "multibyte.txt"
    )
    assistant["timestamp"] = None
    assistant.pop("cwd", None)
    actual_first_timestamp = first_timestamp([assistant])
    assert actual_first_timestamp is None
    actual_session_cwd = session_cwd([assistant])
    assert actual_session_cwd is None


def test_first_timestamp_skips_messages_without_timestamps():
    assistant, _ = _recorded_tool_interaction(
        claude_protocol.READ_TOOL, "multibyte.txt"
    )
    assistant["timestamp"] = None
    user = _recorded_user_message("Remember **CAPTURE_CONTEXT_CEDAR_73**")
    user["timestamp"] = "2026-01-01T00:00:00Z"
    actual_first_timestamp = first_timestamp([assistant, user])
    assert actual_first_timestamp == "2026-01-01T00:00:00Z"


def test_empty_session_has_no_metadata():
    actual_first_timestamp = first_timestamp([])
    assert actual_first_timestamp is None
    actual_session_models = session_models([])
    assert actual_session_models == []
    actual_session_cwd = session_cwd([])
    assert actual_session_cwd is None


def test_native_resumed_session_lists_multiple_models():
    entries = _recorded_session_entries()
    models = session_models(entries)
    assert len(models) >= 2
    for model in models:
        assert isinstance(model, str)
        assert model
    assert len(models) == len(set(models))
    session = eclog.interpret_source(ClaudeSource(Path("session.jsonl"), entries))
    model_commands = []
    for event in session["events"]:
        if event.get("command") == "/model":
            model_commands.append(event)
    assert model_commands


def test_native_continued_conversation_keeps_follow_up_in_one_session():
    entries = _recorded_session_entries()
    session_ids = set()
    for entry in entries:
        if "sessionId" in entry:
            session_ids.add(entry["sessionId"])
    assert session_ids == {TRANSCRIPT.stem}
    events = eclog.interpret_source(ClaudeSource(Path("session.jsonl"), entries))[
        "events"
    ]
    recall_prompts = []
    for event in events:
        if event["role"] != "user":
            continue
        if event["text"].startswith("CAPTURE_POST_COMPACT_RECALL:"):
            recall_prompts.append(event)
    prompt = _require_single_match(recall_prompts, "post-compaction recall prompt")
    assert "without tools" in prompt["text"]
    _assert_assistant_text(events[prompt["i"] + 1 :])


def test_build_raw_envelope_preserves_metadata_and_events():
    entries = _recorded_session_entries()
    envelope = build_raw_envelope(Path("/wherever/sess123.jsonl"), entries)
    assert envelope["session_id"] == _capture_session_id()
    assert envelope["cwd"] == "/tmp/CAPTURE_ROOT/workspace"
    actual_first_timestamp = first_timestamp(entries)
    assert envelope["started_at"] == actual_first_timestamp
    actual_session_models = session_models(entries)
    assert envelope["models"] == actual_session_models
    actual_build_events = build_events(entries)
    assert envelope["events"] == actual_build_events


def test_build_raw_envelope_retains_empty_session():
    envelope = build_raw_envelope(Path("/x/sess.jsonl"), [])
    assert envelope["events"] == []
    assert envelope["session_id"] == "sess"


# --------------------------------------------------------------------------- #
# Subagents
# --------------------------------------------------------------------------- #


def test_summarize_subagent_reads_meta_task_and_result(tmp_path):
    sub = tmp_path / SUBAGENT.name
    shutil.copyfile(SUBAGENT, sub)
    shutil.copyfile(SUBAGENT.with_suffix(".meta.json"), sub.with_suffix(".meta.json"))
    summary = summarize_subagent(sub)
    metadata = json.loads(SUBAGENT.with_suffix(".meta.json").read_text())
    assert summary["agent_type"] == metadata["agentType"]
    assert isinstance(summary["task"], str)
    assert summary["task"].strip()
    assert summary["tool_count"] >= 1
    assert summary["tool_names"]
    for name in summary["tool_names"]:
        assert isinstance(name, str)
        assert name
    assert len(summary["tool_names"]) <= summary["tool_count"]
    assert isinstance(summary["result"], str)
    assert summary["result"].strip()


def test_summarize_subagent_returns_none_for_empty_file():
    sub = Path(tempfile.mkdtemp()) / "agent-2.jsonl"
    sub.write_text("")
    actual_summarize_subagent = summarize_subagent(sub)
    assert actual_summarize_subagent is None


def test_load_subagents_missing_dir_is_empty():
    transcript = Path(tempfile.mkdtemp()) / "sess.jsonl"
    actual_load_subagents = load_subagents(transcript)
    assert actual_load_subagents == []


def test_load_subagents_reads_sidecar_dir(tmp_path):
    transcript = tmp_path / TRANSCRIPT.name
    shutil.copyfile(TRANSCRIPT, transcript)
    shutil.copytree(SUBAGENT.parent, tmp_path / TRANSCRIPT.stem / "subagents")
    actual_load_subagents = load_subagents(transcript)
    actual_load_subagents = load_subagents(TRANSCRIPT)
    assert actual_load_subagents == actual_load_subagents
    actual_load_subagents = load_subagents(transcript)
    assert len(actual_load_subagents) == 1


def test_native_subagent_summary_retains_evidence_without_guessing_ownership():
    subs = load_subagents(TRANSCRIPT)
    assert len(subs) == 1
    assert isinstance(subs[0]["task"], str)
    assert subs[0]["task"].strip()
    assert isinstance(subs[0]["result"], str)
    assert subs[0]["result"].strip()
    meta = json.loads(SUBAGENT.with_suffix(".meta.json").read_text())
    assert subs[0]["agent_type"] == meta["agentType"]
    task_entries = []
    for entry in load_entries(SUBAGENT):
        if entry.get("type") != claude_protocol.USER_RECORD:
            continue
        if _is_tool_result_entry(entry):
            continue
        if _human_text(entry).strip():
            task_entries.append(entry)
    task = _require_single_match(task_entries, "read-only child task")
    assert subs[0]["start_ts"] == task["timestamp"]
    entries = _recorded_session_entries()
    session = eclog.interpret_source(ClaudeSource(Path("session.jsonl"), entries))
    assert "interactions" not in session
    for event in session["events"]:
        assert "subagents" not in event


# --------------------------------------------------------------------------- #
# extract_transcript / CLI
# --------------------------------------------------------------------------- #


def _write_jsonl(path, entries):
    lines = []
    for entry in entries:
        lines.append(json.dumps(entry))
    path.write_text("\n".join(lines) + "\n")


def test_extract_transcript_exports_empty_file(tmp_path):
    transcript = tmp_path / "empty.jsonl"
    transcript.write_text("")
    envelope = extract_transcript(transcript)
    assert envelope["events"] == []


def test_extract_transcript_preserves_native_conversation():
    envelope = extract_transcript(TRANSCRIPT)
    assert envelope["session_id"] == TRANSCRIPT.stem
    context_prompts = []
    for event in envelope["events"]:
        if event["role"] != "user":
            continue
        if event["text"].startswith("Remember **CAPTURE_CONTEXT_CEDAR_73**"):
            context_prompts.append(event)
    prompt = _require_single_match(context_prompts, "context prompt")
    assert "café 雪" in prompt["text"]
    _assert_assistant_text(envelope["events"])


def test_main_writes_json_to_explicit_output():
    transcript = Path(tempfile.mkdtemp()) / "s1.jsonl"
    _write_jsonl(transcript, _recorded_session_entries())
    out_dir = Path(tempfile.mkdtemp())
    out_file = out_dir / "log.json"
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        rc = main(["--transcript", str(transcript), "--output", str(out_file)])
    assert rc == 0
    assert out_file.exists()
    actual_build_events = build_events(_recorded_session_entries())
    assert json.loads(out_file.read_text())["events"] == actual_build_events
    assert list(out_dir.iterdir()) == [out_file]


def test_main_stdout_outputs_json():
    transcript = Path(tempfile.mkdtemp()) / "s1.jsonl"
    _write_jsonl(transcript, _recorded_session_entries())
    out = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = main(["--transcript", str(transcript), "--output", "-"])
    assert rc == 0
    actual_build_events = build_events(_recorded_session_entries())
    assert json.loads(out.getvalue())["events"] == actual_build_events


def test_main_errors_for_missing_transcript():
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        rc = main(["--transcript", "/no/such/file.jsonl"])
    assert rc == 1
    assert "error:" in err.getvalue()


def test_main_default_mode_errors_when_no_project_dir_found():
    with _patched_projects_root():
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = main([])
        assert rc == 1
        assert "no project transcript dir found" in err.getvalue()


def test_main_all_mode_extracts_every_session():
    proj = Path(tempfile.mkdtemp())
    _write_jsonl(proj / "a.jsonl", _recorded_session_entries())
    _write_jsonl(proj / "b.jsonl", _recorded_session_entries())
    out_dir = Path(tempfile.mkdtemp())
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        rc = main(["--all", "--project-dir", str(proj), "--output", str(out_dir)])
    assert rc == 0
    assert (out_dir / "claude_session_log_raw_a.json").exists()
    assert (out_dir / "claude_session_log_raw_b.json").exists()
    assert "done: 2/2" in err.getvalue()


def test_extract_all_reports_error_for_missing_project_dir():
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        rc = _extract_all(Path("/no/such/project/dir"), None)
    assert rc == 1
    assert "no project transcript dir found" in err.getvalue()


def test_extract_all_reports_error_for_no_jsonl_files():
    proj = Path(tempfile.mkdtemp())
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        rc = _extract_all(proj, None)
    assert rc == 1
    assert "no .jsonl transcripts" in err.getvalue()


def test_extract_all_skips_unparseable_file():
    proj = Path(tempfile.mkdtemp())
    (proj / "bad.jsonl").write_text("invalid")
    out_dir = Path(tempfile.mkdtemp())
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        rc = _extract_all(proj, str(out_dir))
    assert rc == 1
    assert "skip bad" in err.getvalue()


def test_retired_turn_interfaces_are_absent():
    assert not hasattr(eclog, "Turn")
    assert not hasattr(eclog, "build_turns")


@pytest.mark.parametrize("explicit_root", [False, True])
def test_cli_stdout_preserves_markdown_and_images(
    tmp_path, monkeypatch, capsys, explicit_root
):
    image_entry = _recorded_user_message("CAPTURE_IMAGES_SAME:")
    text_block = _content_block(image_entry, "text")
    text_block["text"] = "# Keep **recorded Markdown**"
    entries = [image_entry]
    transcript = tmp_path / "session.jsonl"
    _write_jsonl(transcript, entries)
    output = tmp_path / "output"
    output.mkdir()
    if not explicit_root:
        monkeypatch.chdir(output)
    project_root = output if explicit_root else None
    args = [
        "--transcript",
        str(transcript),
        "--tool-result-bytes",
        "3",
        "--output",
        "-",
    ]

    exit_code = main(args, project_root=project_root)

    assert exit_code == 0
    captured = capsys.readouterr()
    envelope = json.loads(captured.out)
    assert list(output.iterdir()) == []
    assert "using transcript:" in captured.err
    assert envelope["schema"] == "raw-session-log/1"
    assert envelope["session_id"] == _capture_session_id()
    actual_build_events = build_events(entries, 3)
    assert envelope["events"] == actual_build_events
    user = _require_single_match(envelope["events"], "image message")
    assert user["text"] == "# Keep **recorded Markdown**"
    assert len(user["images"]) == 2


@pytest.mark.parametrize("explicit_root", [False, True])
@pytest.mark.parametrize("destination", ["explicit", "default"])
def test_cli_file_preserves_markdown_and_images(
    destination, tmp_path, monkeypatch, capsys, explicit_root
):
    image_entry = _recorded_user_message("CAPTURE_IMAGES_SAME:")
    text_block = _content_block(image_entry, "text")
    text_block["text"] = "# Keep **recorded Markdown**"
    entries = [image_entry]
    transcript = tmp_path / "session.jsonl"
    _write_jsonl(transcript, entries)
    output = tmp_path / "output"
    output.mkdir()
    if not explicit_root:
        monkeypatch.chdir(output)
    project_root = output if explicit_root else None
    args = ["--transcript", str(transcript), "--tool-result-bytes", "3"]
    filename = "claude_session_log_raw_session.json"
    if destination == "explicit":
        filename = "custom.json"
        args += ["--output", str(output / filename)]

    exit_code = main(args, project_root=project_root)

    assert exit_code == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "wrote" in captured.err
    assert "using transcript:" in captured.err
    output_names = []
    for path in output.iterdir():
        output_names.append(path.name)
    assert output_names == [filename]
    envelope = json.loads((output / filename).read_text())
    assert envelope["schema"] == "raw-session-log/1"
    assert envelope["session_id"] == _capture_session_id()
    actual_build_events = build_events(entries, 3)
    assert envelope["events"] == actual_build_events
    user = _require_single_match(envelope["events"], "image message")
    assert user["text"] == "# Keep **recorded Markdown**"
    assert len(user["images"]) == 2


@pytest.mark.parametrize("explicit_root", [False, True])
@pytest.mark.parametrize("all_flag", [[], ["--all"]])
def test_cli_batch_emits_only_json(
    all_flag, tmp_path, monkeypatch, capsys, explicit_root
):
    project = tmp_path / "project"
    project.mkdir()
    for name in ["a", "b"]:
        shutil.copyfile(TRANSCRIPT, project / f"{name}.jsonl")
    output = tmp_path / "output"
    if not explicit_root:
        output.mkdir()
        monkeypatch.chdir(output)
    exit_status = main(
        all_flag + ["--project-dir", str(project)],
        project_root=output if explicit_root else None,
    )
    assert exit_status == 0
    output_names = []
    for path in output.iterdir():
        output_names.append(path.name)
    assert sorted(output_names) == [
        "claude_session_log_raw_a.json",
        "claude_session_log_raw_b.json",
    ]
    for path in output.iterdir():
        actual_build_events = build_events(_recorded_session_entries())
        assert json.loads(path.read_text())["events"] == actual_build_events
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "done: 2/2" in captured.err


@pytest.mark.parametrize(
    "flag", [["--raw"], ["--no-raw"], ["--raw-tool-result-bytes", "3"]]
)
def test_cli_rejects_retired_output_flags(flag, tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        main([*flag, "--project-dir", str(tmp_path), "--output", str(tmp_path / "out")])
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert "unrecognized arguments" in captured.err


@pytest.mark.parametrize("limit", [0, 17, -1])
@pytest.mark.parametrize("transcript", [TRANSCRIPT])
def test_cli_preserves_native_payloads(transcript, limit, capsys):
    exit_status = main(
        [
            "--transcript",
            str(transcript),
            "--output",
            "-",
            "--tool-result-bytes",
            str(limit),
        ]
    )
    captured = capsys.readouterr()
    assert exit_status == 0
    envelope = json.loads(captured.out)
    entries = load_entries(transcript)
    expected_events = build_events(entries, limit)
    assert envelope["events"] == expected_events
    assert envelope["schema"] == "raw-session-log/1"
    assert envelope["session_id"] == _capture_session_id()
    actual_session_cwd = session_cwd(entries)
    assert envelope["cwd"] == actual_session_cwd
    actual_first_timestamp = first_timestamp(entries)
    assert envelope["started_at"] == actual_first_timestamp
    actual_session_models = session_models(entries)
    assert envelope["models"] == actual_session_models
    assert envelope["truncation"] == {"tool_result_max_bytes": limit}
    question_calls = []
    for call in _tool_calls_in(envelope):
        if "interaction_questions" in call:
            question_calls.append(call)
    assert question_calls
    assert "interaction_questions" not in envelope
    assert "question_responses" not in envelope
    assert "unmatched_tool_results" not in envelope


@pytest.mark.parametrize("selector", ["path", "prefix", "newest"])
def test_cli_single_selection_paths(selector, tmp_path, capsys):
    transcript = tmp_path / "session-123.jsonl"
    _write_jsonl(transcript, _recorded_session_entries())
    args = ["--project-dir", str(tmp_path), "--output", "-"]
    if selector != "newest":
        args += [str(transcript) if selector == "path" else TRANSCRIPT.stem[:8]]
    exit_status = main(args)
    captured = capsys.readouterr()
    assert exit_status == 0
    assert json.loads(captured.out)["session_id"] == _capture_session_id()


@pytest.mark.parametrize("mode", ["single", "batch"])
def test_cli_reports_write_errors(mode, tmp_path, capsys):
    transcript = tmp_path / "session.jsonl"
    _write_jsonl(transcript, _recorded_session_entries())
    output = tmp_path / "output"
    output.mkdir()
    if mode == "single":
        args = ["--transcript", str(transcript), "--output", str(output)]
    else:
        (output / "claude_session_log_raw_session.json").mkdir()
        args = ["--all", "--project-dir", str(tmp_path), "--output", str(output)]
    exit_status = main(args)
    captured = capsys.readouterr()
    assert exit_status == 1
    assert captured.out == ""
    assert "error" in captured.err


@pytest.mark.parametrize("destination", ["stdout", "file"])
@pytest.mark.parametrize("contents", ["[]\n", "not JSON\n", "null\n42\nbad\n"])
def test_cli_reports_parse_errors(destination, contents, tmp_path, capsys):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(contents)
    target = tmp_path / "result.json"
    output = "-" if destination == "stdout" else str(target)

    status = main(["--transcript", str(transcript), "--output", output])

    captured = capsys.readouterr()
    assert status == 1
    assert not target.exists()
    assert captured.out == ""
    assert str(transcript) in captured.err
    assert "no parseable JSON entries" in captured.err
    assert "skipping" in captured.err


def test_cli_retains_malformed_line_diagnostics(tmp_path, capsys):
    transcript = tmp_path / "truncated.jsonl"
    entries = _recorded_overlapping_options_interaction()
    _write_jsonl(transcript, entries)
    # Append a deliberately interrupted record to a captured call/result pair.
    with transcript.open("a") as stream:
        stream.write('{"partial')
    exit_status = main(["--transcript", str(transcript), "--output", "-"])
    captured = capsys.readouterr()
    assert exit_status == 0
    actual_build_events = build_events(entries)
    assert json.loads(captured.out)["events"] == actual_build_events
    assert "skipping malformed JSON on line 3" in captured.err


def test_cli_file_keeps_size_warning(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(session_log_envelope, "SIZE_WARN_BYTES", 1)
    output = tmp_path / "session.json"
    exit_status = main(["--transcript", str(TRANSCRIPT), "--output", str(output)])
    captured = capsys.readouterr()
    assert exit_status == 0
    assert captured.out == ""
    assert "session-log uploads are capped" in captured.err
    assert json.loads(output.read_text())["harness"] == "claude"


def test_cli_stdout_keeps_size_warning(monkeypatch, capsys):
    monkeypatch.setattr(session_log_envelope, "SIZE_WARN_BYTES", 1)
    transcript = TRANSCRIPT
    exit_status = main(["--transcript", str(transcript), "--output", "-"])
    captured = capsys.readouterr()
    assert exit_status == 0
    assert json.loads(captured.out)["harness"] == "claude"
    assert "warning: stdout JSON is" in captured.err
    assert "consider a lower --tool-result-bytes" in captured.err
    assert "session-log uploads are capped" in captured.err


@pytest.mark.parametrize("destination", ["stdout", "explicit", "default", "batch"])
@pytest.mark.parametrize("contents", ["", " \n\t", '{"unrelated":true}\n', "native"])
def test_cli_exports_empty_sessions(destination, contents, tmp_path, capsys):
    transcript = tmp_path / "session.jsonl"
    entries = []
    if contents == "native":
        for entry in _recorded_session_entries():
            if entry.get("type") == claude_protocol.SYSTEM_RECORD:
                entries.append(entry)
        assert entries, "Capture must contain bookkeeping records"
        _write_jsonl(transcript, entries)
    else:
        transcript.write_text(contents)
    output = tmp_path / "output"
    args = ["--transcript", str(transcript)]
    target = output / "claude_session_log_raw_session.json"
    if destination == "stdout":
        args += ["--output", "-"]
    elif destination == "explicit":
        target = output / "custom.json"
        args += ["--output", str(target)]
    elif destination == "batch":
        args = ["--all", "--project-dir", str(tmp_path), "--output", str(output)]

    status = main(args, project_root=output)

    captured = capsys.readouterr()
    assert status == 0
    if destination == "stdout":
        envelope = json.loads(captured.out)
    else:
        assert captured.out == ""
        envelope = json.loads(target.read_text())
    assert envelope["schema"] == "raw-session-log/1"
    expected_id = _capture_session_id() if contents == "native" else "session"
    assert envelope["session_id"] == expected_id
    assert envelope["events"] == []
    actual_session_cwd = session_cwd(entries)
    assert envelope["cwd"] == actual_session_cwd
    actual_first_timestamp = first_timestamp(entries)
    assert envelope["started_at"] == actual_first_timestamp
    actual_session_models = session_models(entries)
    assert envelope["models"] == actual_session_models
    assert "warning:" not in captured.err


@pytest.mark.parametrize("batch", [False, True])
def test_cli_default_root_uses_current_directory(batch, tmp_path, monkeypatch):
    transcript = tmp_path / "session.jsonl"
    shutil.copyfile(TRANSCRIPT, transcript)
    for name in ["first", "second"]:
        output = tmp_path / name
        output.mkdir()
        monkeypatch.chdir(output)
        arguments = (
            ["--project-dir", str(tmp_path)]
            if batch
            else ["--transcript", str(transcript)]
        )
        exit_status = main(arguments)
        assert exit_status == 0
        assert json.loads((output / "claude_session_log_raw_session.json").read_text())[
            "events"
        ]


@pytest.mark.parametrize("limit", [0, 17, -1])
@pytest.mark.parametrize("fixture", ["complete", "images", "multibyte"])
def test_interpret_supplied_source_without_filesystem(limit, fixture, monkeypatch):
    entries = _recorded_session_entries()
    if fixture == "images":
        entries = [_recorded_user_message("CAPTURE_IMAGES_SAME:")]
    elif fixture == "multibyte":
        entries = _recorded_tool_interaction(claude_protocol.READ_TOOL, "multibyte.txt")
    source = ClaudeSource(Path("/unavailable/session.jsonl"), entries)
    expected_events = build_events(entries, limit)
    expected_id = _capture_session_id()

    def forbidden(*args, **kwargs):
        pytest.fail("interpretation accessed the filesystem")

    with monkeypatch.context() as guard:
        for method in ["open", "is_file", "is_dir", "iterdir", "stat", "read_text"]:
            guard.setattr(Path, method, forbidden)
        session = eclog.interpret_source(source)
        assert session["session_id"] == expected_id
        exported = session_log_envelope.export_session(session, limit)
        assert exported["events"] == expected_events
        actual_session_cwd = session_cwd(entries)
        assert session["cwd"] == actual_session_cwd
        actual_first_timestamp = first_timestamp(entries)
        assert session["started_at"] == actual_first_timestamp
        actual_session_models = session_models(entries)
        assert session["models"] == actual_session_models
        empty_session = eclog.interpret_source(ClaudeSource(source.path, []))
        assert empty_session["events"] == []


@pytest.mark.parametrize("with_metadata", [False, True])
def test_interpret_supplied_subagent_without_filesystem(with_metadata, monkeypatch):
    recorded = read_source(SUBAGENT, read_metadata=with_metadata)
    expected = summarize_subagent(SUBAGENT)
    if not with_metadata:
        expected["agent_type"] = "subagent"
    source = ClaudeSource(
        Path("/unavailable/agent.jsonl"), recorded.entries, recorded.metadata
    )

    def forbidden(*args, **kwargs):
        pytest.fail("interpretation accessed the filesystem")

    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(Path, "is_file", forbidden)
    actual_summarize_subagent_source = eclog.summarize_subagent_source(source)
    assert actual_summarize_subagent_source == expected
    actual_summarize_subagent_source = eclog.summarize_subagent_source(
        ClaudeSource(source.path, [])
    )
    assert actual_summarize_subagent_source is None


@pytest.mark.parametrize("metadata", [None, "bad JSON", "[]", "null", "{}"])
def test_source_reader_metadata_fallback(metadata, tmp_path):
    transcript = tmp_path / "agent.jsonl"
    shutil.copyfile(SUBAGENT, transcript)
    if metadata is not None:
        transcript.with_suffix(".meta.json").write_text(metadata)
    source = read_source(transcript, read_metadata=True)
    assert source.path == transcript
    actual_summarize_subagent_source = eclog.summarize_subagent_source(source)
    assert actual_summarize_subagent_source["agent_type"] == "subagent"


def test_source_reader_metadata_read_error(tmp_path, monkeypatch):
    transcript = tmp_path / "agent.jsonl"
    recorded = SUBAGENT
    shutil.copyfile(recorded, transcript)
    shutil.copyfile(
        recorded.with_suffix(".meta.json"), transcript.with_suffix(".meta.json")
    )

    def unreadable(*args, **kwargs):
        raise OSError("unreadable metadata")

    monkeypatch.setattr(Path, "read_text", unreadable)
    actual_read_source = read_source(transcript, read_metadata=True)
    assert actual_read_source.metadata == {}


def test_source_reader_keeps_diagnostics_and_read_errors(tmp_path, capsys):
    transcript = tmp_path / "session.jsonl"
    transcript.write_bytes(b'\n{"type":"user"}\ninvalid\n{"text":"\xff"}\n[]\n')
    source = read_source(transcript)
    assert source.entries == [{"type": "user"}, {"text": "\ufffd"}]
    captured = capsys.readouterr()
    assert "skipping malformed JSON on line 3" in captured.err
    with pytest.raises(FileNotFoundError):
        read_source(tmp_path / "missing.jsonl")


def test_source_reader_discovers_sidecars_in_filename_order(tmp_path):
    transcript = tmp_path / "session.jsonl"
    actual_read_subagent_sources = read_subagent_sources(transcript)
    assert actual_read_subagent_sources == []
    directory = tmp_path / "session" / "subagents"
    directory.mkdir(parents=True)
    for name in ["b", "a"]:
        shutil.copyfile(
            SUBAGENT,
            directory / f"{name}.jsonl",
        )
    (directory / "ignored.txt").touch()
    (directory / "directory.jsonl").mkdir()
    sources = read_subagent_sources(transcript)
    source_names = []
    recorded_entries = load_entries(SUBAGENT)
    for source in sources:
        source_names.append(source.path.name)
        assert source.path.parent == directory
        assert source.entries == recorded_entries
    assert source_names == ["a.jsonl", "b.jsonl"]


def test_subagent_reader_skips_empty_metadata_and_preserves_summary_order(tmp_path):
    transcript = tmp_path / "session.jsonl"
    directory = tmp_path / "session" / "subagents"
    directory.mkdir(parents=True)
    # Filename order breaks timestamp ties; missing timestamps sort first.
    for name, timestamp in [
        ("a", "2026-01-02"),
        ("b", "2026-01-01"),
        ("c", "2026-01-01"),
        ("d", None),
    ]:
        entries = load_entries(SUBAGENT)
        # Vary timestamps and label each task so ordering remains observable.
        for entry in entries:
            entry["timestamp"] = timestamp
            if entry.get("type") == claude_protocol.USER_RECORD:
                entry["message"]["content"] = name
        _write_jsonl(directory / f"{name}.jsonl", entries)
    (directory / "empty.jsonl").touch()
    # A directory at the metadata path must not be read for an empty sidecar.
    (directory / "empty.meta.json").mkdir()
    ordered_tasks = []
    for summary in load_subagents(transcript):
        ordered_tasks.append(summary["task"])
    assert ordered_tasks == ["d", "b", "c", "a"]


def test_main_source_does_not_read_sidecars_or_metadata(monkeypatch):
    transcript = TRANSCRIPT
    entries = load_entries(transcript)

    def forbidden(*args, **kwargs):
        pytest.fail("main JSON extraction attempted sidecar discovery")

    with monkeypatch.context() as guard:
        guard.setattr(Path, "iterdir", forbidden)
        guard.setattr(Path, "is_file", forbidden)
        guard.setattr(Path, "read_text", forbidden)
        envelope = extract_transcript(transcript)
    actual_envelope = build_raw_envelope(transcript, entries)
    assert envelope == actual_envelope


def test_interpretation_retains_full_results_until_shared_export():
    entries = _recorded_tool_interaction(claude_protocol.READ_TOOL, "multibyte.txt")
    source = ClaudeSource(Path("/unavailable/session.jsonl"), entries)
    session = eclog.interpret_source(source)
    call = _single_tool_call_in(session["events"])
    assert len(call["result"].encode()) > 10_000
    assert "result_truncated" not in call
    exported = session_log_envelope.export_session(session, 17)
    assert len(_single_tool_call_in(exported["events"])["result"].encode()) == 17
    assert len(call["result"].encode()) > 10_000


def test_cli_reaches_shared_exporter(monkeypatch, capsys):
    original = session_log_envelope.export_session
    seen = []

    def exporting(session, tool_result_max_bytes):
        seen.append(session)
        return original(session, tool_result_max_bytes)

    monkeypatch.setattr(session_log_envelope, "export_session", exporting)
    exit_status = main(["--transcript", str(TRANSCRIPT), "--output", "-"])
    captured = capsys.readouterr()
    assert exit_status == 0
    assert json.loads(captured.out)["events"]
    assert len(seen) == 1


@pytest.mark.parametrize("same_id", [False, True])
def test_repeated_calls_keep_order_and_native_result_associations(same_id):
    assistant, result = _recorded_tool_interaction(
        claude_protocol.READ_TOOL, "multibyte.txt"
    )
    again = deepcopy(assistant)
    again_result = deepcopy(result)
    first_call = _content_block(assistant, claude_protocol.TOOL_USE_BLOCK)
    second_call = _content_block(again, claude_protocol.TOOL_USE_BLOCK)
    # Repeat the captured Read, changing only identity/output to expose association.
    if not same_id:
        second_call["id"] = "second-call"
    _content_block(again_result, claude_protocol.TOOL_RESULT_BLOCK)["tool_use_id"] = (
        second_call["id"]
    )
    _content_block(again_result, claude_protocol.TOOL_RESULT_BLOCK)["content"] = (
        "second output"
    )
    if same_id:
        entries = [assistant, result, again, again_result]
    else:
        entries = [assistant, again, again_result, result]
    session = eclog.interpret_source(ClaudeSource(Path("s.jsonl"), entries))
    event_indices = []
    calls = []
    for event in session["events"]:
        event_indices.append(event["i"])
        calls.append(event["tool_calls"][0])
    assert event_indices == [0, 1]
    call_names = []
    for call in calls:
        call_names.append(call["name"])
    assert call_names == ["Read", "Read"]
    assert calls[0]["input"] == first_call["input"]
    assert "CAPTURE_MULTIBYTE" in calls[0]["result"]
    assert calls[1]["result"] == "second output"
    assert calls[0] is not calls[1]


@pytest.mark.parametrize("result_kind", ["missing", "empty", "null", "error"])
def test_missing_and_empty_results_preserve_claude_json_contract(result_kind):
    assistant, result = _recorded_tool_interaction(
        claude_protocol.READ_TOOL, "multibyte.txt"
    )
    block = _content_block(result, claude_protocol.TOOL_RESULT_BLOCK)
    # Keep the captured association; vary only the result boundary under test.
    block["content"] = None if result_kind == "null" else ""
    if result_kind == "error":
        block["is_error"] = True
    entries = [assistant] if result_kind == "missing" else [assistant, result]
    session = eclog.interpret_source(ClaudeSource(Path("s.jsonl"), entries))
    call = _single_tool_call_in(session["events"])
    assert call["result"] == (None if result_kind == "missing" else "")
    exported = _single_tool_call_in(
        session_log_envelope.export_session(session)["events"]
    )
    assert exported["result"] == call["result"]
    assert exported["result_bytes"] == 0
    assert exported["result_truncated"] is False
    assert exported.get("is_error", False) is (result_kind == "error")


def test_images_remain_owned_by_their_recorded_user_event():
    image_entries = [_recorded_user_message("CAPTURE_IMAGES_SAME:")]
    assistant, result = _recorded_tool_interaction(
        claude_protocol.READ_TOOL, "multibyte.txt"
    )
    # Put captured image blocks on result/assistant records: the existing Claude
    # JSON contract includes only human message images; expansion is deferred.
    images = []
    for block in image_entries[0]["message"]["content"]:
        if block["type"] == claude_protocol.IMAGE_BLOCK:
            images.append(block)
    assistant["message"]["content"].extend(deepcopy(images))
    result["message"]["content"].extend(deepcopy(images))
    entries = [image_entries[0], assistant, result, deepcopy(image_entries[0])]
    session = eclog.interpret_source(ClaudeSource(Path("s.jsonl"), entries))
    events = session["events"]
    roles = []
    image_counts = []
    for event in events:
        roles.append(event["role"])
        image_counts.append(len(event.get("images", [])))
    assert roles == ["user", "assistant", "user"]
    assert image_counts == [2, 0, 2]
    assert events[0]["images"] == events[2]["images"]
    assert "CAPTURE_MULTIBYTE" in events[1]["tool_calls"][0]["result"]


def test_recorded_skills_deduplicate_and_ignore_empty_names():
    pairs = _recorded_tool_interactions(claude_protocol.SKILL_TOOL, "capture-leaf")
    assert len(pairs) >= 2
    entries = []
    for pair in pairs:
        entries.extend(pair)
    # Add the same empty-name boundary to each native invocation; none is chosen
    # by occurrence, timestamp, or ID when the invocations are semantically equal.
    for assistant, _ in pairs:
        skill = _content_block(assistant, claude_protocol.TOOL_USE_BLOCK)
        empty = deepcopy(skill)
        empty["input"] = {"skill": ""}
        assistant["message"]["content"].append(empty)
    interpreted_session = eclog.interpret_source(
        ClaudeSource(Path("session.jsonl"), entries)
    )
    assert interpreted_session["skills_used"] == ["capture-leaf"]
    calls = []
    for call in _tool_calls_in(interpreted_session):
        if call["name"] == claude_protocol.SKILL_TOOL:
            calls.append(call)
    assert len(calls) == 2 * len(pairs)
    named_skill_count = 0
    unnamed_skill_count = 0
    for call in calls:
        if call.get("skill") == "capture-leaf":
            named_skill_count += 1
        if call.get("skill") is None:
            unnamed_skill_count += 1
    assert named_skill_count == len(pairs)
    assert unnamed_skill_count == len(pairs)


def test_question_stays_with_its_call_across_a_later_user_message():
    assistant, result = _recorded_multiselect_interaction()
    later = _recorded_user_message("Remember **CAPTURE_CONTEXT_CEDAR_73**")
    # Insert a human message between the recorded call and its result.

    session = eclog.interpret_source(
        ClaudeSource(Path("session.jsonl"), [assistant, later, result])
    )

    call = _single_tool_call_in(session["events"])
    assert call["id"] == _content_block(assistant, "tool_use")["id"]
    assert call["result_record"]["tool_call_id"] == call["id"]
    answer = _response_to_question(call, multi_select=True)
    assert len(answer["selections"]) >= 2
    _assert_answer_uses_distinct_recorded_options(answer, call)
    user_messages = []
    for event in session["events"]:
        if event["role"] == "user":
            user_messages.append(event)
    user_message = _require_single_match(user_messages, "user message")
    assert "CAPTURE_CONTEXT_CEDAR_73" in user_message["text"]
    assert "interactions" not in session
    assert "interpretation" not in session


def test_plan_stays_with_its_call_across_a_later_user_message():
    assistant, result = _recorded_tool_interaction(
        claude_protocol.EXIT_PLAN_MODE, result_contains="## Approved Plan:\n"
    )
    later = _recorded_user_message("Remember **CAPTURE_CONTEXT_CEDAR_73**")
    # Insert a human message between the recorded call and its result.

    session = eclog.interpret_source(
        ClaudeSource(Path("session.jsonl"), [assistant, later, result])
    )

    call = _single_tool_call_in(session["events"])
    assert call["id"] == _content_block(assistant, "tool_use")["id"]
    assert call["result_record"]["tool_call_id"] == call["id"]
    assert call["plan_decisions"][0]["outcome"] == "approved"
    user_messages = []
    for event in session["events"]:
        if event["role"] == "user":
            user_messages.append(event)
    user_message = _require_single_match(user_messages, "user message")
    assert "CAPTURE_CONTEXT_CEDAR_73" in user_message["text"]
    assert "interactions" not in session
    assert "interpretation" not in session


def test_permission_denial_stays_with_its_call_across_a_later_user_message():
    assistant, result = _recorded_tool_interaction(
        claude_protocol.BASH_TOOL, "CAPTURE_SETTINGS_REFUSAL"
    )
    later = _recorded_user_message("Remember **CAPTURE_CONTEXT_CEDAR_73**")
    # Insert a human message between the recorded call and its result.

    session = eclog.interpret_source(
        ClaudeSource(Path("session.jsonl"), [assistant, later, result])
    )

    call = _single_tool_call_in(session["events"])
    assert call["id"] == _content_block(assistant, "tool_use")["id"]
    assert call["result_record"]["tool_call_id"] == call["id"]
    assert call["permission_refusals"][0]["source"] == "settings"
    user_messages = []
    for event in session["events"]:
        if event["role"] == "user":
            user_messages.append(event)
    user_message = _require_single_match(user_messages, "user message")
    assert "CAPTURE_CONTEXT_CEDAR_73" in user_message["text"]
    assert "interactions" not in session
    assert "interpretation" not in session


def test_native_message_and_result_identities_survive_interpretation():
    assistant, result = _recorded_tool_interaction(
        claude_protocol.READ_TOOL, "multibyte.txt"
    )
    session = eclog.interpret_source(
        ClaudeSource(Path("session.jsonl"), [assistant, result])
    )
    message = _require_single_match(session["events"], "assistant message")
    assert message["id"] == assistant["uuid"]
    assert message["parent_id"] == assistant["parentUuid"]
    assert message["message_id"] == assistant["message"]["id"]
    record = message["tool_calls"][0]["result_record"]
    assert record["id"] == result["uuid"]
    assert record["ts"] == result["timestamp"]
    assert record["content"] == _content_block(result, "tool_result")["content"]


def test_unmatched_result_retains_content_without_inventing_an_owner():
    assistant, result = _recorded_tool_interaction(
        claude_protocol.READ_TOOL, "multibyte.txt"
    )
    # Change only the result ID so the captured result has no matching call.
    block = _content_block(result, claude_protocol.TOOL_RESULT_BLOCK)
    block["tool_use_id"] = "unknown-call"
    session = eclog.interpret_source(
        ClaudeSource(Path("session.jsonl"), [assistant, result])
    )
    assert _single_tool_call_in(session["events"])["result"] is None
    assert "result_record" not in _single_tool_call_in(session["events"])
    (record,) = _unmatched_results_in(session)
    assert record["tool_call_id"] == "unknown-call"
    assert record["content"] == block["content"]
    assert record["id"] == result["uuid"]


@pytest.mark.parametrize("options", [None, 7, "invalid", {"label": "section"}])
def test_malformed_question_options_preserve_input_and_allow_extraction(options):
    assistant, result = _recorded_multiselect_interaction()
    entries = [assistant, result]
    block = _content_block(assistant, claude_protocol.TOOL_USE_BLOCK)
    # Vary only the options container in the audited question fixture.
    _native_question_in(block["input"], multi_select=True)["options"] = options
    session = eclog.interpret_source(ClaudeSource(Path("session.jsonl"), entries))
    call = _single_tool_call_in(session["events"])
    assert call["input"] == block["input"]
    for question in call["interaction_questions"]:
        if question.get("multiple_selections"):
            assert question["options"] == []
    answer = _response_to_question(call, multi_select=True)["free_text"]
    assert isinstance(answer, str)
    assert answer.strip()
    exported = session_log_envelope.export_session(session)
    assert _single_tool_call_in(exported["events"])["input"] == block["input"]


@pytest.mark.parametrize("payload", [{"stdout": "output"}, "output", {}, "", False])
def test_top_level_result_preserves_payload_without_inventing_an_owner(payload):
    assistant, result = _recorded_tool_interaction(
        claude_protocol.READ_TOOL, "multibyte.txt"
    )
    # Model the older result-only shape by moving the captured result to the
    # top level and removing the block that carries its call ID.
    result["message"]["content"] = []
    result["toolUseResult"] = payload
    session = eclog.interpret_source(
        ClaudeSource(Path("session.jsonl"), [assistant, result])
    )
    call = _single_tool_call_in(session["events"])
    assert call["result"] is None
    assert "result_record" not in call
    (record,) = _unmatched_results_in(session)
    assert record["tool_call_id"] is None
    assert record["content"] == payload
    assert record["id"] == result["uuid"]
    assert record["parent_id"] == result["parentUuid"]
    assert record["ts"] == result["timestamp"]
    assert record["is_error"] is False


def test_top_level_result_does_not_duplicate_a_recorded_result_block():
    assistant, result = _recorded_tool_interaction(
        claude_protocol.READ_TOOL, "multibyte.txt"
    )
    result["toolUseResult"] = {"stdout": "duplicate output"}
    session = eclog.interpret_source(
        ClaudeSource(Path("session.jsonl"), [assistant, result])
    )
    assert _unmatched_results_in(session) == []
    assert "CAPTURE_MULTIBYTE" in _single_tool_call_in(session["events"])["result"]


def test_unknown_tool_payload_is_preserved_and_reported(tmp_path, capsys):
    assistant, result = _recorded_tool_interaction(
        claude_protocol.READ_TOOL, "multibyte.txt"
    )
    # Change only the captured tool name and extend its input to model a plugin.
    block = _content_block(assistant, claude_protocol.TOOL_USE_BLOCK)
    block["name"] = "plugin__DeployService"
    block["input"]["plugin_options"] = {
        "targets": [None, True, 1.5, {"region": "east"}]
    }
    session = eclog.interpret_source(
        ClaudeSource(Path("session.jsonl"), [assistant, result])
    )
    call = _single_tool_call_in(session["events"])
    assert call["input_schema"] == "unsupported"
    assert session["unsupported_tools"] == ["plugin__DeployService"]
    assert call["input"] == block["input"]
    assert (
        call["result_record"]["content"]
        == _content_block(result, "tool_result")["content"]
    )
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(transcript, [assistant, result, deepcopy(assistant)])
    envelope = extract_transcript(transcript, -1)
    exported_calls = []
    for event in envelope["events"]:
        exported_calls.extend(event.get("tool_calls", []))
    assert len(exported_calls) == 2
    for call in exported_calls:
        assert call["input"] == block["input"]
        assert "input_schema" not in call
    diagnostic = capsys.readouterr().err
    assert "unsupported tool input schemas" in diagnostic
    assert diagnostic.count("plugin__DeployService") == 1


def test_question_options_are_checked_without_losing_recorded_payload():
    assistant, result = _recorded_multiselect_interaction()
    entries = [assistant, result]
    block = _content_block(assistant, claude_protocol.TOOL_USE_BLOCK)
    options = _native_question_in(block["input"], multi_select=True)["options"]
    # Extend a recorded option and introduce malformed known fields. Semantic
    # parsing uses checked options while the complete native input survives.
    assert options
    for option in options:
        option["preview"] = {"markdown": "**keep this**"}
    malformed_options = [{"label": 1}, {"label": "bad", "description": None}]
    options.extend(malformed_options)

    session = eclog.interpret_source(ClaudeSource(Path("session.jsonl"), entries))

    call = _single_tool_call_in(session["events"])
    assert call["input"] == block["input"]
    answer = _response_to_question(call, multi_select=True)
    for question in call["interaction_questions"]:
        if not question.get("multiple_selections"):
            continue
        labels = []
        for option in question["options"]:
            labels.append(option["label"])
            assert "preview" not in option
        expected_labels = []
        for option in options:
            if isinstance(option["label"], str):
                expected_labels.append(option["label"])
        assert labels == expected_labels
    _assert_answer_uses_distinct_recorded_options(answer, call)
    assert len(answer["selections"]) >= 2


@pytest.mark.parametrize("name", ["bash", "view", "edit", "create", "skill"])
def test_claude_reports_copilot_tool_names_as_unsupported(name):
    assistant, result = _recorded_tool_interaction(
        claude_protocol.READ_TOOL, "multibyte.txt"
    )
    # Change only the captured name to the other harness's vocabulary.
    block = _content_block(assistant, claude_protocol.TOOL_USE_BLOCK)
    block["name"] = name
    session = eclog.interpret_source(
        ClaudeSource(Path("session.jsonl"), [assistant, result])
    )
    call = _single_tool_call_in(session["events"])
    assert call["input_schema"] == "unsupported"
    assert session["unsupported_tools"] == [name]
    assert call["input"] == block["input"]
    assert (
        call["result_record"]["content"]
        == _content_block(result, "tool_result")["content"]
    )


def _plan_result_variant(text):
    # Legacy rejection-reason wording is absent from the current CLI capture.
    assistant, result = _recorded_tool_interaction(
        claude_protocol.EXIT_PLAN_MODE, result_contains="CAPTURE_PLAN_REJECT_1:"
    )
    _content_block(result, claude_protocol.TOOL_RESULT_BLOCK)["content"] = text
    return [assistant, result]


def test_legacy_edited_heading_preserves_explicit_version_evidence():
    # The earlier audited plan fixture echoed an unchanged plan under this heading.
    assistant, result = _recorded_tool_interaction(
        claude_protocol.EXIT_PLAN_MODE, result_contains="## Approved Plan:\n"
    )
    block = _content_block(result, claude_protocol.TOOL_RESULT_BLOCK)
    block["content"] = block["content"].replace(
        "## Approved Plan:", "## Approved Plan (edited by user):"
    )
    interpreted_session = eclog.interpret_source(
        ClaudeSource(Path("session.jsonl"), [assistant, result])
    )
    (plan,) = _plan_decisions_in(interpreted_session)
    assert plan["outcome"] == "approved"
    (call,) = _tool_calls_in(interpreted_session)
    assert call["plan_versions"][1]["kind"] == "edited"
    assert call["plan_versions"][1]["text"].strip() == call["input"]["plan"].strip()


@pytest.mark.parametrize("media_type", ["image/jpeg", "image/gif", "image/webp"])
def test_legacy_image_media_types_are_preserved(media_type):
    # Current Claude converts attachments to PNG. Substitute the captured source
    # asset on every image in the semantically selected message for the legacy edge.
    entry = _recorded_user_message("CAPTURE_IMAGES_SAME:")
    filenames = {
        "image/jpeg": "blue.jpg",
        "image/gif": "green.gif",
        "image/webp": "gold.webp",
    }
    data = base64.b64encode(
        (ROOT / "assets/before" / filenames[media_type]).read_bytes()
    ).decode()
    sources = []
    for block in _content_blocks(entry):
        if isinstance(block, dict) and block.get("type") == claude_protocol.IMAGE_BLOCK:
            sources.append(block["source"])
    assert sources
    for source in sources:
        source["media_type"] = media_type
        source["data"] = data
    user = _require_single_match(build_events([entry]), "image event")
    assert len(user["images"]) == len(sources)
    for image in user["images"]:
        assert image["media_type"] == media_type
        assert image["data"] == data


def test_legacy_dont_ask_denial_without_command_is_recorded():
    # Claude 2.1.292 omitted the command from its settings denial preamble.
    assistant, result = _recorded_tool_interaction(
        claude_protocol.BASH_TOOL, "CAPTURE_SETTINGS_REFUSAL"
    )
    _content_block(result, claude_protocol.TOOL_RESULT_BLOCK)["content"] = (
        "Permission to use Bash has been denied."
    )
    interpreted_session = eclog.interpret_source(
        ClaudeSource(Path("session.jsonl"), [assistant, result])
    )
    (call,) = _tool_calls_in(interpreted_session)
    assert call["permission_refusals"][0]["source"] == "settings"
    assert "candidate_feedback" not in call["permission_refusals"][0]


def _question_text_replacements(entries):
    replacements = {}
    for entry in entries:
        for block in _content_blocks(entry):
            if not isinstance(block, dict):
                continue
            if block.get("name") != claude_protocol.ASK_USER_QUESTION:
                continue
            for question in block["input"]["questions"]:
                for field in ("question", "header"):
                    text = question[field]
                    replacements[text] = f"Reworded: {text}"
                for option in question["options"]:
                    for field in ("label", "description"):
                        text = option[field]
                        replacements[text] = f"Alternative {text}"
    return replacements


def _reword_generated_questions(entries):
    replacements = _question_text_replacements(entries)
    # One non-cascading pass updates calls, rendered results, and answer maps.
    longest_first = sorted(replacements, key=len, reverse=True)
    escaped_texts = []
    for text in longest_first:
        escaped_texts.append(re.escape(text))
    pattern = "|".join(escaped_texts)
    serialized_entries = json.dumps(entries, ensure_ascii=False)
    reworded_entries = re.sub(
        pattern, lambda match: replacements[match[0]], serialized_entries
    )
    return json.loads(reworded_entries)


@pytest.fixture
def varied_native_entries():
    """Return capture entries with reworded prose, regenerated IDs, and reversed order."""
    entries = _reword_generated_questions(_recorded_session_entries())
    for entry in entries:
        if "uuid" in entry:
            entry["uuid"] = "regenerated-" + entry["uuid"]
        for block in _content_blocks(entry):
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if (
                entry.get("type") == claude_protocol.ASSISTANT_RECORD
                and block_type == "text"
            ):
                block["text"] = "A different generated response."
            if block_type == claude_protocol.TOOL_USE_BLOCK:
                block["id"] = "regenerated-" + block["id"]
            elif block_type == claude_protocol.TOOL_RESULT_BLOCK:
                block["tool_use_id"] = "regenerated-" + block["tool_use_id"]
        content = entry.get("message", {}).get("content")
        if isinstance(content, list):
            content.reverse()
    entries.reverse()
    return entries


def test_multiselect_selection_tolerates_changed_capture(
    varied_native_entries, monkeypatch
):
    monkeypatch.setattr(
        __name__ + "._recorded_session_entries", lambda: varied_native_entries
    )
    multiselect_entries = _recorded_multiselect_interaction()

    session = eclog.interpret_source(
        ClaudeSource(Path("session.jsonl"), multiselect_entries)
    )

    call = _single_tool_call_in(session["events"])
    answer = _response_to_question(call, multi_select=True)
    _assert_answer_uses_distinct_recorded_options(answer, call)
    assert len(answer["selections"]) >= 2


def test_overlapping_option_selection_tolerates_changed_capture(
    varied_native_entries, monkeypatch
):
    monkeypatch.setattr(
        __name__ + "._recorded_session_entries", lambda: varied_native_entries
    )
    overlapping_entries = _recorded_overlapping_options_interaction()

    session = eclog.interpret_source(
        ClaudeSource(Path("session.jsonl"), overlapping_entries)
    )

    call = _single_tool_call_in(session["events"])
    answer = _response_to_question(call, multi_select=False)
    _assert_answer_uses_distinct_recorded_options(answer, call)
    assert len(answer["selections"]) == 1
    assert "free_text" not in answer


def test_interpretation_accepts_reworded_assistant_prose(varied_native_entries):
    source = ClaudeSource(Path("session.jsonl"), varied_native_entries)

    session = eclog.interpret_source(source)

    _assert_assistant_text(session["events"])


def test_tool_selection_tolerates_changed_capture(varied_native_entries, monkeypatch):
    monkeypatch.setattr(
        __name__ + "._recorded_session_entries", lambda: varied_native_entries
    )
    assistant, result = _recorded_tool_interaction(
        claude_protocol.READ_TOOL, "multibyte.txt"
    )

    call = _content_block(assistant, claude_protocol.TOOL_USE_BLOCK)
    result_block = _content_block(result, claude_protocol.TOOL_RESULT_BLOCK)
    assert Path(call["input"]["file_path"]).name == "multibyte.txt"
    assert result_block["tool_use_id"] == call["id"]
    assert "CAPTURE_MULTIBYTE" in result_block["content"]


def test_user_message_selection_tolerates_changed_capture(
    varied_native_entries, monkeypatch
):
    monkeypatch.setattr(
        __name__ + "._recorded_session_entries", lambda: varied_native_entries
    )
    image_entry = _recorded_user_message("CAPTURE_IMAGES_SAME:")

    text_block = _content_block(image_entry, "text")
    assert "CAPTURE_IMAGES_SAME:" in text_block["text"]


@pytest.mark.parametrize("duplicate", [False, True])
def test_fixture_selection_rejects_missing_or_ambiguous_activity(
    duplicate, monkeypatch
):
    assistant, result = _recorded_tool_interaction(
        claude_protocol.READ_TOOL, "multibyte.txt"
    )
    entries = [assistant, deepcopy(assistant), result] if duplicate else []
    monkeypatch.setattr(__name__ + "._recorded_session_entries", lambda: entries)
    message = "Expected one Read activity" if duplicate else "Missing Read activity"
    with pytest.raises(AssertionError, match=message):
        _recorded_tool_interaction("Read", "multibyte.txt")


@pytest.mark.parametrize("duplicate", [False, True])
def test_fixture_user_selection_rejects_missing_or_ambiguous_messages(
    duplicate, monkeypatch
):
    user = _recorded_user_message("CAPTURE_IMAGES_SAME:")
    entries = [user, deepcopy(user)] if duplicate else []
    monkeypatch.setattr(__name__ + "._recorded_session_entries", lambda: entries)
    with pytest.raises(AssertionError, match="Expected one user message"):
        _recorded_user_message("CAPTURE_IMAGES_SAME:")


@pytest.mark.parametrize("failure", ["input", "write", "read"])
def test_cli_batch_retains_successes_after_failure(
    failure, tmp_path, monkeypatch, capsys
):
    project = tmp_path / "project"
    project.mkdir()
    shutil.copyfile(TRANSCRIPT, project / "populated.jsonl")
    (project / "empty.jsonl").touch()
    broken = project / "broken.jsonl"
    broken.write_text("invalid" if failure == "input" else "{}")
    output = tmp_path / "output"
    output.mkdir()
    if failure == "write":
        (output / "claude_session_log_raw_broken.json").mkdir()
    elif failure == "read":
        original_open = Path.open

        def open_transcript(path, *args, **kwargs):
            if path == broken:
                raise PermissionError("unreadable transcript")
            return original_open(path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", open_transcript)

    status = main(["--all", "--project-dir", str(project), "--output", str(output)])

    captured = capsys.readouterr()
    assert status == 1
    assert captured.out == ""
    failed_path = (
        output / "claude_session_log_raw_broken.json" if failure == "write" else broken
    )
    assert str(failed_path) in captured.err
    assert "2/3" in captured.err
    empty = json.loads((output / "claude_session_log_raw_empty.json").read_text())
    populated = json.loads(
        (output / "claude_session_log_raw_populated.json").read_text()
    )
    assert empty["events"] == []
    actual_build_events = build_events(_recorded_session_entries())
    assert populated["events"] == actual_build_events


def test_cli_batch_reports_output_directory_failure(tmp_path, capsys):
    (tmp_path / "empty.jsonl").touch()
    output = tmp_path / "output"
    output.touch()

    status = main(["--all", "--project-dir", str(tmp_path), "--output", str(output)])

    captured = capsys.readouterr()
    assert status == 1
    assert captured.out == ""
    assert str(output) in captured.err
    assert "error" in captured.err


@pytest.mark.parametrize("destination", ["stdout", "file"])
def test_cli_reports_unreadable_input(destination, tmp_path, monkeypatch, capsys):
    transcript = tmp_path / "session.jsonl"
    transcript.touch()

    def unreadable(*args, **kwargs):
        raise PermissionError(f"unreadable: {transcript}")

    monkeypatch.setattr(Path, "open", unreadable)
    output = "-" if destination == "stdout" else str(tmp_path / "result.json")

    status = main(["--transcript", str(transcript), "--output", output])

    captured = capsys.readouterr()
    assert status == 1
    assert captured.out == ""
    assert str(transcript) in captured.err
    assert "unreadable" in captured.err


def test_cli_diagnoses_non_objects_without_losing_valid_records(tmp_path, capsys):
    transcript = tmp_path / "partial.jsonl"
    entries = _recorded_overlapping_options_interaction()
    _write_jsonl(transcript, entries)
    with transcript.open("a") as stream:
        stream.write("\n[]\nnull\n")

    status = main(["--transcript", str(transcript), "--output", "-"])

    captured = capsys.readouterr()
    assert status == 0
    actual_build_events = build_events(entries)
    assert json.loads(captured.out)["events"] == actual_build_events
    assert str(transcript) in captured.err
    assert "line 4" in captured.err
    assert "line 5" in captured.err
    assert "line 3" not in captured.err


def test_cli_tolerates_skipped_records_in_empty_session(tmp_path, capsys):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("{}\ninvalid\n[]\n")

    status = main(["--transcript", str(transcript), "--output", "-"])

    captured = capsys.readouterr()
    assert status == 0
    assert json.loads(captured.out)["events"] == []
    assert str(transcript) in captured.err
    assert "line 2" in captured.err
    assert "line 3" in captured.err


def test_cli_batch_exports_every_empty_session(tmp_path, capsys):
    project = tmp_path / "project"
    project.mkdir()
    (project / "blank.jsonl").touch()
    (project / "object.jsonl").write_text("{}")
    output = tmp_path / "output"

    status = main(["--all", "--project-dir", str(project), "--output", str(output)])

    captured = capsys.readouterr()
    assert status == 0
    assert captured.out == ""
    assert "done: 2/2" in captured.err
    for session_id in ["blank", "object"]:
        envelope = json.loads(
            (output / f"claude_session_log_raw_{session_id}.json").read_text()
        )
        assert envelope["session_id"] == session_id
        assert envelope["events"] == []


def test_cli_help_explains_empty_and_failure_outcomes(capsys):
    with pytest.raises(SystemExit) as result:
        main(["--help"])

    captured = capsys.readouterr()
    assert result.value.code == 0
    assert "events: []" in captured.out
    assert "malformed/non-object records" in captured.out
    assert "any batch failure" in captured.out


@pytest.mark.parametrize("mode", ["stdout", "explicit", "default", "batch"])
def test_recorded_identity_survives_renaming_in_all_output_modes(
    mode, tmp_path, capsys
):
    transcript = tmp_path / "renamed.jsonl"
    shutil.copyfile(TRANSCRIPT, transcript)
    source = read_source(transcript)
    recorded_ids = set()
    for entry in source.entries:
        if isinstance(entry.get("sessionId"), str) and entry["sessionId"]:
            recorded_ids.add(entry["sessionId"])
    assert len(recorded_ids) == 1
    (expected_id,) = recorded_ids
    session = eclog.interpret_source(source)
    assert session["session_id"] == expected_id

    destination = tmp_path / "explicit.json"
    args = ["--transcript", str(transcript)]
    if mode == "stdout":
        args += ["--output", "-"]
    elif mode == "explicit":
        args += ["--output", str(destination)]
    elif mode == "batch":
        args = ["--all", "--project-dir", str(tmp_path)]
    status = main(args, project_root=tmp_path)
    captured = capsys.readouterr()
    assert status == 0, captured.err
    if mode in {"default", "batch"}:
        destination = tmp_path / "claude_session_log_raw_renamed.json"
    payload = captured.out if mode == "stdout" else destination.read_text()
    actual = json.loads(payload)
    assert actual["session_id"] == expected_id
    expected = session_log_envelope.export_session(session)
    assert actual == expected


@pytest.mark.parametrize("invalid_id", [None, "", 17, [], {}])
def test_unusable_claude_ids_keep_stem_fallback(invalid_id):
    source = read_source(TRANSCRIPT)
    # Replace only native identity fields; agentId must not become session identity.
    for entry in source.entries:
        entry["sessionId"] = invalid_id
        entry["agentId"] = "separate-agent"
    source.path = Path("fallback.jsonl")
    session = eclog.interpret_source(source)
    assert session["session_id"] == "fallback"


def _conflicting_identity_source():
    source = read_source(TRANSCRIPT)
    # Keep the native conversation, but deliberately assemble two session identities.
    for entry in source.entries:
        entry["sessionId"] = "first-native-id"
    source.entries[-1]["sessionId"] = "second-native-id"
    return source


def test_conflicting_claude_ids_fail_during_interpretation():
    source = _conflicting_identity_source()
    with pytest.raises(ValueError, match="session") as error:
        eclog.interpret_source(source)
    diagnostic = str(error.value)
    assert "first-native-id" in diagnostic
    assert "second-native-id" in diagnostic


@pytest.mark.parametrize("batch", [False, True])
def test_claude_identity_conflict_uses_cli_error_handling(batch, tmp_path, capsys):
    source = _conflicting_identity_source()
    transcript = tmp_path / "conflict.jsonl"
    with transcript.open("w") as stream:
        for entry in source.entries:
            stream.write(json.dumps(entry) + "\n")
    args = ["--transcript", str(transcript), "--output", "-"]
    if batch:
        args = ["--all", "--project-dir", str(tmp_path)]
    status = main(args, project_root=tmp_path)
    captured = capsys.readouterr()
    assert status == 1
    assert not captured.out
    assert "first-native-id" in captured.err
    assert "second-native-id" in captured.err
    assert not (tmp_path / "claude_session_log_raw_conflict.json").exists()
