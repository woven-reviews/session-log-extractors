"""Tests for extract_copilot_session_log.

Coverage: pytest --cov=scripts --cov-report=term-missing \
    scripts/test_extract_copilot_session_log.py
"""

from __future__ import annotations

import base64
import json
import re
import sqlite3
import tempfile
from functools import partial
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


def _make_test_db(
    session_id="test-session",
    cwd=None,
    turns=(),
    files=(),
    events=(),
    created_at="2026-01-01T12:00:00Z",
    updated_at="2026-01-01T12:00:00Z",
    repository="test/repo",
):
    """Build a throwaway Copilot SQLite DB with the given rows.

    Returns the DB's path; the caller is responsible for unlinking it.
    """
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        db_path = Path(tf.name)
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, cwd TEXT, repository TEXT, "
        "branch TEXT, summary TEXT, created_at TEXT, updated_at TEXT)"
    )
    cur.execute(
        "CREATE TABLE turns (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, "
        "turn_index INTEGER, user_message TEXT, assistant_response TEXT, timestamp TEXT)"
    )
    cur.execute(
        "CREATE TABLE session_files (session_id TEXT, file_path TEXT, tool_name TEXT, "
        "turn_index INTEGER, first_seen_at TEXT)"
    )
    cur.execute(
        "CREATE TABLE session_refs (session_id TEXT, ref_type TEXT, ref_value TEXT, "
        "turn_index INTEGER, created_at TEXT)"
    )
    cur.execute(
        "CREATE TABLE checkpoints (session_id TEXT, checkpoint_number INTEGER, "
        "title TEXT, overview TEXT, created_at TEXT)"
    )
    cur.execute(
        "CREATE TABLE attachments (session_id TEXT, display_name TEXT, path TEXT, type TEXT)"
    )
    if events:
        cur.execute(
            "CREATE TABLE events (session_id TEXT, usage_model TEXT, timestamp TEXT)"
        )

    cur.execute(
        "INSERT INTO sessions (id, cwd, repository, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            session_id,
            cwd if cwd is not None else str(Path.cwd()),
            repository,
            created_at,
            updated_at,
        ),
    )
    for t in turns:
        cur.execute(
            "INSERT INTO turns (session_id, turn_index, user_message, "
            "assistant_response, timestamp) VALUES (?, ?, ?, ?, ?)",
            (
                session_id,
                t["turn_index"],
                t.get("user_message", ""),
                t.get("assistant_response", ""),
                t.get("timestamp"),
            ),
        )
    for f in files:
        cur.execute(
            "INSERT INTO session_files (session_id, file_path, tool_name, "
            "turn_index, first_seen_at) VALUES (?, ?, ?, ?, ?)",
            (
                session_id,
                f["file_path"],
                f.get("tool_name", ""),
                f.get("turn_index"),
                f.get("first_seen_at"),
            ),
        )
    for e in events:
        cur.execute(
            "INSERT INTO events (session_id, usage_model, timestamp) VALUES (?, ?, ?)",
            (session_id, e.get("usage_model"), e.get("timestamp")),
        )
    conn.commit()
    conn.close()
    return db_path


def _write_events_log(root: Path, session_id: str, lines) -> Path:
    """Write raw JSONL lines to <root>/<session_id>/events.jsonl."""
    session_dir = root / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    path = session_dir / "events.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


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


def test_copilot_image_attachment_is_dumped_to_marker_path(tmp_path, monkeypatch):
    monkeypatch.setattr(
        copilot_log,
        "dump_images",
        partial(copilot_log.dump_images, dump_dir=tmp_path / "images"),
    )
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
            assert dumped.parent == tmp_path / "images"
            assert dumped.exists()
        finally:
            test_db.unlink()


def test_copilot_image_attachment_from_state_events_is_dumped_to_marker_path(tmp_path, monkeypatch):
    monkeypatch.setattr(
        copilot_log,
        "dump_images",
        partial(copilot_log.dump_images, dump_dir=tmp_path / "images"),
    )
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
            assert dumped.parent == tmp_path / "images"
            assert dumped.exists()
        finally:
            copilot_log.COPILOT_STATE_ROOT = original_root
            test_db.unlink()


def _copilot_turn_with_image(tmp_path):
    img = tmp_path / "a.png"
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


def test_build_events_inlines_an_attachment_resolved_from_disk(tmp_path):
    user = build_events([_copilot_turn_with_image(tmp_path)])[0]
    assert base64.b64decode(user["images"][0]["data"]) == base64.b64decode(_PNG_B64)


def test_build_events_records_an_inline_token_with_no_attachment(tmp_path):
    # A [image: name] token with no matching attachment record is common in
    # Copilot logs; it has to stay visible.
    user = build_events([_copilot_turn_with_image(tmp_path)])[0]
    assert user["images"][1] == {"unavailable": True, "ref": "ghost.png"}


