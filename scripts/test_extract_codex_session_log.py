#!/usr/bin/env python3
"""Minimal asserts for extract_codex_session_log.

Run: python3 scripts/test_extract_codex_session_log.py
"""

from __future__ import annotations

import base64
import json
import pytest
import extract_codex_session_log as codex
import os
import shutil
import tempfile
from pathlib import Path

from extract_codex_session_log import (
    build_events,
    build_turns,
    clean_user_text,
    dump_images,
    load_entries,
    main,
    parse_permission_denial,
    render,
    select_transcript,
    skill_names_from_call,
)

CODEX_FIXTURE = (
    Path(__file__).resolve().parent.parent / "tests/fixtures/codex/session.jsonl"
)

CODEX_SKILL_IMAGE_FIXTURE = (
    Path(__file__).resolve().parent.parent
    / "tests/fixtures/codex/skill_and_image_session.jsonl"
)

_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9"
    "awAAAABJRU5ErkJggg=="
)

QUESTIONS = [
    {
        "id": "team",
        "header": "Team",
        "question": "Pick a team",
        "options": [
            {"label": "QUAL", "description": "qualification"},
            {"label": "SE", "description": "solutions"},
        ],
    }
]

def test_codex_structured_skill_call_is_recorded():
    entries = [
        {
            "type": "event_msg",
            "timestamp": "2026-01-01T00:00:00Z",
            "payload": {"type": "user_message", "message": "Make a PDF"},
        },
        {
            "type": "response_item",
            "timestamp": "2026-01-01T00:00:01Z",
            "payload": {
                "type": "function_call",
                "name": "skills.read",
                "call_id": "skill-1",
                "arguments": json.dumps({"package": "pdf:pdf"}),
            },
        },
    ]
    turns, _ = build_turns(entries)
    assert turns[0].skills_used == ["pdf:pdf"]

def test_codex_initial_response_item_user_message_is_exported_once():
    entries = [
        {
            "type": "response_item",
            "timestamp": "2026-01-01T00:00:00Z",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Initial task"}],
            },
        },
        {
            "type": "response_item",
            "timestamp": "2026-01-01T00:00:01Z",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Working on it"}],
            },
        },
        {
            "type": "response_item",
            "timestamp": "2026-01-01T00:00:02Z",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Follow up"}],
            },
        },
        {
            "type": "event_msg",
            "timestamp": "2026-01-01T00:00:02Z",
            "payload": {"type": "user_message", "message": "Follow up"},
        },
    ]

    turns, _ = build_turns(entries)
    assert [turn.user_text for turn in turns] == ["Initial task", "Follow up"]
    assert [event["text"] for event in build_events(entries) if event["role"] == "user"] == [
        "Initial task",
        "Follow up",
    ]

def test_codex_slash_command_is_recorded_and_rendered_like_claude():
    entries = [
        {
            "type": "event_msg",
            "timestamp": "2026-01-01T00:00:00Z",
            "payload": {
                "type": "user_message",
                "message": "/work-ticket QUAL-2012",
            },
        }
    ]

    turns, files_changed = build_turns(entries)

    assert len(turns) == 1
    assert turns[0].user_text == ""
    assert turns[0].command == "/work-ticket"
    assert turns[0].command_args == "QUAL-2012"

    md = render(turns, files_changed, "project", "2026-01-01T00:00:00Z")
    assert "Invoked command: **/work-ticket** `QUAL-2012`" in md
    assert "_Command used:_ **/work-ticket**" in md
    assert "**User invoked command:** /work-ticket `QUAL-2012`" in md

def test_codex_tagged_slash_command_is_recorded():
    entries = [
        {
            "type": "event_msg",
            "timestamp": "2026-01-01T00:00:00Z",
            "payload": {
                "type": "user_message",
                "message": (
                    "<command-name>/review</command-name>\n"
                    "<command-message>review</command-message>\n"
                    "<command-args>frontend src</command-args>"
                ),
            },
        }
    ]

    turns, _ = build_turns(entries)

    assert turns[0].command == "/review"
    assert turns[0].command_args == "frontend src"

def test_codex_skill_md_read_is_recorded():
    # Real capture: an `exec` call whose shell command reads a SKILL.md path
    # (see skill_and_image_session.jsonl) -- the regex-based path match is
    # what actually recognizes it, not a structured "skill" argument.
    entries = load_entries(CODEX_SKILL_IMAGE_FIXTURE)
    call = next(
        e["payload"]
        for e in entries
        if e.get("type") == "response_item"
        and e["payload"].get("type") == "custom_tool_call"
        and "SKILL.md" in (e["payload"].get("input") or "")
    )
    assert skill_names_from_call(call["name"], call["input"]) == ["fixture-skill"]

def test_codex_normalized_mcp_skill_read_is_recorded():
    args = {"package": "presentations:Presentations"}
    assert skill_names_from_call("mcp__skills__read", args) == [
        "presentations:Presentations"
    ]

