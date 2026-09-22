#!/usr/bin/env python3
"""Minimal asserts for extract_copilot_session_log.

Run: python3 scripts/test_extract_copilot_session_log.py
"""

from __future__ import annotations

import base64
import json
import re
import sqlite3
import tempfile
from pathlib import Path

import extract_copilot_session_log as copilot_log
from extract_copilot_session_log import (
    Turn,
    build_events,
    _permissions_from_events,
    _skills_from_events,
    attribute_permissions,
    attribute_skills,
    clean_user_text,
    format_decision,
    get_db_connection,
    get_session_by_id,
    get_session_turns,
    matching_sessions,
    render_session,
)

_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMB/ax6fN8A"
    "AAAASUVORK5CYII="
)


def test_copilot_noise_cleaning():
    assert clean_user_text("<system-notification>test</system-notification>") == ""
    assert clean_user_text("<bash-stdout>output</bash-stdout>") == ""
    assert (
        clean_user_text(
            "Real text\n<system-reminder>ignore</system-reminder>\nMore text"
        )
        == "Real text\n\nMore text"
    )


def test_copilot_skill_invocation_is_recorded_and_attributed():
    lines = [
        json.dumps(
            {
                "type": "skill.invoked",
                "timestamp": "2026-07-01T22:05:00Z",
                "data": {
                    "name": "extract-copilot-session-logs",
                    "path": "/repo/.agents/skills/extract-copilot-session-logs/SKILL.md",
                },
            }
        ),
        json.dumps({"type": "assistant.message", "data": {}}),
    ]
    skills = _skills_from_events(lines)
    assert skills == [
        {
            "name": "extract-copilot-session-logs",
            "path": "/repo/.agents/skills/extract-copilot-session-logs/SKILL.md",
            "timestamp": "2026-07-01T22:05:00Z",
        }
    ]
    turn = Turn("export logs", "", "2026-07-01T22:00:00Z", 0)
    attribute_skills([turn], skills)
    assert turn.skills_used == ["extract-copilot-session-logs"]


def test_copilot_skill_definition_details_are_loaded():
    with tempfile.TemporaryDirectory() as tmp_name:
        skill_path = Path(tmp_name) / "demo" / "SKILL.md"
        skill_path.parent.mkdir()
        skill_path.write_text(
            "---\nname: demo\ndescription: Explains the demo workflow.\n---\n\n"
            "# Demo Skill\n",
            encoding="utf-8",
        )
        turn = Turn("use demo", "", "2026-07-01T22:00:00Z", 0)
        attribute_skills(
            [turn],
            [
                {
                    "name": "demo",
                    "path": str(skill_path),
                    "timestamp": "2026-07-01T22:05:00Z",
                }
            ],
        )
        assert turn.skill_details["demo"]["description"] == (
            "Explains the demo workflow."
        )
        assert turn.skill_details["demo"]["path"] == str(skill_path.resolve())


def test_copilot_permissions_join_approval_and_denial():
    lines = [
        json.dumps(
            {
                "type": "permission.requested",
                "timestamp": "2026-07-01T22:03:50Z",
                "data": {
                    "requestId": "r1",
                    "permissionRequest": {"kind": "shell", "fullCommandText": "pytest"},
                },
            }
        ),
        json.dumps(
            {
                "type": "permission.completed",
                "timestamp": "2026-07-01T22:04:04Z",
                "data": {"requestId": "r1", "result": {"kind": "approved"}},
            }
        ),
        json.dumps(
            {
                "type": "permission.requested",
                "timestamp": "2026-07-01T22:05:00Z",
                "data": {
                    "requestId": "r2",
                    "permissionRequest": {"kind": "write", "intention": "edit crud.py"},
                },
            }
        ),
        json.dumps(
            {
                "type": "permission.completed",
                "timestamp": "2026-07-01T22:05:30Z",
                "data": {
                    "requestId": "r2",
                    "result": {
                        "kind": "denied-interactively-by-user",
                        "feedback": "not like that",
                    },
                },
            }
        ),
    ]
    perms = _permissions_from_events(lines)
    assert [(p["tool"], p["decision"], p["feedback"]) for p in perms] == [
        ("pytest", "approved", ""),
        ("edit crud.py", "denied-interactively-by-user", "not like that"),
    ]