def test_build_events_recovers_tool_name_and_target(tmp_path):
    events = build_events([_copilot_turn_with_image(tmp_path)])
    call = next(c for e in events for c in e.get("tool_calls", []))
    assert call["name"] == "str_replace_editor"
    assert call["input"] == {"target": "backend/app/models.py"}


def test_build_events_must_run_before_dump_images_mutates_user_text(tmp_path):
    # dump_images appends "[Image ... description pending]" markers to
    # turn.user_text in place. The envelope carries the candidate's text, not
    # the markdown's annotation of it — so ordering is load-bearing.
    turn = _copilot_turn_with_image(tmp_path)
    events = build_events([turn])
    copilot_log.dump_images([turn], "sess", dump_dir=tmp_path / "dumped")
    assert "description pending" in turn.user_text
    assert "description pending" not in events[0]["text"]


def test_build_events_emits_an_assistant_event_for_a_reply(tmp_path):
    events = build_events([_copilot_turn_with_image(tmp_path)])
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


# -- tool/result/skill descriptor edge cases -------------------------------


def test_decode_arguments_parses_json_string_and_falls_back_on_invalid():
    assert copilot_log._decode_arguments('{"a": 1}') == {"a": 1}
    assert copilot_log._decode_arguments("not json") == "not json"
    assert copilot_log._decode_arguments({"a": 1}) == {"a": 1}


def test_tool_descriptor_non_dict_and_json_fallback():
    assert copilot_log.tool_descriptor("echo", "just a string") == "just a string"
    assert copilot_log.tool_descriptor("echo", None) == ""
    # No known key present -> falls back to the full JSON blob.
    assert (
        copilot_log.tool_descriptor("mystery", {"other": "value"})
        == '{"other": "value"}'
    )
    # A value json.dumps cannot serialize falls back to "".
    assert copilot_log.tool_descriptor("mystery", {"bad": {1, 2}}) == ""


def test_result_note_handles_none_dict_and_unserializable():
    assert copilot_log.result_note(None) == ""
    assert copilot_log.result_note({"a": 1}) == '{"a": 1}'
    assert copilot_log.result_note({"bad": {1, 2}}) == ""


def test_skill_refs_from_call_finds_named_skill_and_fills_in_its_path():
    # The "skill" call registers the name with no path; the SKILL.md regex
    # match for the same name later fills it in.
    refs = copilot_log.skill_refs_from_call(
        "builtin.skill",
        {"skill": "demo-skill", "note": "see /repo/skills/demo-skill/SKILL.md"},
    )
    assert refs == [{"name": "demo-skill", "path": "/repo/skills/demo-skill/SKILL.md"}]


def test_skill_refs_from_call_survives_unserializable_arguments():
    assert copilot_log.skill_refs_from_call("bash", {"bad": {1, 2}}) == []


def test_copilot_clean_user_text_empty_input_returns_empty():
    assert clean_user_text("") == ""
    assert clean_user_text(None) == ""


# -- timestamp helpers ------------------------------------------------------


def test_copilot_parse_ts_and_format_timestamp_edge_cases():
    assert copilot_log._parse_ts(None) is None
    assert copilot_log._parse_ts(123) is None
    assert copilot_log._parse_ts("not-a-timestamp") is None
    assert copilot_log.format_timestamp(None) == "(no timestamp)"
    assert copilot_log.format_timestamp(123) == "(no timestamp)"
    assert copilot_log.format_timestamp("garbage-ts") == "garbage-ts"


def test_copilot_format_elapsed_edge_cases():
    assert copilot_log.format_elapsed(None, "2026-01-01T00:00:00Z") == ""
    assert copilot_log.format_elapsed("2026-01-01T00:00:00Z", "garbage") == ""
    assert (
        copilot_log.format_elapsed("2026-01-01T00:00:00Z", "2026-01-01T01:02:03Z")
        == "+1:02:03"
    )


def test_copilot_date_only_with_no_timestamp():
    assert copilot_log.date_only(None) == "unknown"


def test_copilot_turn_add_tool_without_descriptor():
    turn = Turn("x", "", "2026-01-01T00:00:00Z", 0)
    turn.add_tool("ls", "")
    assert turn.tool_bullets == ["- ls"]


# -- DB/path helpers ---------------------------------------------------------


def test_copilot_get_db_connection_missing_file_raises():
    try:
        get_db_connection(Path("/nonexistent/copilot/session-store.db"))
        assert False, "expected FileNotFoundError"
    except FileNotFoundError:
        pass


