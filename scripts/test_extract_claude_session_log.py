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
import shutil
import tempfile
from copy import deepcopy
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest
from claude_fixture import ROOT, SUBAGENT, TRANSCRIPT, native_entries, tool_pair

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


def _session(entries):
    return eclog.interpret_source(ClaudeSource(Path("session.jsonl"), entries))


def _calls(entries):
    return [
        call
        for event in _session(entries)["events"]
        for call in event.get("tool_calls", [])
    ]


def _plans(entries):
    return [
        call["plan_decision"] for call in _calls(entries) if "plan_decision" in call
    ]


def _question_calls():
    return [
        c
        for c in _calls(native_entries("multiple_questions_and_multiselect"))
        if "questions" in c
    ]


def test_native_skill_and_command_invocations_are_retained():
    entries = native_entries("repeated_skill_invocation", "custom_command_arguments")
    session = _session(entries)
    assert session["skills_used"] == ["capture-leaf"]
    calls = _calls(entries)
    assert [call["skill"] for call in calls] == ["capture-leaf", "capture-leaf"]
    command = next(event for event in session["events"] if event.get("command"))
    assert command["command"] == "/capture-echo"
    assert command["command_args"] == 'CAPTURE_ARGS hello, "café 雪" — two words!'
    assert any("/capture-echo" in event["text"] for event in build_events(entries))


def test_single_choice():
    assistant, result = tool_pair("single_choice_offered_option")
    question = assistant["message"]["content"][0]["input"]["questions"][0]
    value, chosen = _parse_chosen(
        question["question"],
        question["options"],
        result["message"]["content"][0]["content"],
    )
    assert value == "Leaflet"
    assert [option["label"] for option in chosen] == ["Leaflet"]


def test_multiselect_comma():
    qa = _question_calls()[0]["questions"][0]
    assert qa["answer_text"] == "Green, Red"
    assert qa["chosen_labels"] == ["Green", "Red"]


def test_permission_denial_bare():
    _, result = tool_pair("human_denial_silent")
    assert parse_permission_denial(result["message"]["content"][0]["content"]) == ""


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
    entries = native_entries("settings_driven_bash_denial")
    result = next(
        block["content"]
        for entry in entries
        for block in _content_blocks(entry)
        if isinstance(block, dict) and block.get("type") == "tool_result"
    )
    assert (
        result
        == "Permission to use Bash with command printf CAPTURE_SETTINGS_REFUSAL has been denied."
    )
    assert parse_permission_denial(result) == ""
    denials = [
        c["permission_denial"] for c in _calls(entries) if "permission_denial" in c
    ]
    assert [d["tool"].split(" — ")[0] for d in denials] == ["Bash"]
    assert denials[0]["by"] == "settings"


def test_permission_denial_ignores_isc_license_text():
    assert (
        parse_permission_denial("Permission to use, copy, modify, and/or distribute")
        is None
    )


def test_permission_denials_from_result_names_tool():
    assistant, result = tool_pair("human_denial_silent")
    call = assistant["message"]["content"][0]
    name = "Write — " + call["input"]["file_path"]
    assert permission_denials_from_result(result, {call["id"]: name}) == [
        {"tool": name, "message": "", "by": "user"}
    ]


def test_custom_answer_matches_no_option():
    question = _calls(native_entries("custom_text_plain"))[0]["questions"][0]
    assert question["answer_text"] == "CAPTURE_CUSTOM_PLAIN_MOSS"
    assert question["chosen_labels"] == []


def test_multi_question_anchors_on_its_own_text():
    questions = _question_calls()[0]["questions"]
    assert [qa["answer_text"] for qa in questions] == ["Green, Red", "Deciduous"]
    assert questions[1]["chosen_labels"] == ["Deciduous"]


def test_task_notification_is_stripped_to_empty():
    entry = next(
        e
        for e in native_entries()
        if "<task-notification>" in str(e.get("message", {}).get("content", ""))
        and e.get("type") == "user"
    )
    assert clean_user_text(_human_text(entry)) == ""


def test_bash_output_is_stripped_to_empty():
    (entry,) = native_entries("user_shell_stdout_stderr")
    assert clean_user_text(entry["message"]["content"]) == ""


