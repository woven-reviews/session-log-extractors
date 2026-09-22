#!/usr/bin/env python3
"""Minimal asserts for the option-question parsing in extract_claude_session_log.

Run: python3 scripts/test_extract_claude_session_log.py
"""

from __future__ import annotations

import base64
import os
import shutil
import tempfile
from pathlib import Path, PurePosixPath, PureWindowsPath

import extract_claude_session_log as claude_log
from extract_claude_session_log import (
    Turn,
    _candidate_project_dirs,
    _parse_chosen,
    build_events,
    build_turns,
    clean_user_text,
    dump_images,
    encode_project_path,
    load_entries,
    main,
    parse_permission_denial,
    permission_denials_from_result,
    render,
    select_transcript,
)

# Real, trimmed, redacted Claude Code transcript -- see tests/fixtures/claude/.
CLAUDE_FIXTURE = (
    Path(__file__).resolve().parent.parent / "tests/fixtures/claude/session.jsonl"
)

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


def test_single_choice():
    res = 'Your questions have been answered: "Q"="section". continue.'
    val, chosen = _parse_chosen("Q", OPTS, res)
    assert val == "section", val
    assert [o["label"] for o in chosen] == ["section"]


def test_multiselect_comma():
    res = '"Q"="section, manufacturer_part_no". continue.'
    val, chosen = _parse_chosen("Q", OPTS, res)
    assert [o["label"] for o in chosen] == ["section", "manufacturer_part_no"], chosen


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
    assert out == [{"tool": "Bash — rm -rf build", "message": "no"}]


def test_custom_answer_matches_no_option():
    res = '"Q"="something the user typed". continue.'
    val, chosen = _parse_chosen("Q", OPTS, res)
    assert val == "something the user typed"
    assert chosen == []


def test_multi_question_anchors_on_its_own_text():
    res = '"Q1"="section", "Q2"="Neither". continue.'
    _, c1 = _parse_chosen("Q1", OPTS, res)
    _, c2 = _parse_chosen("Q2", OPTS, res)
    assert [o["label"] for o in c1] == ["section"], c1
    assert [o["label"] for o in c2] == ["Neither"], c2


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


def test_dump_images_writes_file_and_marker():
    turn = Turn("look at this", None)
    turn.images = [{"type": "base64", "media_type": "image/png", "data": _PNG_B64}]
    dump_dir = Path(tempfile.mkdtemp())
    written = dump_images([turn], "sess123", dump_dir=dump_dir)
    assert len(written) == 1
    p = written[0]
    assert p.exists() and p.name == "sess123_turn1_img1.png"
    assert p.read_bytes() == base64.b64decode(_PNG_B64)
    assert "description pending" in turn.user_text
    assert str(p) in turn.user_text


def test_dump_images_noop_without_images():
    turn = Turn("no images here", None)
    dump_dir = Path(tempfile.mkdtemp())
    assert dump_images([turn], "s", dump_dir=dump_dir) == []
    assert turn.user_text == "no images here"


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
    events = build_events(_conversation_entries())
    user = events[0]
    assert user["role"] == "user"
    assert user["text"] == "here is the mock"
    assert base64.b64decode(user["images"][0]["data"]) == base64.b64decode(_PNG_B64)
    assert user["images"][0]["media_type"] == "image/png"


def test_build_events_attaches_tool_results_to_their_call():
    events = build_events(_conversation_entries(), tool_result_max_bytes=100)
    call = events[1]["tool_calls"][0]
    assert call["name"] == "Read"
    assert call["input"] == {"file_path": "/work/app/models.py"}
    assert call["result_bytes"] == 9000
    assert call["result_truncated"] is True
    assert len(call["result"]) == 100


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


def test_build_events_does_not_disturb_the_markdown_turns():
    # The whole point of a second pass: running it must not perturb build_turns.
    entries = _conversation_entries()
    before = [t.user_text for t in build_turns(entries)[0]]
    build_events(entries)
    assert [t.user_text for t in build_turns(entries)[0]] == before


def test_plan_approved_is_captured():
    turns, _ = build_turns(
        _plan_entries(
            "User has approved your plan. You can now start coding.\n\n"
            "## Approved Plan (edited by user):\n# Plan\n\nStep one."
        )
    )
    (plan,) = turns[0].plans
    assert plan["plan"] == "# Plan\n\nStep one."
    assert plan["decision"] == "approved"
    # Unchanged plan echoed back under the "edited" heading is not an edit.
    assert plan["edited_plan"] == ""
    # The plan is rendered as its own block, not as a tool bullet.
    assert turns[0].tool_bullets == []
    assert turns[0].result_notes == []


def test_plan_approved_with_edit_keeps_final_version():
    turns, _ = build_turns(
        _plan_entries(
            "User has approved your plan.\n\n"
            "## Approved Plan (edited by user):\n# Plan\n\nStep one, but smaller."
        )
    )
    (plan,) = turns[0].plans
    assert plan["decision"] == "approved"
    assert plan["edited_plan"] == "# Plan\n\nStep one, but smaller."


