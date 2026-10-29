#!/usr/bin/env python3
"""Minimal asserts for extract_codex_session_log.

Run: uv run --locked python scripts/test_extract_codex_session_log.py
"""

from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import tempfile
from pathlib import Path

import pytest

from session_log_extractors import extract_codex_session_log as mod
from session_log_extractors import session_log_envelope
from session_log_extractors.codex_sources import (
    _cwd_matches,
    _paths_overlap,
    load_entries,
    newest,
    output_identifier,
    select_transcript,
    session_cwd_from_file,
    session_id_from_file,
)
from session_log_extractors.extract_codex_session_log import (
    _decode_jsonish,
    _extract_all,
    _extract_answer_from_item,
    _flatten_result_text,
    _image_bytes_and_ext,
    _parse_chosen,
    _plan_update,
    _structured_answer_for_question,
    _unique_identifier,
    _value_to_answer_text,
    append_unique_path,
    assistant_text_from_payload,
    build_raw_envelope,
    extract_transcript,
    first_timestamp,
    image_refs_from_event,
    image_refs_from_user_message_payload,
    main,
    option_qa_from_output,
    parse_permission_denial,
    patch_paths,
    session_models,
    skill_names_from_call,
    skill_refs_from_call,
)

# 1x1 transparent PNG.
_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9"
    "awAAAABJRU5ErkJggg=="
)


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "codex"


@pytest.mark.parametrize(
    "text",
    [
        "/review fixture",
        "  Hi 🙂\n<system-reminder>context</system-reminder>\n\n\n",
        "<task-notification>agent result</task-notification>",
        "<recommended_plugins>x</recommended_plugins>\n# AGENTS.md instructions\n"
        "<environment_context>y</environment_context>",
    ],
)
def test_codex_preserves_text_and_unknown_authorship_through_cli(
    text, tmp_path, capsys
):
    entries = load_entries(FIXTURES / "slash.jsonl")
    native = next(e for e in entries if e["payload"].get("role") == "user")
    # Vary only the captured input text: wrappers do not establish provenance.
    native["payload"]["content"][0]["text"] = text
    session = _interpret(entries)
    message = session["events"][0]
    expected = [
        {
            "start": 0,
            "end": len(text),
            "source": "unknown",
            "markers": [
                {"field": "type", "value": "response_item"},
                {"field": "payload.type", "value": "message"},
                {"field": "payload.content[0].type", "value": "input_text"},
            ],
            "ts": native["timestamp"],
        }
    ]
    assert message["text"] == text
    assert message["ts"] == native["timestamp"]
    assert message["authorship"] == expected
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("\n".join(json.dumps(e) for e in entries))
    assert main(["--transcript", str(transcript), "--output", "-"]) == 0
    exported = json.loads(capsys.readouterr().out)["events"]
    assert exported[0]["text"] == text
    assert exported[0]["authorship"] == expected
    assert [e["ts"] for e in exported] == [e["ts"] for e in session["events"]]


@pytest.mark.parametrize("startup", [False, True])
def test_codex_distinct_response_and_event_keep_text_and_image_owner(startup):
    entries = load_entries(FIXTURES / "image.jsonl")
    native = next(e for e in entries if e["payload"].get("role") == "user")
    if startup:
        # Replace one captured text block with the formerly skipped startup shape.
        native["payload"]["content"][0]["text"] = (
            "<recommended_plugins>x</recommended_plugins>\n# AGENTS.md instructions\n"
            "<environment_context>y</environment_context>"
        )
    # Insert a distinct event after the image-bearing response, not a duplicate.
    entries.insert(
        entries.index(native) + 1,
        {
            "type": "event_msg",
            "timestamp": "later",
            "payload": {"type": "user_message", "message": "Different input"},
        },
    )
    users = _users(entries)
    assert len(users) == 2
    assert users[0]["text"] == "\n".join(
        block["text"] for block in native["payload"]["content"] if "text" in block
    )
    assert users[0]["images"]
    assert users[1]["text"] == "Different input"
    assert users[1]["ts"] == "later"
    assert "images" not in users[1]


@pytest.mark.parametrize("text", ["", "  keep whitespace 🙂  "])
def test_codex_image_only_and_unfamiliar_source_remain_on_recorded_input(text):
    entries = load_entries(FIXTURES / "image.jsonl")
    native = next(e for e in entries if e["payload"].get("role") == "user")
    # Keep the captured image block, replacing text blocks to exercise no-text input.
    native["payload"]["content"] = [
        {"type": "input_text", "text": text, "source": {"future": [None, "ide"]}},
        *[b for b in native["payload"]["content"] if b["type"] == "input_image"],
    ]
    native["payload"]["source"] = None
    native.pop("timestamp")
    message = _events(entries)[0]
    assert message["text"] == text
    assert message["ts"] is None
    assert message["images"]
    if text:
        span = message["authorship"][0]
        assert span["source"] == "unknown"
        assert "ts" not in span
        assert {"field": "payload.source", "value": None} in span["markers"]
        assert span["markers"][-1] == {
            "field": "payload.content[0].source",
            "value": {"future": [None, "ide"]},
        }
    else:
        assert message["authorship"] == []


