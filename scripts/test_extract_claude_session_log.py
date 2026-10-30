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

from session_log_extractors import extract_claude_session_log as eclog
from session_log_extractors import session_log_envelope
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
    _parse_chosen,
    _result_block_text,
    apply_plan_decisions,
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
    newest,
    option_qa_from_result,
    parse_permission_denial,
    parse_plan_decision,
    permission_denials_from_result,
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

ROOT = Path(__file__).parent / "fixtures" / "claude" / "2.1.312"
(TRANSCRIPT,) = (ROOT / "native").glob("*.jsonl")
(SUBAGENT,) = (TRANSCRIPT.with_suffix("") / "subagents").glob("*.jsonl")


def _recorded_session_entries():
    return load_entries(TRANSCRIPT)


def _require_single_match(matches, description):
    assert len(matches) == 1, f"Expected one {description}, found {len(matches)}"
    (match,) = matches
    return match


def _content_block(entry, kind):
    return _require_single_match(
        [
            block
            for block in _content_blocks(entry)
            if isinstance(block, dict) and block.get("type") == kind
        ],
        f"{kind} block",
    )


def _recorded_user_message(marker):
    return _require_single_match(
        [
            entry
            for entry in _recorded_session_entries()
            if entry.get("type") == "user"
            and not entry.get("isMeta")
            and not entry.get("isCompactSummary")
            and marker in _human_text(entry)
        ],
        f"user message containing {marker!r}",
    )


def _recorded_result_for_call(entries, call):
    # IDs from the selected capture connect records; no recorded ID selects a case.
    return _require_single_match(
        [
            entry
            for entry in entries
            if entry.get("type") == "user"
            and any(
                isinstance(block, dict)
                and block.get("type") == "tool_result"
                and block.get("tool_use_id") == call["id"]
                for block in _content_blocks(entry)
            )
        ],
        f"result for {call['name']} {call['input']!r}",
    )


def _recorded_tool_interactions(name, input_contains="", result_contains=""):
    entries = _recorded_session_entries()
    calls = [
        (entry, block)
        for entry in entries
        if entry.get("type") == "assistant"
        for block in _content_blocks(entry)
        if isinstance(block, dict)
        and block.get("type") == "tool_use"
        and block.get("name") == name
        and input_contains in json.dumps(block.get("input"), ensure_ascii=False)
    ]
    pairs = [(entry, _recorded_result_for_call(entries, call)) for entry, call in calls]
    matches = [
        (entry, result)
        for entry, result in pairs
        if result_contains in _result_block_text(_content_block(result, "tool_result"))
    ]
    assert matches, (
        f"Missing {name} activity matching {input_contains!r} / {result_contains!r}"
    )
    return matches


def _recorded_tool_interaction(name, input_contains="", result_contains=""):
    return _require_single_match(
        _recorded_tool_interactions(name, input_contains, result_contains),
        f"{name} activity matching {input_contains!r} / {result_contains!r}",
    )


def _single_tool_call_in(events):
    return _require_single_match(
        [call for event in events for call in event.get("tool_calls", [])], "tool call"
    )


def _assert_assistant_text(events):
    responses = [event["text"] for event in events if event["role"] == "assistant"]
    assert responses
    assert all(isinstance(text, str) for text in responses)
    assert any(text.strip() for text in responses)


def _question_in(call, *, multi_select):
    native = call.get("input", call)
    question = _require_single_match(
        [q for q in native["questions"] if bool(q.get("multiSelect")) == multi_select],
        f"question with multiSelect={multi_select}",
    )
    return _require_single_match(
        [q for q in call["questions"] if q["question"] == question["question"]],
        "question matching its native input",
    )


def _interpret_interaction(entries):
    return eclog.interpret_source(ClaudeSource(Path("session.jsonl"), entries))


def _tool_calls_in(session):
    return [call for event in session["events"] for call in event.get("tool_calls", [])]


def _plan_decisions_in(session):
    return [
        call["plan_decision"]
        for call in _tool_calls_in(session)
        if "plan_decision" in call
    ]


def _recorded_multiselect_interaction():
    matches = []
    for assistant, result in _recorded_tool_interactions("AskUserQuestion"):
        call = _content_block(assistant, "tool_use")
        questions = call["input"]["questions"]
        if any(question.get("multiSelect") for question in questions):
            matches.append((assistant, result))
    return _require_single_match(matches, "multiselect activity")


def _recorded_overlapping_options_interaction():
    matches = []
    for assistant, result in _recorded_tool_interactions("AskUserQuestion"):
        call = _content_block(assistant, "tool_use")
        questions = call["input"]["questions"]
        if any(_has_overlapping_labels(question["options"]) for question in questions):
            matches.append((assistant, result))
    return _require_single_match(matches, "overlapping-option activity")


def _has_overlapping_labels(options):
    labels = [option["label"] for option in options]
    return any(
        longer != shorter and longer.startswith(shorter)
        for shorter in labels
        for longer in labels
    )


def _assert_answer_uses_distinct_recorded_options(question):
    assert isinstance(question["answer_text"], str)
    assert question["answer_text"].strip()
    labels = question["chosen_labels"]
    assert isinstance(labels, list)
    assert all(isinstance(label, str) and label for label in labels)
    assert len(labels) == len(set(labels))
    assert set(labels) <= {option["label"] for option in question["options"]}


def test_native_skill_and_command_invocations_are_retained():
    entries = _recorded_session_entries()
    session = _interpret_interaction(entries)
    assert session["skills_used"] == ["capture-leaf"]
    calls = [call for call in _tool_calls_in(session) if call["name"] == "Skill"]
    assert len(calls) >= 2
    assert {call["skill"] for call in calls} == {"capture-leaf"}
    command = _require_single_match(
        [
            event
            for event in session["events"]
            if event.get("command") == "/capture-echo"
        ],
        "capture-echo command",
    )
    assert command["command_args"] == 'CAPTURE_ARGS hello, "café 雪" — two words!'
    assert any("/capture-echo" in event["text"] for event in build_events(entries))


def test_single_choice():
    assistant, result = _recorded_overlapping_options_interaction()
    question = _require_single_match(
        _content_block(assistant, "tool_use")["input"]["questions"], "single question"
    )
    value, chosen = _parse_chosen(
        question["question"],
        question["options"],
        _content_block(result, "tool_result")["content"],
    )
    assert isinstance(value, str)
    assert value.strip()
    assert len(chosen) == 1
    assert chosen[0] in question["options"]
    assert value == chosen[0]["label"]


def test_native_multiselect_answer():
    entries = _recorded_multiselect_interaction()

    session = _interpret_interaction(entries)

    call = _single_tool_call_in(session["events"])
    answer = _question_in(call, multi_select=True)
    assert len(answer["chosen_labels"]) >= 2
    _assert_answer_uses_distinct_recorded_options(answer)


def test_parse_chosen_comma_separated_labels():
    # Controlled parser primitive: native captures may use any option labels.
    options = [{"label": label} for label in ("First", "Second", "Third")]
    value, chosen = _parse_chosen(
        "Choose items", options, '"Choose items"="First, Third"'
    )
    assert value == "First, Third"
    assert [option["label"] for option in chosen] == ["First", "Third"]


@pytest.mark.parametrize("labels", [("Plan", "Planet"), ("North", "Northwest")])
def test_parse_chosen_overlapping_labels_selects_only_the_complete_label(labels):
    # Supply the overlap and answer explicitly instead of pinning native wording.
    shorter, longer = labels
    options = [{"label": label} for label in labels]
    value, chosen = _parse_chosen("Choose one", options, f'"Choose one"="{longer}"')
    assert shorter in longer
    assert value == longer
    assert [option["label"] for option in chosen] == [longer]


def test_permission_denial_bare():
    _, result = _recorded_tool_interaction("Write", "denied-feedback.txt")
    assert (
        parse_permission_denial(_content_block(result, "tool_result")["content"]) == ""
    )


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