def test_codex_permission_denial_strips_external_prefix():
    c = (
        "[external_agent_tool_result: error]\nThe user doesn't want to proceed "
        "with this tool use. The tool use was rejected. STOP what you are doing."
    )
    assert parse_permission_denial(c) == ""

def test_codex_permission_denial_with_message():
    c = (
        "[external_agent_tool_result: error]\nPermission for this tool use was "
        "denied. The tool use was rejected. The user said:\nuse a branch"
    )
    assert parse_permission_denial(c) == "use a branch"

def test_codex_permission_denial_ignores_normal_text():
    assert (
        parse_permission_denial("[external_agent_tool_result]\nfile contents") is None
    )
    assert parse_permission_denial("Sure, I'll do that.") is None

def test_codex_denial_recorded_on_turn_not_as_prose():
    entries = [
        {
            "type": "event_msg",
            "timestamp": "2026-07-17T10:00:00Z",
            "payload": {"type": "user_message", "message": "do the thing"},
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "external_agent",
                "call_id": "c1",
                "arguments": "{}",
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": (
                            "[external_agent_tool_result: error]\nThe user doesn't "
                            "want to proceed with this tool use. The tool use was "
                            "rejected. The user said:\nnot like that"
                        ),
                    }
                ],
            },
        },
    ]
    turns, _ = build_turns(entries)
    assert len(turns) == 1
    assert turns[0].permission_denials == [
        {"tool": "external_agent", "message": "not like that"}
    ]
    # The canned denial text must not leak into assistant prose.
    assert turns[0].assistant_text_blocks == []

def test_codex_plan_mode_turn_and_structured_plan_are_rendered():
    entries = [
        {
            "type": "event_msg",
            "timestamp": "2026-08-05T10:00:00Z",
            "payload": {
                "type": "task_started",
                "collaboration_mode_kind": "plan",
            },
        },
        {
            "type": "turn_context",
            "timestamp": "2026-08-05T10:00:01Z",
            "payload": {"collaboration_mode": {"mode": "plan"}},
        },
        {
            "type": "event_msg",
            "timestamp": "2026-08-05T10:00:02Z",
            "payload": {"type": "user_message", "message": "Plan the change"},
        },
        {
            "type": "response_item",
            "timestamp": "2026-08-05T10:00:03Z",
            "payload": {
                "type": "function_call",
                "name": "functions.update_plan",
                "call_id": "plan-1",
                "arguments": json.dumps(
                    {
                        "explanation": "Start with the schema.",
                        "plan": [
                            {"step": "Inspect schema", "status": "completed"},
                            {"step": "Design change", "status": "in_progress"},
                        ],
                    }
                ),
            },
        },
        {
            "type": "response_item",
            "timestamp": "2026-08-05T10:00:04Z",
            "payload": {
                "type": "function_call_output",
                "call_id": "plan-1",
                "output": "Plan updated",
            },
        },
        {
            "type": "response_item",
            "timestamp": "2026-08-05T10:00:05Z",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Here is the plan."}],
            },
        },
    ]

    turns, files_changed = build_turns(entries)
    assert turns[0].collaboration_mode == "plan"
    assert turns[0].tool_bullets == []
    assert turns[0].result_notes == []
    assert turns[0].plan_updates[0]["steps"][1]["step"] == "Design change"

    md = render(turns, files_changed, "project", "2026-08-05T10:00:00Z")
    assert "_Codex Plan mode turn._" in md
    assert "**Mode:** Plan" in md
    assert "**Assistant updated the Plan mode plan:**" in md
    assert "- [x] Inspect schema _(completed)_" in md
    assert "- [ ] Design change _(in_progress)_" in md
    assert "Here is the plan." in md

def test_codex_default_mode_update_plan_remains_execution_tool():
    entries = [
        {
            "type": "event_msg",
            "timestamp": "2026-08-05T10:00:00Z",
            "payload": {
                "type": "task_started",
                "collaboration_mode_kind": "default",
            },
        },
        {
            "type": "event_msg",
            "timestamp": "2026-08-05T10:00:01Z",
            "payload": {"type": "user_message", "message": "Build it"},
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "update_plan",
                "call_id": "plan-1",
                "arguments": json.dumps(
                    {"plan": [{"step": "Implement", "status": "in_progress"}]}
                ),
            },
        },
    ]
    turns, _ = build_turns(entries)
    assert turns[0].collaboration_mode == "default"
    assert turns[0].plan_updates == []
    assert turns[0].tool_bullets[0].startswith("- update_plan")

def test_codex_noise_cleaning():
    assert clean_user_text("<bash-stdout>output</bash-stdout>") == ""
    assert (
        clean_user_text("<bash-input>ls -la</bash-input>")
        == "<bash-input>ls -la</bash-input>"
    )