def test_codex_exact_duplicate_view_keeps_images_identity_and_tools():
    entries = load_entries(FIXTURES / "image.jsonl")
    native = next(e for e in entries if e["payload"].get("role") == "user")
    text = "\n".join(b["text"] for b in native["payload"]["content"] if "text" in b)
    original = _interpret(entries)
    # Add an exact event view, retaining the response view's recorded identity.
    native["payload"]["id"] = "response-id"
    native["payload"]["source"] = "unfamiliar-origin"
    entries.insert(
        entries.index(native) + 1,
        {
            "type": "event_msg",
            "timestamp": native["timestamp"],
            "payload": {"type": "user_message", "message": text},
        },
    )
    session = _interpret(entries)
    assert len(session["events"]) == len(original["events"])
    assert session["events"][0]["id"] == "response-id"
    assert session["events"][0]["images"] == original["events"][0]["images"]
    span = session["events"][0]["authorship"][0]
    assert span["source"] == "unknown"
    assert span["markers"][:2] == [
        {"field": "type", "value": "event_msg"},
        {"field": "payload.type", "value": "user_message"},
    ]
    assert {"field": "type", "value": "response_item"} in span["markers"]
    assert {"field": "payload.source", "value": "unfamiliar-origin"} in span["markers"]
    assert session["events"][1:] == original["events"][1:]


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


def _interpret(entries, image_inputs=None):
    source = mod.CodexSource(
        Path("unused.jsonl"), entries, image_inputs=image_inputs or {}
    )
    return mod.interpret_source(source) or {"events": [], "files_changed": []}


def _events(entries, tool_result_max_bytes=4096, *, image_inputs=None):
    return session_log_envelope.export_session(
        {"harness": "codex", "session_id": "test", **_interpret(entries, image_inputs)},
        tool_result_max_bytes,
    )["events"]


def _users(entries):
    return [e for e in _interpret(entries)["events"] if e["role"] == "user"]


def _calls(entries):
    return [c for e in _interpret(entries)["events"] for c in e.get("tool_calls", [])]


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
    assert _interpret(entries)["skills_used"] == ["pdf:pdf"]


def test_codex_initial_response_item_user_message_is_exported_once():
    entries = load_entries(FIXTURES / "model.jsonl")
    users = [e for e in entries if e["payload"].get("role") == "user"]
    # Add the event_msg view of the second captured input to test deduplication.
    second = users[1]
    entries.insert(
        entries.index(second) + 1,
        {
            "type": "event_msg",
            "timestamp": second["timestamp"],
            "payload": {
                "type": "user_message",
                "message": second["payload"]["content"][0]["text"],
            },
        },
    )
    assert [e["text"] for e in _users(entries)] == [
        "Reply with FIRST_MODEL_FIXTURE_MARKER.",
        "Reply with SECOND_MODEL_FIXTURE_MARKER.",
    ]


def test_codex_slash_command_is_recorded():
    entries = load_entries(FIXTURES / "slash.jsonl")
    users = _users(entries)
    assert len(users) == 1
    assert users[0]["text"] == "/review fixture"
    assert users[0]["command"] == "/review"
    assert users[0]["command_args"] == "fixture"


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

    user = _users(entries)[0]
    assert user["command"] == "/review"
    assert user["command_args"] == "frontend src"


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


def test_codex_handwritten_assistant_denial_branch():
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
    messages = _interpret(entries)["events"]
    assert messages[-1]["permission_denial"] == {
        "tool": "(tool call)",
        "message": "not like that",
        "by": "user",
    }
    assert "permission_denial" not in _calls(entries)[0]
    assert "not like that" in messages[-1]["text"]


def test_codex_native_denial_fixture_records_declined_command():
    entries = load_entries(FIXTURES / "denial.jsonl")
    call = _calls(entries)[0]
    assert call["permission_denial"] == {"tool": "exec", "message": "", "by": "user"}
    assert "rejected by user" in call["result"].lower()


def test_codex_handwritten_plan_mode_update_plan_branch():
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

    session = _interpret(entries)
    assert session["events"][0]["collaboration_mode"] == "plan"
    assert _calls(entries)[0]["plan_update"]["steps"] == [
        {"step": "Inspect schema", "status": "completed"},
        {"step": "Design change", "status": "in_progress"},
    ]
    assert session["events"][-1]["text"] == "Here is the plan."


