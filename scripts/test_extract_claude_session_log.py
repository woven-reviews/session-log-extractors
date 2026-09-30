#!/usr/bin/env python3
"""Minimal asserts for the option-question parsing in extract_claude_session_log.

Run: python3 scripts/test_extract_claude_session_log.py
"""

from __future__ import annotations

import base64
import json
import pytest
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

CLAUDE_FIXTURE = (
    Path(__file__).resolve().parent.parent / "tests/fixtures/claude/session.jsonl"
)

# Real, trimmed, redacted transcript covering a real `/fixture-skill` slash
# invocation and two real non-interactive permission denials (a headless `-p`
# run with no human to answer the prompt) -- see tests/fixtures/claude/.
CLAUDE_SKILL_DENIAL_FIXTURE = (
    Path(__file__).resolve().parent.parent
    / "tests/fixtures/claude/skill_and_denial_session.jsonl"
)

# Real, trimmed, redacted transcript covering a real ExitPlanMode approval
# (via the Agent SDK's canUseTool, driven headless -- see
# scripts/fixtures/generate_fixtures.sh) -- see tests/fixtures/claude/.
CLAUDE_PLAN_FIXTURE = (
    Path(__file__).resolve().parent.parent / "tests/fixtures/claude/plan_session.jsonl"
)

CLAUDE_PASTED_IMAGE_FIXTURE = (
    Path(__file__).resolve().parent.parent
    / "tests/fixtures/claude/pasted_image_session.jsonl"
)

# Real, redacted transcript covering a real subagent spawn (the "Agent" tool,
# general-purpose type, run as an async background task -- see
# scripts/fixtures/generate_fixtures.sh). Its sibling
# tests/fixtures/claude/subagent_session/subagents/ directory is the real
# on-disk shape load_subagents expects. Confirmed by this real capture: the
# background-task completion never sets isSidechain on any main-transcript
# entry (it only arrives as a <task-notification> block that clean_user_text
# already strips as noise), so subagent_count stays 0 even though a real
# subagent ran -- turn.subagents (populated separately, from the subagents/
# directory scan) is what actually carries it. See
# test_real_fixture_captures_subagent_spawn.
CLAUDE_SUBAGENT_FIXTURE = (
    Path(__file__).resolve().parent.parent
    / "tests/fixtures/claude/subagent_session.jsonl"
)

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

# The toolDenialKind fallback (a headless run auto-denying with no human to
# answer) is covered with real fixture data by
# test_real_fixture_captures_non_interactive_auto_deny below.

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
    assert len(loose) > 1
    assert strict[0] == loose[0]

def test_encode_project_path_does_not_collapse_runs():
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
    # Real fixture: an Agent SDK streaming-input pasted image (see
    # pasted_image_session.jsonl) -- a top-level user message image block,
    # not a tool result.
    entries = load_entries(CLAUDE_PASTED_IMAGE_FIXTURE)
    user = build_events(entries)[0]
    assert user["role"] == "user"
    assert user["text"] == "Describe what is in the attached image, in one sentence."
    assert base64.b64decode(user["images"][0]["data"]).startswith(b"\x89PNG")
    assert user["images"][0]["media_type"] == "image/png"

def test_build_events_inlines_images_returned_inside_a_tool_result():
    # `@path` (or any tool reading an image file back, e.g. Read on a
    # screenshot) returns the image nested in a tool_result's own content
    # list, not as a top-level message image like a pasted attachment. Real
    # fixture: the assistant re-reads the same pasted image via a real Read
    # tool call (see pasted_image_session.jsonl).
    entries = load_entries(CLAUDE_PASTED_IMAGE_FIXTURE)
    call = next(c for e in build_events(entries) for c in e.get("tool_calls", []))
    assert call["name"] == "Read"
    assert base64.b64decode(call["images"][0]["data"]).startswith(b"\x89PNG")
    assert call["images"][0]["media_type"] == "image/png"