def test_copilot_permission_pending_when_no_completion():
    lines = [
        json.dumps(
            {
                "type": "permission.requested",
                "timestamp": "2026-07-01T22:05:00Z",
                "data": {
                    "requestId": "r3",
                    "permissionRequest": {"kind": "shell", "fullCommandText": "rm x"},
                },
            }
        )
    ]
    perms = _permissions_from_events(lines)
    assert perms == [
        {
            "tool": "rm x",
            "decision": "pending",
            "feedback": "",
            "timestamp": "2026-07-01T22:05:00Z",
        }
    ]


def test_copilot_format_decision():
    assert format_decision("approved") == "approved"
    assert format_decision("approved-for-location") == "approved"
    assert format_decision("denied-interactively-by-user") == "denied"
    assert format_decision("") == "unknown"


def test_copilot_attribute_permissions_by_timestamp():
    t0 = Turn("first", "", "2026-07-01T22:00:00Z", 0)
    t1 = Turn("second", "", "2026-07-01T22:10:00Z", 1)
    perms = [
        {
            "tool": "a",
            "decision": "approved",
            "feedback": "",
            "timestamp": "2026-07-01T22:05:00Z",
        },
        {
            "tool": "b",
            "decision": "denied",
            "feedback": "no",
            "timestamp": "2026-07-01T22:15:00Z",
        },
    ]
    attribute_permissions([t0, t1], perms)
    assert [p["tool"] for p in t0.permission_decisions] == ["a"]
    assert [p["tool"] for p in t1.permission_decisions] == ["b"]


