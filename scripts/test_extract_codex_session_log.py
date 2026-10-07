#!/usr/bin/env python3
"""Minimal asserts for extract_codex_session_log.

Run: python3 scripts/test_extract_codex_session_log.py
"""

from __future__ import annotations

import base64
import json
import os
import tempfile
from pathlib import Path

from extract_codex_session_log import (
    _cwd_matches,
    _decode_jsonish,
    _derive_context_line,
    _extract_all,
    _extract_answer_from_item,
    _flatten_result_text,
    _image_bytes_and_ext,
    _parse_chosen,
    _parse_ts,
    _paths_overlap,
    _plan_update,
    _structured_answer_for_question,
    _value_to_answer_text,
    append_unique_path,
    assistant_text_from_payload,
    build_events,
    build_turns,
    clean_user_text,
    date_only,
    derive_project_label,
    dump_images,
    first_timestamp,
    format_elapsed,
    format_timestamp,
    image_refs_from_event,
    image_refs_from_user_message_payload,
    is_startup_context,
    load_entries,
    main,
    newest,
    option_qa_from_output,
    output_identifier,
    parse_permission_denial,
    patch_paths,
    render,
    render_summary,
    render_transcript,
    result_note,
    select_transcript,
    session_cwd_from_file,
    session_id_from_file,
    session_models,
    skill_names_from_call,
    skill_refs_from_call,
    tool_descriptor,
    user_text_from_event,
    user_text_from_response_item,
    write_raw_envelope,
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


def test_codex_structured_skill_call_is_recorded_and_rendered():
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
    turns, files_changed = build_turns(entries)
    assert turns[0].skills_used == ["pdf:pdf"]
    md = render(turns, files_changed, "project", "2026-01-01T00:00:00Z")
    assert "pdf:pdf" in md


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


# --- timestamp / formatting helpers ---------------------------------------


def test_parse_permission_denial_rejects_non_string():
    assert parse_permission_denial(None) is None
    assert parse_permission_denial(123) is None


def test_clean_user_text_empty_returns_empty():
    assert clean_user_text("") == ""
    assert clean_user_text(None) == ""


def test_parse_ts_handles_missing_and_bad_values():
    assert _parse_ts(None) is None
    assert _parse_ts(123) is None
    assert _parse_ts("not-a-timestamp") is None


def test_format_timestamp_missing_and_unparsable():
    assert format_timestamp(None) == "(no timestamp)"
    assert format_timestamp(123) == "(no timestamp)"
    assert format_timestamp("garbage") == "garbage"


def test_format_elapsed_missing_timestamps():
    assert format_elapsed(None, "2026-01-01T00:00:00Z") == ""
    assert format_elapsed("2026-01-01T00:00:00Z", None) == ""


def test_format_elapsed_hours_and_minutes():
    start = "2026-01-01T00:00:00Z"
    assert format_elapsed(start, "2026-01-01T01:02:03Z") == "+1:02:03"
    assert format_elapsed(start, "2026-01-01T00:02:03Z") == "+2:03"
    assert format_elapsed(start, "2026-01-01T00:00:05Z") == "+5s"


def test_date_only_missing_timestamp():
    assert date_only(None) == "unknown"


# --- file discovery / selection ---------------------------------------


def test_newest_returns_none_for_empty_and_picks_max_mtime():
    assert newest([]) is None
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        old = tmp / "old.jsonl"
        new = tmp / "new.jsonl"
        old.write_text("{}")
        new.write_text("{}")
        os.utime(old, (1, 1))
        os.utime(new, (1000, 1000))
        assert newest([old, new]) == new


def test_load_entries_skips_blank_and_malformed_lines():
    with tempfile.TemporaryDirectory() as tmp_name:
        path = Path(tmp_name) / "log.jsonl"
        path.write_text(
            "\n".join(
                [
                    "",
                    "not json",
                    json.dumps({"type": "a"}),
                    "[1, 2]",
                ]
            )
        )
        entries = load_entries(path)
        assert entries == [{"type": "a"}]


def test_load_entries_warns_once_past_five_bad_lines():
    with tempfile.TemporaryDirectory() as tmp_name:
        path = Path(tmp_name) / "log.jsonl"
        bad_lines = ["not json"] * 7
        path.write_text("\n".join(bad_lines))
        entries = load_entries(path)
        assert entries == []


def test_session_id_from_file_handles_malformed_line_and_missing_id():
    with tempfile.TemporaryDirectory() as tmp_name:
        path = Path(tmp_name) / "log.jsonl"
        path.write_text(
            "\n".join(
                [
                    "not json",
                    json.dumps({"type": "other"}),
                ]
            )
        )
        assert session_id_from_file(path) is None


def test_session_id_from_file_missing_file_returns_none():
    assert session_id_from_file(Path("/no/such/file.jsonl")) is None


def test_session_cwd_from_file_falls_back_to_turn_context():
    with tempfile.TemporaryDirectory() as tmp_name:
        path = Path(tmp_name) / "log.jsonl"
        path.write_text(
            "\n".join(
                [
                    "not json",
                    json.dumps({"type": "session_meta", "payload": {}}),
                    json.dumps(
                        {"type": "turn_context", "payload": {"cwd": "/work/app"}}
                    ),
                ]
            )
        )
        assert session_cwd_from_file(path) == "/work/app"


def test_session_cwd_from_file_missing_file_and_no_cwd():
    assert session_cwd_from_file(Path("/no/such/file.jsonl")) is None
    with tempfile.TemporaryDirectory() as tmp_name:
        path = Path(tmp_name) / "log.jsonl"
        path.write_text(json.dumps({"type": "session_meta", "payload": {}}))
        assert session_cwd_from_file(path) is None


def test_paths_overlap_reverse_direction_and_no_overlap():
    assert _paths_overlap(Path("/work"), Path("/work/app")) is True
    assert _paths_overlap(Path("/work/one"), Path("/work/two")) is False


def test_cwd_matches_strict_and_non_strict():
    assert _cwd_matches(None, Path("/work")) is False
    assert _cwd_matches("/work", Path("/work"), strict=True) is True
    assert _cwd_matches("/work/app", Path("/work"), strict=True) is False
    assert _cwd_matches("/work/app", Path("/work")) is True


def test_output_identifier_falls_back_to_stem_for_no_session_id():
    with tempfile.TemporaryDirectory() as tmp_name:
        path = Path(tmp_name) / "rollout-abc.jsonl"
        path.write_text(json.dumps({"type": "other"}))
        assert output_identifier(path) == "rollout-abc"


def test_select_transcript_explicit_transcript_missing_raises():
    try:
        select_transcript(None, "/no/such/path.jsonl", None)
        assert False, "expected FileNotFoundError"
    except FileNotFoundError:
        pass


def test_select_transcript_explicit_transcript_present():
    with tempfile.TemporaryDirectory() as tmp_name:
        path = Path(tmp_name) / "t.jsonl"
        path.write_text("{}")
        assert select_transcript(None, str(path), None) == path


def test_select_transcript_positional_file_path():
    with tempfile.TemporaryDirectory() as tmp_name:
        path = Path(tmp_name) / "t.jsonl"
        path.write_text("{}")
        assert select_transcript(str(path), None, None) == path


def test_select_transcript_selector_prefix_single_and_ambiguous_and_missing():
    with tempfile.TemporaryDirectory() as tmp_name:
        sessions = Path(tmp_name) / "sessions"
        sessions.mkdir()
        one = sessions / "rollout-sessA.jsonl"
        two = sessions / "rollout-sessB.jsonl"
        one.write_text(json.dumps({"type": "session_meta", "payload": {"id": "sessA"}}))
        two.write_text(json.dumps({"type": "session_meta", "payload": {"id": "sessB"}}))

        assert select_transcript("sessA", None, str(sessions)) == one

        dup_dir = Path(tmp_name) / "dup"
        dup_dir.mkdir()
        d1 = dup_dir / "rollout-x.jsonl"
        d2 = dup_dir / "rollout-y.jsonl"
        d1.write_text(json.dumps({"type": "session_meta", "payload": {"id": "dupe-1"}}))
        d2.write_text(json.dumps({"type": "session_meta", "payload": {"id": "dupe-2"}}))
        try:
            select_transcript("dupe", None, str(dup_dir))
            assert False, "expected ambiguous FileNotFoundError"
        except FileNotFoundError as exc:
            assert "ambiguous" in str(exc)

        try:
            select_transcript("nope", None, str(sessions))
            assert False, "expected FileNotFoundError"
        except FileNotFoundError:
            pass


def test_select_transcript_falls_back_to_cwd_match_then_newest_anywhere():
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        project = tmp / "project"
        other = tmp / "other"
        sessions = tmp / "sessions"
        project.mkdir()
        other.mkdir()
        sessions.mkdir()

        in_cwd = sessions / "rollout-in.jsonl"
        in_cwd.write_text(
            json.dumps(
                {"type": "session_meta", "payload": {"id": "in", "cwd": str(project)}}
            )
        )
        elsewhere = sessions / "rollout-else.jsonl"
        elsewhere.write_text(
            json.dumps(
                {"type": "session_meta", "payload": {"id": "else", "cwd": str(other)}}
            )
        )

        old_cwd = os.getcwd()
        try:
            os.chdir(project)
            assert select_transcript(None, None, str(sessions)) == in_cwd
        finally:
            os.chdir(old_cwd)

        # No cwd-match: non-strict falls back to newest anywhere.
        only_other_dir = tmp / "only_other_sessions"
        only_other_dir.mkdir()
        only_elsewhere = only_other_dir / "rollout-else.jsonl"
        only_elsewhere.write_text(
            json.dumps(
                {"type": "session_meta", "payload": {"id": "else", "cwd": str(other)}}
            )
        )
        old_cwd = os.getcwd()
        try:
            os.chdir(project)
            assert select_transcript(None, None, str(only_other_dir)) == only_elsewhere
        finally:
            os.chdir(old_cwd)


def test_select_transcript_strict_raises_when_no_exact_match():
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        project = tmp / "project"
        sessions = tmp / "sessions"
        project.mkdir()
        sessions.mkdir()
        descendant = sessions / "rollout-d.jsonl"
        descendant.write_text(
            json.dumps(
                {
                    "type": "session_meta",
                    "payload": {"id": "d", "cwd": str(project / "frontend")},
                }
            )
        )
        old_cwd = os.getcwd()
        try:
            os.chdir(project)
            try:
                select_transcript(None, None, str(sessions), strict=True)
                assert False, "expected FileNotFoundError"
            except FileNotFoundError:
                pass
        finally:
            os.chdir(old_cwd)


def test_select_transcript_raises_when_nothing_anywhere():
    with tempfile.TemporaryDirectory() as tmp_name:
        empty_root = Path(tmp_name) / "empty"
        empty_root.mkdir()
        try:
            select_transcript(None, None, str(empty_root))
            assert False, "expected FileNotFoundError"
        except FileNotFoundError:
            pass


# --- image ref parsing ---------------------------------------


def test_image_refs_from_event_handles_dict_variants_and_junk():
    payload = {
        "local_images": ["/tmp/a.png", 123, "~/b.png"],
        "images": [
            "data:image/png;base64,AAAA",
            {"image_url": "/tmp/c.png"},
            {"path": "/tmp/d.png"},
            {"file_path": "/tmp/e.png"},
            {"data": "QUFB", "media_type": "image/png"},
            {"unrelated": "nope"},
            "not-an-image-string",
            123,
        ],
    }
    refs = image_refs_from_event(payload)
    kinds = [r["type"] for r in refs]
    assert kinds.count("path") >= 4
    assert "data_url" in kinds
    assert "base64" in kinds


def test_image_refs_from_user_message_payload_ignores_non_list_and_other_blocks():
    assert image_refs_from_user_message_payload({"content": "not a list"}) == []
    refs = image_refs_from_user_message_payload(
        {
            "content": [
                {"type": "input_text", "text": "hi"},
                {"type": "input_image", "image_url": 123},
                {"type": "input_image", "image_url": "/tmp/f.png"},
            ]
        }
    )
    assert len(refs) == 1
    assert refs[0]["path"] == "/tmp/f.png"


def test_is_startup_context_requires_all_markers():
    assert not is_startup_context("plain text")
    assert not is_startup_context("<recommended_plugins>only this</recommended_plugins>")


def test_user_text_from_event_non_string_message():
    assert user_text_from_event({"message": 123}) == ""


def test_user_text_from_response_item_strips():
    assert user_text_from_response_item({"content": [{"text": "  hi  "}]}) == "hi"


def test_assistant_text_from_payload_uses_output_text_key():
    assert (
        assistant_text_from_payload({"content": [{"output_text": "hello"}]}) == "hello"
    )


def test_content_text_plain_string_content():
    assert assistant_text_from_payload({"content": "plain string"}) == "plain string"


def test_content_text_list_of_plain_strings():
    assert (
        assistant_text_from_payload({"content": ["line one", "line two"]})
        == "line one\nline two"
    )


# --- tool descriptor / skill refs / result note ---------------------------------------


def test_tool_descriptor_empty_args_and_parallel_tool_uses():
    assert tool_descriptor("noop", "") == ""
    assert tool_descriptor("noop", None) == ""
    args = json.dumps(
        {
            "tool_uses": [
                {"recipient_name": "one"},
                {"recipient_name": "two"},
                {"not_recipient": "x"},
            ]
        }
    )
    assert tool_descriptor("multi_tool_use.parallel", args) == "one, two"


def test_tool_descriptor_falls_back_to_json_dump():
    assert tool_descriptor("thing", {"foo": "bar"}) == '{"foo": "bar"}'


def test_tool_descriptor_unserializable_args_returns_empty():
    class Weird:
        pass

    assert tool_descriptor("thing", {"foo": Weird()}) == ""


def test_skill_refs_from_call_skill_tool_without_value_adds_nothing():
    assert skill_names_from_call("skill", {}) == []


def test_skill_refs_from_call_dedupes_repeated_skill_path_mention():
    # The same SKILL.md path mentioned twice in one call must not duplicate
    # the skill reference.
    blob = "/plugins/documents/skills/pdf/SKILL.md and again /plugins/documents/skills/pdf/SKILL.md"
    refs = skill_names_from_call("exec_command", {"cmd": blob})
    assert refs == ["pdf"]


def test_result_note_handles_dict_and_unserializable():
    assert result_note({"a": 1}) == '{"a": 1}'

    class Weird:
        pass

    assert result_note({"a": Weird()}) == ""


# --- plan / structured-answer parsing ---------------------------------------


def test_plan_update_requires_list_of_steps_with_str_step():
    assert _plan_update("{}") is None
    assert _plan_update(json.dumps({"plan": "not-a-list"})) is None
    assert _plan_update(json.dumps({"plan": [{"status": "pending"}]})) is None
    result = _plan_update(
        json.dumps({"plan": [{"step": "Do it"}], "explanation": "why"})
    )
    assert result == {"explanation": "why", "steps": [{"step": "Do it", "status": "pending"}]}


def test_decode_jsonish_passthrough_and_parse():
    assert _decode_jsonish(123) == 123
    assert _decode_jsonish("not json") == "not json"
    assert _decode_jsonish(json.dumps({"a": 1})) == {"a": 1}


def test_value_to_answer_text_variants():
    assert _value_to_answer_text(True) == "True"
    assert _value_to_answer_text(3.5) == "3.5"
    assert _value_to_answer_text(["a", "", "b"]) == "a, b"
    assert _value_to_answer_text({"value": "picked"}) == "picked"
    assert _value_to_answer_text({"unmapped": "x"}) == ""
    assert _value_to_answer_text(None) == ""


def test_extract_answer_from_item_matches_by_various_ids():
    question = {"id": "q1", "question": "Pick one", "header": "H"}
    assert _extract_answer_from_item(question, "not-a-dict") == ""
    assert _extract_answer_from_item(question, {"id": "other"}) == ""
    assert (
        _extract_answer_from_item(question, {"question_id": "q1", "value": "yes"})
        == "yes"
    )


def test_structured_answer_for_question_container_and_fallback_paths():
    question = {"id": "q1", "question": "Pick one", "header": "H"}

    # Direct top-level key match.
    assert _structured_answer_for_question(question, {"q1": "direct"}) == "direct"

    # Container dict keyed by question id.
    assert (
        _structured_answer_for_question(question, {"answers": {"q1": "via-container"}})
        == "via-container"
    )

    # Container dict whose values must be scanned (list within answers values).
    assert (
        _structured_answer_for_question(
            question, {"answers": {"other": {"question_id": "q1", "value": "scanned"}}}
        )
        == "scanned"
    )

    # Container list of items.
    assert (
        _structured_answer_for_question(
            question, {"responses": [{"id": "q1", "value": "listed"}]}
        )
        == "listed"
    )

    # Fallback: output itself matches via _extract_answer_from_item.
    assert (
        _structured_answer_for_question(question, {"id": "q1", "value": "top"})
        == "top"
    )

    # Output is a bare list.
    assert (
        _structured_answer_for_question(question, [{"id": "q1", "value": "bare-list"}])
        == "bare-list"
    )

    # Nothing matches.
    assert _structured_answer_for_question(question, {"nope": "x"}) == ""
    assert _structured_answer_for_question(question, "plain string") == ""


def test_flatten_result_text_variants():
    assert _flatten_result_text(True) == "True"
    assert _flatten_result_text([1, "a"]) == "1 a"
    assert _flatten_result_text({"message": "hi", "text": "there"}) == "hi there"
    assert _flatten_result_text({"unmapped": "value"}) == "value"
    assert _flatten_result_text({}) == ""


def test_parse_chosen_marker_and_split_fallback():
    options = [{"label": "QUAL"}, {"label": "SE"}]
    val, chosen = _parse_chosen("Pick a team", options, '"Pick a team"="QUAL"')
    assert val == "QUAL"
    assert chosen == [{"label": "QUAL"}]

    val, chosen = _parse_chosen("", options, 'result="SE"')
    assert val == "SE"
    assert chosen == [{"label": "SE"}]

    val, chosen = _parse_chosen("Pick a team", options, "no markers here")
    assert val == ""
    assert chosen == []


def test_option_qa_from_output_no_questions_returns_empty():
    assert option_qa_from_output("anything", None) == []
    assert option_qa_from_output("anything", []) == []


def test_option_qa_from_output_falls_back_to_structured_answer():
    questions = [{"id": "q1", "question": "Pick", "header": "H", "options": []}]
    qas = option_qa_from_output({"q1": "structured-value"}, questions)
    assert qas[0]["answer_text"] == "structured-value"


# --- patch paths / unique path bookkeeping ---------------------------------------


def test_patch_paths_handles_variants():
    assert patch_paths(None) == []
    assert patch_paths(123) == []
    assert patch_paths("*** Add File: a.py\n*** Update File: b.py") == ["a.py", "b.py"]
    assert patch_paths(json.dumps({"input": "*** Delete File: c.py"})) == ["c.py"]
    assert patch_paths({"content": "*** Add File: d.py"}) == ["d.py"]


def test_append_unique_path_skips_empty_and_duplicates():
    paths: list = []
    seen: set = set()
    append_unique_path(paths, seen, "")
    assert paths == []
    append_unique_path(paths, seen, "src/a.py")
    append_unique_path(paths, seen, "project/src/a.py")  # suffix duplicate
    append_unique_path(paths, seen, "src/a.py/extra")  # not a duplicate
    assert paths == ["src/a.py", "src/a.py/extra"]


# --- build_turns edge cases ---------------------------------------


def test_build_turns_turn_context_sets_mode_on_existing_turn_without_one():
    entries = [
        {
            "type": "event_msg",
            "timestamp": "2026-01-01T00:00:00Z",
            "payload": {"type": "user_message", "message": "hi"},
        },
        {
            "type": "turn_context",
            "timestamp": "2026-01-01T00:00:01Z",
            "payload": {"collaboration_mode": {"mode": "plan"}},
        },
    ]
    turns, _ = build_turns(entries)
    assert turns[0].collaboration_mode == "plan"


def test_build_turns_patch_apply_end_records_paths_and_result_note():
    entries = [
        {
            "type": "event_msg",
            "timestamp": "2026-01-01T00:00:00Z",
            "payload": {"type": "user_message", "message": "edit files"},
        },
        {
            "type": "event_msg",
            "timestamp": "2026-01-01T00:00:01Z",
            "payload": {
                "type": "patch_apply_end",
                "changes": {"src/a.py": {}},
                "stdout": "patched ok",
            },
        },
    ]
    turns, files_changed = build_turns(entries)
    assert files_changed == ["src/a.py"]
    assert turns[0].result_notes == ["apply_patch: patched ok"]


def test_build_turns_patch_apply_end_before_any_turn_only_records_path():
    entries = [
        {
            "type": "event_msg",
            "timestamp": "2026-01-01T00:00:00Z",
            "payload": {
                "type": "patch_apply_end",
                "changes": {"src/a.py": {}},
            },
        }
    ]
    turns, files_changed = build_turns(entries)
    assert files_changed == ["src/a.py"]
    assert turns == []


def test_build_turns_assistant_message_before_any_user_turn_creates_turn():
    entries = [
        {
            "type": "response_item",
            "timestamp": "2026-01-01T00:00:00Z",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Hello first"}],
            },
        }
    ]
    turns, _ = build_turns(entries)
    assert len(turns) == 1
    assert turns[0].assistant_text_blocks == ["Hello first"]