def test_build_events_attaches_tool_results_to_their_call():
    entries = load_entries(CLAUDE_FIXTURE)
    events = build_events(entries, tool_result_max_bytes=5)
    call = next(c for e in events for c in e.get("tool_calls", []))
    assert call["name"] == "Bash"
    assert call["input"]["command"].startswith("ls -la .claude/agents")
    assert call["result_truncated"] is True
    assert len(call["result"]) == 5

def test_build_events_keeps_full_tool_input():
    entries = load_entries(CLAUDE_FIXTURE)
    call = next(
        c for e in build_events(entries) for c in e.get("tool_calls", [])
    )
    full_command = call["input"]["command"]
    assert len(full_command) > 100
    assert full_command == entries[3]["message"]["content"][0]["input"]["command"]

def test_build_events_skips_sidechain_and_meta_like_build_turns():
    # Events and turns must describe the same conversation, or a grader reading
    # the raw log sees something the markdown never showed them. No real
    # fixture captured a subagent (isSidechain) entry, so those two noise
    # entries stay synthetic, appended to an otherwise real transcript.
    entries = load_entries(CLAUDE_FIXTURE) + [
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
    entries = load_entries(CLAUDE_SKILL_DENIAL_FIXTURE)
    events = build_events(entries)
    assert events[0]["text"] == "/fixture-skill"

def test_build_events_does_not_disturb_the_markdown_turns():
    # The whole point of a second pass: running it must not perturb build_turns.
    entries = load_entries(CLAUDE_FIXTURE)
    before = [t.user_text for t in build_turns(entries)[0]]
    build_events(entries)
    assert [t.user_text for t in build_turns(entries)[0]] == before

def test_plan_approved_is_captured():
    # Real fixture: a genuine ExitPlanMode approval via the Agent SDK's
    # canUseTool, driven headless (see scripts/fixtures/generate_fixtures.sh
    # and tests/fixtures/claude/plan_session.jsonl). Unedited, the real
    # approval wording carries no "(edited by user)" heading at all.
    turns, _ = build_turns(load_entries(CLAUDE_PLAN_FIXTURE))
    (plan,) = turns[0].plans
    assert "divide(a, b)" in plan["plan"]
    assert plan["decision"] == "approved"
    assert plan["edited_plan"] == ""
    # The plan is rendered as its own block, not as a tool bullet.
    assert turns[0].tool_bullets == []
    assert turns[0].result_notes == []

def test_plan_approved_unchanged_text_under_edited_heading_is_not_an_edit():
    # Defensive case, not confirmed by the real fixture above (which has no
    # "(edited by user)" heading at all when unedited): an unchanged echo of
    # the same plan text under that heading must still collapse to "no edit".
    turns, _ = build_turns(
        _plan_entries(
            "User has approved your plan. You can now start coding.\n\n"
            "## Approved Plan (edited by user):\n# Plan\n\nStep one."
        )
    )
    (plan,) = turns[0].plans
    assert plan["plan"] == "# Plan\n\nStep one."
    assert plan["decision"] == "approved"
    assert plan["edited_plan"] == ""

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

def test_real_fixture_slash_command_invokes_a_real_skill():
    # `/fixture-skill` here is a real invocation via --add-dir (the documented
    # exception to --bare's skill-loading skip), not the synthetic tool_use
    # the earlier unit test above fabricates.
    entries = load_entries(CLAUDE_SKILL_DENIAL_FIXTURE)
    turns, _ = build_turns(entries)
    assert turns[0].command == "/fixture-skill"
    assert turns[0].skills_used == ["fixture-skill"]

    md = render(turns, [], "project", entries[0]["timestamp"])
    assert "_Skill used:_ **fixture-skill**" in md
    assert "Welcome to the Fixture Skill" in md

def test_real_fixture_captures_non_interactive_auto_deny():
    # A headless `-p` run with no human to answer the permission prompt --
    # confirmed to auto-deny with a message that doesn't match either canned
    # prefix, so this only works via the toolDenialKind fallback.
    entries = load_entries(CLAUDE_SKILL_DENIAL_FIXTURE)
    turns, _ = build_turns(entries)
    denials = turns[1].permission_denials
    assert len(denials) == 2
    assert all(d["tool"] == "Bash — rm calculator.py" for d in denials)
    assert "needs approval" in denials[0]["message"]

    md = render(turns, [], "project", entries[0]["timestamp"])
    assert "_user denied permission:_ Bash — rm calculator.py" in md

@pytest.fixture
def claude_interaction_entries():
    """Structured dialog fixture: option answers, meta skills, and sidechains."""
    return [
        {
            "type": "user",
            "timestamp": "2026-01-01T00:00:00Z",
            "message": {"content": "Choose an option"},
        },
        {
            "type": "user",
            "isSidechain": True,
            "message": {"content": "delegate this"},
        },
        {
            "type": "assistant",
            "timestamp": "2026-01-01T00:00:01Z",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "question-1",
                        "name": "AskUserQuestion",
                        "input": {
                            "questions": [
                                {
                                    "header": "Color",
                                    "question": "Which color?",
                                    "options": [
                                        {"label": "Blue", "description": "cool"},
                                        {"label": "Red"},
                                    ],
                                },
                                "malformed-question",
                            ]
                        },
                    },
                    {
                        "type": "tool_use",
                        "id": "write-1",
                        "name": "Write",
                        "input": {"file_path": "out.txt", "content": "x"},
                    },
                    {
                        "type": "tool_use",
                        "id": "write-2",
                        "name": "Write",
                        "input": {"file_path": "out.txt", "content": "again"},
                    },
                ]
            },
        },
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "question-1",
                        "content": [
                            {"type": "text", "text": '"Which color?"="Blue"'},
                            {"type": "image", "source": {"type": "base64", "data": "AA=="}},
                            {"type": "image", "source": {"type": "url", "data": "ignore"}},
                        ],
                    },
                    {
                        "type": "tool_result",
                        "tool_use_id": "write-1",
                        "is_error": True,
                        "content": "write failed",
                    },
                    {
                        "type": "tool_result",
                        "tool_use_id": "write-2",
                        "content": [{"type": "text", "text": "done"}],
                    },
                ]
            },
        },
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "Finished"}]},
        },
        {
            "type": "user",
            "isMeta": True,
            "message": {
                "content": "Base directory for this skill: "
                "/repo/.agents/skills/fixture-skill\n"
            },
        },
        {
            "type": "user",
            "isSidechain": True,
            "message": {"content": "last delegate"},
        },
        {"type": "summary", "message": {"content": "ignored"}},
        {"type": "unknown"},
    ]