def test_codex_native_plan_fixture_records_plan_mode():
    entries = load_entries(FIXTURES / "plan.jsonl")
    session = _interpret(entries)
    assert len(_users(entries)) == 1
    assert session["events"][0]["collaboration_mode"] == "plan"
    assert "<proposed_plan>" in "\n".join(e.get("text", "") for e in session["events"])


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
    assert _users(entries)[0]["collaboration_mode"] == "default"
    call = _calls(entries)[0]
    assert "plan_update" not in call
    assert call["name"] == "update_plan"


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

    assert _interpret(entries)["files_changed"] == []
    assert len(_users(entries)) == 3
    assert _calls(entries)[0]["questions"][0]["chosen_labels"] == ["QUAL"]
    assert _users(entries)[1]["text"] == "<bash-stdout>ignored</bash-stdout>"
    assert _users(entries)[2]["shell_command"] == "ls -la"


def test_codex_native_question_fixture_marks_answered_option():
    entries = load_entries(FIXTURES / "question.jsonl")
    assert _calls(entries)[0]["questions"][0]["chosen_labels"] == ["QUAL"]


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
        written = sorted(path.name for path in out.glob("codex_session_log_raw_*.json"))
        assert written == [
            "codex_session_log_raw_session-one.json",
            "codex_session_log_raw_session-two.json",
        ]
        assert "First session" in (
            out / "codex_session_log_raw_session-one.json"
        ).read_text(encoding="utf-8")
        assert not (out / "codex_session_log_raw_session-other.json").exists()


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
        written = sorted(path.name for path in out.glob("codex_session_log_raw_*.json"))
        assert written == [
            "codex_session_log_raw_session-one.json",
            "codex_session_log_raw_session-two.json",
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
        written = sorted(path.name for path in out.glob("codex_session_log_raw_*.json"))
        assert written == ["codex_session_log_raw_session-one.json"]


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
    ]


def test_interpret_events_keeps_local_and_unresolvable_images():
    # Codex keeps images out of band, so the envelope is where they become
    # portable.
    img = FIXTURES / "pixel.png"
    user = _events(
        _codex_entries(img), image_inputs={str(img): (img.read_bytes(), "png")}
    )[0]
    assert user["role"] == "user"
    assert base64.b64decode(user["images"][0]["data"]) == img.read_bytes()
    assert user["images"][0]["media_type"] == "image/png"
    # A missing screenshot must remain distinguishable from no screenshot.
    assert user["images"][1] == {"unavailable": True, "ref": "/gone/missing.png"}


def test_image_fixture_inlines_response_item_image_in_json(tmp_path):
    destination = tmp_path / "out.json"
    assert (
        main(
            [
                "--transcript",
                str(FIXTURES / "image.jsonl"),
                "--output",
                str(destination),
            ]
        )
        == 0
    )
    events = json.loads(destination.read_text())["events"]
    images = [image for event in events for image in event.get("images", [])]
    assert len(images) == 1
    assert images[0]["media_type"] == "image/png"
    assert base64.b64decode(images[0]["data"]) == (FIXTURES / "pixel.png").read_bytes()


def test_interpret_events_attaches_output_to_its_function_call():
    events = _events(
        load_entries(FIXTURES / "tool_patch_large.jsonl"), tool_result_max_bytes=100
    )
    calls = [c for e in events for c in e.get("tool_calls", [])]
    assert len(calls) == 2
    assert calls[0]["name"] == "exec"
    assert "printf TOOL_FIXTURE_MARKER" in calls[0]["input"]
    assert calls[0]["result"] == "TOOL_FIXTURE_MARKER"
    call = calls[1]
    assert "fixture.txt" in call["input"]
    assert call["result_bytes"] > 100
    assert call["result_truncated"] is True


# --- timestamp / formatting helpers ---------------------------------------


def test_parse_permission_denial_rejects_non_string():
    assert parse_permission_denial(None) is None
    assert parse_permission_denial(123) is None


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
    with pytest.raises(FileNotFoundError):
        select_transcript(None, "/no/such/path.jsonl", None)


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
        with pytest.raises(FileNotFoundError) as exc_info:
            select_transcript("dupe", None, str(dup_dir))

        assert "ambiguous" in str(exc_info.value)

        with pytest.raises(FileNotFoundError):
            select_transcript("nope", None, str(sessions))


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
            with pytest.raises(FileNotFoundError):
                select_transcript(None, None, str(sessions), strict=True)
        finally:
            os.chdir(old_cwd)


def test_select_transcript_raises_when_nothing_anywhere():
    with tempfile.TemporaryDirectory() as tmp_name:
        empty_root = Path(tmp_name) / "empty"
        empty_root.mkdir()
        with pytest.raises(FileNotFoundError):
            select_transcript(None, None, str(empty_root))


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