def test_build_turns_function_call_before_any_user_turn_creates_turn():
    entries = [
        {
            "type": "response_item",
            "timestamp": "2026-01-01T00:00:00Z",
            "payload": {
                "type": "function_call",
                "name": "shell",
                "call_id": "c1",
                "input": json.dumps({"command": "ls"}),
            },
        }
    ]
    turns, _ = build_turns(entries)
    assert len(turns) == 1
    assert turns[0].tool_bullets == ["- shell -> ls"]


def test_build_turns_apply_patch_tool_call_records_file_paths():
    entries = [
        {
            "type": "event_msg",
            "timestamp": "2026-01-01T00:00:00Z",
            "payload": {"type": "user_message", "message": "patch it"},
        },
        {
            "type": "response_item",
            "timestamp": "2026-01-01T00:00:01Z",
            "payload": {
                "type": "function_call",
                "name": "apply_patch",
                "call_id": "c1",
                "arguments": json.dumps(
                    {"input": "*** Add File: new.py"}
                ),
            },
        },
    ]
    _, files_changed = build_turns(entries)
    assert files_changed == ["new.py"]


def test_build_turns_function_call_output_before_any_turn_is_ignored():
    entries = [
        {
            "type": "response_item",
            "timestamp": "2026-01-01T00:00:00Z",
            "payload": {
                "type": "function_call_output",
                "call_id": "c1",
                "output": "result",
            },
        }
    ]
    turns, _ = build_turns(entries)
    assert turns == []