@pytest.fixture
def transcript_fixtures(tmp_path):
    from test_extract_codex_session_log import CODEX_FIXTURE

    claude_path = tmp_path / "claude.jsonl"
    codex_path = tmp_path / "codex.jsonl"
    shutil.copyfile(CLAUDE_FIXTURE, claude_path)
    shutil.copyfile(CODEX_FIXTURE, codex_path)
    return {"claude": claude_path, "codex": codex_path}

def test_claude_interaction_fixture_routes_answers_results_and_subagents(
    claude_interaction_entries,
):
    turns, files = claude_log.build_turns(claude_interaction_entries)
    assert len(turns) == 1
    turn = turns[0]
    assert turn.subagent_count == 2
    assert turn.skills_used == ["fixture-skill"]
    assert files == ["out.txt"]
    assert turn.option_qas[0]["chosen_labels"] == ["Blue"]
    assert turn.result_notes == ["error: write failed done"]
    assert turn.assistant_text_blocks == ["Finished"]
    assert claude_log._is_tool_result_entry(
        {"toolUseResult": {"stdout": "ok"}, "message": {"content": ""}}
    )
    assert not claude_log._is_tool_result_entry({"message": {"content": 5}})

def test_claude_result_and_descriptor_helpers_cover_fallbacks():
    assert claude_log._content_blocks({"message": {"content": 9}}) == []
    assert claude_log._human_text({"message": {"content": ["raw", {"type": "text"}]}}) == (
        "raw"
    )
    assert claude_log.extract_file_path(None) is None
    assert claude_log.extract_file_path({"file_path": "", "path": "src/a.py"}) == "src/a.py"
    assert claude_log.tool_descriptor("Tool", None) == ""
    assert claude_log.tool_descriptor("Tool", {"file_path": 3, "query": "find me"}) == (
        "find me"
    )
    assert claude_log.result_note(
        {"toolUseResult": {"stderr": "first", "output": "second"}}
    ) == "first"
    assert claude_log.result_note(
        {
            "message": {
                "content": [
                    {"type": "tool_result", "is_error": True, "content": []}
                ]
            }
        }
    ) == "error"
    assert claude_log._result_block_text({"content": 5}) == ""
    assert claude_log._result_block_images({"content": "not a list"}) == []
    assert claude_log._parse_chosen(
        "question", [{"label": "A"}], 'Other="free answer'
    ) == ("free answer", [])
    assert claude_log.option_qa_from_result({}, {}) == []
    assert claude_log.apply_plan_decisions({}, {}) is False
    assert claude_log.parse_plan_decision("User has approved your plan.") == (
        "approved",
        "",
        "",
    )
    assert claude_log.parse_plan_decision(None) == ("unknown", "", "")
    assert claude_log._skill_from_meta_companion(
        {"message": {"content": "Base directory for this skill:\n"}}
    ) is None