def test_copilot_table_columns_without_row_factory():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        db_path = Path(tf.name)
    try:
        conn = sqlite3.connect(db_path)  # no row_factory set -> plain tuples
        conn.execute("CREATE TABLE t (id INTEGER, name TEXT)")
        cols = copilot_log._table_columns(conn, "t")
        assert cols == {"id", "name"}
        conn.close()
    finally:
        db_path.unlink()


def test_copilot_paths_overlap_both_directions_and_neither():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        child = root / "a" / "b"
        child.mkdir(parents=True)
        other = root / "c"
        other.mkdir()
        assert copilot_log._paths_overlap(root, child) is True
        assert copilot_log._paths_overlap(child, root) is True
        assert copilot_log._paths_overlap(child, other) is False


def test_copilot_cwd_matches_blank_and_strict():
    assert copilot_log._cwd_matches(None, Path.cwd()) is False
    assert copilot_log._cwd_matches("", Path.cwd()) is False
    assert copilot_log._cwd_matches(str(Path.cwd()), Path.cwd(), strict=True) is True
    with tempfile.TemporaryDirectory() as tmp:
        assert (
            copilot_log._cwd_matches(str(Path(tmp) / "sub"), Path(tmp), strict=True)
            is False
        )


def test_copilot_newest_session_falls_back_to_global_newest():
    db_path = _make_test_db(session_id="elsewhere", cwd="/somewhere/else")
    try:
        conn = get_db_connection(db_path)
        session = copilot_log.newest_session(conn, Path.cwd(), strict=False)
        assert session is not None
        assert session["id"] == "elsewhere"
        conn.close()
    finally:
        db_path.unlink()


def test_copilot_newest_session_strict_with_no_match_returns_none():
    db_path = _make_test_db(session_id="elsewhere", cwd="/somewhere/else")
    try:
        conn = get_db_connection(db_path)
        assert copilot_log.newest_session(conn, Path.cwd(), strict=True) is None
        conn.close()
    finally:
        db_path.unlink()


def test_copilot_newest_session_returns_none_for_empty_db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        db_path = Path(tf.name)
    try:
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE sessions (id TEXT, cwd TEXT, repository TEXT, branch TEXT, "
            "summary TEXT, created_at TEXT, updated_at TEXT)"
        )
        conn.commit()
        conn.close()
        conn = get_db_connection(db_path)
        assert copilot_log.newest_session(conn, Path.cwd(), strict=False) is None
        conn.close()
    finally:
        db_path.unlink()


def test_copilot_newest_session_picks_most_recently_updated():
    db_path = _make_test_db(session_id="s1", updated_at="2026-01-01T00:00:00Z")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO sessions (id, cwd, repository, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            "s2",
            str(Path.cwd()),
            "test/repo",
            "2026-01-02T00:00:00Z",
            "2026-01-02T00:00:00Z",
        ),
    )
    conn.commit()
    conn.close()
    try:
        conn = get_db_connection(db_path)
        session = copilot_log.newest_session(conn, Path.cwd(), strict=False)
        assert session["id"] == "s2"
        conn.close()
    finally:
        db_path.unlink()


# -- per-session events log edge cases ---------------------------------------


def test_copilot_get_state_attachments_skips_malformed_and_irrelevant_lines(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(copilot_log, "COPILOT_STATE_ROOT", tmp_path)
    lines = [
        "not json at all",
        json.dumps({"type": "assistant.message", "data": {}}),
        json.dumps({"type": "user.message", "data": "not-a-dict"}),
        json.dumps({"type": "user.message", "data": {"attachments": "not-a-list"}}),
        json.dumps(
            {
                "type": "user.message",
                "data": {
                    "attachments": [
                        "not-a-dict",
                        {
                            "displayName": "shot.png",
                            "path": "/tmp/shot.png",
                            "mimeType": "image/png",
                            "dataUrl": "data:image/png;base64,AAAA",
                            "data": "AAAA",
                            "mediaType": "image/png",
                        },
                    ]
                },
            }
        ),
    ]
    _write_events_log(tmp_path, "sess-attach", lines)
    attachments = copilot_log.get_state_attachments("sess-attach")
    assert len(attachments) == 1
    rec = attachments[0]
    assert rec["display_name"] == "shot.png"
    assert rec["data_url"] == "data:image/png;base64,AAAA"
    assert rec["data"] == "AAAA"
    assert rec["media_type"] == "image/png"


def test_copilot_state_log_readers_return_empty_on_os_error(tmp_path, monkeypatch):
    monkeypatch.setattr(copilot_log, "COPILOT_STATE_ROOT", tmp_path)
    _write_events_log(tmp_path, "sess-oserr", ["{}"])
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda self, *a, **k: (_ for _ in ()).throw(OSError("boom")),
    )
    assert copilot_log.get_state_attachments("sess-oserr") == []
    assert copilot_log.get_state_permissions("sess-oserr") == []
    assert copilot_log.get_state_skills("sess-oserr") == []
    assert copilot_log.get_state_tools("sess-oserr") == []