# --- skill references ---------------------------------------


def test_skill_refs_from_call_skill_tool_without_value_adds_nothing():
    assert skill_names_from_call("skill", {}) == []


def test_skill_refs_from_call_dedupes_repeated_skill_path_mention():
    # The same SKILL.md path mentioned twice in one call must not duplicate
    # the skill reference.
    blob = "/plugins/documents/skills/pdf/SKILL.md and again /plugins/documents/skills/pdf/SKILL.md"
    refs = skill_names_from_call("exec_command", {"cmd": blob})
    assert refs == ["pdf"]


# --- plan / structured-answer parsing ---------------------------------------


def test_plan_update_requires_list_of_steps_with_str_step():
    assert _plan_update("{}") is None
    assert _plan_update(json.dumps({"plan": "not-a-list"})) is None
    assert _plan_update(json.dumps({"plan": [{"status": "pending"}]})) is None
    result = _plan_update(
        json.dumps({"plan": [{"step": "Do it"}], "explanation": "why"})
    )
    assert result == {
        "explanation": "why",
        "steps": [{"step": "Do it", "status": "pending"}],
    }


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
        _structured_answer_for_question(question, {"id": "q1", "value": "top"}) == "top"
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


# --- canonical interpretation edge cases ---------------------------------------


def test_interpret_turn_context_sets_mode_on_existing_turn_without_one():
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
    assert _users(entries)[0]["collaboration_mode"] == "plan"


def test_interpret_patch_apply_end_records_paths_and_stdout():
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
    session = _interpret(entries)
    assert session["files_changed"] == ["src/a.py"]
    assert session["patch_results"][0]["stdout"] == "patched ok"


def test_interpret_patch_apply_end_before_any_turn_only_records_path():
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
    session = _interpret(entries)
    assert session["files_changed"] == ["src/a.py"]
    assert session["events"] == []


def test_interpret_assistant_message_before_any_user_is_preserved():
    entries = load_entries(FIXTURES / "model.jsonl")
    # Omit preceding inputs to exercise an assistant-only partial transcript.
    assistant = next(e for e in entries if e["payload"].get("role") == "assistant")
    assert _interpret([assistant])["events"][0]["text"] == "FIRST_MODEL_FIXTURE_MARKER"


def test_interpret_function_call_before_any_user_turn_creates_turn():
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
    assert _calls(entries)[0]["name"] == "shell"
    assert _calls(entries)[0]["input"] == {"command": "ls"}


def test_interpret_apply_patch_tool_call_records_file_paths():
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
                "arguments": json.dumps({"input": "*** Add File: new.py"}),
            },
        },
    ]
    assert _interpret(entries)["files_changed"] == ["new.py"]


def test_interpret_function_call_output_before_any_user_is_retained():
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
    session = _interpret(entries)
    assert session["events"] == []
    assert session["unmatched_tool_results"][0]["content"] == "result"


def test_interpret_empty_tool_input_is_preserved():
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
    assert _calls(entries)[0]["name"] == "noop_tool"
    assert _calls(entries)[0]["input"] == ""


# --- event export edge cases ---------------------------------------


def test_interpret_events_skips_blank_user_event_without_text_or_images():
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
    events = _events(entries)
    assert len(events) == 1
    assert events[0]["text"] == "real text"


@pytest.mark.parametrize("text", ["", "   ", "  reply 🙂  "])
def test_interpret_events_assistant_text_is_preserved(text):
    entries = load_entries(FIXTURES / "slash.jsonl")
    native = next(e for e in entries if e["payload"].get("role") == "assistant")
    # Vary the captured reply to cover empty and whitespace-preserving boundaries.
    native["payload"]["content"][0]["text"] = text
    entries = [native]
    events = _events(entries)
    if text:
        assert events[0]["text"] == text
        assert events[0]["authorship"][0]["source"] == "unknown"
        assert events[0]["authorship"][0]["end"] == len(text)
    else:
        assert events == []


def test_interpret_events_function_call_flushes_pending_user_message():
    entries = load_entries(FIXTURES / "question.jsonl")
    calls = [
        entry["payload"]
        for entry in entries
        if entry["payload"].get("type") in ("function_call", "function_call_output")
    ]
    assert [call["type"] for call in calls] == ["function_call", "function_call_output"]
    assert calls[0]["call_id"] == calls[1]["call_id"]

    events = _events(entries)
    assert events[0]["role"] == "user"
    assert (
        "Which team" in events[1]["tool_calls"][0]["input"]["questions"][0]["question"]
    )
    assert events[1]["tool_calls"][0]["name"] == "request_user_input"
    assert events[1]["tool_calls"][0]["result"] == "QUAL"