def test_codex_summary_options_and_shell_command():
    entries = [
        {
            "type": "event_msg",
            "timestamp": "2026-01-01T00:00:01Z",
            "payload": {"type": "user_message", "message": "Need help\nnow"},
        },
        {
            "type": "response_item",
            "timestamp": "2026-01-01T00:00:02Z",
            "payload": {
                "type": "function_call",
                "name": "functions.request_user_input",
                "call_id": "call-1",
                "arguments": json.dumps({"questions": QUESTIONS}),
            },
        },
        {
            "type": "response_item",
            "timestamp": "2026-01-01T00:00:03Z",
            "payload": {
                "type": "function_call_output",
                "call_id": "call-1",
                "output": json.dumps({"answers": {"team": "QUAL"}}),
            },
        },
        {
            "type": "event_msg",
            "timestamp": "2026-01-01T00:00:04Z",
            "payload": {
                "type": "user_message",
                "message": "<bash-stdout>ignored</bash-stdout>",
            },
        },
        {
            "type": "event_msg",
            "timestamp": "2026-01-01T00:00:05Z",
            "payload": {
                "type": "user_message",
                "message": "<bash-input>ls -la</bash-input>",
            },
        },
    ]

    turns, files_changed = build_turns(entries)
    assert files_changed == []
    assert len(turns) == 2
    assert turns[0].option_qas[0]["chosen_labels"] == ["QUAL"]
    assert turns[1].shell_command == "ls -la"

    md = render(turns, files_changed, "project", "2026-01-01T00:00:00Z")
    assert md.index("## Summary - user inputs") < md.index("# Full turn-by-turn detail")
    assert "> Need help" in md
    assert "- [x] **QUAL**" in md
    assert "```sh\nls -la\n```" in md

def test_codex_dumps_local_images_from_user_event():
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        source = tmp / "clip.png"
        source.write_bytes(base64.b64decode(_PNG_B64))

        turns, _ = build_turns(
            [
                {
                    "type": "event_msg",
                    "timestamp": "2026-01-01T00:00:01Z",
                    "payload": {
                        "type": "user_message",
                        "message": "Look [Image #1]",
                        "images": [],
                        "local_images": [str(source)],
                        "text_elements": [],
                    },
                }
            ]
        )

        dump_dir = tmp / "dumped"
        written = dump_images(turns, "sess123", dump_dir=dump_dir)
        assert len(written) == 1
        assert written[0].name == "sess123_turn1_img1.png"
        assert written[0].read_bytes() == source.read_bytes()
        assert "description pending" in turns[0].user_text
        assert str(written[0]) in turns[0].user_text

def test_codex_dumps_base64_image_from_response_item_user_message():
    entries = [
        {
            "type": "response_item",
            "timestamp": "2026-01-01T00:00:01Z",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [
                    {
                        "type": "input_image",
                        "image_url": f"data:image/png;base64,{_PNG_B64}",
                    },
                    {"type": "input_text", "text": "Look [Image #1]"},
                ],
            },
        },
        {
            "type": "event_msg",
            "timestamp": "2026-01-01T00:00:02Z",
            "payload": {
                "type": "user_message",
                "message": "Look [Image #1]",
                "images": [],
                "local_images": [],
                "text_elements": [],
            },
        },
    ]

    with tempfile.TemporaryDirectory() as tmp_name:
        turns, _ = build_turns(entries)
        written = dump_images(turns, "sess123", dump_dir=Path(tmp_name))

        assert len(turns) == 1
        assert len(written) == 1
        assert written[0].name == "sess123_turn1_img1.png"
        assert written[0].read_bytes() == base64.b64decode(_PNG_B64)
        assert "description pending" in turns[0].user_text

def _write_jsonl(path: Path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(entry) for entry in entries) + "\n",
        encoding="utf-8",
    )

def _session_entries(session_id: str, cwd: Path, message: str):
    return [
        {
            "type": "session_meta",
            "timestamp": "2026-01-01T00:00:00Z",
            "payload": {"id": session_id, "cwd": str(cwd)},
        },
        {
            "type": "event_msg",
            "timestamp": "2026-01-01T00:00:01Z",
            "payload": {"type": "user_message", "message": message},
        },
    ]