def test_build_turns_empty_tool_bullet_has_no_arrow():
    entries = [
        {
            "type": "event_msg",
            "timestamp": "2026-01-01T00:00:00Z",
            "payload": {"type": "user_message", "message": "do it"},
        },
        {
            "type": "response_item",
            "timestamp": "2026-01-01T00:00:01Z",
            "payload": {
                "type": "function_call",
                "name": "noop_tool",
                "call_id": "c1",
                "arguments": "",
            },
        },
    ]
    turns, _ = build_turns(entries)
    assert turns[0].tool_bullets == ["- noop_tool"]


# --- build_events edge cases ---------------------------------------


def test_build_events_skips_blank_user_event_without_text_or_images():
    entries = [
        {
            "type": "event_msg",
            "timestamp": "2026-01-01T00:00:00Z",
            "payload": {"type": "user_message", "message": ""},
        },
        {
            "type": "event_msg",
            "timestamp": "2026-01-01T00:00:01Z",
            "payload": {"type": "user_message", "message": "real text"},
        },
    ]
    events = build_events(entries)
    assert len(events) == 1
    assert events[0]["text"] == "real text"


def test_build_events_startup_context_sets_pending_images_not_text():
    startup_text = "<recommended_plugins>x</recommended_plugins>\n# AGENTS.md instructions\n<environment_context>y</environment_context>"
    entries = [
        {
            "type": "response_item",
            "timestamp": "2026-01-01T00:00:00Z",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": startup_text}],
            },
        },
        {
            "type": "event_msg",
            "timestamp": "2026-01-01T00:00:01Z",
            "payload": {"type": "user_message", "message": "actual turn"},
        },
    ]
    events = build_events(entries)
    assert len(events) == 1
    assert events[0]["text"] == "actual turn"