def test_export_omits_unmatched_result_without_inventing_a_message():
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
    assert _events(entries) == []


# --- image byte decoding ---------------------------------------


def test_image_bytes_and_ext_invalid_data_url_and_bad_base64():
    assert (
        _image_bytes_and_ext({"type": "data_url", "data_url": "not-a-data-url"}) is None
    )
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


# --- session-level helpers ---------------------------------------


def test_first_timestamp_empty_entries_returns_none():
    assert first_timestamp([]) is None
    assert first_timestamp([{"no": "timestamp"}]) is None


def test_session_models_dedupes_and_ignores_missing():
    entries = load_entries(FIXTURES / "model.jsonl")
    entries += [
        {"type": "turn_context", "payload": {"model": "gpt-6-luna"}},
        {"type": "turn_context", "payload": {}},
    ]
    assert session_models(entries) == ["gpt-6.1-sol", "gpt-6-luna"]


# --- retained interpretation edge cases ---------------------------------------


def test_custom_answer_without_chosen_label():
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
    assert (
        _calls(entries)[0]["questions"][0]["answer_text"] == "Something else entirely"
    )


def test_multiple_plan_updates():
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
    assert [c["plan_update"]["steps"][0]["step"] for c in _calls(entries)] == [
        "Step 0",
        "Step 1",
    ]


def test_records_files_changed():
    entries = load_entries(FIXTURES / "tool_patch_large.jsonl")
    assert _interpret(entries)["files_changed"] == ["/workspace/fixture.txt"]


# --- extract_transcript / build_raw_envelope / _extract_all / main errors ---------------------------------------


def test_extract_transcript_raises_for_no_parseable_entries():
    with tempfile.TemporaryDirectory() as tmp_name:
        path = Path(tmp_name) / "empty.jsonl"
        path.write_text("not json\n")
        with pytest.raises(ValueError, match="no parseable JSON entries found"):
            extract_transcript(path)


def test_build_raw_envelope_returns_none_for_no_events():
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        transcript = tmp / "t.jsonl"
        transcript.write_text(json.dumps({"type": "session_meta", "payload": {}}))
        assert build_raw_envelope(mod.read_source(transcript)) is None


def test_extract_all_reports_error_when_nothing_matches():
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        sessions = tmp / "sessions"
        sessions.mkdir()
        rc = _extract_all(str(sessions), str(tmp / "out"))
        assert rc == 1


def test_extract_all_skips_unparseable_transcript_and_still_succeeds(monkeypatch):
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

        original_interpret = mod.interpret_source

        def flaky_build_envelope(source, *args):
            if "bad" in source.path.name:
                raise ValueError("boom")
            return original_interpret(source, *args)

        monkeypatch.setattr(mod, "interpret_source", flaky_build_envelope)
        monkeypatch.chdir(project)
        rc = mod._extract_all(str(sessions), str(out))

        assert rc == 0
        written = sorted(p.name for p in out.glob("codex_session_log_raw_*.json"))
        assert written == ["codex_session_log_raw_good.json"]


# --- main() CLI paths ---------------------------------------


def test_main_errors_when_transcript_not_found():
    rc = main(["--transcript", "/no/such/file.jsonl"])
    assert rc == 1


def test_main_single_session_to_stdout():
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        rc = main(
            ["--transcript", str(FIXTURES / "tool_patch_large.jsonl"), "--output", "-"]
        )
    assert rc == 0
    assert json.loads(captured.getvalue())["harness"] == "codex"
    assert "TOOL_FIXTURE_MARKER" in captured.getvalue()
    assert "/workspace/fixture.txt" in captured.getvalue()


def test_main_single_session_writes_only_json(tmp_path):
    destination = tmp_path / "out.json"
    assert (
        main(
            [
                "--transcript",
                str(FIXTURES / "model.jsonl"),
                "--output",
                str(destination),
            ]
        )
        == 0
    )
    envelope = json.loads(destination.read_text())
    assert envelope["models"] == ["gpt-6.1-sol", "gpt-6-luna"]
    assert "SECOND_MODEL_FIXTURE_MARKER" in destination.read_text()
    assert list(tmp_path.iterdir()) == [destination]


def test_main_single_session_parse_error_returns_1():
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        transcript = tmp / "t.jsonl"
        transcript.write_text("not json\n")
        rc = main(["--transcript", str(transcript), "--output", "-"])
        assert rc == 1


# --- remaining small-gap coverage ---------------------------------------


def test_jsonl_files_returns_empty_for_missing_root():
    with pytest.raises(FileNotFoundError):
        select_transcript(None, None, "/no/such/sessions/root")


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


def test_interpret_tagged_command_without_leading_slash_is_ignored():
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
    assert "command" not in _users(entries)[0]
    assert _users(entries)[0]["text"] == entries[0]["payload"]["message"]