def test_codex_all_extracts_matching_sessions_to_unique_files():
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        project = tmp / "project"
        other = tmp / "other"
        sessions = tmp / "sessions" / "2026" / "01" / "01"
        out = tmp / "out"
        project.mkdir()
        other.mkdir()

        _write_jsonl(
            sessions / "rollout-one.jsonl",
            _session_entries("session-one", project, "First session"),
        )
        _write_jsonl(
            sessions / "rollout-two.jsonl",
            _session_entries("session-two", project / "frontend", "Second session"),
        )
        _write_jsonl(
            sessions / "rollout-other.jsonl",
            _session_entries("session-other", other, "Wrong project"),
        )

        old_cwd = os.getcwd()
        try:
            os.chdir(project)
            rc = main(
                [
                    "--all",
                    "--sessions-root",
                    str(tmp / "sessions"),
                    "--output",
                    str(out),
                ]
            )
        finally:
            os.chdir(old_cwd)

        assert rc == 0
        written = sorted(path.name for path in out.glob("codex_session_log_*.md"))
        assert written == [
            "codex_session_log_session-one.md",
            "codex_session_log_session-two.md",
        ]
        assert "First session" in (out / "codex_session_log_session-one.md").read_text(
            encoding="utf-8"
        )
        assert not (out / "codex_session_log_session-other.md").exists()

def test_codex_no_selector_extracts_all_by_default():
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        project = tmp / "project"
        sessions = tmp / "sessions" / "2026" / "01" / "01"
        out = tmp / "out"
        project.mkdir()

        _write_jsonl(
            sessions / "rollout-one.jsonl",
            _session_entries("session-one", project, "First session"),
        )
        _write_jsonl(
            sessions / "rollout-two.jsonl",
            _session_entries("session-two", project, "Second session"),
        )

        old_cwd = os.getcwd()
        try:
            os.chdir(project)
            rc = main(
                [
                    "--sessions-root",
                    str(tmp / "sessions"),
                    "--output",
                    str(out),
                ]
            )
        finally:
            os.chdir(old_cwd)

        assert rc == 0
        written = sorted(path.name for path in out.glob("codex_session_log_*.md"))
        assert written == [
            "codex_session_log_session-one.md",
            "codex_session_log_session-two.md",
        ]

def test_codex_strict_requires_exact_cwd():
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        project = tmp / "project"
        sessions = tmp / "sessions" / "2026" / "01" / "01"
        out = tmp / "out"
        project.mkdir()

        _write_jsonl(
            sessions / "rollout-one.jsonl",
            _session_entries("session-one", project, "Exact match"),
        )
        _write_jsonl(
            sessions / "rollout-two.jsonl",
            _session_entries("session-two", project / "frontend", "Descendant"),
        )

        old_cwd = os.getcwd()
        try:
            os.chdir(project)
            rc = main(
                [
                    "--all",
                    "--strict",
                    "--sessions-root",
                    str(tmp / "sessions"),
                    "--output",
                    str(out),
                ]
            )
        finally:
            os.chdir(old_cwd)

        assert rc == 0
        # Strict drops the descendant; only the exact-cwd session survives.
        written = sorted(path.name for path in out.glob("codex_session_log_*.md"))
        assert written == ["codex_session_log_session-one.md"]

def _codex_entries(image_path, missing_path="/gone/missing.png"):
    return [
        {"type": "session_meta", "payload": {"id": "sess-1", "cwd": "/work/app"}},
        {
            "type": "event_msg",
            "timestamp": "2026-08-01T10:00:00Z",
            "payload": {
                "type": "user_message",
                "message": "here is the mock",
                "images": [str(image_path), missing_path],
            },
        },
        {
            "type": "response_item",
            "timestamp": "2026-08-01T10:00:02Z",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "On it."}],
            },
        },
        {
            "type": "response_item",
            "timestamp": "2026-08-01T10:00:03Z",
            "payload": {
                "type": "function_call",
                "name": "shell",
                "call_id": "c1",
                "arguments": json.dumps({"command": "ls -la"}),
            },
        },
        {
            "type": "response_item",
            "timestamp": "2026-08-01T10:00:04Z",
            "payload": {
                "type": "function_call_output",
                "call_id": "c1",
                "output": "A" * 9000,
            },
        },
    ]

def _png_on_disk():
    path = Path(tempfile.mkdtemp()) / "shot.png"
    path.write_bytes(base64.b64decode(_PNG_B64))
    return path

def test_build_events_inlines_an_image_stored_as_a_path():
    img = _png_on_disk()
    user = build_events(_codex_entries(img))[0]
    assert user["role"] == "user"
    assert base64.b64decode(user["images"][0]["data"]) == base64.b64decode(_PNG_B64)
    assert user["images"][0]["media_type"] == "image/png"

def test_build_events_records_an_unresolvable_image_rather_than_dropping_it():
    # dump_images silently skips these; the envelope must not, or "pasted a
    # screenshot we can no longer read" becomes indistinguishable from "pasted
    # nothing".
    user = build_events(_codex_entries(_png_on_disk()))[0]
    assert user["images"][1] == {"unavailable": True, "ref": "/gone/missing.png"}

def test_build_events_attaches_output_to_its_function_call():
    entries = load_entries(CODEX_SKILL_IMAGE_FIXTURE)
    events = build_events(entries, tool_result_max_bytes=20)
    calls = [c for e in events for c in e.get("tool_calls", [])]
    assert [c["name"] for c in calls] == ["exec", "exec"]
    assert [c["result_bytes"] for c in calls] == [353, 81]
    assert all(c["result_truncated"] for c in calls)

