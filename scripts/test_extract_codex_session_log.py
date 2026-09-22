#!/usr/bin/env python3
"""Minimal asserts for extract_codex_session_log.

Run: python3 scripts/test_extract_codex_session_log.py
"""

from __future__ import annotations

import base64
import json
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

# Real, trimmed, redacted Codex transcript -- see tests/fixtures/codex/.
CODEX_FIXTURE = (
    Path(__file__).resolve().parent.parent / "tests/fixtures/codex/session.jsonl"
)

# 1x1 transparent PNG.
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
    args = {"cmd": "sed -n '1,200p' /tmp/plugins/documents/skills/documents/SKILL.md"}
    assert skill_names_from_call("exec_command", args) == ["documents"]


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

        # Exact-cwd session and a descendant-cwd session.
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
    # Codex keeps images out of band, so the envelope is where they become
    # portable.
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
    events = build_events(_codex_entries(_png_on_disk()), tool_result_max_bytes=100)
    call = next(c for e in events for c in e.get("tool_calls", []))
    assert call["name"] == "shell"
    assert call["input"] == {"command": "ls -la"}
    assert call["result_bytes"] == 9000
    assert call["result_truncated"] is True


def test_build_events_does_not_disturb_the_markdown_turns():
    entries = _codex_entries(_png_on_disk())
    before = [t.user_text for t in build_turns(entries)[0]]
    build_events(entries)
    assert [t.user_text for t in build_turns(entries)[0]] == before


# --------------------------------------------------------------------------- #
# Fixture-driven: real (trimmed, redacted) transcript, session selection,
# --strict, malformed input, and main() end-to-end.
# --------------------------------------------------------------------------- #


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
    # The real fixture's session id is 01a0c43e-367f-7833-91d9-4d9283a3fbd5.
    assert select_transcript("01a0c43e", None, str(tmp_path)) == target


def test_select_transcript_ambiguous_session_id_raises(tmp_path):
    shutil.copy(CODEX_FIXTURE, tmp_path / "rollout-a.jsonl")
    shutil.copy(CODEX_FIXTURE, tmp_path / "rollout-b.jsonl")
    # Two files sharing the same real session id (e.g. one copied as a
    # backup) -- the prefix match is ambiguous between them.
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

    # Exact cwd match: found both strict and non-strict.
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