def test_interpret_events_function_call_arguments_fallback_to_input():
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
    events = _events(entries)
    call = events[0]["tool_calls"][0]
    assert call["input"] == {"command": "ls"}


def test_image_bytes_and_ext_data_url_bad_base64_padding():
    ref = {"type": "data_url", "data_url": "data:image/png;base64,abc"}
    assert _image_bytes_and_ext(ref) is None


def test_unique_identifier_suffixes_duplicates():
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
        # Pre-create a directory where the JSON file would be written so
        # write_text() raises OSError (IsADirectoryError).
        (out / "codex_session_log_raw_session-one.json").mkdir()

        old_cwd = os.getcwd()
        try:
            os.chdir(project)
            rc = _extract_all(str(sessions), str(out))
        finally:
            os.chdir(old_cwd)

        assert rc == 1


def test_main_write_failure_for_explicit_output_path(tmp_path):
    destination = tmp_path / "out.json"
    destination.mkdir()
    assert (
        main(
            [
                "--transcript",
                str(FIXTURES / "model.jsonl"),
                "--output",
                str(destination),
            ]
        )
        == 1
    )


@pytest.mark.parametrize("mode", ["stdout", "file", "default", "batch"])
def test_json_entrypoints_bypass_presentation(mode, tmp_path, monkeypatch, capsys):
    transcript = tmp_path / "session.jsonl"
    entries = _session_entries("s1", tmp_path, "# Recorded Markdown")
    entries += load_entries(FIXTURES / "image.jsonl")[1:]
    entries += load_entries(FIXTURES / "tool_patch_large.jsonl")[1:]
    _write_jsonl(transcript, entries)
    expected = _events(entries)
    monkeypatch.chdir(tmp_path)

    assert not hasattr(mod, "build_turns")
    args = ["--transcript", str(transcript)]
    destination = tmp_path / "out" / "codex_session_log_raw_s1.json"
    if mode == "stdout":
        args += ["--output", "-"]
    elif mode == "file":
        destination = tmp_path / "explicit.json"
        args += ["--output", str(destination)]
    elif mode == "batch":
        args = ["--all", "--sessions-root", str(tmp_path)]
    assert main(args, project_root=tmp_path / "out") == 0
    captured = capsys.readouterr()
    envelope = json.loads(captured.out if mode == "stdout" else destination.read_text())
    assert envelope["events"] == expected
    assert envelope["session_id"] == "s1"
    assert envelope["harness"] == "codex"
    assert envelope["cwd"] == str(tmp_path)
    assert "using transcript:" in captured.err or "extracting" in captured.err
    assert not list(tmp_path.rglob("*.md"))
    assert not list(tmp_path.rglob("*.png"))
    if mode != "stdout":
        assert captured.out == ""


@pytest.mark.parametrize("cap", [0, 7, -1])
@pytest.mark.parametrize("batch", [False, True])
def test_cli_preserves_nested_tools_and_result_cap(
    cap, batch, tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    entries = _session_entries("s1", tmp_path, "é **content**")
    tool_input = {
        "tool_uses": [{"recipient_name": "exec", "parameters": {"cmd": "pwd"}}]
    }
    result = "é" * 10
    entries += [
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "multi_tool_use.parallel",
                "call_id": "c1",
                "arguments": json.dumps(tool_input),
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "c1",
                "output": {"content": [{"type": "text", "text": result}]},
            },
        },
    ]
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(transcript, entries)
    args = ["--tool-result-bytes", str(cap)]
    if batch:
        args += ["--sessions-root", str(tmp_path), "--output", str(tmp_path / "out")]
    else:
        args += ["--transcript", str(transcript), "--output", "-"]
    assert main(args) == 0
    captured = capsys.readouterr()
    envelope = json.loads(
        (tmp_path / "out" / "codex_session_log_raw_s1.json").read_text()
        if batch
        else captured.out
    )
    assert envelope["truncation"] == {"tool_result_max_bytes": cap}
    assert envelope["events"][0]["text"] == "é **content**"
    call = envelope["events"][1]["tool_calls"][0]
    assert call["input"] == tool_input
    assert call["result_bytes"] == 20
    assert call["result"] == (
        result if cap < 0 else result.encode()[:cap].decode(errors="ignore")
    )
    assert call["result_truncated"] is (cap >= 0)


@pytest.mark.parametrize(
    "flag", [["--raw"], ["--no-raw"], ["--raw-tool-result-bytes", "3"]]
)
def test_cli_rejects_removed_flags(flag):
    with pytest.raises(SystemExit) as exc:
        main(flag)
    assert exc.value.code == 2