def test_native_settings_denial_is_recorded():
    entries = _recorded_tool_interaction("Bash", "CAPTURE_SETTINGS_REFUSAL")
    _, result_entry = entries
    result = _content_block(result_entry, "tool_result")["content"]
    assert isinstance(result, str)
    assert result.strip()
    assert parse_permission_denial(result) == ""
    interpreted_session = _interpret_interaction(entries)
    denial = _require_single_match(
        [
            call["permission_denial"]
            for call in _tool_calls_in(interpreted_session)
            if "permission_denial" in call
        ],
        "settings denial",
    )
    assert denial["tool"].split(" — ")[0] == "Bash"
    assert denial["by"] == "settings"


def test_permission_denial_ignores_isc_license_text():
    assert (
        parse_permission_denial("Permission to use, copy, modify, and/or distribute")
        is None
    )


def test_permission_denials_from_result_names_tool():
    assistant, result = _recorded_tool_interaction("Write", "denied-feedback.txt")
    call = _content_block(assistant, "tool_use")
    name = "Write — " + call["input"]["file_path"]
    assert permission_denials_from_result(result, {call["id"]: name}) == [
        {"tool": name, "message": "", "by": "user"}
    ]


def test_custom_answer_matches_no_option():
    entries = _recorded_tool_interaction(
        "AskUserQuestion", result_contains="CAPTURE_CUSTOM_PLAIN_MOSS"
    )

    session = _interpret_interaction(entries)

    call = _single_tool_call_in(session["events"])
    question = _question_in(call, multi_select=False)
    assert question["answer_text"] == "CAPTURE_CUSTOM_PLAIN_MOSS"
    assert question["chosen_labels"] == []


def test_multi_question_anchors_on_its_own_text():
    entries = _recorded_multiselect_interaction()
    interpreted_session = _interpret_interaction(entries)
    call = _single_tool_call_in(interpreted_session["events"])
    answer = _question_in(call, multi_select=True)
    _assert_answer_uses_distinct_recorded_options(answer)
    assert len(answer["chosen_labels"]) >= 2
    answer = _question_in(call, multi_select=False)
    _assert_answer_uses_distinct_recorded_options(answer)
    assert len(answer["chosen_labels"]) == 1
    assert answer["answer_text"] == answer["chosen_labels"][0]


def test_task_notification_is_stripped_to_empty():
    entry = _recorded_user_message("<task-notification>")
    assert clean_user_text(_human_text(entry)) == ""


def test_bash_output_is_stripped_to_empty():
    entry = _recorded_user_message("<bash-stdout>CAPTURE_USER_STDOUT_1")
    assert clean_user_text(entry["message"]["content"]) == ""


def test_bash_input_survives_cleaning_for_detection():
    entry = _recorded_user_message("<bash-input>printf")
    text = entry["message"]["content"]
    assert clean_user_text(text) == text
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
    assert (
        encode_project_path(PureWindowsPath(r"C:\Users\me\proj")) == "C--Users-me-proj"
    )
    assert encode_project_path(PurePosixPath("/Users/me/proj")) == "-Users-me-proj"


def test_build_events_inlines_pasted_images_as_base64():
    entries = [
        _recorded_user_message(marker)
        for marker in (
            "CAPTURE_IMAGES_SAME:",
            "CAPTURE_IMAGES_LATER_GIF:",
            "CAPTURE_IMAGES_LATER_WEBP:",
        )
    ]
    events = build_events(entries)
    assert [event["role"] for event in events] == ["user"] * 3
    assert [len(event["images"]) for event in events] == [2, 1, 1]
    assert any("CAPTURE_IMAGES_SAME" in event["text"] for event in events)
    # Claude 2.1.312/314 converts all four source formats to PNG on attachment.
    assert [image["media_type"] for event in events for image in event["images"]] == [
        "image/png"
    ] * 4
    assert [image["data"] for event in events for image in event["images"]] == [
        block["source"]["data"]
        for entry in entries
        for block in entry["message"]["content"]
        if block["type"] == "image"
    ]


def test_build_events_attaches_tool_results_to_their_call():
    events = build_events(
        _recorded_tool_interaction("Read", "multibyte.txt"), tool_result_max_bytes=100
    )
    call = _single_tool_call_in(events)
    assert call["name"] == "Read"
    assert call["input"]["file_path"] == "/tmp/CAPTURE_ROOT/workspace/multibyte.txt"
    assert call["result_bytes"] > 10_000
    assert call["result_truncated"] is True
    assert 0 < len(call["result"].encode()) <= 100