def test_build_events_assistant_blank_text_is_skipped():
    entries = [
        {
            "type": "response_item",
            "timestamp": "2026-01-01T00:00:00Z",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "   "}],
            },
        }
    ]
    assert build_events(entries) == []


def test_build_events_function_call_flushes_pending_user_message():
    entries = [
        {
            "type": "response_item",
            "timestamp": "2026-01-01T00:00:00Z",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "do the thing"}],
            },
        },
        {
            "type": "response_item",
            "timestamp": "2026-01-01T00:00:01Z",
            "payload": {
                "type": "function_call",
                "name": "shell",
                "call_id": "c1",
                "arguments": json.dumps({"command": "ls"}),
            },
        },
    ]
    events = build_events(entries)
    assert events[0]["role"] == "user"
    assert events[0]["text"] == "do the thing"
    assert events[1]["tool_calls"][0]["name"] == "shell"


def test_build_events_output_with_unknown_call_id_is_ignored():
    entries = [
        {
            "type": "response_item",
            "timestamp": "2026-01-01T00:00:00Z",
            "payload": {
                "type": "function_call_output",
                "call_id": "unknown",
                "output": "whatever",
            },
        }
    ]
    assert build_events(entries) == []


# --- image byte decoding ---------------------------------------


def test_image_bytes_and_ext_invalid_data_url_and_bad_base64():
    assert _image_bytes_and_ext({"type": "data_url", "data_url": "not-a-data-url"}) is None
    assert _image_bytes_and_ext({"type": "data_url", "data_url": 123}) is None
    assert _image_bytes_and_ext({"type": "base64", "data": 123}) is None
    assert _image_bytes_and_ext({"type": "base64", "data": "!!!not-base64!!!"}) is None
    assert _image_bytes_and_ext({"type": "unknown"}) is None