def test_copilot_db_operations():
    """Test database operations with a temporary test database."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        test_db = Path(tf.name)

    try:
        # Create test database
        conn = sqlite3.connect(test_db)
        cursor = conn.cursor()

        # Create schema
        cursor.execute("""
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                cwd TEXT,
                repository TEXT,
                branch TEXT,
                summary TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            )
        """)

        cursor.execute("""
            CREATE TABLE turns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL REFERENCES sessions(id),
                turn_index INTEGER NOT NULL,
                user_message TEXT,
                assistant_response TEXT,
                timestamp TEXT DEFAULT (datetime('now')),
                UNIQUE(session_id, turn_index)
            )
        """)

        cursor.execute("""
            CREATE TABLE session_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL REFERENCES sessions(id),
                file_path TEXT NOT NULL,
                tool_name TEXT,
                turn_index INTEGER,
                first_seen_at TEXT DEFAULT (datetime('now')),
                UNIQUE(session_id, file_path)
            )
        """)

        cursor.execute("""
            CREATE TABLE session_refs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL REFERENCES sessions(id),
                ref_type TEXT NOT NULL,
                ref_value TEXT NOT NULL,
                turn_index INTEGER,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)

        cursor.execute("""
            CREATE TABLE checkpoints (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL REFERENCES sessions(id),
                checkpoint_number INTEGER NOT NULL,
                title TEXT,
                overview TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                UNIQUE(session_id, checkpoint_number)
            )
        """)

        # Insert test data
        test_cwd = str(Path.cwd())
        cursor.execute(
            """
            INSERT INTO sessions (id, cwd, repository, branch, summary, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
            (
                "test-session-1",
                test_cwd,
                "test/repo",
                "main",
                "Test session",
                "2026-01-01T12:00:00Z",
                "2026-01-01T12:30:00Z",
            ),
        )

        cursor.execute(
            """
            INSERT INTO turns (session_id, turn_index, user_message, assistant_response, timestamp)
            VALUES (?, ?, ?, ?, ?)
        """,
            (
                "test-session-1",
                0,
                "Hello, can you help?",
                "Of course! What do you need?",
                "2026-01-01T12:00:05Z",
            ),
        )

        cursor.execute(
            """
            INSERT INTO turns (session_id, turn_index, user_message, assistant_response, timestamp)
            VALUES (?, ?, ?, ?, ?)
        """,
            (
                "test-session-1",
                1,
                "Create a test file",
                "I'll create that for you.",
                "2026-01-01T12:10:00Z",
            ),
        )

        cursor.execute(
            """
            INSERT INTO session_files (session_id, file_path, tool_name, turn_index)
            VALUES (?, ?, ?, ?)
        """,
            ("test-session-1", "/tmp/test.py", "create", 1),
        )

        cursor.execute(
            """
            INSERT INTO session_refs (session_id, ref_type, ref_value, turn_index)
            VALUES (?, ?, ?, ?)
        """,
            ("test-session-1", "commit", "abc123", 1),
        )

        conn.commit()
        conn.close()

        # Test database operations
        conn = get_db_connection(test_db)

        # Test session retrieval
        session = get_session_by_id(conn, "test-session-1")
        assert session is not None
        assert session["id"] == "test-session-1"
        assert session["repository"] == "test/repo"
        assert session["summary"] == "Test session"

        # Test turns retrieval
        turns = get_session_turns(conn, "test-session-1")
        assert len(turns) == 2
        assert turns[0]["user_message"] == "Hello, can you help?"
        assert turns[1]["assistant_response"] == "I'll create that for you."

        # Test matching sessions
        sessions = matching_sessions(conn, Path.cwd(), strict=False)
        assert len(sessions) >= 1
        found = any(s["id"] == "test-session-1" for s in sessions)
        assert found

        # Test render
        markdown = render_session(conn, session)
        assert "test-session-1" in markdown
        assert "test/repo" in markdown
        assert "Hello, can you help?" in markdown
        assert "## Summary - user inputs" in markdown
        assert "# Full turn-by-turn detail" in markdown
        assert "Turn 1 -" in markdown
        assert "Turn 2 -" in markdown
        assert "/tmp/test.py" in markdown
        assert "- create -> /tmp/test.py" in markdown
        assert "abc123" in markdown

        conn.close()

    finally:
        # Cleanup
        test_db.unlink()


def test_copilot_prefix_matching():
    """Test that session ID prefix matching works."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        test_db = Path(tf.name)

    try:
        conn = sqlite3.connect(test_db)
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                cwd TEXT,
                repository TEXT,
                branch TEXT,
                summary TEXT,
                created_at TEXT,
                updated_at TEXT
            )
        """)

        cursor.execute(
            """
            INSERT INTO sessions (id, cwd, repository, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
        """,
            (
                "6c7682e8-3849-4784-b7f2-92f3617212ff",
                str(Path.cwd()),
                "test/repo",
                "2026-01-01T12:00:00Z",
                "2026-01-01T12:00:00Z",
            ),
        )

        conn.commit()
        conn.close()

        conn = get_db_connection(test_db)

        # Test full ID
        session = get_session_by_id(conn, "6c7682e8-3849-4784-b7f2-92f3617212ff")
        assert session is not None

        # Test prefix
        session = get_session_by_id(conn, "6c7682e8")
        assert session is not None
        assert session["id"] == "6c7682e8-3849-4784-b7f2-92f3617212ff"

        conn.close()

    finally:
        test_db.unlink()


def test_copilot_image_reference_without_attachment_is_flagged():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        test_db = Path(tf.name)

    try:
        conn = sqlite3.connect(test_db)
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                cwd TEXT,
                repository TEXT,
                branch TEXT,
                summary TEXT,
                created_at TEXT,
                updated_at TEXT
            )
        """)
        cursor.execute("""
            CREATE TABLE turns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL REFERENCES sessions(id),
                turn_index INTEGER NOT NULL,
                user_message TEXT,
                assistant_response TEXT,
                timestamp TEXT
            )
        """)
        cursor.execute(
            "CREATE TABLE session_files (session_id TEXT, file_path TEXT, tool_name TEXT, turn_index INTEGER, first_seen_at TEXT)"
        )
        cursor.execute(
            "CREATE TABLE session_refs (session_id TEXT, ref_type TEXT, ref_value TEXT, turn_index INTEGER, created_at TEXT)"
        )
        cursor.execute(
            "CREATE TABLE checkpoints (session_id TEXT, checkpoint_number INTEGER, title TEXT, overview TEXT, created_at TEXT)"
        )

        cursor.execute(
            "INSERT INTO sessions (id, cwd, repository, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (
                "img-session",
                str(Path.cwd()),
                "test/repo",
                "2026-01-01T12:00:00Z",
                "2026-01-01T12:00:00Z",
            ),
        )
        cursor.execute(
            "INSERT INTO turns (session_id, turn_index, user_message, assistant_response, timestamp) VALUES (?, ?, ?, ?, ?)",
            (
                "img-session",
                0,
                "[image: copilot-image-test.png] please summarize",
                "Done",
                "2026-01-01T12:00:05Z",
            ),
        )
        conn.commit()
        conn.close()

        conn = get_db_connection(test_db)
        session = get_session_by_id(conn, "img-session")
        assert session is not None
        markdown = render_session(conn, session)
        assert "source file unavailable; description pending" in markdown
        conn.close()
    finally:
        test_db.unlink()