def test_copilot_permissions_from_events_skips_malformed_lines():
    lines = [
        "not json",
        json.dumps([1, 2, 3]),  # valid JSON, but not a dict
        json.dumps({"type": "permission.requested", "data": "not-a-dict"}),
    ]
    assert _permissions_from_events(lines) == []


def test_copilot_skills_from_events_skips_malformed_and_bad_data():
    lines = [
        "not json",
        json.dumps({"type": "skill.invoked", "data": "not-a-dict"}),
    ]
    assert _skills_from_events(lines) == []


def test_copilot_result_text_variants():
    assert copilot_log._result_text({"other": "x"}) == ""
    assert copilot_log._result_text("plain string") == "plain string"
    assert copilot_log._result_text(None) == ""
    assert copilot_log._result_text([1, 2, 3]) == "[1, 2, 3]"
    assert copilot_log._result_text({1, 2}) == ""


def test_copilot_tools_from_events_skips_malformed_lines_and_orphan_events():
    lines = [
        "not json",
        json.dumps([1, 2]),  # not a dict
        json.dumps({"type": "tool.execution_start", "data": "not-a-dict"}),
        json.dumps({"type": "tool.execution_start", "data": {"toolName": "x"}}),
        json.dumps(
            {"type": "tool.execution_complete", "data": {"toolCallId": "ghost"}}
        ),
    ]
    assert copilot_log._tools_from_events(lines) == []


# -- attribute_* fallback paths -----------------------------------------------


def test_copilot_attribute_tools_falls_back_to_first_turn_without_timestamp():
    turn = Turn("do it", "", "2026-01-01T00:00:00Z", 0)
    copilot_log.attribute_tools(
        [turn], [{"name": "grep", "arguments": None, "success": True, "result": "ok"}]
    )
    assert turn.tool_bullets == ["- grep"]


def test_copilot_attribute_tools_with_no_turns_is_a_noop():
    copilot_log.attribute_tools(
        [],
        [{"name": "grep", "arguments": {}, "timestamp": "2026-01-01T00:00:00Z"}],
    )


def test_copilot_attribute_tools_registers_skill_from_call():
    turn = Turn("use the demo skill", "", "2026-01-01T00:00:00Z", 0)
    copilot_log.attribute_tools(
        [turn],
        [
            {
                "name": "builtin.skill",
                "arguments": {"skill": "demo-skill"},
                "success": True,
                "result": "ok",
                "timestamp": "2026-01-01T00:00:01Z",
            }
        ],
    )
    assert turn.skills_used == ["demo-skill"]


def test_copilot_attribute_skills_falls_back_to_first_turn_without_timestamp():
    turn = Turn("hi", "", "2026-01-01T00:00:00Z", 0)
    copilot_log.attribute_skills([turn], [{"name": "demo"}])
    assert turn.skills_used == ["demo"]


def test_copilot_attribute_permissions_falls_back_to_first_turn_without_timestamp():
    turn = Turn("hi", "", "2026-01-01T00:00:00Z", 0)
    copilot_log.attribute_permissions(
        [turn], [{"tool": "a", "decision": "approved", "feedback": ""}]
    )
    assert [p["tool"] for p in turn.permission_decisions] == ["a"]


def test_copilot_get_session_models_returns_distinct_models_in_order():
    db_path = _make_test_db(
        session_id="sess-models",
        events=[
            {"usage_model": "gpt-5", "timestamp": "2026-01-01T00:00:00Z"},
            {"usage_model": "gpt-5", "timestamp": "2026-01-01T00:01:00Z"},
            {"usage_model": "claude", "timestamp": "2026-01-01T00:02:00Z"},
            {"usage_model": None, "timestamp": "2026-01-01T00:03:00Z"},
        ],
    )
    try:
        conn = get_db_connection(db_path)
        assert copilot_log.get_session_models(conn, "sess-models") == [
            "gpt-5",
            "claude",
        ]
        conn.close()
    finally:
        db_path.unlink()


def test_copilot_attachment_lookup_skips_empty_and_captures_variants():
    lookup = copilot_log._attachment_lookup(
        [
            {"display_name": "", "path": "", "type": "image/png"},  # unusable, skip
            {
                "display_name": "shot.png",
                "path": "",
                "type": "image/png",
                "data_url": "data:image/png;base64,AAAA",
            },
            {
                "display_name": "shot2.png",
                "type": "image/png",
                "data": "AAAA",
                "media_type": "image/png",
            },
        ]
    )
    assert lookup["shot.png"]["data_url"] == "data:image/png;base64,AAAA"
    assert lookup["shot2.png"]["data"] == "AAAA"
    assert lookup["shot2.png"]["media_type"] == "image/png"