def test_image_bytes_and_ext_base64_without_media_type_defaults_to_img():
    raw, ext = _image_bytes_and_ext({"type": "base64", "data": _PNG_B64})
    assert raw == base64.b64decode(_PNG_B64)
    assert ext == "img"


def test_image_bytes_and_ext_path_missing_file_returns_none():
    assert _image_bytes_and_ext({"type": "path", "path": "/no/such/file.png"}) is None
    assert _image_bytes_and_ext({"type": "path", "path": 123}) is None


def test_dump_images_skips_unresolvable_ref():
    turns, _ = build_turns(
        [
            {
                "type": "event_msg",
                "timestamp": "2026-01-01T00:00:00Z",
                "payload": {
                    "type": "user_message",
                    "message": "broken image",
                    "images": ["/no/such/file.png"],
                },
            }
        ]
    )
    with tempfile.TemporaryDirectory() as tmp_name:
        written = dump_images(turns, "sess", dump_dir=Path(tmp_name))
        assert written == []
        assert "description pending" not in turns[0].user_text


# --- session-level helpers ---------------------------------------


def test_first_timestamp_empty_entries_returns_none():
    assert first_timestamp([]) is None
    assert first_timestamp([{"no": "timestamp"}]) is None


def test_session_models_dedupes_and_ignores_missing():
    entries = [
        {"type": "turn_context", "payload": {"model": "gpt-a"}},
        {"type": "turn_context", "payload": {"model": "gpt-a"}},
        {"type": "turn_context", "payload": {}},
        {"type": "turn_context", "payload": {"model": "gpt-b"}},
    ]
    assert session_models(entries) == ["gpt-a", "gpt-b"]