def test_copilot_image_attachment_is_dumped_to_marker_path():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        test_db = Path(tf.name)

    with tempfile.TemporaryDirectory() as tmpdir:
        img_path = Path(tmpdir) / "copilot-image-test.png"
        img_path.write_bytes(base64.b64decode(_PNG_B64))

        try:
            conn = sqlite3.connect(test_db)
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE sessions (
                    id TEXT PRIMARY KEY,
                    cwd TEXT,
                    repository TEXT,
                    branch TEXT,
                    summary TEXT,
                    created_at TEXT,
                    updated_at TEXT
                )
            """)
            cursor.execute("""
                CREATE TABLE turns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL REFERENCES sessions(id),
                    turn_index INTEGER NOT NULL,
                    user_message TEXT,
                    assistant_response TEXT,
                    timestamp TEXT
                )
            """)
            cursor.execute(
                "CREATE TABLE session_files (session_id TEXT, file_path TEXT, tool_name TEXT, turn_index INTEGER, first_seen_at TEXT)"
            )
            cursor.execute(
                "CREATE TABLE session_refs (session_id TEXT, ref_type TEXT, ref_value TEXT, turn_index INTEGER, created_at TEXT)"
            )
            cursor.execute(
                "CREATE TABLE checkpoints (session_id TEXT, checkpoint_number INTEGER, title TEXT, overview TEXT, created_at TEXT)"
            )
            cursor.execute(
                "CREATE TABLE attachments (session_id TEXT, display_name TEXT, path TEXT, type TEXT)"
            )

            cursor.execute(
                "INSERT INTO sessions (id, cwd, repository, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (
                    "img-session-2",
                    str(Path.cwd()),
                    "test/repo",
                    "2026-01-01T12:00:00Z",
                    "2026-01-01T12:00:00Z",
                ),
            )
            cursor.execute(
                "INSERT INTO turns (session_id, turn_index, user_message, assistant_response, timestamp) VALUES (?, ?, ?, ?, ?)",
                (
                    "img-session-2",
                    0,
                    "[image: copilot-image-test.png] please summarize",
                    "Done",
                    "2026-01-01T12:00:05Z",
                ),
            )
            cursor.execute(
                "INSERT INTO attachments (session_id, display_name, path, type) VALUES (?, ?, ?, ?)",
                ("img-session-2", "copilot-image-test.png", str(img_path), "image/png"),
            )
            conn.commit()
            conn.close()

            conn = get_db_connection(test_db)
            session = get_session_by_id(conn, "img-session-2")
            assert session is not None
            markdown = render_session(conn, session)
            conn.close()

            marker = re.search(
                r"\[Image dumped to `([^`]+)` — description pending\]", markdown
            )
            assert marker is not None
            dumped = Path(marker.group(1))
            assert dumped.exists()
            dumped.unlink()
        finally:
            test_db.unlink()


def test_copilot_image_attachment_from_state_events_is_dumped_to_marker_path():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        test_db = Path(tf.name)

    with tempfile.TemporaryDirectory() as tmpdir:
        img_path = Path(tmpdir) / "copilot-image-test.png"
        img_path.write_bytes(base64.b64decode(_PNG_B64))

        state_root = Path(tmpdir) / "state-root"
        session_dir = state_root / "img-session-3"
        session_dir.mkdir(parents=True, exist_ok=True)
        events_path = session_dir / "events.jsonl"
        events_path.write_text(
            json.dumps(
                {
                    "type": "user.message",
                    "data": {
                        "content": "[image: copilot-image-test.png] please summarize",
                        "attachments": [
                            {
                                "type": "file",
                                "path": str(img_path),
                                "displayName": "copilot-image-test.png",
                                "mimeType": "image/png",
                            }
                        ],
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )

        original_root = copilot_log.COPILOT_STATE_ROOT
        copilot_log.COPILOT_STATE_ROOT = state_root
        try:
            conn = sqlite3.connect(test_db)
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE sessions (
                    id TEXT PRIMARY KEY,
                    cwd TEXT,
                    repository TEXT,
                    branch TEXT,
                    summary TEXT,
                    created_at TEXT,
                    updated_at TEXT
                )
            """)
            cursor.execute("""
                CREATE TABLE turns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL REFERENCES sessions(id),
                    turn_index INTEGER NOT NULL,
                    user_message TEXT,
                    assistant_response TEXT,
                    timestamp TEXT
                )
            """)
            cursor.execute(
                "CREATE TABLE session_files (session_id TEXT, file_path TEXT, tool_name TEXT, turn_index INTEGER, first_seen_at TEXT)"
            )
            cursor.execute(
                "CREATE TABLE session_refs (session_id TEXT, ref_type TEXT, ref_value TEXT, turn_index INTEGER, created_at TEXT)"
            )
            cursor.execute(
                "CREATE TABLE checkpoints (session_id TEXT, checkpoint_number INTEGER, title TEXT, overview TEXT, created_at TEXT)"
            )

            cursor.execute(
                "INSERT INTO sessions (id, cwd, repository, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (
                    "img-session-3",
                    str(Path.cwd()),
                    "test/repo",
                    "2026-01-01T12:00:00Z",
                    "2026-01-01T12:00:00Z",
                ),
            )
            cursor.execute(
                "INSERT INTO turns (session_id, turn_index, user_message, assistant_response, timestamp) VALUES (?, ?, ?, ?, ?)",
                (
                    "img-session-3",
                    0,
                    "[image: copilot-image-test.png] please summarize",
                    "Done",
                    "2026-01-01T12:00:05Z",
                ),
            )
            conn.commit()
            conn.close()

            conn = get_db_connection(test_db)
            session = get_session_by_id(conn, "img-session-3")
            assert session is not None
            markdown = render_session(conn, session)
            conn.close()

            marker = re.search(
                r"\[Image dumped to `([^`]+)` — description pending\]", markdown
            )
            assert marker is not None
            dumped = Path(marker.group(1))
            assert dumped.exists()
            dumped.unlink()
        finally:
            copilot_log.COPILOT_STATE_ROOT = original_root
            test_db.unlink()