def test_claude_rendering_includes_all_optional_turn_sections():
    turn = claude_log.Turn("first\n\nsecond", "unused")
    turn.timestamp = "2026-01-01T00:00:01Z"
    turn.command = "/review"
    turn.command_args = "src"
    turn.skills_used = ["skill"]
    turn.skill_details = {"skill": {"description": "does things"}}
    turn.option_qas = [
        {
            "header": "",
            "question": "Pick one",
            "options": [{"label": "A", "description": "alpha"}],
            "chosen_labels": [],
            "answer_text": "Other answer",
        }
    ]
    turn.permission_denials = [{"tool": "Write", "message": "no\nthanks"}]
    turn.plans = [
        {
            "plan": "step one",
            "decision": "rejected",
            "edited_plan": "",
            "message": "change it",
        }
    ]
    turn.result_notes = [f"result {i}" for i in range(8)]
    turn.subagents = [
        {
            "agent_type": "explore",
            "task": "inspect",
            "tool_count": 2,
            "tool_names": ["Read"],
            "result": "found it",
        },
        {"agent_type": "task", "task": "", "tool_count": 0, "tool_names": [], "result": ""},
    ]
    markdown = claude_log.render([turn], ["out.py"], "project", turn.timestamp, ["model"])
    assert "_(after editing it)_" not in markdown
    assert "Other answer" in markdown
    assert "change it" in markdown
    assert "(+2 more results)" in markdown
    assert "subagent (explore):_ inspect" in markdown
    assert "subagent (task)_" in markdown
    assert "## Files changed during the session" in markdown
    assert "Model: model." in markdown
    assert claude_log.render([], [], "project", None).find("No conversational turns") > 0
    assert "No user inputs" in "\n".join(claude_log.render_summary([], None))
    assert claude_log.derive_project_label(Path("/tmp/session.jsonl")) == (
        "/tmp  (dir: tmp)"
    )
    assert claude_log.derive_project_label(Path("/tmp/-repo/session.jsonl"), []) == (
        "/repo  (dir: -repo)"
    )
    assert claude_log.session_cwd([{"cwd": "/work"}, {"cwd": "/other"}]) == "/work"
    assert claude_log.session_cwd([{}]) is None