def test_build_events_does_not_disturb_the_markdown_turns():
    entries = load_entries(CODEX_SKILL_IMAGE_FIXTURE)
    before = [t.user_text for t in build_turns(entries)[0]]
    build_events(entries)
    assert [t.user_text for t in build_turns(entries)[0]] == before

def test_load_entries_parses_the_real_fixture():
    entries = load_entries(CODEX_FIXTURE)
    assert len(entries) == 6
    assert entries[0]["type"] == "session_meta"
    assert entries[0]["payload"]["cwd"] == "/redacted/project"

def test_load_entries_skips_malformed_and_blank_lines(tmp_path):
    corrupted = tmp_path / "session.jsonl"
    real_text = CODEX_FIXTURE.read_text(encoding="utf-8")
    corrupted.write_text(real_text + "\n{this is not json\n\n", encoding="utf-8")
    entries = load_entries(corrupted)
    assert len(entries) == 6

def test_select_transcript_sessions_root_picks_newest(tmp_path):
    older = tmp_path / "rollout-a.jsonl"
    newer = tmp_path / "rollout-b.jsonl"
    shutil.copy(CODEX_FIXTURE, older)
    shutil.copy(CODEX_FIXTURE, newer)
    os.utime(older, (1, 1))
    os.utime(newer, (2, 2))
    assert select_transcript(None, None, str(tmp_path)) == newer

def test_select_transcript_session_id_prefix_match(tmp_path):
    target = tmp_path / "rollout-target.jsonl"
    shutil.copy(CODEX_FIXTURE, target)
    assert select_transcript("01a0c43e", None, str(tmp_path)) == target

def test_select_transcript_ambiguous_session_id_raises(tmp_path):
    shutil.copy(CODEX_FIXTURE, tmp_path / "rollout-a.jsonl")
    shutil.copy(CODEX_FIXTURE, tmp_path / "rollout-b.jsonl")
    try:
        select_transcript("01a0c43e", None, str(tmp_path))
    except FileNotFoundError as exc:
        assert "ambiguous" in str(exc)
    else:
        raise AssertionError("expected FileNotFoundError")

def test_select_transcript_strict_requires_exact_cwd_and_disables_fallback(
    monkeypatch, tmp_path
):
    shutil.copy(CODEX_FIXTURE, tmp_path / "rollout.jsonl")

    monkeypatch.setattr(Path, "cwd", staticmethod(lambda: Path("/redacted/project")))
    assert select_transcript(None, None, str(tmp_path), strict=True) == (
        tmp_path / "rollout.jsonl"
    )

    # A cwd nested under the recorded one only overlaps -- strict rejects it.
    monkeypatch.setattr(
        Path, "cwd", staticmethod(lambda: Path("/redacted/project/sub"))
    )
    assert select_transcript(None, None, str(tmp_path), strict=False) == (
        tmp_path / "rollout.jsonl"
    )
    try:
        select_transcript(None, None, str(tmp_path), strict=True)
    except FileNotFoundError as exc:
        assert "--strict" in str(exc)
    else:
        raise AssertionError("expected FileNotFoundError")

def test_main_end_to_end_renders_the_real_fixture(tmp_path):
    out_path = tmp_path / "out.md"
    rc = main(
        [
            "--transcript",
            str(CODEX_FIXTURE),
            "--output",
            str(out_path),
            "--no-raw",
        ]
    )
    assert rc == 0
    assert out_path.is_file()
    markdown = out_path.read_text(encoding="utf-8")
    assert "What does this skill do?" in markdown
    assert "relentless interview" in markdown

def test_main_reports_missing_transcript_and_exits_nonzero(tmp_path, capsys):
    missing = tmp_path / "nope.jsonl"
    rc = main(["--transcript", str(missing)])
    assert rc == 1
    assert "does not exist" in capsys.readouterr().err

def test_real_fixture_slash_command_invokes_a_real_skill():
    # /fixture-skill triggered an actual `exec` read of that skill's SKILL.md,
    # unlike the synthetic skill tests above which fabricate the call.
    entries = load_entries(CODEX_SKILL_IMAGE_FIXTURE)
    turns, _ = build_turns(entries)
    assert turns[0].command == "/fixture-skill"
    assert turns[0].skills_used == ["fixture-skill"]

    md = render(turns, [], "project", entries[0]["timestamp"])
    assert "_Skill used:_ **fixture-skill**" in md
    assert "Hello! Python files here:" in md