def _copilot_turn_with_image():
    img = Path(tempfile.mkdtemp()) / "a.png"
    img.write_bytes(base64.b64decode(_PNG_B64))
    turn = Turn(
        "look at [image: a.png] and fix it",
        "Sure, fixing.",
        "2026-08-01T10:00:00Z",
        0,
    )
    turn.image_refs = [
        {"name": "a.png", "path": str(img), "type": "image/png"},
        {"name": "ghost.png"},
    ]
    turn.add_tool("str_replace_editor", "backend/app/models.py")
    return turn


def test_build_events_inlines_an_attachment_resolved_from_disk():
    user = build_events([_copilot_turn_with_image()])[0]
    assert base64.b64decode(user["images"][0]["data"]) == base64.b64decode(_PNG_B64)


def test_build_events_records_an_inline_token_with_no_attachment():
    # A [image: name] token with no matching attachment record is common in
    # Copilot logs; it has to stay visible.
    user = build_events([_copilot_turn_with_image()])[0]
    assert user["images"][1] == {"unavailable": True, "ref": "ghost.png"}


def test_build_events_recovers_tool_name_and_target():
    events = build_events([_copilot_turn_with_image()])
    call = next(c for e in events for c in e.get("tool_calls", []))
    assert call["name"] == "str_replace_editor"
    assert call["input"] == {"target": "backend/app/models.py"}


