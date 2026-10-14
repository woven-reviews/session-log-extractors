"""Checks for the on-demand native Copilot fixture capture."""

import json
import sqlite3

from generate_copilot_fixtures import clean_event_lines, copy_session


def test_clean_event_lines_preserves_shape_and_redacts_private_paths(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    rows = [
        {"type": "user.message", "data": {"content": str(workspace / "file.py")}},
        {"type": "assistant.message", "data": {"content": "contact user@example.com"}},
    ]
    cleaned = clean_event_lines([json.dumps(row) for row in rows], workspace)
    assert [row["type"] for row in map(json.loads, cleaned.splitlines())] == [
        "user.message", "assistant.message"
    ]
    assert "/workspace/file.py" in cleaned
    assert "user@example.com" not in cleaned


def test_copy_session_keeps_native_schema_and_only_requested_rows(tmp_path):
    source = tmp_path / "source.db"
    destination = tmp_path / "fixture.db"
    workspace = tmp_path / "workspace"
    with sqlite3.connect(source) as conn:
        conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, cwd TEXT)")
        conn.execute("CREATE TABLE turns (session_id TEXT, user_message TEXT)")
        conn.executemany("INSERT INTO sessions VALUES (?, ?)", [
            ("fixture-id", str(workspace)), ("private-id", "/private/project")
        ])
        conn.executemany("INSERT INTO turns VALUES (?, ?)", [
            ("fixture-id", "hello"), ("private-id", "private conversation")
        ])
    copy_session(source, "fixture-id", destination, workspace)
    with sqlite3.connect(destination) as conn:
        assert conn.execute("SELECT id, cwd FROM sessions").fetchall() == [
            ("fixture-id", "/workspace")
        ]
        assert conn.execute("SELECT session_id, user_message FROM turns").fetchall() == [
            ("fixture-id", "hello")
        ]
        assert "CREATE TABLE sessions" in conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='sessions'"
        ).fetchone()[0]