def test_real_fixture_attached_image_is_captured_and_inlined():
    # A real `codex exec --image` attachment, not a synthetic input_image block.
    entries = load_entries(CODEX_SKILL_IMAGE_FIXTURE)
    turns, _ = build_turns(entries)
    assert "Describe what is in the attached image" in turns[1].user_text

    events = build_events(entries)
    user = next(e for e in events if e["role"] == "user" and e.get("images"))
    assert user["images"][0]["media_type"] == "image/png"
    assert base64.b64decode(user["images"][0]["data"]).startswith(b"\x89PNG")

@pytest.fixture
def codex_answer_questions():
    return [
        {
            "id": "team",
            "header": "Team",
            "question": "Pick a team",
            "options": [
                {"label": "QUAL", "description": "qualification"},
                {"label": "SE"},
            ],
        },
        "bad question",
    ]

@pytest.fixture
def transcript_fixtures(tmp_path):
    codex_path = tmp_path / "codex.jsonl"
    shutil.copyfile(CODEX_FIXTURE, codex_path)
    return {"codex": codex_path}

def test_codex_transcript_metadata_and_cli_edge_cases(
    transcript_fixtures, monkeypatch, tmp_path, capsys
):
    source = transcript_fixtures["codex"]
    assert codex._jsonl_files(tmp_path / "missing") == []
    assert codex.session_id_from_file(tmp_path / "missing") is None
    assert codex.session_cwd_from_file(tmp_path / "missing") is None
    malformed = tmp_path / "metadata.jsonl"
    malformed.write_text(
        "[]\nnot json\n"
        + json.dumps({"type": "turn_context", "payload": {"cwd": "/work"}})
        + "\n"
        + json.dumps({"type": "session_meta", "payload": {"id": 2, "cwd": 4}})
        + "\n",
        encoding="utf-8",
    )
    assert codex.session_id_from_file(malformed) is None
    assert codex.session_cwd_from_file(malformed) == "/work"
    assert codex.output_identifier(tmp_path / "rollout-!.jsonl") == "rollout"
    assert codex.newest([]) is None
    with pytest.raises(FileNotFoundError, match="no Codex transcript matching"):
        codex.select_transcript("absent", None, str(tmp_path))

    root = tmp_path / "sessions"
    root.mkdir()
    old = os.getcwd()
    try:
        os.chdir(tmp_path)
        assert codex._extract_all(str(root), str(tmp_path / "out")) == 1
    finally:
        os.chdir(old)
    assert "no Codex transcripts" in capsys.readouterr().err

    assert codex.main(
        ["--transcript", str(source), "--output", "-", "--no-raw"]
    ) == 0
    assert "Codex Session Conversation Log" in capsys.readouterr().out
    empty = tmp_path / "empty.jsonl"
    empty.write_text("bad json", encoding="utf-8")
    assert codex.main(["--transcript", str(empty), "--output", "-"]) == 1
    assert "no parseable JSON entries" in capsys.readouterr().err

def test_codex_all_session_export_uses_unique_ids_and_skips_failed_parse(
    transcript_fixtures, monkeypatch, tmp_path, capsys
):
    source = transcript_fixtures["codex"]
    root = tmp_path / "sessions"
    root.mkdir()
    first = root / "rollout-a.jsonl"
    second = root / "rollout-b.jsonl"
    shutil.copyfile(source, first)
    shutil.copyfile(source, second)
    monkeypatch.setattr(Path, "cwd", staticmethod(lambda: Path("/redacted/project")))
    original_render = codex.render_transcript

    def fail_first(path, *args, **kwargs):
        if path == first:
            raise ValueError("damaged test capture")
        return original_render(path, *args, **kwargs)

    monkeypatch.setattr(codex, "render_transcript", fail_first)
    out = tmp_path / "out"
    assert codex._extract_all(str(root), str(out), raw=False) == 0
    assert len(list(out.glob("codex_session_log_*.md"))) == 1
    assert "skip" in capsys.readouterr().err

def test_codex_answer_and_patch_fixtures_cover_fallback_shapes(
    codex_answer_questions,
):
    assert codex._value_to_answer_text(
        {"answer": [True, {"selected": "QUAL"}, 3], "value": "ignored"}
    ) == "True, QUAL, 3"
    assert codex._value_to_answer_text({"empty": "none"}) == ""
    question = codex_answer_questions[0]
    assert codex._extract_answer_from_item(
        question, {"questionId": "team", "answer": "SE"}
    ) == "SE"
    assert codex._extract_answer_from_item(question, {"id": "other"}) == ""
    assert codex._structured_answer_for_question(
        question, {"responses": [{"question": "Pick a team", "value": "QUAL"}]}
    ) == "QUAL"
    assert codex._structured_answer_for_question(
        question, {"selections": {"unrelated": "value"}}
    ) == ""
    assert codex._flatten_result_text(
        {"unrecognized": {"deep": "answer"}}
    ) == "answer"
    assert codex.option_qa_from_output(
        {"answers": {"team": "SE"}}, codex_answer_questions
    )[0]["chosen_labels"] == ["SE"]
    assert codex.option_qa_from_output("nothing", None) == []
    assert codex.patch_paths(
        {
            "patch": "*** Begin Patch\n"
            "*** Add File: src/new.py\n"
            "*** Update File: src/old.py\n"
            "*** Delete File: src/gone.py"
        }
    ) == ["src/new.py", "src/old.py", "src/gone.py"]
    assert codex.patch_paths({"content": 4}) == []
    paths = ["src/old.py"]
    seen = set(paths)
    codex.append_unique_path(paths, seen, "old.py")
    codex.append_unique_path(paths, seen, "src/new.py")
    codex.append_unique_path(paths, seen, "")
    assert paths == ["src/old.py", "src/new.py"]