def test_build_events_must_run_before_dump_images_mutates_user_text():
    # dump_images appends "[Image ... description pending]" markers to
    # turn.user_text in place. The envelope carries the candidate's text, not
    # the markdown's annotation of it — so ordering is load-bearing.
    turn = _copilot_turn_with_image()
    events = build_events([turn])
    copilot_log.dump_images([turn], "sess", dump_dir=Path(tempfile.mkdtemp()))
    assert "description pending" in turn.user_text
    assert "description pending" not in events[0]["text"]


def test_build_events_emits_an_assistant_event_for_a_reply():
    events = build_events([_copilot_turn_with_image()])
    assistant = [e for e in events if e["role"] == "assistant"]
    assert assistant and assistant[0]["text"] == "Sure, fixing."


def test_copilot_tools_from_events_join_start_and_complete():
    lines = [
        json.dumps(
            {
                "type": "tool.execution_start",
                "timestamp": "2026-07-01T22:00:01Z",
                "data": {
                    "toolCallId": "call_1",
                    "toolName": "grep",
                    "arguments": {"pattern": "audit"},
                },
            }
        ),
        json.dumps(
            {
                "type": "tool.execution_complete",
                "timestamp": "2026-07-01T22:00:02Z",
                "data": {
                    "toolCallId": "call_1",
                    "success": True,
                    "result": {"content": "match a\nmatch b", "detailedContent": "x"},
                },
            }
        ),
    ]
    tools = copilot_log._tools_from_events(lines)
    assert len(tools) == 1
    assert tools[0]["name"] == "grep"
    assert tools[0]["arguments"] == {"pattern": "audit"}
    assert tools[0]["success"] is True
    # detailedContent is preferred as the fuller result text.
    assert tools[0]["result"] == "x"


def test_copilot_attribute_tools_populates_bullet_note_and_envelope():
    turn = Turn("find audits", "", "2026-07-01T22:00:00Z", 0)
    tools = [
        {
            "name": "grep",
            "arguments": {"pattern": "audit"},
            "success": True,
            "result": "backend/app/models.py",
            "timestamp": "2026-07-01T22:00:01Z",
        }
    ]
    copilot_log.attribute_tools([turn], tools)
    assert turn.tool_bullets == ["- grep -> audit"]
    assert turn.result_notes == ["grep: backend/app/models.py"]
    # Structured call is carried into the raw envelope with its result.
    call = next(c for e in build_events([turn]) for c in e.get("tool_calls", []))
    assert call["name"] == "grep"
    assert call["input"] == {"pattern": "audit"}
    assert call["result"] == "backend/app/models.py"


def test_copilot_attribute_tools_flags_failure():
    turn = Turn("run it", "", "2026-07-01T22:00:00Z", 0)
    copilot_log.attribute_tools(
        [turn],
        [
            {
                "name": "bash",
                "arguments": {"command": "false"},
                "success": False,
                "result": "boom",
                "timestamp": "2026-07-01T22:00:01Z",
            }
        ],
    )
    assert turn.result_notes == ["bash: error: boom"]