def test_derive_project_label_falls_back_to_cwd_without_sid_and_to_path():
    entries_with_cwd_no_id = [
        {"type": "session_meta", "payload": {"cwd": "/work/app"}},
    ]
    assert (
        derive_project_label(Path("/x.jsonl"), entries_with_cwd_no_id) == "/work/app"
    )

    assert derive_project_label(Path("/x.jsonl"), []) == "/x.jsonl"


def test_derive_context_line_falls_back_when_no_text_or_command():
    class EmptyTurn:
        command = None
        user_text = ""

    assert _derive_context_line([EmptyTurn()]) == "a Codex session"


# --- render() edge cases ---------------------------------------


def test_render_summary_no_input_turns():
    class ToolOnlyTurn:
        user_text = ""
        command = None

    out = render_summary([ToolOnlyTurn()], None)
    assert "_(No user inputs in this transcript.)_" in out


def test_render_handles_empty_turns_list():
    md = render([], [], "project", None)
    assert "_(No conversational turns found in this Codex transcript.)_" in md


def test_render_includes_models_line():
    entries = [
        {
            "type": "event_msg",
            "timestamp": "2026-01-01T00:00:00Z",
            "payload": {"type": "user_message", "message": "hi"},
        }
    ]
    turns, files_changed = build_turns(entries)
    md = render(turns, files_changed, "project", "2026-01-01T00:00:00Z", models=["gpt-codex"])
    assert "Model: gpt-codex." in md


def test_render_no_user_text_placeholder_in_full_detail():
    entries = [
        {
            "type": "response_item",
            "timestamp": "2026-01-01T00:00:00Z",
            "payload": {
                "type": "function_call",
                "name": "shell",
                "call_id": "c1",
                "arguments": json.dumps({"command": "ls"}),
            },
        }
    ]
    turns, files_changed = build_turns(entries)
    md = render(turns, files_changed, "project", "2026-01-01T00:00:00Z")
    assert "**User:** _(no user text - pre-conversation activity)_" in md


def test_render_custom_answer_without_chosen_label():
    questions = [
        {
            "id": "team",
            "header": "Team",
            "question": "Pick a team",
            "options": [{"label": "QUAL"}],
        }
    ]
    entries = [
        {
            "type": "event_msg",
            "timestamp": "2026-01-01T00:00:00Z",
            "payload": {"type": "user_message", "message": "need help"},
        },
        {
            "type": "response_item",
            "timestamp": "2026-01-01T00:00:01Z",
            "payload": {
                "type": "function_call",
                "name": "functions.request_user_input",
                "call_id": "call-1",
                "arguments": json.dumps({"questions": questions}),
            },
        },
        {
            "type": "response_item",
            "timestamp": "2026-01-01T00:00:02Z",
            "payload": {
                "type": "function_call_output",
                "call_id": "call-1",
                "output": json.dumps({"team": "Something else entirely"}),
            },
        },
    ]
    turns, files_changed = build_turns(entries)
    md = render(turns, files_changed, "project", "2026-01-01T00:00:00Z")
    assert "_(custom answer)_ Something else entirely" in md


def test_render_permission_denial_in_summary_and_detail():
    entries = [
        {
            "type": "event_msg",
            "timestamp": "2026-01-01T00:00:00Z",
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
    turns, files_changed = build_turns(entries)
    md = render(turns, files_changed, "project", "2026-01-01T00:00:00Z")
    assert "_Denied permission:_ **external_agent**" in md
    assert "> not like that" in md
    assert '_user denied permission:_ external_agent -> "not like that"' in md


def test_render_truncates_many_result_notes():
    entries = [
        {
            "type": "event_msg",
            "timestamp": "2026-01-01T00:00:00Z",
            "payload": {"type": "user_message", "message": "run things"},
        }
    ]
    for i in range(8):
        entries.append(
            {
                "type": "response_item",
                "timestamp": f"2026-01-01T00:00:0{i}Z",
                "payload": {
                    "type": "function_call",
                    "name": "shell",
                    "call_id": f"c{i}",
                    "arguments": json.dumps({"command": f"cmd{i}"}),
                },
            }
        )
        entries.append(
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": f"c{i}",
                    "output": f"out{i}",
                },
            }
        )
    turns, files_changed = build_turns(entries)
    md = render(turns, files_changed, "project", "2026-01-01T00:00:00Z")
    assert "(+2 more results)" in md


def test_render_numbers_multiple_plan_updates():
    entries = [
        {
            "type": "event_msg",
            "timestamp": "2026-01-01T00:00:00Z",
            "payload": {"type": "task_started", "collaboration_mode_kind": "plan"},
        },
        {
            "type": "event_msg",
            "timestamp": "2026-01-01T00:00:01Z",
            "payload": {"type": "user_message", "message": "plan twice"},
        },
    ]
    for i in range(2):
        entries.append(
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "update_plan",
                    "call_id": f"plan-{i}",
                    "arguments": json.dumps(
                        {"plan": [{"step": f"Step {i}", "status": "pending"}]}
                    ),
                },
            }
        )
    turns, files_changed = build_turns(entries)
    md = render(turns, files_changed, "project", "2026-01-01T00:00:00Z")
    assert "**Assistant updated the Plan mode plan (1):**" in md
    assert "**Assistant updated the Plan mode plan (2):**" in md