def test_codex_image_extraction_accepts_paths_urls_and_inline_payloads(tmp_path):
    image = tmp_path / "photo.png"
    image.write_bytes(b"png bytes")
    payload = {
        "local_images": [str(image), "data:image/png;base64,QQ==", "ignored"],
        "images": [
            {"image_url": f"data:image/jpeg;base64,QQ=="},
            {"path": str(image)},
            {"data": "QQ==", "mime_type": "image/png"},
            {"data": 42},
            "relative.png",
        ],
    }
    refs = codex.image_refs_from_event(payload)
    assert [ref["type"] for ref in refs] == [
        "path",
        "data_url",
        "data_url",
        "path",
        "base64",
    ]
    assert codex.image_refs_from_user_message_payload(
        {"content": [{"type": "input_image", "image_url": "relative.png"}]}
    ) == []
    assert codex._image_ref_from_string("relative.png") is None
    assert codex.user_text_from_event({"message": None}) == ""
    assert codex.result_note(None) == "null"
    assert codex._plan_update({"plan": [{"bad": "step"}]}) is None
    assert codex._plan_update(
        {"plan": [{"step": "valid"}], "explanation": 5}
    ) == {"explanation": "", "steps": [{"step": "valid", "status": "pending"}]}

def test_codex_turn_builder_handles_startup_context_patch_and_denials():
    startup = (
        "<recommended_plugins>\n# AGENTS.md instructions\n"
        "<environment_context>\nworkspace\n"
    )
    entries = [
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": startup},
                    {"type": "input_image", "image_url": "data:image/png;base64,QQ=="},
                ],
            },
        },
        {
            "type": "turn_context",
            "payload": {"collaboration_mode_kind": " PLAN "},
        },
        {
            "type": "event_msg",
            "timestamp": "2026-01-01T00:00:00Z",
            "payload": {"type": "user_message", "message": "Build it"},
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "patch_apply_end",
                "changes": {"src/a.py": {}, "src/b.py": {}},
                "stderr": "warning",
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "apply_patch",
                "call_id": "patch",
                "input": "*** Begin Patch\n*** Add File: src/a.py",
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "patch",
                "output": "applied",
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": (
                            "[external_agent_tool_result: error] "
                            "Permission for this tool use was denied. "
                            "The user said:\nstop"
                        ),
                    }
                ],
            },
        },
        {
            "type": "response_item",
            "payload": {"type": "message", "role": "developer", "content": "ignore"},
        },
        {"type": "event_msg", "payload": {"type": "other"}},
        {"type": "response_item", "payload": {"type": "other"}},
    ]
    turns, paths = codex.build_turns(entries)
    assert len(turns) == 1
    assert turns[0].user_text == "Build it"
    assert turns[0].collaboration_mode == "plan"
    assert turns[0].result_notes == ["apply_patch: warning", "apply_patch: applied"]
    assert turns[0].permission_denials == [{"tool": "apply_patch", "message": "stop"}]
    assert paths == ["src/a.py", "src/b.py"]
    assert codex.is_startup_context(startup)
    assert not codex.is_startup_context("ordinary user text")

def test_codex_helper_fallbacks_cover_timestamps_arguments_and_answers():
    assert codex.parse_permission_denial(None) is None
    assert codex.clean_user_text("") == ""
    assert codex.format_timestamp("bad timestamp") == "bad timestamp"
    assert codex.format_timestamp("2026-01-01T01:00:00+01:00") == (
        "2026-01-01 00:00:00 UTC"
    )
    assert codex.format_elapsed("bad", "2026-01-01T00:00:00Z") == ""
    assert codex.format_elapsed(
        "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"
    ) == "+0s"
    assert codex.format_elapsed(
        "2026-01-01T00:00:00Z", "2026-01-01T00:01:01Z"
    ) == "+1:01"
    assert codex.format_elapsed(
        "2026-01-01T00:00:00Z", "2026-01-01T01:01:01Z"
    ) == "+1:01:01"
    assert codex.date_only(None) == "unknown"
    assert codex._content_text({"unsupported": "content"}, ("text",)) == ""
    assert codex._content_text(["plain", {"output_text": "text"}], ("text",)) == "plain"
    assert codex.tool_descriptor("tool", {"tool_uses": [{"recipient_name": "rg"}]}) == "rg"
    assert codex.tool_descriptor("tool", {"misc": "value"}).startswith("{")
    assert codex.skill_names_from_call("skill", {"name": "audit"}) == ["audit"]
    assert codex.skill_names_from_call("view", {"path": "/skills/audit/SKILL.md"}) == [
        "audit"
    ]
    assert codex._flatten_result_text({"message": {"nested": "x"}}) == "x"
    assert codex._parse_chosen("Q", [{"label": "A"}], 'Other="value') == ("value", [])
    assert codex.first_timestamp([{}, {"timestamp": 4}, {"timestamp": "now"}]) == "now"
    assert codex.derive_project_label(Path("/tmp/rollout.jsonl"), []) == (
        "/tmp/rollout.jsonl"
    )
    assert codex._derive_context_line([]) == "a Codex session"