def test_large_native_result_reaches_envelope_size_warning():
    events = build_events(
        _recorded_tool_interaction("Read", "multibyte.txt"),
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


def test_build_events_keeps_full_tool_input():
    entries = _recorded_session_entries()
    calls = [
        call for event in build_events(entries) for call in event.get("tool_calls", [])
    ]
    paths = {
        call["input"]["file_path"]
        for call in calls
        if "nested space 雪" in call["input"].get("file_path", "")
    }
    long_path = _require_single_match(paths, "generated long path")
    assert len(long_path) > 100
    assert long_path.endswith("greeting.py")
    writes = [
        call["input"]
        for call in calls
        if call["name"] == "Write"
        and Path(call["input"]["file_path"]).name == "large-input.txt"
    ]
    assert writes
    assert all(len(payload["content"].encode()) > 4096 for payload in writes)
    assert writes == [
        block["input"]
        for entry in entries
        if entry.get("type") == "assistant"
        for block in _content_blocks(entry)
        if isinstance(block, dict)
        and block.get("type") == "tool_use"
        and block.get("name") == "Write"
        and Path(block["input"]["file_path"]).name == "large-input.txt"
    ]


def test_messages_skip_sidechain_and_meta():
    entries = _recorded_session_entries()
    sidechain = load_entries(SUBAGENT)
    assert any(entry.get("isMeta") for entry in entries)
    assert any(entry.get("isSidechain") for entry in sidechain)
    expected = build_events([entry for entry in entries if not entry.get("isMeta")])
    session = _interpret_interaction(entries + sidechain)
    assert build_events(entries + sidechain) == expected
    assert session["subagent_count"] == 1
    assert "interactions" not in session


def test_build_events_surfaces_slash_command_invocations():
    entry = _recorded_user_message("<command-name>/capture-echo</command-name>")
    event = _require_single_match(build_events([entry]), "command event")
    assert event["text"] == '/capture-echo CAPTURE_ARGS hello, "café 雪" — two words!'
    assert event["role"] == "user"


def test_plan_approved_is_captured():
    entries = _recorded_tool_interaction(
        "ExitPlanMode", result_contains="## Approved Plan:\n"
    )
    interpreted_session = _interpret_interaction(entries)
    (plan,) = _plan_decisions_in(interpreted_session)
    assert isinstance(plan["plan"], str)
    assert plan["plan"].strip()
    assert plan["decision"] == "approved"
    assert plan["edited_plan"] == ""


def test_plan_approved_with_edit_keeps_final_version():
    entries = _recorded_tool_interaction(
        "ExitPlanMode", result_contains="(edited by user):"
    )
    interpreted_session = _interpret_interaction(entries)
    (plan,) = _plan_decisions_in(interpreted_session)
    assert plan["decision"] == "approved"
    assert plan["plan"] == (ROOT / "assets/plan-before-editor.md").read_text()
    assert (
        plan["edited_plan"]
        == (ROOT / "assets/plan-after-editor.md").read_text().rstrip()
    )


def test_plan_rejected_keeps_steering_message():
    entries = _recorded_tool_interaction(
        "ExitPlanMode", result_contains="CAPTURE_PLAN_REJECT_1:"
    )
    interpreted_session = _interpret_interaction(entries)
    (call,) = _tool_calls_in(interpreted_session)
    plan = call["plan_decision"]
    assert plan["decision"] == "rejected"
    assert (
        plan["message"]
        == "CAPTURE_PLAN_REJECT_1: print CAPTURE_PLAN_REVISED instead of CAPTURE_PLAN_HELLO. Keep everything else tiny and local."
    )
    assert "permission_denial" not in call


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
    interpreted_session = _interpret_interaction(entries)
    plans = _plan_decisions_in(interpreted_session)
    (plan,) = plans
    assert plan["decision"] == "rejected"
    assert (
        plan["message"] == "wrong table, use line_items\nand keep it in one migration"
    )


def test_plan_without_result_is_marked_undecided():
    assistant, _ = _recorded_tool_interaction(
        "ExitPlanMode", result_contains="## Approved Plan:\n"
    )
    interpreted_session = _interpret_interaction([assistant])
    (plan,) = _plan_decisions_in(interpreted_session)
    assert plan["decision"] == "no decision recorded"


def test_plan_decision_unknown_for_unrecognized_content():
    assert parse_plan_decision("just some other result text") == ("unknown", "", "")
    assert parse_plan_decision(None) == ("unknown", "", "")


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
    assert resolve_project_dir("/some/explicit", Path("/cwd")) == Path("/some/explicit")


def test_resolve_project_dir_matches_parent_unless_strict():
    with _patched_projects_root() as root:
        cwd = Path("/Users/me/work/app")
        parent_encoded = encode_project_path(Path("/Users/me/work"))
        (root / parent_encoded).mkdir(parents=True)
        assert resolve_project_dir(None, cwd, strict=False) == root / parent_encoded
        assert resolve_project_dir(None, cwd, strict=True) is None


def test_jsonl_files_missing_dir_is_empty():
    assert _jsonl_files(Path(tempfile.mkdtemp()) / "nope") == []


def test_newest_empty_and_picks_latest_mtime():
    assert newest([]) is None
    tmp = Path(tempfile.mkdtemp())
    a, b = tmp / "a.jsonl", tmp / "b.jsonl"
    a.write_bytes(TRANSCRIPT.read_bytes())
    b.write_bytes(TRANSCRIPT.read_bytes())
    os.utime(a, (1, 1))
    os.utime(b, (1000, 1000))
    assert newest([a, b]) == b


def test_select_transcript_explicit_transcript_wins():
    f = Path(tempfile.mkdtemp()) / "a.jsonl"
    f.write_bytes(TRANSCRIPT.read_bytes())
    assert select_transcript(None, str(f), None) == f


def test_select_transcript_explicit_transcript_missing_raises():
    with pytest.raises(FileNotFoundError):
        select_transcript(None, "/no/such/file.jsonl", None)


def test_select_transcript_selector_as_file_path():
    f = Path(tempfile.mkdtemp()) / "sess.jsonl"
    f.write_bytes(TRANSCRIPT.read_bytes())
    assert select_transcript(str(f), None, None) == f


def test_select_transcript_by_session_id_exact_and_suffix():
    proj = Path(tempfile.mkdtemp())
    (proj / "abc123.jsonl").write_bytes(TRANSCRIPT.read_bytes())
    assert select_transcript("abc123", None, str(proj)) == proj / "abc123.jsonl"
    (proj / "xyz").write_bytes(TRANSCRIPT.read_bytes())
    assert select_transcript("xyz", None, str(proj)) == proj / "xyz"


def test_select_transcript_prefix_match_unique_and_ambiguous():
    proj = Path(tempfile.mkdtemp())
    (proj / "sess-aaa.jsonl").write_bytes(TRANSCRIPT.read_bytes())
    assert select_transcript("sess-aaa", None, str(proj)) == proj / "sess-aaa.jsonl"
    (proj / "sess-bbb.jsonl").write_bytes(TRANSCRIPT.read_bytes())
    assert select_transcript("sess-a", None, str(proj)) == proj / "sess-aaa.jsonl"

    proj2 = Path(tempfile.mkdtemp())
    (proj2 / "sess-aaa.jsonl").write_bytes(TRANSCRIPT.read_bytes())
    (proj2 / "sess-aab.jsonl").write_bytes(TRANSCRIPT.read_bytes())
    with pytest.raises(FileNotFoundError) as exc_info:
        select_transcript("sess-aa", None, str(proj2))

    assert "ambiguous" in str(exc_info.value)


def test_select_transcript_selector_not_found_in_project_dir():
    proj = Path(tempfile.mkdtemp())  # no .jsonl files at all -- newest() yields None
    with pytest.raises(FileNotFoundError) as exc_info:
        select_transcript("missing-id", None, str(proj))

    assert "missing-id" in str(exc_info.value) and str(proj) in str(exc_info.value)


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
    assert select_transcript(None, None, str(proj)) == b


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
        assert select_transcript(None, None, None, strict=False) == f


# --------------------------------------------------------------------------- #
# JSONL loading
# --------------------------------------------------------------------------- #


def test_load_entries_skips_malformed_lines_with_warning(tmp_path, capsys):
    path = tmp_path / "truncated.jsonl"
    entries = _recorded_overlapping_options_interaction()
    _write_jsonl(path, entries)
    # A completed native capture cannot supply a corrupt trailing line.
    with path.open("a") as stream:
        stream.write('{"partial')
    assert load_entries(path) == list(entries)
    assert "skipping malformed JSON on line 3" in capsys.readouterr().err


def test_load_entries_counts_non_dict_and_skips_blank_lines():
    tmp = Path(tempfile.mkdtemp()) / "t.jsonl"
    tmp.write_text('{"type": "user"}\n\n42\n[]\n' + "bad\n" * 4)
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        entries = load_entries(tmp)
    assert [e["type"] for e in entries] == ["user"]
    assert "skipped 6 malformed/blank lines total" in buf.getvalue()


def test_load_entries_warns_once_for_many_bad_lines():
    tmp = Path(tempfile.mkdtemp()) / "t.jsonl"
    tmp.write_text("bad\n" * 7)
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        entries = load_entries(tmp)
    assert entries == []
    assert "skipped 7 malformed/blank lines total" in buf.getvalue()


# --------------------------------------------------------------------------- #
# Content-block / descriptor / result-note helpers
# --------------------------------------------------------------------------- #


def test_content_blocks_handles_none_and_non_list_content():
    entry = _recorded_user_message("Remember **CAPTURE_CONTEXT_CEDAR_73**")
    assert _content_blocks(entry) == [
        {"type": "text", "text": entry["message"]["content"]}
    ]
    # Malformed content containers cannot be obtained from the native capture.
    entry["message"]["content"] = None
    assert _content_blocks(entry) == []
    entry["message"]["content"] = {"weird": True}
    assert _content_blocks(entry) == []


def test_tool_descriptor_shell_search_and_fallback():
    assistant, _ = _recorded_tool_interaction("Bash", "CAPTURE_BASH_OK")
    call = _content_block(assistant, "tool_use")
    descriptor = tool_descriptor(call["name"], call["input"])
    assert isinstance(descriptor, str)
    assert descriptor.strip()
    # The current CLI has no Grep tool; retain its legacy descriptor boundary.
    assert tool_descriptor("Grep", {"pattern": "foo"}) == "foo"
    assert tool_descriptor("X", {"odd": 1}) == '{"odd": 1}'
    assert tool_descriptor("X", None) == ""
    assert tool_descriptor("X", "raw string input") == "raw string input"


def test_extract_file_path_variants():
    assistant, _ = _recorded_tool_interaction("Read", "multibyte.txt")
    read_input = _content_block(assistant, "tool_use")["input"]
    assert extract_file_path(read_input) == "/tmp/CAPTURE_ROOT/workspace/multibyte.txt"
    assistant, _ = _recorded_tool_interaction("NotebookEdit", "sample.ipynb")
    notebook_input = _content_block(assistant, "tool_use")["input"]
    assert (
        extract_file_path(notebook_input) == "/tmp/CAPTURE_ROOT/workspace/sample.ipynb"
    )
    assert extract_file_path({}) is None
    assert extract_file_path("nope") is None


def test_option_qa_from_result_builds_record():
    assistant, result = _recorded_overlapping_options_interaction()
    call = _content_block(assistant, "tool_use")
    out = option_qa_from_result(result, {call["id"]: call["input"]["questions"]})
    answer = _require_single_match(out, "choice answer")
    _assert_answer_uses_distinct_recorded_options(answer)
    assert len(answer["chosen_labels"]) == 1
    assert answer["answer_text"] == answer["chosen_labels"][0]
    native = _require_single_match(call["input"]["questions"], "native question")
    assert answer["header"] == native["header"]


def test_option_qa_from_result_ignores_untracked_tool():
    _, result = _recorded_overlapping_options_interaction()
    assert option_qa_from_result(result, {}) == []


def test_option_qa_from_result_skips_non_tool_result_blocks_and_bad_questions():
    assistant, result = _recorded_overlapping_options_interaction()
    call = _content_block(assistant, "tool_use")
    pending = {call["id"]: ["not-a-dict"]}
    # Replace only the question payload with malformed data; include a native text block.
    text = _recorded_user_message("CAPTURE_IMAGES_SAME:")
    result["message"]["content"].insert(0, _content_block(text, "text"))
    assert option_qa_from_result(result, pending) == []


def test_is_tool_result_entry_true_for_bare_tool_use_result():
    _, entry = _recorded_tool_interaction("Bash", "CAPTURE_BASH_OK")
    # Legacy top-level result shape: remove only the result block carrying its ID.
    entry["message"]["content"] = []
    assert entry["toolUseResult"]
    assert _is_tool_result_entry(entry) is True


def test_human_text_includes_plain_string_blocks():
    entry = _recorded_user_message("Remember **CAPTURE_CONTEXT_CEDAR_73**")
    text = entry["message"]["content"]
    # Legacy content accepts raw strings inside a block list.
    entry["message"]["content"] = [text]
    assert _human_text(entry) == text


def test_clean_user_text_empty_input_is_empty():
    assert clean_user_text("") == ""
    assert clean_user_text(None) == ""


def test_result_block_text_flattens_list_content():
    _, result = _recorded_tool_interaction("Agent")
    block = _content_block(result, "tool_result")
    original = _require_single_match(
        [part["text"] for part in block["content"] if part.get("type") == "text"],
        "result text",
    )
    assert _result_block_text(block) == original
    # Repeat the captured text block to exercise multi-block flattening.
    block["content"].append(
        deepcopy(_require_single_match(block["content"], "result content block"))
    )
    assert _result_block_text(block) == original + " " + original


def test_parse_chosen_single_question_fallback_when_text_mismatches():
    assistant, result = _recorded_overlapping_options_interaction()
    question = _require_single_match(
        _content_block(assistant, "tool_use")["input"]["questions"], "single question"
    )
    # Only the lookup text differs, exercising the single-question fallback.
    value, chosen = _parse_chosen(
        "Nomatch", question["options"], _content_block(result, "tool_result")["content"]
    )
    assert isinstance(value, str)
    assert value.strip()
    assert len(chosen) == 1
    assert chosen[0] in question["options"]
    assert value == chosen[0]["label"]


def test_apply_plan_decisions_ignores_non_tool_result_blocks():
    entry = _recorded_user_message("CAPTURE_IMAGES_SAME:")
    assert apply_plan_decisions(entry, {}) is False


def test_permission_denials_from_result_ignores_non_tool_result_blocks():
    entry = _recorded_user_message("CAPTURE_IMAGES_SAME:")
    assert permission_denials_from_result(entry, {}) == []


def test_legacy_plan_helper_consumes_native_matches_and_updates_plan_records():
    entries = (
        list(
            _recorded_tool_interaction(
                "ExitPlanMode", result_contains="CAPTURE_PLAN_REJECT_1:"
            )
        )
        + list(
            _recorded_tool_interaction(
                "ExitPlanMode", result_contains="## Approved Plan:\n"
            )
        )
        + list(
            _recorded_tool_interaction(
                "ExitPlanMode", result_contains="(edited by user):"
            )
        )
    )
    interpreted_session = _interpret_interaction(entries)
    calls = _tool_calls_in(interpreted_session)
    records = [
        {
            "plan": call["input"]["plan"],
            "decision": "no decision recorded",
            "edited_plan": "",
            "message": "",
        }
        for call in calls
    ]
    pending = {call["id"]: record for call, record in zip(calls, records, strict=True)}
    assert sum(apply_plan_decisions(entry, pending) for entry in entries) == 3
    assert pending == {}
    assert [record["decision"] for record in records] == [
        "rejected",
        "approved",
        "approved",
    ]
    assert records[1]["edited_plan"] == ""
    assert (
        records[2]["edited_plan"]
        == (ROOT / "assets/plan-after-editor.md").read_text().rstrip()
    )
    assert records[0]["message"].startswith("CAPTURE_PLAN_REJECT_1:")


@pytest.mark.parametrize("tool_id", [None, "unknown-call"])
def test_legacy_plan_helper_leaves_unmatched_plans_pending(tool_id):
    entries = _recorded_tool_interaction(
        "ExitPlanMode", result_contains="## Approved Plan:\n"
    )
    result = next(entry for entry in entries if _is_tool_result_entry(entry))
    # Change only the captured result ID to exercise absent/unmatched associations.
    _content_block(result, "tool_result")["tool_use_id"] = tool_id
    interpreted_session = _interpret_interaction(entries)
    pending = {
        call["id"]: call["plan_decision"]
        for call in _tool_calls_in(interpreted_session)
    }
    original = deepcopy(pending)
    assert apply_plan_decisions(result, pending) is False
    assert pending == original


@pytest.mark.parametrize(
    ("fixture", "expected"),
    [
        (("Read", "multibyte.txt"), []),
        (
            ("Bash", "CAPTURE_SETTINGS_REFUSAL"),
            [{"tool": "Bash", "message": "", "by": "settings"}],
        ),
    ],
)
def test_legacy_denial_helper_matches_native_result_blocks(fixture, expected):
    entries = _recorded_tool_interaction(*fixture)
    interpreted_session = _interpret_interaction(entries)
    tool_names = {
        call["id"]: call["name"] for call in _tool_calls_in(interpreted_session)
    }
    denials = [
        denial
        for entry in entries
        for denial in permission_denials_from_result(entry, tool_names)
    ]
    assert denials == expected


def test_activity_keeps_commands_shell_subagents_questions_and_files():
    entries = _recorded_session_entries() + load_entries(SUBAGENT)
    session = _interpret_interaction(entries)
    assert any(event.get("command") == "/capture-echo" for event in session["events"])
    assert any(
        "CAPTURE_USER_STDOUT_1" in event.get("shell_command", "")
        for event in session["events"]
    )
    assert session["subagent_count"] > 0
    assert any(path.endswith("greeting.py") for path in session["files_changed"])
    calls = _tool_calls_in(session)
    question = _require_single_match(
        [
            call
            for call in calls
            if call["name"] == "AskUserQuestion"
            and any(q.get("multiSelect") for q in call["input"]["questions"])
        ],
        "tree question call",
    )
    answer = _question_in(question, multi_select=True)
    _assert_answer_uses_distinct_recorded_options(answer)
    assert len(answer["chosen_labels"]) >= 2
    assert any(call["name"] == "Write" for call in calls)
    denial = _require_single_match(
        [
            call["permission_denial"]
            for call in calls
            if call["name"] == "Write"
            and "denied-feedback.txt" in json.dumps(call["input"])
        ],
        "silent write denial",
    )
    assert denial["by"] == "user"
    assert denial["message"] == ""


def test_build_events_skips_non_dict_assistant_blocks_and_unmatched_calls():
    entries = [
        {"type": "summary"},
        {
            "type": "assistant",
            "timestamp": None,
            "message": {"content": ["x"]},  # non-dict block, skipped
        },
        {
            "type": "user",
            "timestamp": None,
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": "unknown", "content": "x"}
                ]
            },
        },
    ]
    # No text/tool calls ever accumulate, and the orphan tool_result has no
    # matching pending call, so no events are produced.
    assert build_events(entries) == []


