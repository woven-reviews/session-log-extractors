#!/usr/bin/env python3
"""Minimal asserts for the option-question parsing in extract_claude_session_log.

Run: uv run --locked python scripts/test_extract_claude_session_log.py
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

from session_log_extractors import extract_claude_session_log as eclog
from session_log_extractors.claude_sources import (
    ClaudeSource,
    read_source,
    read_subagent_sources,
)
from session_log_extractors.extract_claude_session_log import (
    Turn,
    _candidate_project_dirs,
    _content_blocks,
    _extract_all,
    _human_text,
    _is_tool_result_entry,
    _jsonl_files,
    _parse_chosen,
    _result_block_text,
    apply_plan_decisions,
    attribute_subagents,
    build_events,
    build_raw_envelope,
    build_turns,
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
    result_note,
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

# 1x1 transparent PNG.
_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9"
    "awAAAABJRU5ErkJggg=="
)

OPTS = [
    {"label": "section", "description": "a"},
    {"label": "manufacturer_part_no", "description": "b"},
    {"label": "Neither", "description": "c"},
]

FIXTURES = Path(__file__).parent / "fixtures" / "claude"


def _fixture_entries(name):
    return load_entries(FIXTURES / name)


def test_skill_tool_use_is_recorded():
    entries = [
        {
            "type": "user",
            "timestamp": "2026-01-01T00:00:00Z",
            "message": {"content": "Make a document"},
        },
        {
            "type": "assistant",
            "timestamp": "2026-01-01T00:00:01Z",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_skill",
                        "name": "Skill",
                        "input": {"skill": "documents", "args": ""},
                    }
                ]
            },
        },
    ]
    turns, _ = build_turns(entries)
    assert turns[0].skills_used == ["documents"]


def test_native_skill_and_command_invocations_are_retained():
    entries = _fixture_entries("skill_command.jsonl")
    turns, _ = build_turns(entries)
    assert turns[0].skills_used == ["fixture-skill"]
    assert turns[1].command == "/fixture-command"
    events = build_events(entries)
    calls = [call for event in events for call in event.get("tool_calls", [])]
    assert any(call["input"].get("skill") == "fixture-skill" for call in calls)
    assert any("/fixture-command" in event["text"] for event in events)


def test_single_choice():
    res = 'Your questions have been answered: "Q"="section". continue.'
    val, chosen = _parse_chosen("Q", OPTS, res)
    assert val == "section", val
    assert [o["label"] for o in chosen] == ["section"]


def test_multiselect_comma():
    turns, _ = build_turns(_fixture_entries("questions.jsonl"))
    qa = turns[0].option_qas[0]
    assert qa["answer_text"] == "section, manufacturer_part_no"
    assert qa["chosen_labels"] == ["section", "manufacturer_part_no"]


def test_permission_denial_bare():
    c = (
        "The user doesn't want to proceed with this tool use. The tool use was "
        "rejected (eg. if it was a file edit, the new_string was NOT written to "
        "the file). STOP what you are doing and wait for the user to tell you how "
        "to proceed.\n\nNote: The user's next message may contain a correction."
    )
    assert parse_permission_denial(c) == ""


def test_permission_denial_with_message():
    c = (
        "Permission for this tool use was denied. The tool use was rejected (eg. "
        "if it was a file edit, the new_string was NOT written to the file). The "
        "user said:\nWe need a new branch for this"
    )
    assert parse_permission_denial(c) == "We need a new branch for this"


def test_permission_denial_ignores_normal_result():
    assert parse_permission_denial("42 files changed") is None
    assert parse_permission_denial("error: file not found") is None


def test_native_dont_ask_denial_is_recorded():
    entries = _fixture_entries("permission_denial.jsonl")
    result = next(
        block["content"]
        for entry in entries
        for block in _content_blocks(entry)
        if isinstance(block, dict) and block.get("type") == "tool_result"
    )
    assert "Permission to use Bash has been denied" in result
    assert parse_permission_denial(result) == ""
    turns, _ = build_turns(entries)
    assert [d["tool"].split(" — ")[0] for d in turns[0].permission_denials] == ["Bash"]
    assert turns[0].permission_denials[0]["by"] == "settings"
    assert not any("has been denied" in note for note in turns[0].result_notes)


def test_permission_denial_ignores_isc_license_text():
    assert (
        parse_permission_denial("Permission to use, copy, modify, and/or distribute")
        is None
    )


def test_permission_denials_from_result_names_tool():
    entry = {
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_1",
                    "is_error": True,
                    "content": (
                        "Permission for this tool use was denied. The tool use "
                        "was rejected. The user said:\nno"
                    ),
                }
            ]
        }
    }
    out = permission_denials_from_result(entry, {"toolu_1": "Bash — rm -rf build"})
    assert out == [{"tool": "Bash — rm -rf build", "message": "no", "by": "user"}]


def test_custom_answer_matches_no_option():
    turns, _ = build_turns(_fixture_entries("questions.jsonl"))
    qa = turns[1].option_qas[0]
    assert qa["answer_text"] == "custom_value"
    assert qa["chosen_labels"] == []


def test_multi_question_anchors_on_its_own_text():
    turns, _ = build_turns(_fixture_entries("questions.jsonl"))
    assert [qa["answer_text"] for qa in turns[1].option_qas] == ["custom_value", "JSON"]
    assert turns[1].option_qas[1]["chosen_labels"] == ["JSON"]


def test_task_notification_is_stripped_to_empty():
    assert clean_user_text("<task-notification>\nstuff\n</task-notification>") == ""


def test_bash_output_is_stripped_to_empty():
    msg = "<bash-stdout>some output</bash-stdout><bash-stderr>a warning</bash-stderr>"
    assert clean_user_text(msg) == ""


def test_bash_input_survives_cleaning_for_detection():
    # The command itself must NOT be stripped; build_turns extracts it.
    assert (
        clean_user_text("<bash-input>ls -la</bash-input>")
        == "<bash-input>ls -la</bash-input>"
    )


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


def _conversation_entries():
    """A user turn with a pasted image, an assistant reply, and a tool call
    whose result arrives as its own later entry."""
    return [
        {
            "type": "user",
            "timestamp": "2026-08-01T10:00:00Z",
            "cwd": "/work/app",
            "message": {
                "role": "user",
                "content": [
                    {"type": "text", "text": "here is the mock"},
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": _PNG_B64,
                        },
                    },
                ],
            },
        },
        {
            "type": "assistant",
            "timestamp": "2026-08-01T10:00:02Z",
            "message": {
                "role": "assistant",
                "model": "claude-opus-4-8",
                "content": [
                    {"type": "text", "text": "On it."},
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Read",
                        "input": {"file_path": "/work/app/models.py"},
                    },
                ],
            },
        },
        {
            "type": "user",
            "timestamp": "2026-08-01T10:00:03Z",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": "B" * 9000,
                    }
                ],
            },
        },
    ]


def _plan_entries(result_text: str, plan: str = "# Plan\n\nStep one."):
    return [
        {
            "type": "user",
            "timestamp": "2026-01-01T00:00:00Z",
            "message": {"content": "build it"},
        },
        {
            "type": "assistant",
            "timestamp": "2026-01-01T00:00:01Z",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_plan",
                        "name": "ExitPlanMode",
                        "input": {"plan": plan},
                    }
                ]
            },
        },
        {
            "type": "user",
            "timestamp": "2026-01-01T00:00:02Z",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_plan",
                        "content": result_text,
                    }
                ]
            },
        },
    ]


def test_build_events_inlines_pasted_images_as_base64():
    entries = _fixture_entries("images.jsonl")
    events = build_events(entries)
    user = events[0]
    assert user["role"] == "user"
    assert user["text"] == "Compare these four tiny images"
    assert [image["media_type"] for image in user["images"]] == [
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
    ]
    assert [image["data"] for image in user["images"]] == [
        block["source"]["data"]
        for block in entries[0]["message"]["content"]
        if block["type"] == "image"
    ]


def test_build_events_attaches_tool_results_to_their_call():
    events = build_events(
        _fixture_entries("large_result.jsonl"), tool_result_max_bytes=100
    )
    call = events[1]["tool_calls"][0]
    assert call["name"] == "Read"
    assert call["input"]["file_path"] == "/workspace/large.txt"
    assert call["result_bytes"] > 10_000
    assert call["result_truncated"] is True
    assert len(call["result"]) == 100


def test_large_native_result_reaches_envelope_size_warning():
    events = build_events(
        _fixture_entries("large_result.jsonl"), tool_result_max_bytes=100_000
    )
    # Repeated native Read results simulate a long session without checking in 8 MB.
    copies = SIZE_WARN_BYTES // len(json.dumps(events).encode()) + 2
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
    # The condensed markdown truncates a tool descriptor to 100 chars; the
    # envelope is the place the whole input survives.
    long_path = "/work/" + ("nested/" * 40) + "file.py"
    entries = [
        {
            "type": "assistant",
            "timestamp": None,
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t9",
                        "name": "Read",
                        "input": {"file_path": long_path},
                    }
                ],
            },
        }
    ]
    assert build_events(entries)[0]["tool_calls"][0]["input"]["file_path"] == long_path


def test_build_events_skips_sidechain_and_meta_like_build_turns():
    # Events and turns must describe the same conversation, or a grader reading
    # the raw log sees something the markdown never showed them.
    entries = _conversation_entries() + [
        {
            "type": "user",
            "isSidechain": True,
            "message": {"role": "user", "content": [{"type": "text", "text": "sub"}]},
        },
        {
            "type": "user",
            "isMeta": True,
            "message": {"role": "user", "content": [{"type": "text", "text": "meta"}]},
        },
    ]
    events = build_events(entries)
    turns, _ = build_turns(entries)
    assert [e["text"] for e in events if e["role"] == "user"] == [
        t.user_text for t in turns
    ]


def test_build_events_surfaces_slash_command_invocations():
    # A slash command cleans to empty prose but is a real thing the human did.
    entries = [
        {
            "type": "user",
            "timestamp": None,
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "<command-name>/extract-claude-session-logs</command-name>"
                            "<command-args>--strict</command-args>"
                        ),
                    }
                ],
            },
        }
    ]
    events = build_events(entries)
    assert events[0]["text"] == "/extract-claude-session-logs --strict"


def test_build_events_does_not_disturb_interpreted_turns():
    # The whole point of a second pass: running it must not perturb build_turns.
    entries = _conversation_entries()
    before = [t.user_text for t in build_turns(entries)[0]]
    build_events(entries)
    assert [t.user_text for t in build_turns(entries)[0]] == before


def test_plan_approved_is_captured():
    turns, _ = build_turns(_fixture_entries("plan_modes.jsonl"))
    (plan,) = turns[0].plans
    assert plan["plan"] == "# Plan\n\nStep one."
    assert plan["decision"] == "approved"
    # Unchanged plan echoed back under the "edited" heading is not an edit.
    assert plan["edited_plan"] == ""
    # The plan is rendered as its own block, not as a tool bullet.
    assert turns[0].tool_bullets == []
    assert turns[0].result_notes == []


def test_plan_approved_with_edit_keeps_final_version():
    turns, _ = build_turns(_fixture_entries("plan_modes.jsonl"))
    (plan,) = turns[1].plans
    assert plan["decision"] == "approved"
    assert plan["edited_plan"] == "# Plan\n\nStep one, but smaller."


def test_plan_rejected_keeps_steering_message():
    turns, _ = build_turns(_fixture_entries("plan_modes.jsonl"))
    (plan,) = turns[2].plans
    assert plan["decision"] == "rejected"
    assert plan["message"] == "Too big, split it up"
    # Routed to the plan record, not the generic permission-denial list.
    assert turns[2].permission_denials == []


def test_plan_rejection_reason_is_captured():
    # The wording the harness actually uses when a plan is sent back in plan mode.
    turns, _ = build_turns(
        _plan_entries(
            "The user doesn't want to proceed with this tool use. The tool use "
            "was rejected (eg. if it was a file edit, the new_string was NOT "
            "written to the file). STOP what you are doing and wait for the user "
            "to tell you how to proceed. The user provided the following reason "
            "for the rejection:\nwrong table, use line_items\nand keep it in one "
            "migration\n\nNote: The user's next message may contain a correction."
        )
    )
    (plan,) = turns[0].plans
    assert plan["decision"] == "rejected"
    assert (
        plan["message"] == "wrong table, use line_items\nand keep it in one migration"
    )


def test_plan_without_result_is_marked_undecided():
    entries = _plan_entries("")[:2]
    (plan,) = build_turns(entries)[0][0].plans
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
    a.write_text("{}")
    b.write_text("{}")
    os.utime(a, (1, 1))
    os.utime(b, (1000, 1000))
    assert newest([a, b]) == b


def test_select_transcript_explicit_transcript_wins():
    f = Path(tempfile.mkdtemp()) / "a.jsonl"
    f.write_text("{}")
    assert select_transcript(None, str(f), None) == f


def test_select_transcript_explicit_transcript_missing_raises():
    with pytest.raises(FileNotFoundError):
        select_transcript(None, "/no/such/file.jsonl", None)


def test_select_transcript_selector_as_file_path():
    f = Path(tempfile.mkdtemp()) / "sess.jsonl"
    f.write_text("{}")
    assert select_transcript(str(f), None, None) == f


def test_select_transcript_by_session_id_exact_and_suffix():
    proj = Path(tempfile.mkdtemp())
    (proj / "abc123.jsonl").write_text("{}")
    assert select_transcript("abc123", None, str(proj)) == proj / "abc123.jsonl"
    (proj / "xyz").write_text("{}")
    assert select_transcript("xyz", None, str(proj)) == proj / "xyz"


def test_select_transcript_prefix_match_unique_and_ambiguous():
    proj = Path(tempfile.mkdtemp())
    (proj / "sess-aaa.jsonl").write_text("{}")
    assert select_transcript("sess-aaa", None, str(proj)) == proj / "sess-aaa.jsonl"
    (proj / "sess-bbb.jsonl").write_text("{}")
    assert select_transcript("sess-a", None, str(proj)) == proj / "sess-aaa.jsonl"

    proj2 = Path(tempfile.mkdtemp())
    (proj2 / "sess-aaa.jsonl").write_text("{}")
    (proj2 / "sess-aab.jsonl").write_text("{}")
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
    a.write_text("{}")
    b.write_text("{}")
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
        f.write_text("{}")
        assert select_transcript(None, None, None, strict=False) == f


# --------------------------------------------------------------------------- #
# JSONL loading
# --------------------------------------------------------------------------- #


def test_load_entries_skips_malformed_lines_with_warning():
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        entries = _fixture_entries("truncated.jsonl")
    assert [e["type"] for e in entries] == ["user", "assistant"]
    assert "skipping malformed JSON on line 3" in buf.getvalue()


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
    assert _content_blocks({"message": {"content": None}}) == []
    assert _content_blocks({"message": {"content": {"weird": True}}}) == []
    assert _content_blocks({"message": {"content": "plain"}}) == [
        {"type": "text", "text": "plain"}
    ]


def test_tool_descriptor_shell_search_and_fallback():
    assert tool_descriptor("Bash", {"command": "ls -la"}) == "ls -la"
    assert tool_descriptor("Grep", {"pattern": "foo"}) == "foo"
    assert tool_descriptor("X", {"odd": 1}) == '{"odd": 1}'
    assert tool_descriptor("X", None) == ""
    assert tool_descriptor("X", "raw string input") == "raw string input"


def test_extract_file_path_variants():
    assert extract_file_path({"file_path": "/a"}) == "/a"
    assert extract_file_path({"notebook_path": "/b.ipynb"}) == "/b.ipynb"
    assert extract_file_path({}) is None
    assert extract_file_path("nope") is None


def test_result_note_from_tool_use_result_fallback():
    entry = {"message": {"content": []}, "toolUseResult": {"stdout": "all good"}}
    assert result_note(entry) == "all good"
    entry2 = {"message": {"content": []}, "toolUseResult": "plain string result"}
    assert result_note(entry2) == "plain string result"


def test_result_note_error_without_text_is_bare_error():
    entry = {
        "message": {
            "content": [{"type": "tool_result", "is_error": True, "content": ""}]
        }
    }
    assert result_note(entry) == "error"


def test_option_qa_from_result_builds_record():
    pending = {"q1": [{"question": "Which?", "header": "H", "options": OPTS}]}
    entry = {
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "q1",
                    "content": '"Which?"="section". ok',
                }
            ]
        }
    }
    out = option_qa_from_result(entry, pending)
    assert out[0]["chosen_labels"] == ["section"]
    assert out[0]["header"] == "H"


def test_option_qa_from_result_ignores_untracked_tool():
    entry = {
        "message": {
            "content": [{"type": "tool_result", "tool_use_id": "other", "content": "x"}]
        }
    }
    assert option_qa_from_result(entry, {}) == []


def test_option_qa_from_result_skips_non_tool_result_blocks_and_bad_questions():
    pending = {"q1": ["not-a-dict"]}
    entry = {
        "message": {
            "content": [
                {"type": "text", "text": "ignored"},
                {"type": "tool_result", "tool_use_id": "q1", "content": "x"},
            ]
        }
    }
    assert option_qa_from_result(entry, pending) == []


def test_is_tool_result_entry_true_for_bare_tool_use_result():
    entry = {"message": {"content": []}, "toolUseResult": {"stdout": "ok"}}
    assert _is_tool_result_entry(entry) is True


def test_human_text_includes_plain_string_blocks():
    entry = {"message": {"content": ["plain string block"]}}
    assert _human_text(entry) == "plain string block"


def test_clean_user_text_empty_input_is_empty():
    assert clean_user_text("") == ""
    assert clean_user_text(None) == ""


def test_result_note_skips_non_tool_result_blocks_and_reads_list_content():
    entry = {
        "message": {
            "content": [
                {"type": "text", "text": "ignored"},
                {
                    "type": "tool_result",
                    "content": [{"type": "text", "text": "line one"}],
                },
            ]
        }
    }
    assert result_note(entry) == "line one"


def test_result_block_text_flattens_list_content():
    block = {"content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}
    assert _result_block_text(block) == "a b"


def test_parse_chosen_single_question_fallback_when_text_mismatches():
    # question_text doesn't appear verbatim in result_text, so the anchored
    # lookup misses and the single-question fallback takes over.
    val, chosen = _parse_chosen("Nomatch", OPTS, '"Other"="Neither". done')
    assert val == "Neither"
    assert [o["label"] for o in chosen] == ["Neither"]


def test_apply_plan_decisions_ignores_non_tool_result_blocks():
    entry = {"message": {"content": [{"type": "text", "text": "ignored"}]}}
    assert apply_plan_decisions(entry, {}) is False


def test_permission_denials_from_result_ignores_non_tool_result_blocks():
    entry = {"message": {"content": [{"type": "text", "text": "ignored"}]}}
    assert permission_denials_from_result(entry, {}) == []


def test_turn_add_tool_without_descriptor():
    t = Turn("x", None)
    t.add_tool("Read", "")
    assert t.tool_bullets == ["- Read"]


def test_turn_add_command_sets_fields_and_defaults_args_to_none():
    t = Turn("x", None)
    t.add_command("/foo", "bar")
    assert t.command == "/foo"
    assert t.command_args == "bar"
    t.add_command("/foo")
    assert t.command_args is None


def test_build_turns_covers_command_args_shell_subagents_questions_and_files():
    entries = [
        {"type": "summary"},  # summary/system entries are skipped outright
        {"type": "user", "isSidechain": True, "message": {"content": "spawn"}},
        {
            "type": "user",
            "timestamp": "2026-01-01T00:00:00Z",
            "message": {
                "content": (
                    "<command-name>/do-thing</command-name>"
                    "<command-args>--flag</command-args>"
                    "<bash-input>ls -la</bash-input>"
                )
            },
        },
        {
            "type": "assistant",
            "timestamp": "2026-01-01T00:00:01Z",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "aq1",
                        "name": "AskUserQuestion",
                        "input": {
                            "questions": [
                                {"question": "Which?", "header": "H", "options": OPTS}
                            ]
                        },
                    },
                    {
                        "type": "tool_use",
                        "id": "w1",
                        "name": "Write",
                        "input": {"file_path": "/new/file.py"},
                    },
                    {
                        "type": "tool_use",
                        "id": "b1",
                        "name": "Bash",
                        "input": {"command": "rm -rf build"},
                    },
                ]
            },
        },
        {
            "type": "user",
            "timestamp": "2026-01-01T00:00:02Z",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "aq1",
                        "content": '"Which?"="section". ok',
                    },
                    {
                        "type": "tool_result",
                        "tool_use_id": "b1",
                        "is_error": True,
                        "content": (
                            "Permission for this tool use was denied. The tool use "
                            "was rejected. The user said:\nno"
                        ),
                    },
                ]
            },
        },
    ]
    turns, files_changed = build_turns(entries)
    assert len(turns) == 1
    assert turns[0].command == "/do-thing"
    assert turns[0].command_args == "--flag"
    assert turns[0].shell_command == "ls -la"
    assert turns[0].subagent_count == 1
    assert files_changed == ["/new/file.py"]
    assert turns[0].option_qas[0]["chosen_labels"] == ["section"]
    assert turns[0].permission_denials == [
        {"tool": "Bash — rm -rf build", "message": "no", "by": "user"}
    ]


def test_build_turns_makes_stub_for_leading_assistant_activity():
    # Assistant content arriving before any human turn, and a non-dict content
    # block, both before a real user message exists yet.
    entries = [{"type": "assistant", "timestamp": None, "message": {"content": ["x"]}}]
    turns, _ = build_turns(entries)
    assert len(turns) == 1
    assert turns[0].user_text == ""
    assert turns[0].tool_bullets == []


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
    entries = [
        {"type": "user", "timestamp": None},
        {
            "type": "assistant",
            "timestamp": "2026-01-01T00:00:00Z",
            "message": {"model": "<synthetic>"},
            "cwd": "/work",
        },
        {
            "type": "assistant",
            "timestamp": "2026-01-01T00:00:01Z",
            "message": {"model": "claude-opus"},
        },
    ]
    assert first_timestamp(entries) == "2026-01-01T00:00:00Z"
    assert session_models(entries) == ["claude-opus"]
    assert session_cwd(entries) == "/work"
    assert first_timestamp([]) is None
    assert session_cwd([]) is None


def test_native_resumed_session_lists_both_models():
    entries = _fixture_entries("model.jsonl")
    assert session_models(entries) == ["claude-haiku-4-5-20251001", "claude-sonnet-5-5"]
    turns, _ = build_turns(entries)
    assert any(
        turn.command == "/model" and turn.command_args == "sonnet" for turn in turns
    )


def test_native_continued_conversation_keeps_follow_up_in_one_session():
    entries = _fixture_entries("continued.jsonl")
    assert len({entry["sessionId"] for entry in entries}) == 1
    turns, _ = build_turns(entries)
    assert len(turns) == 2
    assert "Read" in turns[0].tool_bullets[0]
    assert "RED-FOX-23" in " ".join(turns[0].assistant_text_blocks)
    assert "previous answer" in turns[1].user_text
    assert "RED-FOX-23" in " ".join(turns[1].assistant_text_blocks)
    events = build_events(entries)
    assert [event["role"] for event in events] == [
        "user",
        "assistant",
        "assistant",
        "user",
        "assistant",
    ]
    assert "RED-FOX-23" in events[1]["tool_calls"][0]["result"]
    assert events[4].get("tool_calls", []) == []


def test_build_raw_envelope_preserves_metadata_and_events():
    entries = _conversation_entries()
    envelope = build_raw_envelope(Path("/wherever/sess123.jsonl"), entries)
    assert envelope["session_id"] == "sess123"
    assert envelope["cwd"] == "/work/app"
    assert envelope["started_at"] == first_timestamp(entries)
    assert envelope["models"] == session_models(entries)
    assert envelope["events"] == build_events(entries)


def test_build_raw_envelope_returns_none_when_no_events():
    assert build_raw_envelope(Path("/x/sess.jsonl"), []) is None


# --------------------------------------------------------------------------- #
# Subagents
# --------------------------------------------------------------------------- #


def test_summarize_subagent_reads_meta_task_and_result():
    d = Path(tempfile.mkdtemp())
    sub = d / "agent-1.jsonl"
    sub.write_text(
        json.dumps(
            {
                "type": "user",
                "timestamp": "2026-01-01T00:00:00Z",
                "message": {"content": "do the thing"},
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Bash",
                            "input": {"command": "ls"},
                        },
                        {"type": "text", "text": "done"},
                    ]
                },
            }
        )
        + "\n"
    )
    (d / "agent-1.meta.json").write_text(json.dumps({"agentType": "Explore"}))
    summary = summarize_subagent(sub)
    assert summary["agent_type"] == "Explore"
    assert summary["task"] == "do the thing"
    assert summary["tool_count"] == 1
    assert summary["tool_names"] == ["Bash"]
    assert summary["result"] == "done"


def test_summarize_subagent_returns_none_for_empty_file():
    sub = Path(tempfile.mkdtemp()) / "agent-2.jsonl"
    sub.write_text("")
    assert summarize_subagent(sub) is None


def test_load_subagents_missing_dir_is_empty():
    transcript = Path(tempfile.mkdtemp()) / "sess.jsonl"
    assert load_subagents(transcript) == []


def test_load_subagents_reads_sidecar_dir():
    base = Path(tempfile.mkdtemp())
    transcript = base / "sess.jsonl"
    transcript.write_text("{}")
    sub_dir = base / "sess" / "subagents"
    sub_dir.mkdir(parents=True)
    (sub_dir / "agent-1.jsonl").write_text(
        json.dumps(
            {
                "type": "user",
                "timestamp": "2026-01-01T00:00:00Z",
                "message": {"content": "task"},
            }
        )
        + "\n"
    )
    subs = load_subagents(transcript)
    assert len(subs) == 1
    assert subs[0]["task"] == "task"


def test_native_subagent_sidecar_is_attributed_to_turn():
    transcript = FIXTURES / "subagent.jsonl"
    subs = load_subagents(transcript)
    assert len(subs) == 1
    assert "2 + 2" in subs[0]["task"]
    assert "4" in subs[0]["result"]

    # Surround the native turn with earlier and later turns so attribution must
    # use the sidecar's real start time rather than the first-turn fallback.
    def turn(text, ts):
        return {"type": "user", "timestamp": ts, "message": {"content": text}}

    entries = [
        turn("earlier", "2000-01-01T00:00:00Z"),
        *load_entries(transcript),
        turn("later", "2999-01-01T00:00:00Z"),
    ]
    turns, _ = build_turns(entries)
    assert [t.subagents for t in turns] == [[], [], []]
    attribute_subagents(turns, subs)
    assert [t.subagents for t in turns] == [[], subs, []]
    meta = json.loads(
        (FIXTURES / "subagent/subagents/agent-fixture.meta.json").read_text()
    )
    assert subs[0]["agent_type"] == meta["agentType"]


def test_attribute_subagents_assigns_by_start_time_and_falls_back():
    t1 = Turn("first", "2026-01-01T00:00:00Z")
    t2 = Turn("second", "2026-01-01T00:10:00Z")
    subs = [
        {"start_ts": "2026-01-01T00:05:00Z", "agent_type": "a"},
        {"start_ts": None, "agent_type": "b"},
    ]
    attribute_subagents([t1, t2], subs)
    assert t1.subagents == [subs[0], subs[1]]
    assert t2.subagents == []


def test_attribute_subagents_noop_when_no_turns():
    attribute_subagents([], [{"start_ts": "2026-01-01T00:00:00Z"}])  # must not raise


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
    envelope = extract_transcript(FIXTURES / "baseline.jsonl")
    assert [(event["role"], event["text"]) for event in envelope["events"]] == [
        ("user", "Reply with BASELINE_FIXTURE_MARKER and nothing else."),
        ("assistant", "BASELINE_FIXTURE_MARKER"),
    ]


def test_main_writes_json_to_explicit_output():
    transcript = Path(tempfile.mkdtemp()) / "s1.jsonl"
    _write_jsonl(transcript, _conversation_entries())
    out_dir = Path(tempfile.mkdtemp())
    out_file = out_dir / "log.json"
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        rc = main(["--transcript", str(transcript), "--output", str(out_file)])
    assert rc == 0
    assert out_file.exists()
    assert json.loads(out_file.read_text())["events"] == build_events(
        _conversation_entries()
    )
    assert list(out_dir.iterdir()) == [out_file]


def test_main_stdout_outputs_json():
    transcript = Path(tempfile.mkdtemp()) / "s1.jsonl"
    _write_jsonl(transcript, _conversation_entries())
    out = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = main(["--transcript", str(transcript), "--output", "-"])
    assert rc == 0
    assert json.loads(out.getvalue())["events"] == build_events(_conversation_entries())


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
    _write_jsonl(proj / "a.jsonl", _conversation_entries())
    _write_jsonl(proj / "b.jsonl", _conversation_entries())
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
    entries = _conversation_entries()
    entries[0]["message"]["content"][0]["text"] = "# Keep **recorded Markdown**"
    _write_jsonl(transcript, entries)
    output = tmp_path / "output"
    output.mkdir()

    def no_turn_rendering(*args, **kwargs):
        pytest.fail("JSON extraction must not assemble Markdown turns")

    monkeypatch.setattr(eclog, "build_turns", no_turn_rendering)
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
    assert envelope["events"][0]["images"][0]["data"] == _PNG_B64


@pytest.mark.parametrize("explicit_root", [False, True])
@pytest.mark.parametrize("all_flag", [[], ["--all"]])
def test_cli_batch_emits_only_json(
    all_flag, tmp_path, monkeypatch, capsys, explicit_root
):
    project = tmp_path / "project"
    project.mkdir()
    for name in ["a", "b"]:
        _write_jsonl(project / f"{name}.jsonl", _conversation_entries())
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
            _conversation_entries()
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
@pytest.mark.parametrize(
    "fixture",
    [
        "baseline.jsonl",
        "images.jsonl",
        "large_result.jsonl",
        "questions.jsonl",
        "model.jsonl",
        "continued.jsonl",
        "subagent.jsonl",
        "plan_modes.jsonl",
        "permission_denial.jsonl",
        "skill_command.jsonl",
    ],
)
def test_cli_preserves_native_payloads(fixture, limit, capsys):
    transcript = FIXTURES / fixture
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
    _write_jsonl(transcript, _conversation_entries())
    args = ["--project-dir", str(tmp_path), "--output", "-"]
    if selector != "newest":
        args += [str(transcript) if selector == "path" else "session-1"]
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["session_id"] == "session-123"


@pytest.mark.parametrize("mode", ["single", "batch"])
def test_cli_reports_write_errors(mode, tmp_path, capsys):
    transcript = tmp_path / "session.jsonl"
    _write_jsonl(transcript, _conversation_entries())
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


def test_cli_retains_malformed_line_diagnostics(capsys):
    assert (
        main(["--transcript", str(FIXTURES / "truncated.jsonl"), "--output", "-"]) == 0
    )
    captured = capsys.readouterr()
    assert len(json.loads(captured.out)["events"]) == 2
    assert "skipping malformed JSON on line 3" in captured.err


def test_cli_file_keeps_size_warning(tmp_path, monkeypatch, capsys):
    from session_log_extractors import session_log_envelope

    monkeypatch.setattr(session_log_envelope, "SIZE_WARN_BYTES", 1)
    output = tmp_path / "session.json"
    assert (
        main(
            ["--transcript", str(FIXTURES / "baseline.jsonl"), "--output", str(output)]
        )
        == 0
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "session-log uploads are capped" in captured.err
    assert json.loads(output.read_text())["harness"] == "claude"


def test_cli_stdout_keeps_size_warning(monkeypatch, capsys):
    from session_log_extractors import session_log_envelope

    monkeypatch.setattr(session_log_envelope, "SIZE_WARN_BYTES", 1)
    transcript = FIXTURES / "baseline.jsonl"
    assert main(["--transcript", str(transcript), "--output", "-"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["harness"] == "claude"
    assert "warning: stdout JSON is" in captured.err
    assert "consider a lower --tool-result-bytes" in captured.err
    assert "session-log uploads are capped" in captured.err


@pytest.mark.parametrize("mode", ["single", "batch"])
def test_cli_no_event_input_keeps_existing_envelope_omission(mode, tmp_path, capsys):
    transcript = tmp_path / "session.jsonl"
    _write_jsonl(transcript, [{"type": "system"}])
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
    _write_jsonl(transcript, _conversation_entries())
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
def test_interpret_supplied_source_without_filesystem(limit, monkeypatch):
    entries = _conversation_entries()
    source = ClaudeSource(Path("/unavailable/session.jsonl"), entries)

    def forbidden(*args, **kwargs):
        pytest.fail("interpretation accessed the filesystem")

    with monkeypatch.context() as guard:
        for method in ["open", "is_file", "is_dir", "iterdir", "stat", "read_text"]:
            guard.setattr(Path, method, forbidden)
        session = eclog.interpret_source(source, limit)
        assert session["session_id"] == "session"
        assert session["events"] == build_events(entries, limit)
        assert session["cwd"] == "/work/app"
        assert session["started_at"] == first_timestamp(entries)
        assert session["models"] == session_models(entries)
        assert session["events"][0]["images"][0]["data"] == _PNG_B64
        assert eclog.interpret_source(ClaudeSource(source.path, [])) is None


def test_interpret_supplied_subagent_without_filesystem(monkeypatch):
    entries = [
        {
            "type": "user",
            "timestamp": "2026-01-01T00:00:00Z",
            "message": {"content": "task"},
        },
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "first"},
                    {"type": "tool_use", "name": "Read"},
                    {"type": "tool_use", "name": "Read"},
                    {"type": "text", "text": "last"},
                ]
            },
        },
    ]
    source = ClaudeSource(
        Path("/unavailable/agent.jsonl"), entries, {"agentType": "Explore"}
    )

    def forbidden(*args, **kwargs):
        pytest.fail("interpretation accessed the filesystem")

    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(Path, "is_file", forbidden)
    assert eclog.summarize_subagent_source(source) == {
        "agent_type": "Explore",
        "start_ts": "2026-01-01T00:00:00Z",
        "task": "task",
        "tool_count": 2,
        "tool_names": ["Read"],
        "result": "last",
    }
    assert eclog.summarize_subagent_source(ClaudeSource(source.path, [])) is None


@pytest.mark.parametrize("metadata", [None, "bad JSON", "[]", "null", "{}"])
def test_source_reader_metadata_fallback(metadata, tmp_path):
    transcript = tmp_path / "agent.jsonl"
    _write_jsonl(transcript, [{"type": "user", "message": {"content": "task"}}])
    if metadata is not None:
        transcript.with_suffix(".meta.json").write_text(metadata)
    source = read_source(transcript, read_metadata=True)
    assert source.path == transcript
    assert eclog.summarize_subagent_source(source)["agent_type"] == "subagent"


def test_source_reader_metadata_read_error(tmp_path, monkeypatch):
    transcript = tmp_path / "agent.jsonl"
    _write_jsonl(transcript, [{"type": "user"}])
    transcript.with_suffix(".meta.json").touch()

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
        _write_jsonl(directory / f"{name}.jsonl", [{"type": "user"}])
    (directory / "ignored.txt").touch()
    (directory / "directory.jsonl").mkdir()
    sources = read_subagent_sources(transcript)
    assert [source.path.name for source in sources] == ["a.jsonl", "b.jsonl"]
    assert all(source.path.parent == directory for source in sources)


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
        _write_jsonl(
            directory / f"{name}.jsonl",
            [{"type": "user", "timestamp": timestamp, "message": {"content": name}}],
        )
    (directory / "empty.jsonl").touch()
    # A directory at the metadata path must not be read for an empty sidecar.
    (directory / "empty.meta.json").mkdir()
    assert [summary["task"] for summary in load_subagents(transcript)] == [
        "d",
        "b",
        "c",
        "a",
    ]


def test_main_source_does_not_read_sidecars_or_metadata(tmp_path, monkeypatch):
    transcript = tmp_path / "session.jsonl"
    entries = _conversation_entries()
    _write_jsonl(transcript, entries)

    def forbidden(*args, **kwargs):
        pytest.fail("main JSON extraction attempted sidecar discovery")

    with monkeypatch.context() as guard:
        guard.setattr(Path, "iterdir", forbidden)
        guard.setattr(Path, "is_file", forbidden)
        guard.setattr(Path, "read_text", forbidden)
        envelope = extract_transcript(transcript)
    assert envelope == build_raw_envelope(transcript, entries)