def test_copilot_build_turn_objects_defaults_turn_index_and_parses_shell_command():
    raw_turns = [
        {
            "user_message": "<bash-input>pytest -q</bash-input>",
            "assistant_response": "ran it",
            "timestamp": "2026-01-01T00:00:00Z",
            # no turn_index key -> falls back to enumerate position
        }
    ]
    turns = copilot_log._build_turn_objects(raw_turns, [], [])
    assert turns[0].turn_index == 0
    assert turns[0].shell_command == "pytest -q"


def test_copilot_build_turn_objects_ignores_blank_image_token():
    raw_turns = [
        {
            "turn_index": 0,
            "user_message": "[image:   ] look",
            "assistant_response": "",
            "timestamp": "2026-01-01T00:00:00Z",
        }
    ]
    turns = copilot_log._build_turn_objects(raw_turns, [], [])
    assert turns[0].image_refs == []


# -- raw envelope writing and image byte resolution --------------------------


def test_copilot_write_raw_envelope_returns_none_without_events(tmp_path):
    assert (
        copilot_log.write_raw_envelope({"id": "s1"}, [], [], tmp_path)
        is None
    )


def test_copilot_write_raw_envelope_writes_json_file(tmp_path):
    turn = Turn("hello", "hi back", "2026-01-01T00:00:00Z", 0)
    session = {
        "id": "raw-session-1",
        "cwd": "/repo",
        "created_at": "2026-01-01T00:00:00Z",
    }
    out_path = copilot_log.write_raw_envelope(session, [turn], ["gpt-5"], tmp_path)
    assert out_path is not None
    assert out_path.exists()
    envelope = json.loads(out_path.read_text(encoding="utf-8"))
    assert envelope["harness"] == "copilot"
    assert envelope["models"] == ["gpt-5"]


def test_copilot_image_bytes_and_ext_from_data_url():
    data_url = "data:image/png;base64," + _PNG_B64
    raw, ext = copilot_log._image_bytes_and_ext({"data_url": data_url})
    assert ext == "png"
    assert raw == base64.b64decode(_PNG_B64)


def test_copilot_image_bytes_and_ext_bad_data_url_base64_returns_none():
    assert (
        copilot_log._image_bytes_and_ext({"data_url": "data:image/png;base64,A"})
        is None
    )


def test_copilot_image_bytes_and_ext_from_inline_data_field():
    raw, ext = copilot_log._image_bytes_and_ext(
        {"data": _PNG_B64, "media_type": "image/png"}
    )
    assert ext == "png"
    assert raw == base64.b64decode(_PNG_B64)


def test_copilot_image_bytes_and_ext_bad_inline_data_returns_none():
    assert copilot_log._image_bytes_and_ext({"data": "A"}) is None


def test_copilot_image_bytes_and_ext_missing_path_returns_none():
    assert (
        copilot_log._image_bytes_and_ext({"path": "/does/not/exist.png"}) is None
    )


def test_copilot_image_bytes_and_ext_guesses_extension_from_type_when_no_suffix(
    tmp_path,
):
    img = tmp_path / "noext"
    img.write_bytes(base64.b64decode(_PNG_B64))
    raw, ext = copilot_log._image_bytes_and_ext({"path": str(img), "type": "image/png"})
    assert ext == "png"
    assert raw == base64.b64decode(_PNG_B64)


# -- render() branches --------------------------------------------------------


def test_copilot_render_with_no_turns():
    md = copilot_log.render({"id": "s1"}, [], [], [], [])
    assert "No conversational turns found" in md


def test_copilot_render_with_blank_user_text_has_fallback_context_and_summary():
    turn = Turn("", "", "2026-01-01T00:00:00Z", 0)
    md = copilot_log.render({"id": "s1"}, [turn], [], [], [])
    assert "a Copilot CLI session." in md
    assert "No user inputs in this transcript" in md
    assert "_(no user text captured)_" in md
    assert "_(no response captured)_" in md


def test_copilot_render_shell_command_appears_in_summary_and_detail():
    turn = Turn("<bash-input>pytest -q</bash-input>", "", "2026-01-01T00:00:00Z", 0)
    turn.shell_command = "pytest -q"
    md = copilot_log.render({"id": "s1"}, [turn], [], [], [])
    assert "Ran shell command:" in md
    assert "**User ran shell command:**" in md
    assert "pytest -q" in md


def test_copilot_render_blank_assistant_with_tool_bullets_shows_bare_header():
    turn = Turn("hello", "", "2026-01-01T00:00:00Z", 0)
    turn.add_tool("grep", "pattern")
    md = copilot_log.render({"id": "s1"}, [turn], [], [], [])
    assert "**Assistant:**\n\n- grep -> pattern" in md