# --------------------------------------------------------------------------- #
# Envelope metadata
# --------------------------------------------------------------------------- #


def test_first_timestamp_comes_from_the_native_capture():
    entries = _recorded_session_entries()
    assert first_timestamp(entries) in {
        entry["timestamp"] for entry in entries if entry.get("timestamp")
    }


def test_session_cwd_comes_from_the_native_capture():
    assert session_cwd(_recorded_session_entries()) == "/tmp/CAPTURE_ROOT/workspace"


def test_synthetic_model_is_excluded():
    assistant, _ = _recorded_tool_interaction("Read", "multibyte.txt")
    assistant["message"]["model"] = "<synthetic>"
    assert session_models([assistant]) == []


def test_metadata_is_absent_when_not_recorded():
    assistant, _ = _recorded_tool_interaction("Read", "multibyte.txt")
    assistant["timestamp"] = None
    assistant.pop("cwd", None)
    assert first_timestamp([assistant]) is None
    assert session_cwd([assistant]) is None


def test_first_timestamp_skips_messages_without_timestamps():
    assistant, _ = _recorded_tool_interaction("Read", "multibyte.txt")
    assistant["timestamp"] = None
    user = _recorded_user_message("Remember **CAPTURE_CONTEXT_CEDAR_73**")
    user["timestamp"] = "2026-01-01T00:00:00Z"
    assert first_timestamp([assistant, user]) == "2026-01-01T00:00:00Z"