def test_copilot_attribute_tools_by_timestamp():
    t0 = Turn("first", "", "2026-07-01T22:00:00Z", 0)
    t1 = Turn("second", "", "2026-07-01T22:10:00Z", 1)
    tools = [
        {
            "name": "view",
            "arguments": {"path": "a.py"},
            "success": True,
            "result": "ok",
            "timestamp": "2026-07-01T22:12:00Z",
        }
    ]
    copilot_log.attribute_tools([t0, t1], tools)
    assert t0.tool_bullets == []
    assert t1.tool_bullets == ["- view -> a.py"]


def test_copilot_build_events_emits_one_timestamped_event_per_tool_call():
    turn = Turn("do two things", "On it.", "2026-07-01T22:00:00Z", 0)
    copilot_log.attribute_tools(
        [turn],
        [
            {
                "name": "grep",
                "arguments": {"pattern": "a"},
                "success": True,
                "result": "r1",
                "timestamp": "2026-07-01T22:00:01Z",
            },
            {
                "name": "view",
                "arguments": {"path": "b.py"},
                "success": True,
                "result": "r2",
                "timestamp": "2026-07-01T22:00:02Z",
            },
        ],
    )
    events = build_events([turn])
    # user + assistant prose + one event per tool call.
    assert [e["role"] for e in events] == ["user", "assistant", "assistant", "assistant"]
    tool_events = [e for e in events if e.get("tool_calls")]
    assert len(tool_events) == 2
    # Each tool event carries exactly one call stamped with that call's own time.
    assert tool_events[0]["ts"] == "2026-07-01T22:00:01Z"
    assert tool_events[0]["tool_calls"][0]["name"] == "grep"
    assert tool_events[1]["ts"] == "2026-07-01T22:00:02Z"
    assert tool_events[1]["tool_calls"][0]["name"] == "view"
    # Indices stay contiguous across the split-out events.
    assert [e["i"] for e in events] == [0, 1, 2, 3]


def test_copilot_main_defaults_to_all_without_a_selector(monkeypatch, tmp_path):
    called = {}

    def fake_extract_all(conn, output, strict, raw, tool_result_max_bytes):
        called["output"] = output
        called["strict"] = strict
        return 0

    monkeypatch.setattr(copilot_log, "_extract_all", fake_extract_all)
    monkeypatch.setattr(
        copilot_log, "get_db_connection", lambda p: sqlite3.connect(":memory:")
    )
    rc = copilot_log.main(["--db", str(tmp_path / "x.db")])
    assert rc == 0
    assert called == {"output": None, "strict": False}


if __name__ == "__main__":
    test_copilot_skill_invocation_is_recorded_and_attributed()
    print("✓ test_copilot_skill_invocation_is_recorded_and_attributed")

    test_copilot_skill_definition_details_are_loaded()
    print("✓ test_copilot_skill_definition_details_are_loaded")

    test_copilot_noise_cleaning()
    print("✓ test_copilot_noise_cleaning")

    test_copilot_db_operations()
    print("✓ test_copilot_db_operations")

    test_copilot_prefix_matching()
    print("✓ test_copilot_prefix_matching")

    test_copilot_image_reference_without_attachment_is_flagged()
    print("✓ test_copilot_image_reference_without_attachment_is_flagged")

    test_copilot_image_attachment_is_dumped_to_marker_path()
    print("✓ test_copilot_image_attachment_is_dumped_to_marker_path")

    test_copilot_image_attachment_from_state_events_is_dumped_to_marker_path()
    print("✓ test_copilot_image_attachment_from_state_events_is_dumped_to_marker_path")

    test_copilot_permissions_join_approval_and_denial()
    print("✓ test_copilot_permissions_join_approval_and_denial")

    test_copilot_permission_pending_when_no_completion()
    print("✓ test_copilot_permission_pending_when_no_completion")

    test_copilot_format_decision()
    print("✓ test_copilot_format_decision")

    test_copilot_attribute_permissions_by_timestamp()
    print("✓ test_copilot_attribute_permissions_by_timestamp")

    print("\nAll tests passed!")