def test_bash_input_survives_cleaning_for_detection():
    (entry,) = native_entries("user_shell_input")
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
    entries = native_entries(
        "same_message_attachments", "later_message_attachment", "source_format_WebP"
    )
    events = build_events(entries)
    assert [event["role"] for event in events] == ["user"] * 3
    assert [len(event["images"]) for event in events] == [2, 1, 1]
    assert "CAPTURE_IMAGES_SAME" in events[0]["text"]
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
        native_entries("multibyte_result_over_10kb"), tool_result_max_bytes=100
    )
    call = events[0]["tool_calls"][0]
    assert call["name"] == "Read"
    assert call["input"]["file_path"] == "/tmp/CAPTURE_ROOT/workspace/multibyte.txt"
    assert call["result_bytes"] > 10_000
    assert call["result_truncated"] is True
    assert len(call["result"].encode()) == 100


def test_large_native_result_reaches_envelope_size_warning():
    events = build_events(
        native_entries("multibyte_result_over_10kb"), tool_result_max_bytes=100_000
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
    entries = native_entries("long_generated_path", "write_input_over_4kb")
    calls = [
        call for event in build_events(entries) for call in event.get("tool_calls", [])
    ]
    long_path = next(
        call["input"]["file_path"]
        for call in calls
        if "nested space 雪" in call["input"].get("file_path", "")
    )
    assert len(long_path) > 100
    assert long_path.endswith("greeting.py")
    large_write = next(
        call
        for call in calls
        if call["name"] == "Write" and "large-input.txt" in call["input"]["file_path"]
    )
    assert len(large_write["input"]["content"].encode()) > 4096
    assert large_write["input"] == next(
        block["input"]
        for entry in entries
        for block in _content_blocks(entry)
        if isinstance(block, dict)
        and block.get("input", {}).get("file_path") == large_write["input"]["file_path"]
    )


def test_messages_skip_sidechain_and_meta():
    entries = native_entries()
    sidechain = load_entries(SUBAGENT)
    assert any(entry.get("isMeta") for entry in entries)
    assert any(entry.get("isSidechain") for entry in sidechain)
    expected = build_events(entries)
    session = _session(entries + sidechain)
    assert build_events(entries + sidechain) == expected
    assert session["subagent_count"] == 1
    assert "interactions" not in session


def test_build_events_surfaces_slash_command_invocations():
    events = build_events(native_entries("custom_command_arguments"))
    assert (
        events[0]["text"] == '/capture-echo CAPTURE_ARGS hello, "café 雪" — two words!'
    )
    assert events[0]["role"] == "user"


def test_plan_approved_is_captured():
    (plan,) = _plans(native_entries("revised_plan_ordinary_approval"))
    assert "CAPTURE_PLAN_REVISED" in plan["plan"]
    assert plan["decision"] == "approved"
    assert plan["edited_plan"] == ""


def test_plan_approved_with_edit_keeps_final_version():
    (plan,) = _plans(native_entries("genuine_editor_edited_plan_approval"))
    assert plan["decision"] == "approved"
    assert plan["plan"] == (ROOT / "assets/plan-before-editor.md").read_text()
    assert (
        plan["edited_plan"]
        == (ROOT / "assets/plan-after-editor.md").read_text().rstrip()
    )


def test_plan_rejected_keeps_steering_message():
    (call,) = _calls(native_entries("plan_reject_written_feedback"))
    plan = call["plan_decision"]
    assert plan["decision"] == "rejected"
    assert (
        plan["message"]
        == "CAPTURE_PLAN_REJECT_1: print CAPTURE_PLAN_REVISED instead of CAPTURE_PLAN_HELLO. Keep everything else tiny and local."
    )
    assert "permission_denial" not in call


def test_plan_rejection_reason_is_captured():
    # The wording the harness actually uses when a plan is sent back in plan mode.
    plans = _plans(
        _plan_result_variant(
            "The user doesn't want to proceed with this tool use. The tool use "
            "was rejected (eg. if it was a file edit, the new_string was NOT "
            "written to the file). STOP what you are doing and wait for the user "
            "to tell you how to proceed. The user provided the following reason "
            "for the rejection:\nwrong table, use line_items\nand keep it in one "
            "migration\n\nNote: The user's next message may contain a correction."
        )
    )
    (plan,) = plans
    assert plan["decision"] == "rejected"
    assert (
        plan["message"] == "wrong table, use line_items\nand keep it in one migration"
    )


def test_plan_without_result_is_marked_undecided():
    assistant, _ = tool_pair("revised_plan_ordinary_approval")
    (plan,) = _plans([assistant])
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
    entries = native_entries("single_choice_offered_option")
    _write_jsonl(path, entries)
    # A completed native capture cannot supply a corrupt trailing line.
    with path.open("a") as stream:
        stream.write('{"partial')
    assert load_entries(path) == entries
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
    (entry,) = native_entries("ordinary_markdown_unicode_context")
    assert _content_blocks(entry) == [
        {"type": "text", "text": entry["message"]["content"]}
    ]
    # Malformed content containers cannot be obtained from the native capture.
    entry["message"]["content"] = None
    assert _content_blocks(entry) == []
    entry["message"]["content"] = {"weird": True}
    assert _content_blocks(entry) == []


def test_tool_descriptor_shell_search_and_fallback():
    assistant, _ = tool_pair("successful_shell")
    call = assistant["message"]["content"][0]
    assert tool_descriptor(call["name"], call["input"]) == "printf CAPTURE_BASH_OK"
    # The current CLI has no Grep tool; retain its legacy descriptor boundary.
    assert tool_descriptor("Grep", {"pattern": "foo"}) == "foo"
    assert tool_descriptor("X", {"odd": 1}) == '{"odd": 1}'
    assert tool_descriptor("X", None) == ""
    assert tool_descriptor("X", "raw string input") == "raw string input"


def test_extract_file_path_variants():
    assistant, _ = tool_pair("multibyte_result_over_10kb")
    read_input = assistant["message"]["content"][0]["input"]
    assert extract_file_path(read_input) == "/tmp/CAPTURE_ROOT/workspace/multibyte.txt"
    assistant, _ = tool_pair("native_notebook_edit")
    notebook_input = assistant["message"]["content"][0]["input"]
    assert (
        extract_file_path(notebook_input) == "/tmp/CAPTURE_ROOT/workspace/sample.ipynb"
    )
    assert extract_file_path({}) is None
    assert extract_file_path("nope") is None


def test_option_qa_from_result_builds_record():
    assistant, result = tool_pair("single_choice_offered_option")
    call = assistant["message"]["content"][0]
    out = option_qa_from_result(result, {call["id"]: call["input"]["questions"]})
    assert out[0]["chosen_labels"] == ["Leaflet"]
    assert out[0]["header"] == "Choice"


def test_option_qa_from_result_ignores_untracked_tool():
    _, result = tool_pair("single_choice_offered_option")
    assert option_qa_from_result(result, {}) == []


def test_option_qa_from_result_skips_non_tool_result_blocks_and_bad_questions():
    assistant, result = tool_pair("single_choice_offered_option")
    call = assistant["message"]["content"][0]
    pending = {call["id"]: ["not-a-dict"]}
    # Replace only the question payload with malformed data; include a native text block.
    (text,) = native_entries("same_message_attachments")
    result["message"]["content"].insert(0, text["message"]["content"][0])
    assert option_qa_from_result(result, pending) == []


def test_is_tool_result_entry_true_for_bare_tool_use_result():
    _, entry = tool_pair("successful_shell")
    # Legacy top-level result shape: remove only the result block carrying its ID.
    entry["message"]["content"] = []
    assert entry["toolUseResult"]
    assert _is_tool_result_entry(entry) is True


def test_human_text_includes_plain_string_blocks():
    (entry,) = native_entries("ordinary_markdown_unicode_context")
    text = entry["message"]["content"]
    # Legacy content accepts raw strings inside a block list.
    entry["message"]["content"] = [text]
    assert _human_text(entry) == text


def test_clean_user_text_empty_input_is_empty():
    assert clean_user_text("") == ""
    assert clean_user_text(None) == ""


def test_result_block_text_flattens_list_content():
    _, result = tool_pair("one_read_only_subagent")
    block = result["message"]["content"][0]
    original = block["content"][0]["text"]
    assert _result_block_text(block) == original
    # Repeat the captured text block to exercise multi-block flattening.
    block["content"].append(deepcopy(block["content"][0]))
    assert _result_block_text(block) == original + " " + original


def test_parse_chosen_single_question_fallback_when_text_mismatches():
    assistant, result = tool_pair("single_choice_offered_option")
    question = assistant["message"]["content"][0]["input"]["questions"][0]
    # Only the lookup text differs, exercising the single-question fallback.
    value, chosen = _parse_chosen(
        "Nomatch", question["options"], result["message"]["content"][0]["content"]
    )
    assert value == "Leaflet"
    assert [option["label"] for option in chosen] == ["Leaflet"]


def test_apply_plan_decisions_ignores_non_tool_result_blocks():
    (entry,) = native_entries("same_message_attachments")
    assert apply_plan_decisions(entry, {}) is False


def test_permission_denials_from_result_ignores_non_tool_result_blocks():
    (entry,) = native_entries("same_message_attachments")
    assert permission_denials_from_result(entry, {}) == []


def test_legacy_plan_helper_consumes_native_matches_and_updates_plan_records():
    entries = native_entries(
        "plan_reject_written_feedback",
        "revised_plan_ordinary_approval",
        "genuine_editor_edited_plan_approval",
    )
    calls = _calls(entries)
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
    entries = native_entries("revised_plan_ordinary_approval")
    result = next(entry for entry in entries if _is_tool_result_entry(entry))
    # Change only the captured result ID to exercise absent/unmatched associations.
    result["message"]["content"][0]["tool_use_id"] = tool_id
    pending = {call["id"]: call["plan_decision"] for call in _calls(entries)}
    original = deepcopy(pending)
    assert apply_plan_decisions(result, pending) is False
    assert pending == original


@pytest.mark.parametrize(
    ("fixture", "expected"),
    [
        ("multibyte_result_over_10kb", []),
        (
            "settings_driven_bash_denial",
            [{"tool": "Bash", "message": "", "by": "settings"}],
        ),
    ],
)
def test_legacy_denial_helper_matches_native_result_blocks(fixture, expected):
    entries = native_entries(fixture)
    tool_names = {call["id"]: call["name"] for call in _calls(entries)}
    denials = [
        denial
        for entry in entries
        for denial in permission_denials_from_result(entry, tool_names)
    ]
    assert denials == expected


def test_activity_keeps_commands_shell_subagents_questions_and_files():
    entries = native_entries(
        "custom_command_arguments",
        "user_shell_input",
        "one_read_only_subagent",
        "multiple_questions_and_multiselect",
        "native_write_read_edit_read",
        "human_denial_silent",
    )
    # Include actual sidechain and system records to exercise activity filtering.
    entries += load_entries(SUBAGENT)
    entries += [entry for entry in native_entries() if entry.get("type") == "system"]
    session = _session(entries)
    assert any(event.get("command") == "/capture-echo" for event in session["events"])
    assert any(
        "CAPTURE_USER_STDOUT_1" in event.get("shell_command", "")
        for event in session["events"]
    )
    assert session["subagent_count"] > 0
    assert any(path.endswith("greeting.py") for path in session["files_changed"])
    calls = _calls(entries)
    question = next(call for call in calls if call["name"] == "AskUserQuestion")
    assert question["questions"][0]["chosen_labels"] == ["Green", "Red"]
    assert any(call["name"] == "Write" for call in calls)
    denial = next(
        call["permission_denial"] for call in calls if "permission_denial" in call
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


def test_first_timestamp_session_models_and_cwd():
    entries = native_entries()
    assert first_timestamp(entries) == "2026-10-30T14:36:04.243Z"
    assert session_models(entries) == ["claude-opus-5-5", "claude-sonnet-5-5"]
    assert session_cwd(entries) == "/tmp/CAPTURE_ROOT/workspace"
    # Exercise missing timestamps/cwd and the synthetic model exclusion in isolation.
    assistant = deepcopy(
        next(entry for entry in entries if entry.get("type") == "assistant")
    )
    assistant["message"]["model"] = "<synthetic>"
    assistant["timestamp"] = None
    assistant.pop("cwd", None)
    assert first_timestamp([assistant]) is None
    assert session_models([assistant]) == []
    assert session_cwd([assistant]) is None
    assert first_timestamp([]) is None
    assert session_cwd([]) is None


def test_native_resumed_session_lists_both_models():
    entries = native_entries()
    assert session_models(entries) == ["claude-opus-5-5", "claude-sonnet-5-5"]
    assert any(
        event.get("command") == "/model" for event in _session(entries)["events"]
    )


def test_native_continued_conversation_keeps_follow_up_in_one_session():
    entries = native_entries()
    assert {entry["sessionId"] for entry in entries if "sessionId" in entry} == {
        TRANSCRIPT.stem
    }
    events = _session(entries)["events"]
    prompt = next(
        i
        for i, event in enumerate(events)
        if "CAPTURE_POST_COMPACT_RECALL:" in event["text"]
    )
    assert events[prompt]["role"] == "user"
    answer = next(
        event
        for event in events[prompt + 1 :]
        if event["role"] == "assistant" and event["text"]
    )
    assert "CAPTURE_CONTEXT_CEDAR_73" in answer["text"]
    assert "CAPTURE_PLAN_EDIT_VIM" in answer["text"]
    assert answer.get("tool_calls", []) == []
    assert any(
        "CAPTURE_MULTIBYTE" in (call.get("result") or "") for call in _calls(entries)
    )


def test_build_raw_envelope_preserves_metadata_and_events():
    entries = native_entries()
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
    assert summary["agent_type"] == "Explore"
    assert "Read-only task." in summary["task"]
    assert summary["tool_count"] == 1
    assert summary["tool_names"] == ["Read"]
    assert summary["result"] == "CAPTURE_CHILD_RESULT: CAPTURE_PLAN_EDIT_VIM"


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
    assert "hello.py" in subs[0]["task"]
    assert subs[0]["result"] == "CAPTURE_CHILD_RESULT: CAPTURE_PLAN_EDIT_VIM"
    meta = json.loads(SUBAGENT.with_suffix(".meta.json").read_text())
    assert subs[0]["agent_type"] == meta["agentType"]
    assert subs[0]["start_ts"] == "2026-10-30T14:48:26.785Z"
    session = _session(native_entries())
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
    assert envelope["events"][0]["role"] == "user"
    assert "**CAPTURE_CONTEXT_CEDAR_73** and café 雪" in envelope["events"][0]["text"]
    assert any(
        "CAPTURE_PLAN_EDIT_VIM" in event["text"]
        for event in envelope["events"]
        if event["role"] == "assistant"
    )


def test_main_writes_json_to_explicit_output():
    transcript = Path(tempfile.mkdtemp()) / "s1.jsonl"
    _write_jsonl(transcript, native_entries())
    out_dir = Path(tempfile.mkdtemp())
    out_file = out_dir / "log.json"
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        rc = main(["--transcript", str(transcript), "--output", str(out_file)])
    assert rc == 0
    assert out_file.exists()
    assert json.loads(out_file.read_text())["events"] == build_events(native_entries())
    assert list(out_dir.iterdir()) == [out_file]


def test_main_stdout_outputs_json():
    transcript = Path(tempfile.mkdtemp()) / "s1.jsonl"
    _write_jsonl(transcript, native_entries())
    out = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = main(["--transcript", str(transcript), "--output", "-"])
    assert rc == 0
    assert json.loads(out.getvalue())["events"] == build_events(native_entries())


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
    _write_jsonl(proj / "a.jsonl", native_entries())
    _write_jsonl(proj / "b.jsonl", native_entries())
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
    entries = native_entries("same_message_attachments")
    # Keep the Markdown preservation probe while retaining recorded image blocks.
    entries[0]["message"]["content"][0]["text"] = "# Keep **recorded Markdown**"
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
    assert envelope["events"][0]["text"] == "# Keep **recorded Markdown**"
    assert len(envelope["events"][0]["images"]) == 2


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
        assert json.loads(path.read_text())["events"] == build_events(native_entries())
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
    _write_jsonl(transcript, native_entries())
    args = ["--project-dir", str(tmp_path), "--output", "-"]
    if selector != "newest":
        args += [str(transcript) if selector == "path" else "session-1"]
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["session_id"] == "session-123"


@pytest.mark.parametrize("mode", ["single", "batch"])
def test_cli_reports_write_errors(mode, tmp_path, capsys):
    transcript = tmp_path / "session.jsonl"
    _write_jsonl(transcript, native_entries())
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
    entries = native_entries("single_choice_offered_option")
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
        [entry for entry in native_entries() if entry.get("type") == "system"],
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
@pytest.mark.parametrize(
    "fixture", [None, "same_message_attachments", "multibyte_result_over_10kb"]
)
def test_interpret_supplied_source_without_filesystem(limit, fixture, monkeypatch):
    entries = native_entries(fixture) if fixture else native_entries()
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
    entries = native_entries("multibyte_result_over_10kb")
    source = ClaudeSource(Path("/unavailable/session.jsonl"), entries)
    session = eclog.interpret_source(source)
    call = session["events"][0]["tool_calls"][0]
    assert len(call["result"].encode()) > 10_000
    assert "result_truncated" not in call
    exported = session_log_envelope.export_session(session, 17)
    assert len(exported["events"][0]["tool_calls"][0]["result"].encode()) == 17
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


def _native_tool_pair():
    return tool_pair()


@pytest.mark.parametrize("same_id", [False, True])
def test_repeated_calls_keep_order_and_native_result_associations(same_id):
    assistant, result = _native_tool_pair()
    again = deepcopy(assistant)
    again_result = deepcopy(result)
    first_call = assistant["message"]["content"][0]
    second_call = again["message"]["content"][0]
    # Repeat the captured Read, changing only identity/output to expose association.
    if not same_id:
        second_call["id"] = "second-call"
    again_result["message"]["content"][0]["tool_use_id"] = second_call["id"]
    again_result["message"]["content"][0]["content"] = "second output"
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
    assistant, result = _native_tool_pair()
    block = result["message"]["content"][0]
    # Keep the captured association; vary only the result boundary under test.
    block["content"] = None if result_kind == "null" else ""
    if result_kind == "error":
        block["is_error"] = True
    entries = [assistant] if result_kind == "missing" else [assistant, result]
    session = eclog.interpret_source(ClaudeSource(Path("s.jsonl"), entries))
    call = session["events"][0]["tool_calls"][0]
    assert call["result"] == (None if result_kind == "missing" else "")
    exported = session_log_envelope.export_session(session)["events"][0]["tool_calls"][
        0
    ]
    assert exported["result"] == call["result"]
    assert exported["result_bytes"] == 0
    assert exported["result_truncated"] is False
    assert exported.get("is_error", False) is (result_kind == "error")


def test_images_remain_owned_by_their_recorded_user_event():
    image_entries = native_entries("same_message_attachments")
    assistant, result = _native_tool_pair()
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
    entries = native_entries("repeated_skill_invocation")
    content = next(
        e["message"]["content"]
        for e in entries
        if any(
            isinstance(b, dict) and b.get("name") == "Skill" for b in _content_blocks(e)
        )
    )
    skill = next(b for b in content if b.get("name") == "Skill")
    # Repeat the native invocation and add an empty name without dropping calls.
    empty = deepcopy(skill)
    empty["input"] = {"skill": ""}
    content.append(empty)
    assert _session(entries)["skills_used"] == ["capture-leaf"]
    calls = [c for c in _calls(entries) if c["name"] == "Skill"]
    assert len(calls) == 3
    assert [c.get("skill") for c in calls] == ["capture-leaf", None, "capture-leaf"]


@pytest.mark.parametrize(
    "behavior",
    [
        "multiple_questions_and_multiselect",
        "revised_plan_ordinary_approval",
        "settings_driven_bash_denial",
    ],
)
def test_decisions_follow_native_calls_across_later_user_messages(behavior):
    assistant, result = tool_pair(behavior)
    (later,) = native_entries("ordinary_markdown_unicode_context")
    # Move a recorded human message between call and result; retain call IDs.
    session = _session([assistant, later, result])
    call = session["events"][0]["tool_calls"][0]
    assert call["id"] == assistant["message"]["content"][0]["id"]
    assert call["result_record"]["tool_call_id"] == call["id"]
    if behavior == "multiple_questions_and_multiselect":
        assert call["questions"][0]["chosen_labels"] == ["Green", "Red"]
    elif behavior == "revised_plan_ordinary_approval":
        assert call["plan_decision"]["decision"] == "approved"
    else:
        assert call["permission_denial"]["by"] == "settings"
    assert "CAPTURE_CONTEXT_CEDAR_73" in session["events"][1]["text"]
    assert "interactions" not in session
    assert "interpretation" not in session


def test_native_message_and_result_identities_survive_interpretation():
    assistant, result = _native_tool_pair()
    session = _session([assistant, result])
    message = session["events"][0]
    assert message["id"] == assistant["uuid"]
    assert message["parent_id"] == assistant["parentUuid"]
    assert message["message_id"] == assistant["message"]["id"]
    record = message["tool_calls"][0]["result_record"]
    assert record["id"] == result["uuid"]
    assert record["ts"] == result["timestamp"]
    assert record["content"] == result["message"]["content"][0]["content"]


def test_unmatched_result_retains_content_without_inventing_an_owner():
    assistant, result = _native_tool_pair()
    # Change only the result ID so the captured result has no matching call.
    block = result["message"]["content"][0]
    block["tool_use_id"] = "unknown-call"
    session = _session([assistant, result])
    assert session["events"][0]["tool_calls"][0]["result"] is None
    assert "result_record" not in session["events"][0]["tool_calls"][0]
    (record,) = session["unmatched_tool_results"]
    assert record["tool_call_id"] == "unknown-call"
    assert record["content"] == block["content"]
    assert record["id"] == result["uuid"]


@pytest.mark.parametrize("options", [None, 7, "invalid", {"label": "section"}])
def test_malformed_question_options_preserve_input_and_allow_extraction(options):
    entries = native_entries("multiple_questions_and_multiselect")
    block = entries[0]["message"]["content"][0]
    # Vary only the options container in the audited question fixture.
    block["input"]["questions"][0]["options"] = options
    session = _session(entries)
    call = session["events"][0]["tool_calls"][0]
    assert call["input"] == block["input"]
    assert call["questions"][0]["options"] == []
    assert call["questions"][0]["answer_text"] == "Green, Red"
    exported = session_log_envelope.export_session(session)
    assert exported["events"][0]["tool_calls"][0]["input"] == block["input"]


@pytest.mark.parametrize("payload", [{"stdout": "output"}, "output", {}, "", False])
def test_top_level_result_preserves_payload_without_inventing_an_owner(payload):
    assistant, result = _native_tool_pair()
    # Model the older result-only shape by moving the captured result to the
    # top level and removing the block that carries its call ID.
    result["message"]["content"] = []
    result["toolUseResult"] = payload
    session = _session([assistant, result])
    call = session["events"][0]["tool_calls"][0]
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
    assistant, result = _native_tool_pair()
    result["toolUseResult"] = {"stdout": "duplicate output"}
    session = _session([assistant, result])
    assert session["unmatched_tool_results"] == []
    assert "CAPTURE_MULTIBYTE" in session["events"][0]["tool_calls"][0]["result"]


def test_unknown_tool_payload_is_preserved_and_reported(tmp_path, capsys):
    assistant, result = _native_tool_pair()
    # Change only the captured tool name and extend its input to model a plugin.
    block = assistant["message"]["content"][0]
    block["name"] = "plugin__DeployService"
    block["input"]["plugin_options"] = {
        "targets": [None, True, 1.5, {"region": "east"}]
    }
    session = _session([assistant, result])
    call = session["events"][0]["tool_calls"][0]
    assert call["input_schema"] == "unsupported"
    assert session["unsupported_tools"] == ["plugin__DeployService"]
    assert call["input"] == block["input"]
    assert (
        call["result_record"]["content"] == result["message"]["content"][0]["content"]
    )
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(transcript, [assistant, result, deepcopy(assistant)])
    envelope = extract_transcript(transcript, -1)
    assert envelope["events"][0]["tool_calls"][0]["input"] == block["input"]
    assert "input_schema" not in envelope["events"][0]["tool_calls"][0]
    diagnostic = capsys.readouterr().err
    assert "unsupported tool input schemas" in diagnostic
    assert diagnostic.count("plugin__DeployService") == 1


def test_question_options_are_checked_without_losing_recorded_payload():
    entries = native_entries("multiple_questions_and_multiselect")
    block = entries[0]["message"]["content"][0]
    options = block["input"]["questions"][0]["options"]
    # Extend a recorded option and introduce malformed known fields. Semantic
    # parsing uses checked options while the complete native input survives.
    options[0]["preview"] = {"markdown": "**keep this**"}
    options.extend([{"label": 1}, {"label": "bad", "description": None}])
    session = _session(entries)
    call = session["events"][0]["tool_calls"][0]
    assert call["input"] == block["input"]
    assert call["questions"][0]["options"] == options[:-2]
    assert call["questions"][0]["options"][0]["preview"] == {
        "markdown": "**keep this**"
    }
    assert call["questions"][0]["chosen_labels"] == ["Green", "Red"]


@pytest.mark.parametrize("name", ["bash", "view", "edit", "create", "skill"])
def test_claude_reports_copilot_tool_names_as_unsupported(name):
    assistant, result = _native_tool_pair()
    # Change only the captured name to the other harness's vocabulary.
    block = assistant["message"]["content"][0]
    block["name"] = name
    session = _session([assistant, result])
    call = session["events"][0]["tool_calls"][0]
    assert call["input_schema"] == "unsupported"
    assert session["unsupported_tools"] == [name]
    assert call["input"] == block["input"]
    assert (
        call["result_record"]["content"] == result["message"]["content"][0]["content"]
    )


def _plan_result_variant(text):
    # Legacy rejection-reason wording is absent from the current CLI capture.
    assistant, result = tool_pair("plan_reject_written_feedback")
    result["message"]["content"][0]["content"] = text
    return [assistant, result]


def test_legacy_edited_heading_with_unchanged_plan_is_not_an_edit():
    # The earlier audited plan fixture echoed an unchanged plan under this heading.
    assistant, result = tool_pair("revised_plan_ordinary_approval")
    block = result["message"]["content"][0]
    block["content"] = block["content"].replace(
        "## Approved Plan:", "## Approved Plan (edited by user):"
    )
    (plan,) = _plans([assistant, result])
    assert plan["decision"] == "approved"
    assert plan["edited_plan"] == ""


@pytest.mark.parametrize("media_type", ["image/jpeg", "image/gif", "image/webp"])
def test_legacy_image_media_types_are_preserved(media_type):
    # 2.1.312/314 converts all attachments to PNG; legacy logs can contain
    # these MIME types. Substitute only the source from captured before assets.
    (entry,) = native_entries("same_message_attachments")
    filenames = {
        "image/jpeg": "blue.jpg",
        "image/gif": "green.gif",
        "image/webp": "gold.webp",
    }
    source = entry["message"]["content"][1]["source"]
    source["media_type"] = media_type
    source["data"] = base64.b64encode(
        (ROOT / "assets/before" / filenames[media_type]).read_bytes()
    ).decode()
    image = build_events([entry])[0]["images"][0]
    assert image["media_type"] == media_type
    assert image["data"] == source["data"]


def test_legacy_dont_ask_denial_without_command_is_recorded():
    # Claude 2.1.292 omitted the command from its settings denial preamble.
    assistant, result = tool_pair("settings_driven_bash_denial")
    result["message"]["content"][0]["content"] = (
        "Permission to use Bash has been denied."
    )
    (call,) = _calls([assistant, result])
    assert call["permission_denial"]["by"] == "settings"
    assert call["permission_denial"]["message"] == ""