def test_render_lists_files_changed():
    entries = [
        {
            "type": "event_msg",
            "timestamp": "2026-01-01T00:00:00Z",
            "payload": {"type": "user_message", "message": "edit"},
        },
        {
            "type": "event_msg",
            "timestamp": "2026-01-01T00:00:01Z",
            "payload": {
                "type": "patch_apply_end",
                "changes": {"src/a.py": {}},
            },
        },
    ]
    turns, files_changed = build_turns(entries)
    md = render(turns, files_changed, "project", "2026-01-01T00:00:00Z")
    assert "## Files changed during the session" in md
    assert "src/a.py" in md


# --- render_transcript / write_raw_envelope / _extract_all / main errors ---------------------------------------


def test_render_transcript_raises_for_no_parseable_entries():
    with tempfile.TemporaryDirectory() as tmp_name:
        path = Path(tmp_name) / "empty.jsonl"
        path.write_text("not json\n")
        try:
            render_transcript(path)
            assert False, "expected ValueError"
        except ValueError:
            pass


def test_write_raw_envelope_returns_none_for_no_events():
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        transcript = tmp / "t.jsonl"
        transcript.write_text(json.dumps({"type": "session_meta", "payload": {}}))
        entries = load_entries(transcript)
        assert write_raw_envelope(transcript, entries, tmp) is None


def test_extract_all_reports_error_when_nothing_matches():
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        sessions = tmp / "sessions"
        sessions.mkdir()
        rc = _extract_all(str(sessions), str(tmp / "out"))
        assert rc == 1


def test_extract_all_skips_unparseable_transcript_and_still_succeeds(monkeypatch):
    import extract_codex_session_log as mod

    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        project = tmp / "project"
        sessions = tmp / "sessions"
        out = tmp / "out"
        project.mkdir()
        sessions.mkdir()

        _write_jsonl(
            sessions / "rollout-good.jsonl",
            _session_entries("good", project, "Good session"),
        )
        _write_jsonl(
            sessions / "rollout-bad.jsonl",
            _session_entries("bad", project, "Bad session"),
        )

        original_render_transcript = mod.render_transcript

        def flaky_render_transcript(transcript, **kwargs):
            if "bad" in transcript.name:
                raise ValueError("boom")
            return original_render_transcript(transcript, **kwargs)

        monkeypatch.setattr(mod, "render_transcript", flaky_render_transcript)
        monkeypatch.chdir(project)
        rc = mod._extract_all(str(sessions), str(out))

        assert rc == 0
        written = sorted(p.name for p in out.glob("codex_session_log_*.md"))
        assert written == ["codex_session_log_good.md"]


# --- main() CLI paths ---------------------------------------


def test_main_errors_when_transcript_not_found():
    rc = main(["--transcript", "/no/such/file.jsonl"])
    assert rc == 1


def test_main_single_session_to_stdout():
    import contextlib
    import io

    with tempfile.TemporaryDirectory() as tmp_name:
        path = Path(tmp_name) / "t.jsonl"
        path.write_text(
            "\n".join(
                json.dumps(e)
                for e in [
                    {"type": "session_meta", "payload": {"id": "s1"}},
                    {
                        "type": "event_msg",
                        "timestamp": "2026-01-01T00:00:00Z",
                        "payload": {"type": "user_message", "message": "hi"},
                    },
                ]
            )
        )
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            rc = main(["--transcript", str(path), "--output", "-"])
        assert rc == 0
        assert "# Codex Session Conversation Log" in captured.getvalue()


def test_main_single_session_writes_markdown_and_raw_file():
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        transcript = tmp / "t.jsonl"
        transcript.write_text(
            "\n".join(
                json.dumps(e)
                for e in [
                    {"type": "session_meta", "payload": {"id": "s1"}},
                    {
                        "type": "event_msg",
                        "timestamp": "2026-01-01T00:00:00Z",
                        "payload": {"type": "user_message", "message": "hi"},
                    },
                ]
            )
        )
        out_md = tmp / "out.md"
        rc = main(["--transcript", str(transcript), "--output", str(out_md)])
        assert rc == 0
        assert out_md.exists()
        assert (tmp / "codex_session_log_raw_s1.json").exists()


def test_main_single_session_no_raw_skips_raw_file():
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        transcript = tmp / "t.jsonl"
        transcript.write_text(
            "\n".join(
                json.dumps(e)
                for e in [
                    {"type": "session_meta", "payload": {"id": "s1"}},
                    {
                        "type": "event_msg",
                        "timestamp": "2026-01-01T00:00:00Z",
                        "payload": {"type": "user_message", "message": "hi"},
                    },
                ]
            )
        )
        out_md = tmp / "out.md"
        rc = main(
            ["--transcript", str(transcript), "--output", str(out_md), "--no-raw"]
        )
        assert rc == 0
        assert out_md.exists()
        assert not (tmp / "codex_session_log_raw_s1.json").exists()


def test_main_single_session_render_error_returns_1():
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        transcript = tmp / "t.jsonl"
        transcript.write_text("not json\n")
        rc = main(["--transcript", str(transcript), "--output", "-"])
        assert rc == 1


# --- remaining small-gap coverage ---------------------------------------


def test_jsonl_files_returns_empty_for_missing_root():
    try:
        select_transcript(None, None, "/no/such/sessions/root")
        assert False, "expected FileNotFoundError"
    except FileNotFoundError:
        pass


def test_flatten_result_text_unhandled_type_returns_empty():
    assert _flatten_result_text(None) == ""