def test_copilot_render_includes_skill_usage_in_summary_and_detail():
    turn = Turn("use demo skill", "ok", "2026-01-01T00:00:00Z", 0)
    turn.add_skill("demo")
    md = copilot_log.render({"id": "s1"}, [turn], [], [], [])
    assert md.count("demo") >= 2


def test_copilot_render_truncates_result_notes_list_and_counts_extra():
    turn = Turn("do many things", "ok", "2026-01-01T00:00:00Z", 0)
    turn.result_notes = [f"note{i}" for i in range(8)]
    md = copilot_log.render({"id": "s1"}, [turn], [], [], [])
    assert "(+2 more results)" in md


def test_copilot_render_permission_decision_with_feedback_in_summary_and_detail():
    turn = Turn("do it", "ok", "2026-01-01T00:00:00Z", 0)
    turn.permission_decisions = [
        {
            "tool": "rm -rf",
            "decision": "denied-interactively-by-user",
            "feedback": "too risky\nstop",
        }
    ]
    md = copilot_log.render({"id": "s1"}, [turn], [], [], [])
    assert md.count("rm -rf") == 2
    assert "> too risky" in md
    assert "> stop" in md
    assert '-> "too risky stop"' in md


def test_copilot_render_lists_models_when_present():
    turn = Turn("hi", "ok", "2026-01-01T00:00:00Z", 0)
    md = copilot_log.render({"id": "s1"}, [turn], [], [], [], models=["gpt-5", "claude"])
    assert "Model: gpt-5, claude." in md


def test_copilot_render_includes_refs_and_checkpoints():
    turn = Turn("hi", "ok", "2026-01-01T00:00:00Z", 0)
    refs = [{"ref_type": "commit", "ref_value": "abc123", "turn_index": 0}]
    checkpoints = [
        {
            "checkpoint_number": 1,
            "title": "Checkpoint one",
            "overview": "did stuff",
            "created_at": "2026-01-01T00:05:00Z",
        }
    ]
    md = copilot_log.render({"id": "s1"}, [turn], checkpoints, [], refs)
    assert "## References" in md
    assert "abc123" in md
    assert "(turn 0)" in md
    assert "## Checkpoints (1)" in md
    assert "Checkpoint one" in md
    assert "did stuff" in md


def test_copilot_output_identifier_without_created_at_omits_date():
    assert copilot_log.output_identifier({"id": "abcdef1234"}) == "abcdef12"


def test_copilot_unique_identifier_increments_on_collision():
    used = set()
    assert copilot_log._unique_identifier("day_id", used) == "day_id"
    assert copilot_log._unique_identifier("day_id", used) == "day_id-2"
    assert copilot_log._unique_identifier("day_id", used) == "day_id-3"


# -- render_session integration -----------------------------------------------