@pytest.mark.parametrize("batch", [False, True])
def test_cli_no_events_writes_no_json(batch, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    transcript = tmp_path / "empty.jsonl"
    _write_jsonl(transcript, _session_entries("empty", tmp_path, "")[:1])
    args = (
        ["--sessions-root", str(tmp_path), "--output", str(tmp_path / "out")]
        if batch
        else ["--transcript", str(transcript), "--output", "-"]
    )
    assert main(args) == (1 if batch else 0)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "no exportable events" in captured.err
    assert not list(tmp_path.rglob("*.json"))


@pytest.mark.parametrize("stdout", [False, True])
def test_cli_warnings_stay_on_stderr(stdout, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(session_log_envelope, "SIZE_WARN_BYTES", 1)
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(transcript, _session_entries("s1", tmp_path, "hello"))
    with transcript.open("a") as stream:
        stream.write("bad json\n")
    destination = tmp_path / "out.json"
    assert (
        main(
            [
                "--transcript",
                str(transcript),
                "--output",
                "-" if stdout else str(destination),
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    assert (
        json.loads(captured.out if stdout else destination.read_text())["session_id"]
        == "s1"
    )
    assert "skipping malformed JSON" in captured.err
    assert "consider a lower --tool-result-bytes" in captured.err
    assert "--raw-tool-result-bytes" not in captured.err


def test_batch_duplicate_identifiers_preserve_both_json_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name, text in [("a", "first"), ("b", "second")]:
        _write_jsonl(
            tmp_path / f"{name}.jsonl", _session_entries("same", tmp_path, text)
        )
    out = tmp_path / "out"
    assert main(["--sessions-root", str(tmp_path), "--output", str(out)]) == 0
    envelopes = [json.loads(path.read_text()) for path in out.glob("*.json")]
    assert {e["events"][0]["text"] for e in envelopes} == {"first", "second"}
    assert {e["session_id"] for e in envelopes} == {"same"}
    assert {p.name for p in out.iterdir()} == {
        "codex_session_log_raw_same.json",
        "codex_session_log_raw_same-2.json",
    }


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("explicit_root", [False, True])
def test_main_output_root_is_resolved_at_call_time(
    batch, explicit_root, tmp_path, monkeypatch
):
    for name in ["first", "second"]:
        cwd = tmp_path / name
        cwd.mkdir()
        monkeypatch.chdir(cwd)
        transcript = cwd / "session.jsonl"
        _write_jsonl(transcript, _session_entries("s1", cwd, "hello"))
        args = (
            ["--all", "--sessions-root", str(cwd)]
            if batch
            else ["--transcript", str(transcript)]
        )
        output_root = cwd / "exports" if explicit_root else cwd
        assert main(args, project_root=output_root if explicit_root else None) == 0
        output = output_root / "codex_session_log_raw_s1.json"
        assert json.loads(output.read_text())["events"][0]["text"] == "hello"
        if explicit_root:
            assert not (cwd / output.name).exists()


@pytest.mark.parametrize("batch", [False, True])
def test_codex_cli_uses_public_stages(batch, tmp_path, monkeypatch, capsys):
    stages = []

    class RecordingExtractor:
        def read_source(self, request):
            stages.append("read")
            return mod.read_source(request)

        def interpret_source(self, source):
            stages.append("interpret")
            return mod.interpret_source(source)

    monkeypatch.setattr(mod, "EXTRACTOR", RecordingExtractor(), raising=False)
    entries = load_entries(FIXTURES / "question.jsonl")
    entries[0]["payload"]["cwd"] = str(tmp_path)
    _write_jsonl(tmp_path / "s.jsonl", entries)
    monkeypatch.chdir(tmp_path)
    args = (
        ["--sessions-root", str(tmp_path)]
        if batch
        else ["--transcript", str(tmp_path / "s.jsonl"), "--output", "-"]
    )
    assert main(args) == 0
    assert stages == ["read", "interpret"]


def test_codex_preserves_native_results_and_identity():
    # Retain the native tool shapes, changing only IDs/results to exercise
    # repeated names, unmatched results, and missing/null/empty boundaries.
    entries = load_entries(FIXTURES / "tool_patch_large.jsonl")
    calls = [e for e in entries if e["payload"].get("type") == "custom_tool_call"]
    outputs = [
        e for e in entries if e["payload"].get("type") == "custom_tool_call_output"
    ]
    calls[0]["payload"]["id"] = "native-call"
    outputs[0]["payload"]["output"] = {"nested": [None, "", {"unknown": True}]}
    outputs[0]["payload"]["id"] = "native-result"
    outputs[1]["payload"]["output"] = None
    missing = json.loads(json.dumps(calls[0]))
    missing["payload"]["call_id"] = "no-result"
    empty = json.loads(json.dumps(outputs[1]))
    empty["payload"]["call_id"] = "unmatched"
    empty["payload"]["output"] = ""
    entries.extend([missing, empty])
    source = mod.CodexSource(Path("unused.jsonl"), entries)
    session = mod.interpret_source(source)
    assert session is not None
    interpreted = [c for e in session["events"] for c in e.get("tool_calls", [])]
    assert [c["name"] for c in interpreted] == ["exec"] * 3
    assert interpreted[0]["id"] == calls[0]["payload"]["call_id"]
    assert interpreted[0]["result_record"]["content"] == outputs[0]["payload"]["output"]
    assert interpreted[0]["result_record"]["id"] == "native-result"
    assert interpreted[1]["result"] is None
    assert "result" not in interpreted[2]
    assert session["unmatched_tool_results"][0]["content"] == ""
    assert session["unsupported_tools"] == ["exec"]
    assert all("result_truncated" not in c for c in interpreted)


def test_native_results_match_ids_across_user_messages_and_export_without_mutation():
    entries = load_entries(FIXTURES / "tool_patch_large.jsonl")
    calls = [e for e in entries if e["payload"].get("type") == "custom_tool_call"]
    outputs = [
        e for e in entries if e["payload"].get("type") == "custom_tool_call_output"
    ]
    user = next(e for e in entries if e["payload"].get("role") == "user")
    # Retain the native envelope and call IDs, delaying the results past a
    # second human message and returning them in reverse call order.
    outputs[0]["payload"]["output"] = "é" * 12_000
    outputs[0]["payload"]["is_error"] = True
    entries = [user, *calls, user, *reversed(outputs)]
    session = _interpret(entries)
    interpreted = [c for e in session["events"] for c in e.get("tool_calls", [])]
    assert interpreted[0]["result"] == "é" * 12_000
    assert interpreted[0]["result_record"]["content"] == "é" * 12_000
    assert interpreted[0]["is_error"] is True
    assert interpreted[1]["result_record"]["content"] == outputs[1]["payload"]["output"]
    original = json.dumps(session)
    capped = session_log_envelope.export_session(session, 7)
    exported = capped["events"][1]["tool_calls"][0]
    assert exported["result"] == "é" * 3
    assert exported["result_bytes"] == 24_000
    assert exported["result_truncated"] is True
    assert (
        session_log_envelope.export_session(session, -1)["events"][1]["tool_calls"][0][
            "result"
        ]
        == "é" * 12_000
    )
    assert json.dumps(session) == original


@pytest.mark.parametrize("output", ["", None, "missing"])
def test_native_result_missing_null_and_empty_stay_distinct(output):
    source = mod.read_source(FIXTURES / "question.jsonl")
    result = next(
        e["payload"]
        for e in source.entries
        if e["payload"].get("type") == "function_call_output"
    )
    if output == "missing":
        del result["output"]
    else:
        result["output"] = output
    session = mod.interpret_source(source)
    call = next(c for e in session["events"] for c in e.get("tool_calls", []))
    exported = next(
        c
        for e in session_log_envelope.export_session(session)["events"]
        for c in e.get("tool_calls", [])
    )
    if output == "missing":
        assert "content" not in call["result_record"]
        assert "result" not in call
        assert "result" not in exported
    else:
        assert call["result_record"]["content"] == output
        assert call["result"] == output
        assert exported["result"] == output


def test_native_message_ids_and_image_ownership_survive():
    source = mod.read_source(FIXTURES / "image.jsonl")
    user = next(e for e in source.entries if e["payload"].get("role") == "user")
    user["payload"].update(
        id="image-owner", message_id="message-1", parent_id="parent-1"
    )
    # Add a second, text-only user input after the captured conversation.
    source.entries.append(
        {
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "Continue"},
        }
    )
    session = mod.interpret_source(source)
    users = [e for e in session["events"] if e["role"] == "user"]
    assert users[0]["id"] == "image-owner"
    assert users[0]["message_id"] == "message-1"
    assert users[0]["parent_id"] == "parent-1"
    assert len(users[0]["images"]) == 1
    assert "images" not in users[-1]


def test_cli_reports_unsupported_schema_once_and_retains_input(tmp_path, capsys):
    source = mod.read_source(FIXTURES / "tool_patch_large.jsonl")
    _write_jsonl(tmp_path / "s.jsonl", source.entries)
    assert main(["--transcript", str(tmp_path / "s.jsonl"), "--output", "-"]) == 0
    captured = capsys.readouterr()
    assert captured.err.count("warning: unsupported tool input schemas: exec") == 1
    calls = [
        c for e in json.loads(captured.out)["events"] for c in e.get("tool_calls", [])
    ]
    assert len(calls) == 2
    assert "printf TOOL_FIXTURE_MARKER" in calls[0]["input"]