def test_empty_session_has_no_metadata():
    assert first_timestamp([]) is None
    assert session_models([]) == []
    assert session_cwd([]) is None


def test_native_resumed_session_lists_multiple_models():
    entries = _recorded_session_entries()
    models = session_models(entries)
    assert len(models) >= 2
    assert all(isinstance(model, str) and model for model in models)
    assert len(models) == len(set(models))
    assert any(
        event.get("command") == "/model"
        for event in _interpret_interaction(entries)["events"]
    )


def test_native_continued_conversation_keeps_follow_up_in_one_session():
    entries = _recorded_session_entries()
    assert {entry["sessionId"] for entry in entries if "sessionId" in entry} == {
        TRANSCRIPT.stem
    }
    events = _interpret_interaction(entries)["events"]
    prompt = _require_single_match(
        [
            event
            for event in events
            if event["role"] == "user"
            and event["text"].startswith("CAPTURE_POST_COMPACT_RECALL:")
        ],
        "post-compaction recall prompt",
    )
    assert "without tools" in prompt["text"]
    _assert_assistant_text(events[prompt["i"] + 1 :])


def test_build_raw_envelope_preserves_metadata_and_events():
    entries = _recorded_session_entries()
    envelope = build_raw_envelope(Path("/wherever/sess123.jsonl"), entries)
    assert envelope["session_id"] == "sess123"
    assert envelope["cwd"] == "/tmp/CAPTURE_ROOT/workspace"
    assert envelope["started_at"] == first_timestamp(entries)
    assert envelope["models"] == session_models(entries)
    assert envelope["events"] == build_events(entries)


def test_build_raw_envelope_returns_none_when_no_events():
    assert build_raw_envelope(Path("/x/sess.jsonl"), []) is None


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
    assert all(isinstance(name, str) and name for name in summary["tool_names"])
    assert len(summary["tool_names"]) <= summary["tool_count"]
    assert isinstance(summary["result"], str)
    assert summary["result"].strip()


def test_summarize_subagent_returns_none_for_empty_file():
    sub = Path(tempfile.mkdtemp()) / "agent-2.jsonl"
    sub.write_text("")
    assert summarize_subagent(sub) is None


def test_load_subagents_missing_dir_is_empty():
    transcript = Path(tempfile.mkdtemp()) / "sess.jsonl"
    assert load_subagents(transcript) == []


def test_load_subagents_reads_sidecar_dir(tmp_path):
    transcript = tmp_path / TRANSCRIPT.name
    shutil.copyfile(TRANSCRIPT, transcript)
    shutil.copytree(SUBAGENT.parent, tmp_path / TRANSCRIPT.stem / "subagents")
    assert load_subagents(transcript) == load_subagents(TRANSCRIPT)
    assert len(load_subagents(transcript)) == 1


def test_native_subagent_summary_retains_evidence_without_guessing_ownership():
    subs = load_subagents(TRANSCRIPT)
    assert len(subs) == 1
    assert isinstance(subs[0]["task"], str)
    assert subs[0]["task"].strip()
    assert isinstance(subs[0]["result"], str)
    assert subs[0]["result"].strip()
    meta = json.loads(SUBAGENT.with_suffix(".meta.json").read_text())
    assert subs[0]["agent_type"] == meta["agentType"]
    task = _require_single_match(
        [
            entry
            for entry in load_entries(SUBAGENT)
            if entry.get("type") == "user"
            and not _is_tool_result_entry(entry)
            and _human_text(entry).strip()
        ],
        "read-only child task",
    )
    assert subs[0]["start_ts"] == task["timestamp"]
    entries = _recorded_session_entries()
    session = _interpret_interaction(entries)
    assert "interactions" not in session
    assert not any("subagents" in event for event in session["events"])


# --------------------------------------------------------------------------- #
# extract_transcript / CLI
# --------------------------------------------------------------------------- #


def _write_jsonl(path, entries):
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")


def test_extract_transcript_raises_for_empty_file(tmp_path):
    transcript = tmp_path / "empty.jsonl"
    transcript.write_text("")
    with pytest.raises(ValueError, match="no parseable JSON entries found"):
        extract_transcript(transcript)


def test_extract_transcript_preserves_native_conversation():
    envelope = extract_transcript(TRANSCRIPT)
    assert envelope["session_id"] == TRANSCRIPT.stem
    prompt = _require_single_match(
        [
            event
            for event in envelope["events"]
            if event["role"] == "user"
            and event["text"].startswith("Remember **CAPTURE_CONTEXT_CEDAR_73**")
        ],
        "context prompt",
    )
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
    assert json.loads(out_file.read_text())["events"] == build_events(
        _recorded_session_entries()
    )
    assert list(out_dir.iterdir()) == [out_file]


def test_main_stdout_outputs_json():
    transcript = Path(tempfile.mkdtemp()) / "s1.jsonl"
    _write_jsonl(transcript, _recorded_session_entries())
    out = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = main(["--transcript", str(transcript), "--output", "-"])
    assert rc == 0
    assert json.loads(out.getvalue())["events"] == build_events(
        _recorded_session_entries()
    )


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
    (proj / "bad.jsonl").write_text("")
    out_dir = Path(tempfile.mkdtemp())
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        rc = _extract_all(proj, str(out_dir))
    assert rc == 1
    assert "skip bad" in err.getvalue()


@pytest.mark.parametrize("explicit_root", [False, True])
@pytest.mark.parametrize("destination", ["stdout", "explicit", "default"])
def test_cli_json_destinations(
    destination, tmp_path, monkeypatch, capsys, explicit_root
):
    transcript = tmp_path / "session.jsonl"
    entries = [_recorded_user_message("CAPTURE_IMAGES_SAME:")]
    # Keep the Markdown preservation probe while retaining recorded image blocks.
    _content_block(_require_single_match(entries, "image message"), "text")["text"] = (
        "# Keep **recorded Markdown**"
    )
    _write_jsonl(transcript, entries)
    output = tmp_path / "output"
    output.mkdir()

    assert not hasattr(eclog, "Turn")
    assert not hasattr(eclog, "build_turns")
    args = ["--transcript", str(transcript), "--tool-result-bytes", "3"]
    if destination == "stdout":
        args += ["--output", "-"]
    elif destination == "explicit":
        args += ["--output", str(output / "custom.json")]
    if not explicit_root:
        monkeypatch.chdir(output)
    assert main(args, project_root=output if explicit_root else None) == 0
    captured = capsys.readouterr()
    if destination == "stdout":
        envelope = json.loads(captured.out)
        assert list(output.iterdir()) == []
    else:
        name = (
            "custom.json"
            if destination == "explicit"
            else "claude_session_log_raw_session.json"
        )
        assert [p.name for p in output.iterdir()] == [name]
        envelope = json.loads((output / name).read_text())
        assert captured.out == ""
        assert "wrote" in captured.err
    assert "using transcript:" in captured.err
    assert envelope["schema"] == "raw-session-log/1"
    assert envelope["session_id"] == "session"
    assert envelope["events"] == build_events(entries, 3)
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
    assert (
        main(
            all_flag + ["--project-dir", str(project)],
            project_root=output if explicit_root else None,
        )
        == 0
    )
    assert sorted(p.name for p in output.iterdir()) == [
        "claude_session_log_raw_a.json",
        "claude_session_log_raw_b.json",
    ]
    for path in output.iterdir():
        assert json.loads(path.read_text())["events"] == build_events(
            _recorded_session_entries()
        )
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
    assert "unrecognized arguments" in capsys.readouterr().err