def test_option_qa_from_output_skips_non_dict_question():
    questions = [
        "not-a-dict",
        {"id": "q1", "question": "Pick", "header": "H", "options": []},
    ]
    qas = option_qa_from_output({"q1": "val"}, questions)
    assert len(qas) == 1
    assert qas[0]["answer_text"] == "val"


def test_skill_refs_from_call_backfills_path_for_already_seen_skill():
    args = {
        "skill": "pdf",
        "cmd": "/plugins/documents/skills/pdf/SKILL.md",
    }
    refs = skill_refs_from_call("skill", args)
    assert refs == [{"name": "pdf", "path": "/plugins/documents/skills/pdf/SKILL.md"}]


def test_skill_refs_from_call_unserializable_args_skips_regex_scan():
    class Weird:
        pass

    assert skill_names_from_call("exec_command", {"cmd": Weird()}) == []


def test_build_turns_tagged_command_without_leading_slash_is_ignored():
    entries = [
        {
            "type": "event_msg",
            "timestamp": "2026-01-01T00:00:00Z",
            "payload": {
                "type": "user_message",
                "message": "<command-name>not-a-command</command-name>\nsome text",
            },
        }
    ]
    turns, _ = build_turns(entries)
    assert turns[0].command is None
    assert turns[0].user_text


def test_build_turns_startup_context_response_item_sets_pending_images_only():
    startup_text = (
        "<recommended_plugins>x</recommended_plugins>\n"
        "# AGENTS.md instructions\n<environment_context>y</environment_context>"
    )
    entries = [
        {
            "type": "response_item",
            "timestamp": "2026-01-01T00:00:00Z",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": startup_text}],
            },
        },
        {
            "type": "event_msg",
            "timestamp": "2026-01-01T00:00:01Z",
            "payload": {"type": "user_message", "message": "actual turn"},
        },
    ]
    turns, _ = build_turns(entries)
    assert [t.user_text for t in turns] == ["actual turn"]


def test_build_events_function_call_arguments_fallback_to_input():
    entries = [
        {
            "type": "response_item",
            "timestamp": "2026-01-01T00:00:00Z",
            "payload": {
                "type": "function_call",
                "name": "shell",
                "call_id": "c1",
                "input": json.dumps({"command": "ls"}),
            },
        }
    ]
    events = build_events(entries)
    call = events[0]["tool_calls"][0]
    assert call["input"] == {"command": "ls"}


def test_image_bytes_and_ext_data_url_bad_base64_padding():
    ref = {"type": "data_url", "data_url": "data:image/png;base64,abc"}
    assert _image_bytes_and_ext(ref) is None


def test_unique_identifier_suffixes_duplicates():
    from extract_codex_session_log import _unique_identifier

    used: set = set()
    assert _unique_identifier("sess", used) == "sess"
    assert _unique_identifier("sess", used) == "sess-2"
    assert _unique_identifier("sess", used) == "sess-3"


def test_extract_all_write_failure_is_skipped():
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        project = tmp / "project"
        sessions = tmp / "sessions"
        out = tmp / "out"
        project.mkdir()
        sessions.mkdir()
        out.mkdir()

        _write_jsonl(
            sessions / "rollout-one.jsonl",
            _session_entries("session-one", project, "First session"),
        )
        # Pre-create a directory where the markdown file would be written so
        # write_text() raises OSError (IsADirectoryError).
        (out / "codex_session_log_session-one.md").mkdir()

        old_cwd = os.getcwd()
        try:
            os.chdir(project)
            rc = _extract_all(str(sessions), str(out))
        finally:
            os.chdir(old_cwd)

        assert rc == 1


def test_main_default_output_path_and_no_trailing_newline_write(monkeypatch):
    import extract_codex_session_log as mod

    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        transcript = tmp / "t.jsonl"
        transcript.write_text(
            json.dumps({"type": "session_meta", "payload": {"id": "s1"}})
            + "\n"
            + json.dumps(
                {
                    "type": "event_msg",
                    "timestamp": "2026-01-01T00:00:00Z",
                    "payload": {"type": "user_message", "message": "hi"},
                }
            )
        )

        fake_default = tmp / "default_out.md"
        monkeypatch.setattr(mod, "DEFAULT_OUTPUT", fake_default)
        monkeypatch.setattr(mod, "render_transcript", lambda *a, **k: "no trailing newline")
        rc = mod.main(["--transcript", str(transcript)])
        assert rc == 0
        assert fake_default.read_text(encoding="utf-8") == "no trailing newline"


def test_main_stdout_adds_trailing_newline_when_missing(monkeypatch):
    import contextlib
    import io

    import extract_codex_session_log as mod

    with tempfile.TemporaryDirectory() as tmp_name:
        transcript = Path(tmp_name) / "t.jsonl"
        transcript.write_text(json.dumps({"type": "session_meta", "payload": {}}))

        monkeypatch.setattr(mod, "render_transcript", lambda *a, **k: "no newline here")
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            rc = mod.main(["--transcript", str(transcript), "--output", "-"])
        assert rc == 0
        assert captured.getvalue() == "no newline here\n"


def test_main_write_failure_for_explicit_output_path(monkeypatch):
    import extract_codex_session_log as mod

    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        transcript = tmp / "t.jsonl"
        transcript.write_text(json.dumps({"type": "session_meta", "payload": {}}))
        out_dir_as_file_target = tmp / "out.md"
        out_dir_as_file_target.mkdir()  # a directory, so write_text() raises OSError

        monkeypatch.setattr(mod, "render_transcript", lambda *a, **k: "content\n")
        rc = mod.main(
            [
                "--transcript",
                str(transcript),
                "--output",
                str(out_dir_as_file_target),
            ]
        )
        assert rc == 1