def test_copilot_render_session_prefers_events_log_tools_over_db_files(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(copilot_log, "COPILOT_STATE_ROOT", tmp_path)
    db_path = _make_test_db(
        session_id="sess-prefer",
        turns=[
            {
                "turn_index": 0,
                "user_message": "do it",
                "assistant_response": "ok",
                "timestamp": "2026-01-01T00:00:00Z",
            }
        ],
        files=[{"file_path": "/tmp/from_db.py", "tool_name": "edit", "turn_index": 0}],
    )
    _write_events_log(
        tmp_path,
        "sess-prefer",
        [
            json.dumps(
                {
                    "type": "tool.execution_start",
                    "timestamp": "2026-01-01T00:00:01Z",
                    "data": {
                        "toolCallId": "c1",
                        "toolName": "grep",
                        "arguments": {"pattern": "x"},
                    },
                }
            ),
            json.dumps(
                {
                    "type": "tool.execution_complete",
                    "timestamp": "2026-01-01T00:00:02Z",
                    "data": {
                        "toolCallId": "c1",
                        "success": True,
                        "result": {"content": "found"},
                    },
                }
            ),
        ],
    )
    try:
        conn = get_db_connection(db_path)
        session = get_session_by_id(conn, "sess-prefer")
        md = render_session(conn, session)
        conn.close()
    finally:
        db_path.unlink()
    # The DB's file-edit bullet is dropped in favor of the events-log tool
    # call; the file still shows up in the unrelated "Files changed" footer,
    # which is sourced from get_session_files() regardless.
    assert "- edit -> /tmp/from_db.py" not in md
    assert "- grep -> x" in md


def test_copilot_render_session_writes_raw_envelope_when_requested(tmp_path):
    db_path = _make_test_db(
        session_id="sess-raw",
        turns=[
            {
                "turn_index": 0,
                "user_message": "do it",
                "assistant_response": "ok",
                "timestamp": "2026-01-01T00:00:00Z",
            }
        ],
    )
    try:
        conn = get_db_connection(db_path)
        session = get_session_by_id(conn, "sess-raw")
        render_session(conn, session, raw_output=tmp_path)
        conn.close()
    finally:
        db_path.unlink()
    raw_files = list(tmp_path.glob("copilot_session_log_raw_*.json"))
    assert len(raw_files) == 1


# -- _extract_all --------------------------------------------------------------


def test_copilot_extract_all_reports_error_when_no_sessions(capsys):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE sessions (id TEXT, cwd TEXT, repository TEXT, branch TEXT, "
        "summary TEXT, created_at TEXT, updated_at TEXT)"
    )
    rc = copilot_log._extract_all(conn, None)
    captured = capsys.readouterr()
    assert rc == 1
    assert "no Copilot sessions" in captured.err


def test_copilot_extract_all_writes_one_file_per_session(tmp_path, capsys):
    db_path = _make_test_db(
        session_id="sess-a",
        turns=[
            {
                "turn_index": 0,
                "user_message": "hi",
                "assistant_response": "ok",
                "timestamp": "2026-01-01T00:00:00Z",
            }
        ],
    )
    conn2 = sqlite3.connect(db_path)
    conn2.execute(
        "INSERT INTO sessions (id, cwd, repository, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            "sess-b",
            str(Path.cwd()),
            "test/repo",
            "2026-01-02T00:00:00Z",
            "2026-01-02T00:00:00Z",
        ),
    )
    conn2.execute(
        "INSERT INTO turns (session_id, turn_index, user_message, "
        "assistant_response, timestamp) VALUES (?, ?, ?, ?, ?)",
        ("sess-b", 0, "hi again", "ok again", "2026-01-02T00:00:05Z"),
    )
    conn2.commit()
    conn2.close()
    try:
        conn = get_db_connection(db_path)
        rc = copilot_log._extract_all(conn, str(tmp_path), raw=False)
        conn.close()
    finally:
        db_path.unlink()
    captured = capsys.readouterr()
    assert rc == 0
    written = sorted(p.name for p in tmp_path.glob("copilot_session_log_*.md"))
    assert len(written) == 2
    assert "done: 2/2 session(s) written" in captured.err


def test_copilot_extract_all_skips_session_that_fails_to_render(
    tmp_path, monkeypatch, capsys
):
    db_path = _make_test_db(
        session_id="sess-bad",
        turns=[
            {
                "turn_index": 0,
                "user_message": "hi",
                "assistant_response": "ok",
                "timestamp": "2026-01-01T00:00:00Z",
            }
        ],
    )
    try:
        conn = get_db_connection(db_path)

        def boom(*args, **kwargs):
            raise ValueError("render exploded")

        monkeypatch.setattr(copilot_log, "render_session", boom)
        rc = copilot_log._extract_all(conn, str(tmp_path))
        conn.close()
    finally:
        db_path.unlink()
    captured = capsys.readouterr()
    assert rc == 1
    assert "skip" in captured.err
    assert list(tmp_path.glob("*.md")) == []


def test_copilot_extract_all_reports_write_failure(tmp_path, monkeypatch, capsys):
    db_path = _make_test_db(
        session_id="sess-write-fail",
        turns=[
            {
                "turn_index": 0,
                "user_message": "hi",
                "assistant_response": "ok",
                "timestamp": "2026-01-01T00:00:00Z",
            }
        ],
    )
    try:
        conn = get_db_connection(db_path)

        original_write_text = Path.write_text

        def failing_write_text(self, *args, **kwargs):
            if self.suffix == ".md":
                raise OSError("disk full")
            return original_write_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", failing_write_text)
        rc = copilot_log._extract_all(conn, str(tmp_path), raw=False)
        conn.close()
    finally:
        db_path.unlink()
    captured = capsys.readouterr()
    assert rc == 1
    assert "error writing" in captured.err


# -- main() ---------------------------------------------------------------


def test_copilot_main_reports_missing_database(tmp_path, capsys):
    rc = copilot_log.main(["--db", str(tmp_path / "missing.db")])
    captured = capsys.readouterr()
    assert rc == 1
    assert "error" in captured.err


def test_copilot_main_session_not_found_returns_error(monkeypatch, tmp_path):
    monkeypatch.setattr(
        copilot_log, "get_db_connection", lambda p: sqlite3.connect(":memory:")
    )
    monkeypatch.setattr(copilot_log, "get_session_by_id", lambda conn, sid: None)
    rc = copilot_log.main(["bogus-id", "--db", str(tmp_path / "x.db")])
    assert rc == 1