def test_real_fixture_captures_subagent_spawn():
    # Real capture: a genuine "Agent" tool spawn (general-purpose type), run
    # as Claude's async background-task path -- see CLAUDE_SUBAGENT_FIXTURE
    # above for what this confirms and the one real nuance it exposes.
    entries = claude_log.load_entries(CLAUDE_SUBAGENT_FIXTURE)
    turns, files = claude_log.build_turns(entries)
    subs = claude_log.load_subagents(CLAUDE_SUBAGENT_FIXTURE)
    claude_log.attribute_subagents(turns, subs)

    assert len(turns) == 1
    turn = turns[0]
    # The confirmed nuance: subagent_count (isSidechain-based) stays 0 for
    # this real background-task shape; turn.subagents (directory-scan-based)
    # is what actually reflects the real subagent run.
    assert turn.subagent_count == 0
    assert len(turn.subagents) == 1
    subagent = turn.subagents[0]
    assert subagent["agent_type"] == "general-purpose"
    assert subagent["tool_names"] == ["Read"]
    assert "subtract" in subagent["result"]

    md = claude_log.render(turns, files, "project", entries[0]["timestamp"])
    assert "_subagent (general-purpose):_" in md

def test_claude_subagent_malformed_files_and_timestamp_fallback(tmp_path):
    # Defensive/error-handling paths not exercised by the real fixture above
    # (a malformed subagent file, and an unparseable start_ts during
    # attribution) -- kept synthetic since these are edge cases, not a real
    # captured shape.
    session = tmp_path / "session"
    subdir = session / "subagents"
    subdir.mkdir(parents=True)
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("{}", encoding="utf-8")
    agent = subdir / "agent-1.jsonl"
    agent.write_text(
        "\n".join(
            json.dumps(item)
            for item in [
                {"type": "user", "timestamp": "2026-01-01T00:00:02Z", "message": {"content": "task"}},
                {"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "name": "Read"},
                    {"type": "text", "text": "partial"},
                    {"type": "text", "text": "final"},
                ]}},
            ]
        ),
        encoding="utf-8",
    )
    (subdir / "agent-1.meta.json").write_text(
        '{"agentType":"explore"}', encoding="utf-8"
    )
    (subdir / "agent-bad.jsonl").write_text("not json", encoding="utf-8")
    (subdir / "agent-bad.meta.json").write_text("{", encoding="utf-8")
    assert claude_log.summarize_subagent(agent) == {
        "agent_type": "explore",
        "start_ts": "2026-01-01T00:00:02Z",
        "task": "task",
        "tool_count": 1,
        "tool_names": ["Read"],
        "result": "final",
    }
    assert len(claude_log.load_subagents(transcript)) == 1
    first = claude_log.Turn("", None)
    second = claude_log.Turn("", "unused")
    first.timestamp = "bad"
    second.timestamp = "2026-01-01T00:00:03Z"
    claude_log.attribute_subagents(
        [first, second],
        [{"start_ts": "invalid", "agent_type": "subagent"}],
    )
    assert len(first.subagents) == 1
    assert second.subagents == []


def test_claude_selection_loading_and_single_session_cli(
    transcript_fixtures, monkeypatch, tmp_path, capsys
):
    source = transcript_fixtures["claude"]
    assert claude_log.select_transcript(str(source), None, None) == source
    assert claude_log.select_transcript(None, str(source), None) == source
    with pytest.raises(FileNotFoundError):
        claude_log.select_transcript(None, str(tmp_path / "missing.jsonl"), None)
    assert claude_log.newest([]) is None

    projects = tmp_path / "projects"
    projects.mkdir()
    fallback_dir = projects / "-elsewhere"
    fallback_dir.mkdir()
    fallback = fallback_dir / "global.jsonl"
    shutil.copyfile(source, fallback)
    monkeypatch.setattr(claude_log, "PROJECTS_ROOT", projects)
    monkeypatch.setattr(Path, "cwd", staticmethod(lambda: tmp_path / "unknown"))
    assert claude_log.select_transcript(None, None, None) == fallback
    with pytest.raises(FileNotFoundError, match="--strict"):
        claude_log.select_transcript(None, None, None, strict=True)

    out = claude_log.main(["--transcript", str(source), "--output", "-", "--no-raw"])
    assert out == 0
    assert "Session Conversation Log" in capsys.readouterr().out

    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    assert claude_log.main(["--transcript", str(empty), "--output", "-"]) == 1
    assert "no parseable JSON entries" in capsys.readouterr().err
    assert claude_log.render_transcript(source, None, raw_output=tmp_path)
    assert list(tmp_path.glob("claude_session_log_raw_*.json"))