def test_plan_rejected_keeps_steering_message():
    turns, _ = build_turns(
        _plan_entries(
            "The user doesn't want to proceed with this tool use. The tool use "
            "was rejected. the user said:\nToo big, split it up"
        )
    )
    (plan,) = turns[0].plans
    assert plan["decision"] == "rejected"
    assert plan["message"] == "Too big, split it up"
    # Routed to the plan record, not the generic permission-denial list.
    assert turns[0].permission_denials == []


def test_plan_rejection_reason_is_captured_and_rendered():
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
    md = render(turns, [], "proj", None)
    assert "> wrong table, use line_items" in md
    assert "> and keep it in one migration" in md


def test_plan_without_result_is_marked_undecided():
    entries = _plan_entries("")[:2]
    (plan,) = build_turns(entries)[0][0].plans
    assert plan["decision"] == "no decision recorded"


# --------------------------------------------------------------------------- #
# Fixture-driven: real (trimmed, redacted) transcript, session selection,
# --strict, malformed input, and main() end-to-end.
# --------------------------------------------------------------------------- #


def test_load_entries_parses_the_real_fixture():
    entries = load_entries(CLAUDE_FIXTURE)
    assert len(entries) == 10
    assert entries[1]["type"] == "user"
    assert "subagent definition" in entries[1]["message"]["content"]


def test_load_entries_skips_malformed_and_blank_lines(tmp_path):
    corrupted = tmp_path / "session.jsonl"
    real_text = CLAUDE_FIXTURE.read_text(encoding="utf-8")
    corrupted.write_text(real_text + "\n{this is not json\n\n", encoding="utf-8")
    entries = load_entries(corrupted)
    # The real entries all still parse; only the corrupt/blank tail is skipped.
    assert len(entries) == 10


def test_select_transcript_project_dir_picks_newest(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    older = proj / "aaaaaaaa-0000-0000-0000-000000000001.jsonl"
    newer = proj / "bbbbbbbb-0000-0000-0000-000000000002.jsonl"
    shutil.copy(CLAUDE_FIXTURE, older)
    shutil.copy(CLAUDE_FIXTURE, newer)
    os.utime(older, (1, 1))
    os.utime(newer, (2, 2))
    assert select_transcript(None, None, str(proj)) == newer


def test_select_transcript_session_id_prefix_match(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    target = proj / "cccccccc-1111-2222-3333-444444444444.jsonl"
    other = proj / "dddddddd-1111-2222-3333-444444444444.jsonl"
    shutil.copy(CLAUDE_FIXTURE, target)
    shutil.copy(CLAUDE_FIXTURE, other)
    assert select_transcript("cccccccc", None, str(proj)) == target


def test_select_transcript_ambiguous_prefix_raises(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    shutil.copy(CLAUDE_FIXTURE, proj / "abc11111-0000-0000-0000-000000000000.jsonl")
    shutil.copy(CLAUDE_FIXTURE, proj / "abc22222-0000-0000-0000-000000000000.jsonl")
    try:
        select_transcript("abc", None, str(proj))
    except FileNotFoundError as exc:
        assert "ambiguous" in str(exc)
    else:
        raise AssertionError("expected FileNotFoundError")


def test_select_transcript_strict_disables_parent_walk_and_global_fallback(
    monkeypatch, tmp_path
):
    projects_root = tmp_path / "claude_projects"
    projects_root.mkdir()
    cwd = tmp_path / "work" / "sub"
    cwd.mkdir(parents=True)
    parent_dir = projects_root / claude_log.encode_project_path(cwd.parent)
    parent_dir.mkdir()
    shutil.copy(CLAUDE_FIXTURE, parent_dir / "session.jsonl")

    monkeypatch.setattr(claude_log, "PROJECTS_ROOT", projects_root)
    monkeypatch.setattr(Path, "cwd", staticmethod(lambda: cwd))

    # Non-strict: the parent-dir walk finds the parent's transcript dir.
    assert select_transcript(None, None, None, strict=False) == (
        parent_dir / "session.jsonl"
    )

    # Strict: no parent-walk and no global fallback -- nothing matches.
    try:
        select_transcript(None, None, None, strict=True)
    except FileNotFoundError as exc:
        assert "--strict" in str(exc)
    else:
        raise AssertionError("expected FileNotFoundError")


def test_main_end_to_end_renders_the_real_fixture(tmp_path):
    out_path = tmp_path / "out.md"
    rc = main(
        [
            "--transcript",
            str(CLAUDE_FIXTURE),
            "--output",
            str(out_path),
            "--no-raw",
        ]
    )
    assert rc == 0
    assert out_path.is_file()
    markdown = out_path.read_text(encoding="utf-8")
    assert "subagent definition" in markdown
    assert "Trade-off is cost/speed" in markdown


def test_main_reports_missing_transcript_and_exits_nonzero(tmp_path, capsys):
    missing = tmp_path / "nope.jsonl"
    rc = main(["--transcript", str(missing)])
    assert rc == 1
    assert "does not exist" in capsys.readouterr().err


if __name__ == "__main__":
    import inspect

    for name, fn in sorted(globals().items()):
        if not (name.startswith("test_") and callable(fn)):
            continue
        if inspect.signature(fn).parameters:
            continue  # needs pytest fixtures (monkeypatch/tmp_path/...); run via pytest
        fn()
        print(f"ok  {name}")
    print("all passed")