def test_codex_image_resolution_keeps_unavailable_refs_and_dump_names(tmp_path):
    image = tmp_path / "input"
    image.write_bytes(b"raw")
    assert codex._image_bytes_and_ext({"type": "data_url", "data_url": "bad"}) is None
    assert codex._image_bytes_and_ext(
        {"type": "data_url", "data_url": "data:image/custom;base64,QQ=="}
    ) == (b"A", "img")
    assert codex._image_bytes_and_ext({"type": "data_url", "data_url": 3}) is None
    assert codex._image_bytes_and_ext(
        {"type": "base64", "data": "QQ==", "media_type": "image/gif"}
    ) == (b"A", "gif")
    assert codex._image_bytes_and_ext({"type": "base64", "data": 3}) is None
    assert codex._image_bytes_and_ext({"type": "base64", "data": "%%%"}) == (
        b"",
        "img",
    )
    assert codex._image_bytes_and_ext({"type": "path", "path": str(image)}) == (
        b"raw",
        "img",
    )
    assert codex._image_bytes_and_ext(
        {"type": "path", "path": str(tmp_path / "missing")}
    ) is None
    assert codex._image_bytes_and_ext({"type": "other"}) is None
    turn = codex.Turn("image", "2026-01-01T00:00:00Z")
    turn.images = [{"type": "unknown"}, {"type": "path", "path": str(image)}]
    dumped = codex.dump_images([turn], "sid", tmp_path / "dump")
    assert [path.name for path in dumped] == ["sid_turn1_img2.img"]
    assert "description pending" in turn.user_text

def test_jsonl_warning_rate_limit_and_non_object_records(tmp_path, capsys):
    import extract_claude_session_log as claude
    path = tmp_path / "damaged.jsonl"
    path.write_text(
        "\n".join(["bad json"] * 7 + ["42", json.dumps({"type": "user"})]),
        encoding="utf-8",
    )
    for loader in (claude.load_entries, codex.load_entries):
        assert loader(path) == [{"type": "user"}]
    warning_output = capsys.readouterr().err
    assert warning_output.count("warning: skipping malformed JSON") == 10
    assert warning_output.count("warning: skipped 8 malformed/blank lines total") == 2

def test_render_summaries_cover_shell_skills_and_feedback():
    import extract_copilot_session_log as copilot
    copilot_turn = copilot.Turn("line one\n\nline two", "", None, 0)
    copilot_turn.shell_command = "pwd"
    copilot_turn.skills_used = ["fixture-skill"]
    copilot_turn.permission_decisions = [
        {"decision": "denied", "tool": "shell", "feedback": "not\nnow"}
    ]
    summary = "\n".join(copilot.render_summary([copilot_turn], None))
    assert "Ran shell command:" in summary
    assert "_Permission denied:_ **shell**" in summary
    assert "> not\n> now" in summary

    codex_turn = codex.Turn("task", "2026-01-01T00:00:01Z", "plan")
    codex_turn.shell_command = "pwd"
    codex_turn.plan_updates = [
        {"explanation": "first", "steps": [{"step": "A", "status": "completed"}]},
        {"explanation": "", "steps": [{"step": "B", "status": "in_progress"}]},
    ]
    codex_turn.permission_denials = [{"tool": "write", "message": "no\nthanks"}]
    summary = "\n".join(codex.render_summary([codex_turn], None))
    assert "Ran shell command:" in summary
    assert "Structured plan updated 2 times" in summary
    assert "- [ ] B _(in_progress)_" in summary
    assert "> thanks" in summary

def test_codex_single_session_cli_reports_write_error(transcript_fixtures, tmp_path, capsys):
    source = transcript_fixtures["codex"]
    output_dir = tmp_path / "directory"
    output_dir.mkdir()
    assert codex.main(["--transcript", str(source), "--output", str(output_dir), "--no-raw"]) == 1
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