def test_copilot_main_no_newest_session_returns_error(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(
        copilot_log, "get_db_connection", lambda p: sqlite3.connect(":memory:")
    )
    monkeypatch.setattr(
        copilot_log, "newest_session", lambda conn, cwd, strict=False: None
    )
    rc = copilot_log.main(["--output", "-", "--db", str(tmp_path / "x.db")])
    captured = capsys.readouterr()
    assert rc == 1
    assert "no Copilot sessions" in captured.err


def test_copilot_main_writes_markdown_to_stdout(monkeypatch, tmp_path, capsys):
    session = {
        "id": "sess-main2",
        "cwd": str(Path.cwd()),
        "created_at": "2026-01-01T00:00:00Z",
    }
    monkeypatch.setattr(
        copilot_log, "get_db_connection", lambda p: sqlite3.connect(":memory:")
    )
    monkeypatch.setattr(
        copilot_log, "newest_session", lambda conn, cwd, strict=False: session
    )
    monkeypatch.setattr(
        copilot_log,
        "render_session",
        lambda conn, s, raw_output=None, tool_result_max_bytes=0: "no newline body",
    )
    rc = copilot_log.main(["--output", "-", "--db", str(tmp_path / "x.db")])
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out == "no newline body\n"


def test_copilot_main_reports_render_session_error(monkeypatch, tmp_path, capsys):
    session = {
        "id": "sess-main3",
        "cwd": str(Path.cwd()),
        "created_at": "2026-01-01T00:00:00Z",
    }
    monkeypatch.setattr(
        copilot_log, "get_db_connection", lambda p: sqlite3.connect(":memory:")
    )
    monkeypatch.setattr(
        copilot_log, "newest_session", lambda conn, cwd, strict=False: session
    )

    def boom(*a, **k):
        raise ValueError("bad render")

    monkeypatch.setattr(copilot_log, "render_session", boom)
    rc = copilot_log.main(["--output", "-", "--db", str(tmp_path / "x.db")])
    captured = capsys.readouterr()
    assert rc == 1
    assert "error" in captured.err


def test_copilot_main_with_explicit_session_id_and_default_output(
    monkeypatch, tmp_path
):
    session = {
        "id": "sess-explicit",
        "cwd": str(Path.cwd()),
        "created_at": "2026-01-01T00:00:00Z",
    }
    monkeypatch.setattr(
        copilot_log, "get_db_connection", lambda p: sqlite3.connect(":memory:")
    )
    monkeypatch.setattr(copilot_log, "get_session_by_id", lambda conn, sid: session)
    captured_raw_output = {}

    def fake_render_session(conn, s, raw_output=None, tool_result_max_bytes=0):
        captured_raw_output["value"] = raw_output
        return "body"

    monkeypatch.setattr(copilot_log, "render_session", fake_render_session)
    monkeypatch.setattr(Path, "write_text", lambda self, *a, **k: None)
    rc = copilot_log.main(["sess-explicit", "--db", str(tmp_path / "x.db")])
    assert rc == 0
    assert captured_raw_output["value"] == copilot_log.DEFAULT_OUTPUT.parent


def test_copilot_main_with_explicit_session_id_and_output_path_writes_there(
    monkeypatch, tmp_path
):
    session = {
        "id": "sess-explicit2",
        "cwd": str(Path.cwd()),
        "created_at": "2026-01-01T00:00:00Z",
    }
    monkeypatch.setattr(
        copilot_log, "get_db_connection", lambda p: sqlite3.connect(":memory:")
    )
    monkeypatch.setattr(copilot_log, "get_session_by_id", lambda conn, sid: session)
    monkeypatch.setattr(
        copilot_log,
        "render_session",
        lambda conn, s, raw_output=None, tool_result_max_bytes=0: "body text",
    )
    out_path = tmp_path / "custom.md"
    rc = copilot_log.main(
        ["sess-explicit2", "--output", str(out_path), "--db", str(tmp_path / "x.db")]
    )
    assert rc == 0
    assert out_path.read_text(encoding="utf-8") == "body text"


def test_copilot_main_reports_output_write_error(monkeypatch, tmp_path, capsys):
    session = {
        "id": "sess-main4",
        "cwd": str(Path.cwd()),
        "created_at": "2026-01-01T00:00:00Z",
    }
    monkeypatch.setattr(
        copilot_log, "get_db_connection", lambda p: sqlite3.connect(":memory:")
    )
    monkeypatch.setattr(copilot_log, "get_session_by_id", lambda conn, sid: session)
    monkeypatch.setattr(
        copilot_log,
        "render_session",
        lambda conn, s, raw_output=None, tool_result_max_bytes=0: "body",
    )
    monkeypatch.setattr(
        Path,
        "write_text",
        lambda self, *a, **k: (_ for _ in ()).throw(OSError("disk full")),
    )
    rc = copilot_log.main(
        ["sess-main4", "--output", str(tmp_path / "out.md"), "--db", str(tmp_path / "x.db")]
    )
    captured = capsys.readouterr()
    assert rc == 1
    assert "error" in captured.err