def test_claude_all_session_export_skips_bad_transcripts(tmp_path, capsys):
    project = tmp_path / "project"
    project.mkdir()
    shutil.copyfile(CLAUDE_FIXTURE, project / "valid.jsonl")
    (project / "empty.jsonl").write_text("", encoding="utf-8")
    empty_dir = tmp_path / "empty-dir"
    empty_dir.mkdir()
    out = tmp_path / "out"
    assert claude_log._extract_all(project, str(out), raw=False) == 0
    assert (out / "session_log_valid.md").is_file()
    assert "skip empty" in capsys.readouterr().err
    assert claude_log._extract_all(project / "missing", str(out)) == 1
    assert claude_log._extract_all(project / "empty-dir", str(out)) == 1

def test_claude_dump_and_timing_helpers_handle_invalid_inputs(tmp_path):
    turn = claude_log.Turn("image", "unused")
    turn.images = [
        {"data": "%%%", "media_type": "image/png"},
        {"data": "QQ==", "media_type": "image/svg+xml"},
    ]
    dumped = claude_log.dump_images([turn], "session", tmp_path)
    assert [path.name for path in dumped] == [
        "session_turn1_img1.png",
        "session_turn1_img2.img",
    ]
    assert dumped[0].read_bytes() == b""
    assert "description pending" in turn.user_text
    assert claude_log.parse_plan_decision("not a decision") == ("unknown", "", "")
    assert claude_log.format_timestamp("not a date") == "not a date"
    assert claude_log.format_elapsed("2026-01-01T00:00:02Z", "2026-01-01T00:00:01Z") == (
        "+0s"
    )
    assert claude_log.first_timestamp([{}, {"timestamp": None}]) is None
    assert claude_log.derive_project_label(Path("bare.jsonl"), []) == "."

def test_result_and_tool_descriptors_reject_non_json_values():
    import extract_codex_session_log as codex
    import extract_copilot_session_log as copilot
    assert copilot.tool_descriptor("tool", "not json") == "not json"
    assert copilot.tool_descriptor("tool", {"custom": object()}) == ""
    assert copilot.result_note(None) == ""
    assert copilot.result_note(object()) == ""
    assert copilot.result_note({"output": [1, 2]}) == '{"output": [1, 2]}'
    assert codex.result_note({"output": "x"}) == '{"output": "x"}'
    assert codex.tool_descriptor("tool", {"arg": object()}) == ""
    assert claude_log.tool_descriptor("tool", {"arg": object()}) == ""
    assert copilot.format_timestamp("not a date") == "not a date"
    assert copilot.format_elapsed("bad", "2026-01-01T00:00:00Z") == ""
    assert copilot.format_elapsed(
        "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"
    ) == "+0s"
    assert copilot.format_elapsed(
        "2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z"
    ) == "+1:00:00"
    assert copilot.date_only(None) == "unknown"
    turn = copilot.Turn("", "", None, 0)
    turn.add_tool("Read", "")
    assert turn.tool_bullets == ["- Read"]

def test_claude_single_session_cli_reports_write_error(transcript_fixtures, tmp_path, capsys):
    source = transcript_fixtures["claude"]
    output_dir = tmp_path / "directory"
    output_dir.mkdir()
    assert claude_log.main(["--transcript", str(source), "--output", str(output_dir), "--no-raw"]) == 1
    assert "could not write" in capsys.readouterr().err

if __name__ == "__main__":
    import inspect

    for name, fn in sorted(globals().items()):
        if not (name.startswith("test_") and callable(fn)):
            continue
        if inspect.signature(fn).parameters:
            continue
        fn()
        print(f"ok  {name}")
    print("all passed")