@pytest.mark.parametrize("limit", [0, 17, -1])
@pytest.mark.parametrize("transcript", [TRANSCRIPT])
def test_cli_preserves_native_payloads(transcript, limit, capsys):
    assert (
        main(
            [
                "--transcript",
                str(transcript),
                "--output",
                "-",
                "--tool-result-bytes",
                str(limit),
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    envelope = json.loads(captured.out)
    entries = load_entries(transcript)
    assert envelope == build_envelope(
        Session(
            harness="claude",
            session_id=transcript.stem,
            events=build_events(entries, limit),
            cwd=session_cwd(entries),
            started_at=first_timestamp(entries),
            models=session_models(entries),
        ),
        tool_result_max_bytes=limit,
    )


@pytest.mark.parametrize("selector", ["path", "prefix", "newest"])
def test_cli_single_selection_paths(selector, tmp_path, capsys):
    transcript = tmp_path / "session-123.jsonl"
    _write_jsonl(transcript, _recorded_session_entries())
    args = ["--project-dir", str(tmp_path), "--output", "-"]
    if selector != "newest":
        args += [str(transcript) if selector == "path" else "session-1"]
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["session_id"] == "session-123"


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
    assert main(args) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "error" in captured.err


@pytest.mark.parametrize("contents", ["", "not JSON\n"])
def test_cli_reports_parse_errors(contents, tmp_path, capsys):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(contents)
    assert main(["--transcript", str(transcript), "--output", "-"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "no parseable JSON entries" in captured.err
    if contents:
        assert "skipping malformed JSON" in captured.err


def test_cli_retains_malformed_line_diagnostics(tmp_path, capsys):
    transcript = tmp_path / "truncated.jsonl"
    entries = _recorded_overlapping_options_interaction()
    _write_jsonl(transcript, entries)
    # Append a deliberately interrupted record to a captured call/result pair.
    with transcript.open("a") as stream:
        stream.write('{"partial')
    assert main(["--transcript", str(transcript), "--output", "-"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["events"] == build_events(entries)
    assert "skipping malformed JSON on line 3" in captured.err


def test_cli_file_keeps_size_warning(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(session_log_envelope, "SIZE_WARN_BYTES", 1)
    output = tmp_path / "session.json"
    assert main(["--transcript", str(TRANSCRIPT), "--output", str(output)]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "session-log uploads are capped" in captured.err
    assert json.loads(output.read_text())["harness"] == "claude"


def test_cli_stdout_keeps_size_warning(monkeypatch, capsys):
    monkeypatch.setattr(session_log_envelope, "SIZE_WARN_BYTES", 1)
    transcript = TRANSCRIPT
    assert main(["--transcript", str(transcript), "--output", "-"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["harness"] == "claude"
    assert "warning: stdout JSON is" in captured.err
    assert "consider a lower --tool-result-bytes" in captured.err
    assert "session-log uploads are capped" in captured.err


@pytest.mark.parametrize("mode", ["single", "batch"])
def test_cli_no_event_input_keeps_existing_envelope_omission(mode, tmp_path, capsys):
    transcript = tmp_path / "session.jsonl"
    _write_jsonl(
        transcript,
        [
            entry
            for entry in _recorded_session_entries()
            if entry.get("type") == "system"
        ],
    )
    output = tmp_path / "output"
    if mode == "single":
        args = ["--transcript", str(transcript), "--output", "-"]
    else:
        args = ["--all", "--project-dir", str(tmp_path), "--output", str(output)]
    assert main(args) == (0 if mode == "single" else 1)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "session: no exportable events" in captured.err
    assert "no JSON written" in captured.err
    assert not list(tmp_path.rglob("*.json"))


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
        assert main(arguments) == 0
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
        entries = _recorded_tool_interaction("Read", "multibyte.txt")
    source = ClaudeSource(Path("/unavailable/session.jsonl"), entries)
    expected_events = build_events(entries, limit)

    def forbidden(*args, **kwargs):
        pytest.fail("interpretation accessed the filesystem")

    with monkeypatch.context() as guard:
        for method in ["open", "is_file", "is_dir", "iterdir", "stat", "read_text"]:
            guard.setattr(Path, method, forbidden)
        session = eclog.interpret_source(source)
        assert session["session_id"] == "session"
        assert (
            session_log_envelope.export_session(session, limit)["events"]
            == expected_events
        )
        assert session["cwd"] == session_cwd(entries)
        assert session["started_at"] == first_timestamp(entries)
        assert session["models"] == session_models(entries)
        assert eclog.interpret_source(ClaudeSource(source.path, [])) is None


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
    assert eclog.summarize_subagent_source(source) == expected
    assert eclog.summarize_subagent_source(ClaudeSource(source.path, [])) is None


@pytest.mark.parametrize("metadata", [None, "bad JSON", "[]", "null", "{}"])
def test_source_reader_metadata_fallback(metadata, tmp_path):
    transcript = tmp_path / "agent.jsonl"
    shutil.copyfile(SUBAGENT, transcript)
    if metadata is not None:
        transcript.with_suffix(".meta.json").write_text(metadata)
    source = read_source(transcript, read_metadata=True)
    assert source.path == transcript
    assert eclog.summarize_subagent_source(source)["agent_type"] == "subagent"


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
    assert read_source(transcript, read_metadata=True).metadata == {}


def test_source_reader_keeps_diagnostics_and_read_errors(tmp_path, capsys):
    transcript = tmp_path / "session.jsonl"
    transcript.write_bytes(b'\n{"type":"user"}\ninvalid\n{"text":"\xff"}\n[]\n')
    source = read_source(transcript)
    assert source.entries == [{"type": "user"}, {"text": "\ufffd"}]
    assert "skipping malformed JSON on line 3" in capsys.readouterr().err
    with pytest.raises(FileNotFoundError):
        read_source(tmp_path / "missing.jsonl")


def test_source_reader_discovers_sidecars_in_filename_order(tmp_path):
    transcript = tmp_path / "session.jsonl"
    assert read_subagent_sources(transcript) == []
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
    assert [source.path.name for source in sources] == ["a.jsonl", "b.jsonl"]
    assert all(source.path.parent == directory for source in sources)
    assert all(source.entries == load_entries(SUBAGENT) for source in sources)


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
            if entry.get("type") == "user":
                entry["message"]["content"] = name
        _write_jsonl(directory / f"{name}.jsonl", entries)
    (directory / "empty.jsonl").touch()
    # A directory at the metadata path must not be read for an empty sidecar.
    (directory / "empty.meta.json").mkdir()
    assert [summary["task"] for summary in load_subagents(transcript)] == [
        "d",
        "b",
        "c",
        "a",
    ]


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
    assert envelope == build_raw_envelope(transcript, entries)


def test_interpretation_retains_full_results_until_shared_export():
    entries = _recorded_tool_interaction("Read", "multibyte.txt")
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
    assert main(["--transcript", str(TRANSCRIPT), "--output", "-"]) == 0
    assert json.loads(capsys.readouterr().out)["events"]
    assert len(seen) == 1


@pytest.mark.parametrize("same_id", [False, True])
def test_repeated_calls_keep_order_and_native_result_associations(same_id):
    assistant, result = _recorded_tool_interaction("Read", "multibyte.txt")
    again = deepcopy(assistant)
    again_result = deepcopy(result)
    first_call = _content_block(assistant, "tool_use")
    second_call = _content_block(again, "tool_use")
    # Repeat the captured Read, changing only identity/output to expose association.
    if not same_id:
        second_call["id"] = "second-call"
    _content_block(again_result, "tool_result")["tool_use_id"] = second_call["id"]
    _content_block(again_result, "tool_result")["content"] = "second output"
    if same_id:
        entries = [assistant, result, again, again_result]
    else:
        entries = [assistant, again, again_result, result]
    session = eclog.interpret_source(ClaudeSource(Path("s.jsonl"), entries))
    assert [e["i"] for e in session["events"]] == [0, 1]
    calls = [e["tool_calls"][0] for e in session["events"]]
    assert [c["name"] for c in calls] == ["Read", "Read"]
    assert calls[0]["input"] == first_call["input"]
    assert "CAPTURE_MULTIBYTE" in calls[0]["result"]
    assert calls[1]["result"] == "second output"
    assert calls[0] is not calls[1]


@pytest.mark.parametrize("result_kind", ["missing", "empty", "null", "error"])
def test_missing_and_empty_results_preserve_claude_json_contract(result_kind):
    assistant, result = _recorded_tool_interaction("Read", "multibyte.txt")
    block = _content_block(result, "tool_result")
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
    assistant, result = _recorded_tool_interaction("Read", "multibyte.txt")
    # Put captured image blocks on result/assistant records: the existing Claude
    # JSON contract includes only human message images; expansion is deferred.
    images = [b for b in image_entries[0]["message"]["content"] if b["type"] == "image"]
    assistant["message"]["content"].extend(deepcopy(images))
    result["message"]["content"].extend(deepcopy(images))
    entries = [image_entries[0], assistant, result, deepcopy(image_entries[0])]
    session = eclog.interpret_source(ClaudeSource(Path("s.jsonl"), entries))
    events = session["events"]
    assert [e["role"] for e in events] == ["user", "assistant", "user"]
    assert [len(e.get("images", [])) for e in events] == [2, 0, 2]
    assert events[0]["images"] == events[2]["images"]
    assert "CAPTURE_MULTIBYTE" in events[1]["tool_calls"][0]["result"]


def test_recorded_skills_deduplicate_and_ignore_empty_names():
    pairs = _recorded_tool_interactions("Skill", "capture-leaf")
    assert len(pairs) >= 2
    entries = [entry for pair in pairs for entry in pair]
    # Add the same empty-name boundary to each native invocation; none is chosen
    # by occurrence, timestamp, or ID when the invocations are semantically equal.
    for assistant, _ in pairs:
        skill = _content_block(assistant, "tool_use")
        empty = deepcopy(skill)
        empty["input"] = {"skill": ""}
        assistant["message"]["content"].append(empty)
    interpreted_session = _interpret_interaction(entries)
    assert interpreted_session["skills_used"] == ["capture-leaf"]
    calls = [
        call for call in _tool_calls_in(interpreted_session) if call["name"] == "Skill"
    ]
    assert len(calls) == 2 * len(pairs)
    assert sum(call.get("skill") == "capture-leaf" for call in calls) == len(pairs)
    assert sum(call.get("skill") is None for call in calls) == len(pairs)


def test_question_stays_with_its_call_across_a_later_user_message():
    assistant, result = _recorded_multiselect_interaction()
    later = _recorded_user_message("Remember **CAPTURE_CONTEXT_CEDAR_73**")
    # Insert a human message between the recorded call and its result.

    session = _interpret_interaction([assistant, later, result])

    call = _single_tool_call_in(session["events"])
    assert call["id"] == _content_block(assistant, "tool_use")["id"]
    assert call["result_record"]["tool_call_id"] == call["id"]
    answer = _question_in(call, multi_select=True)
    assert len(answer["chosen_labels"]) >= 2
    _assert_answer_uses_distinct_recorded_options(answer)
    (user_message,) = [event for event in session["events"] if event["role"] == "user"]
    assert "CAPTURE_CONTEXT_CEDAR_73" in user_message["text"]
    assert "interactions" not in session
    assert "interpretation" not in session


def test_plan_stays_with_its_call_across_a_later_user_message():
    assistant, result = _recorded_tool_interaction(
        "ExitPlanMode", result_contains="## Approved Plan:\n"
    )
    later = _recorded_user_message("Remember **CAPTURE_CONTEXT_CEDAR_73**")
    # Insert a human message between the recorded call and its result.

    session = _interpret_interaction([assistant, later, result])

    call = _single_tool_call_in(session["events"])
    assert call["id"] == _content_block(assistant, "tool_use")["id"]
    assert call["result_record"]["tool_call_id"] == call["id"]
    assert call["plan_decision"]["decision"] == "approved"
    (user_message,) = [event for event in session["events"] if event["role"] == "user"]
    assert "CAPTURE_CONTEXT_CEDAR_73" in user_message["text"]
    assert "interactions" not in session
    assert "interpretation" not in session


def test_permission_denial_stays_with_its_call_across_a_later_user_message():
    assistant, result = _recorded_tool_interaction("Bash", "CAPTURE_SETTINGS_REFUSAL")
    later = _recorded_user_message("Remember **CAPTURE_CONTEXT_CEDAR_73**")
    # Insert a human message between the recorded call and its result.

    session = _interpret_interaction([assistant, later, result])

    call = _single_tool_call_in(session["events"])
    assert call["id"] == _content_block(assistant, "tool_use")["id"]
    assert call["result_record"]["tool_call_id"] == call["id"]
    assert call["permission_denial"]["by"] == "settings"
    (user_message,) = [event for event in session["events"] if event["role"] == "user"]
    assert "CAPTURE_CONTEXT_CEDAR_73" in user_message["text"]
    assert "interactions" not in session
    assert "interpretation" not in session


def test_native_message_and_result_identities_survive_interpretation():
    assistant, result = _recorded_tool_interaction("Read", "multibyte.txt")
    session = _interpret_interaction([assistant, result])
    message = _require_single_match(session["events"], "assistant message")
    assert message["id"] == assistant["uuid"]
    assert message["parent_id"] == assistant["parentUuid"]
    assert message["message_id"] == assistant["message"]["id"]
    record = message["tool_calls"][0]["result_record"]
    assert record["id"] == result["uuid"]
    assert record["ts"] == result["timestamp"]
    assert record["content"] == _content_block(result, "tool_result")["content"]


def test_unmatched_result_retains_content_without_inventing_an_owner():
    assistant, result = _recorded_tool_interaction("Read", "multibyte.txt")
    # Change only the result ID so the captured result has no matching call.
    block = _content_block(result, "tool_result")
    block["tool_use_id"] = "unknown-call"
    session = _interpret_interaction([assistant, result])
    assert _single_tool_call_in(session["events"])["result"] is None
    assert "result_record" not in _single_tool_call_in(session["events"])
    (record,) = session["unmatched_tool_results"]
    assert record["tool_call_id"] == "unknown-call"
    assert record["content"] == block["content"]
    assert record["id"] == result["uuid"]


@pytest.mark.parametrize("options", [None, 7, "invalid", {"label": "section"}])
def test_malformed_question_options_preserve_input_and_allow_extraction(options):
    assistant, result = _recorded_multiselect_interaction()
    entries = [assistant, result]
    block = _content_block(assistant, "tool_use")
    # Vary only the options container in the audited question fixture.
    _question_in(block["input"], multi_select=True)["options"] = options
    session = _interpret_interaction(entries)
    call = _single_tool_call_in(session["events"])
    assert call["input"] == block["input"]
    assert _question_in(call, multi_select=True)["options"] == []
    answer = _question_in(call, multi_select=True)["answer_text"]
    assert isinstance(answer, str)
    assert answer.strip()
    exported = session_log_envelope.export_session(session)
    assert _single_tool_call_in(exported["events"])["input"] == block["input"]


@pytest.mark.parametrize("payload", [{"stdout": "output"}, "output", {}, "", False])
def test_top_level_result_preserves_payload_without_inventing_an_owner(payload):
    assistant, result = _recorded_tool_interaction("Read", "multibyte.txt")
    # Model the older result-only shape by moving the captured result to the
    # top level and removing the block that carries its call ID.
    result["message"]["content"] = []
    result["toolUseResult"] = payload
    session = _interpret_interaction([assistant, result])
    call = _single_tool_call_in(session["events"])
    assert call["result"] is None
    assert "result_record" not in call
    (record,) = session["unmatched_tool_results"]
    assert record["tool_call_id"] is None
    assert record["content"] == payload
    assert record["id"] == result["uuid"]
    assert record["parent_id"] == result["parentUuid"]
    assert record["ts"] == result["timestamp"]
    assert record["is_error"] is False


def test_top_level_result_does_not_duplicate_a_recorded_result_block():
    assistant, result = _recorded_tool_interaction("Read", "multibyte.txt")
    result["toolUseResult"] = {"stdout": "duplicate output"}
    session = _interpret_interaction([assistant, result])
    assert session["unmatched_tool_results"] == []
    assert "CAPTURE_MULTIBYTE" in _single_tool_call_in(session["events"])["result"]


def test_unknown_tool_payload_is_preserved_and_reported(tmp_path, capsys):
    assistant, result = _recorded_tool_interaction("Read", "multibyte.txt")
    # Change only the captured tool name and extend its input to model a plugin.
    block = _content_block(assistant, "tool_use")
    block["name"] = "plugin__DeployService"
    block["input"]["plugin_options"] = {
        "targets": [None, True, 1.5, {"region": "east"}]
    }
    session = _interpret_interaction([assistant, result])
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
    exported_calls = [
        call for event in envelope["events"] for call in event.get("tool_calls", [])
    ]
    assert len(exported_calls) == 2
    assert all(
        call["input"] == block["input"] and "input_schema" not in call
        for call in exported_calls
    )
    diagnostic = capsys.readouterr().err
    assert "unsupported tool input schemas" in diagnostic
    assert diagnostic.count("plugin__DeployService") == 1


def test_question_options_are_checked_without_losing_recorded_payload():
    assistant, result = _recorded_multiselect_interaction()
    entries = [assistant, result]
    block = _content_block(assistant, "tool_use")
    options = _question_in(block["input"], multi_select=True)["options"]
    # Extend a recorded option and introduce malformed known fields. Semantic
    # parsing uses checked options while the complete native input survives.
    assert options
    for option in options:
        option["preview"] = {"markdown": "**keep this**"}
    options.extend([{"label": 1}, {"label": "bad", "description": None}])
    session = _interpret_interaction(entries)
    call = _single_tool_call_in(session["events"])
    assert call["input"] == block["input"]
    assert _question_in(call, multi_select=True)["options"] == options[:-2]
    assert all(
        option["preview"] == {"markdown": "**keep this**"}
        for option in _question_in(call, multi_select=True)["options"]
    )
    answer = _question_in(call, multi_select=True)
    _assert_answer_uses_distinct_recorded_options(answer)
    assert len(answer["chosen_labels"]) >= 2


@pytest.mark.parametrize("name", ["bash", "view", "edit", "create", "skill"])
def test_claude_reports_copilot_tool_names_as_unsupported(name):
    assistant, result = _recorded_tool_interaction("Read", "multibyte.txt")
    # Change only the captured name to the other harness's vocabulary.
    block = _content_block(assistant, "tool_use")
    block["name"] = name
    session = _interpret_interaction([assistant, result])
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
        "ExitPlanMode", result_contains="CAPTURE_PLAN_REJECT_1:"
    )
    _content_block(result, "tool_result")["content"] = text
    return [assistant, result]


def test_legacy_edited_heading_with_unchanged_plan_is_not_an_edit():
    # The earlier audited plan fixture echoed an unchanged plan under this heading.
    assistant, result = _recorded_tool_interaction(
        "ExitPlanMode", result_contains="## Approved Plan:\n"
    )
    block = _content_block(result, "tool_result")
    block["content"] = block["content"].replace(
        "## Approved Plan:", "## Approved Plan (edited by user):"
    )
    interpreted_session = _interpret_interaction([assistant, result])
    (plan,) = _plan_decisions_in(interpreted_session)
    assert plan["decision"] == "approved"
    assert plan["edited_plan"] == ""


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
    sources = [
        block["source"]
        for block in _content_blocks(entry)
        if isinstance(block, dict) and block.get("type") == "image"
    ]
    assert sources
    for source in sources:
        source["media_type"] = media_type
        source["data"] = data
    user = _require_single_match(build_events([entry]), "image event")
    assert len(user["images"]) == len(sources)
    assert all(
        image["media_type"] == media_type and image["data"] == data
        for image in user["images"]
    )


def test_legacy_dont_ask_denial_without_command_is_recorded():
    # Claude 2.1.292 omitted the command from its settings denial preamble.
    assistant, result = _recorded_tool_interaction("Bash", "CAPTURE_SETTINGS_REFUSAL")
    _content_block(result, "tool_result")["content"] = (
        "Permission to use Bash has been denied."
    )
    interpreted_session = _interpret_interaction([assistant, result])
    (call,) = _tool_calls_in(interpreted_session)
    assert call["permission_denial"]["by"] == "settings"
    assert call["permission_denial"]["message"] == ""


def _reword_generated_questions(entries):
    replacements = {}
    for entry in entries:
        for block in _content_blocks(entry):
            if not isinstance(block, dict) or block.get("name") != "AskUserQuestion":
                continue
            for question in block["input"]["questions"]:
                for field in ("question", "header"):
                    text = question[field]
                    replacements[text] = f"Reworded: {text}"
                for option in question["options"]:
                    for field in ("label", "description"):
                        text = option[field]
                        replacements[text] = f"Alternative {text}"
    # One non-cascading pass updates calls, rendered results, and answer maps.
    pattern = "|".join(
        re.escape(text) for text in sorted(replacements, key=len, reverse=True)
    )
    return json.loads(
        re.sub(
            pattern,
            lambda match: replacements[match[0]],
            json.dumps(entries, ensure_ascii=False),
        )
    )


def test_fixture_selection_ignores_order_regenerated_ids_and_prose(monkeypatch):
    entries = _reword_generated_questions(_recorded_session_entries())
    for entry in entries:
        if "uuid" in entry:
            entry["uuid"] = "regenerated-" + entry["uuid"]
        for block in _content_blocks(entry):
            if (
                entry.get("type") == "assistant"
                and isinstance(block, dict)
                and block.get("type") == "text"
            ):
                block["text"] = "A different generated response."
            if isinstance(block, dict) and block.get("type") == "tool_use":
                block["id"] = "regenerated-" + block["id"]
            elif isinstance(block, dict) and block.get("type") == "tool_result":
                block["tool_use_id"] = "regenerated-" + block["tool_use_id"]
        content = entry.get("message", {}).get("content")
        if isinstance(content, list):
            content.reverse()
    entries.reverse()
    native_entries = entries
    monkeypatch.setattr(__name__ + "._recorded_session_entries", lambda: native_entries)
    entries = _recorded_multiselect_interaction()
    interpreted_session = _interpret_interaction(entries)
    answer = _question_in(
        _single_tool_call_in(interpreted_session["events"]), multi_select=True
    )
    _assert_answer_uses_distinct_recorded_options(answer)
    assert len(answer["chosen_labels"]) >= 2
    entries = _recorded_overlapping_options_interaction()
    overlapping_session = _interpret_interaction(entries)
    overlap = _single_tool_call_in(overlapping_session["events"])
    answer = _question_in(overlap, multi_select=False)
    _assert_answer_uses_distinct_recorded_options(answer)
    assert len(answer["chosen_labels"]) == 1
    assert answer["answer_text"] == answer["chosen_labels"][0]
    full_session = _interpret_interaction(native_entries)
    _assert_assistant_text(full_session["events"])
    assistant, result = _recorded_tool_interaction("Read", "multibyte.txt")
    call = _content_block(assistant, "tool_use")
    assert Path(call["input"]["file_path"]).name == "multibyte.txt"
    assert _content_block(result, "tool_result")["tool_use_id"] == call["id"]
    assert "CAPTURE_MULTIBYTE" in _content_block(result, "tool_result")["content"]
    image_entry = _recorded_user_message("CAPTURE_IMAGES_SAME:")
    assert "CAPTURE_IMAGES_SAME:" in _content_block(image_entry, "text")["text"]


@pytest.mark.parametrize("duplicate", [False, True])
def test_fixture_selection_rejects_missing_or_ambiguous_activity(
    duplicate, monkeypatch
):
    assistant, result = _recorded_tool_interaction("Read", "multibyte.txt")
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
