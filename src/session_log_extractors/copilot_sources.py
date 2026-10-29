"""Copilot database discovery, native row/state readers, and image inputs.

Connections are opened read-only and owned by the caller. Source snapshots hold
ordinary records and bytes, so interpretation never needs a live connection.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from session_log_extractors.json_types import (
    JsonValue,
    json_value,
    parse_json,
    string_value,
)

COPILOT_DB = Path.home() / ".copilot" / "session-store.db"
COPILOT_STATE_ROOT = Path.home() / ".copilot" / "session-state"

ImageInputs = Dict[str, Optional[bytes]]


@dataclass
class CopilotSource:
    """Loaded session metadata, native rows, ordered state, and optional images."""

    session: Dict[str, JsonValue]
    turns: List[Dict[str, JsonValue]]
    files: List[Dict[str, JsonValue]] = field(default_factory=list)
    attachments: List[Dict[str, JsonValue]] = field(default_factory=list)
    state_records: List[Dict[str, JsonValue]] = field(default_factory=list)
    models: List[str] = field(default_factory=list)
    image_inputs: ImageInputs = field(default_factory=dict)


def get_db_connection(db_path: Path) -> sqlite3.Connection:
    """Open a read-only connection to the Copilot session database."""
    if not db_path.exists():
        raise FileNotFoundError(f"Copilot database not found: {db_path}")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ? LIMIT 1",
        (name,),
    ).fetchone()
    return row is not None


def _table_columns(conn: sqlite3.Connection, name: str) -> set[str]:
    if not _table_exists(conn, name):
        return set()
    rows = conn.execute(f"PRAGMA table_info({name})").fetchall()
    cols = set()
    for row in rows:
        if isinstance(row, sqlite3.Row):
            col = row["name"]
        else:
            col = row[1]
        if isinstance(col, str):
            cols.add(col)
    return cols


def _paths_overlap(a: Path, b: Path) -> bool:
    try:
        a.resolve().relative_to(b.resolve())
        return True
    except ValueError:
        pass
    try:
        b.resolve().relative_to(a.resolve())
        return True
    except ValueError:
        return False


def _cwd_matches(session_cwd: Optional[str], cwd: Path, strict: bool = False) -> bool:
    if not session_cwd:
        return False
    rec = Path(session_cwd).expanduser()
    if strict:
        return rec.resolve() == cwd.resolve()
    return _paths_overlap(rec, cwd)


def _row_record(row: sqlite3.Row) -> Dict[str, JsonValue]:
    """Check SQLite's untyped cell values when snapshotting a native row."""
    return {key: json_value(row[key]) for key in row.keys()}


def matching_sessions(
    conn: sqlite3.Connection, cwd: Path, strict: bool = False
) -> List[Dict[str, JsonValue]]:
    """Return every Copilot session whose cwd overlaps the given path.

    In ``strict`` mode the recorded cwd must equal ``cwd`` exactly (no
    ancestor/descendant overlap).
    """
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, cwd, repository, branch, summary, created_at, updated_at
        FROM sessions
        ORDER BY created_at DESC
    """)

    sessions = []
    for row in cursor:
        session = _row_record(row)
        if _cwd_matches(string_value(session.get("cwd")), cwd, strict=strict):
            sessions.append(session)

    return sessions


def newest_session(
    conn: sqlite3.Connection, cwd: Path, strict: bool = False
) -> Optional[Dict[str, JsonValue]]:
    """Return the most recently updated session for cwd (or newest globally)."""
    sessions = matching_sessions(conn, cwd, strict=strict)
    if not sessions and not strict:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, cwd, repository, branch, summary, created_at, updated_at
            FROM sessions
            ORDER BY updated_at DESC, created_at DESC
            LIMIT 1
        """)
        row = cursor.fetchone()
        return _row_record(row) if row else None
    if not sessions:
        return None
    return max(
        sessions,
        key=lambda s: string_value(s.get("updated_at", s.get("created_at"))) or "",
    )


def get_session_by_id(
    conn: sqlite3.Connection, session_id: str
) -> Optional[Dict[str, JsonValue]]:
    """Retrieve a session by its ID."""
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id, cwd, repository, branch, summary, created_at, updated_at
        FROM sessions
        WHERE id = ? OR id LIKE ?
    """,
        (session_id, f"{session_id}%"),
    )
    row = cursor.fetchone()
    return _row_record(row) if row else None


def get_session_turns(
    conn: sqlite3.Connection, session_id: str
) -> List[Dict[str, JsonValue]]:
    """Retrieve all turns for a session."""
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT turn_index, user_message, assistant_response, timestamp
        FROM turns
        WHERE session_id = ?
        ORDER BY turn_index
    """,
        (session_id,),
    )
    return [_row_record(row) for row in cursor]


def get_session_checkpoints(
    conn: sqlite3.Connection, session_id: str
) -> List[Dict[str, JsonValue]]:
    """Retrieve all checkpoints for a session."""
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT checkpoint_number, title, overview, created_at
        FROM checkpoints
        WHERE session_id = ?
        ORDER BY checkpoint_number
    """,
        (session_id,),
    )
    return [_row_record(row) for row in cursor]


def get_session_files(
    conn: sqlite3.Connection, session_id: str
) -> List[Dict[str, JsonValue]]:
    """Retrieve all files touched in a session."""
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT file_path, tool_name, turn_index, first_seen_at
        FROM session_files
        WHERE session_id = ?
        ORDER BY first_seen_at
    """,
        (session_id,),
    )
    return [_row_record(row) for row in cursor]


def get_session_refs(
    conn: sqlite3.Connection, session_id: str
) -> List[Dict[str, JsonValue]]:
    """Retrieve all refs (commits, PRs, issues) for a session."""
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT ref_type, ref_value, turn_index, created_at
        FROM session_refs
        WHERE session_id = ?
        ORDER BY created_at
    """,
        (session_id,),
    )
    return [_row_record(row) for row in cursor]


def get_session_attachments(
    conn: sqlite3.Connection, session_id: str
) -> List[Dict[str, JsonValue]]:
    """Retrieve image attachment records for a session if available."""
    cols = _table_columns(conn, "attachments")
    required = {"session_id", "display_name", "path", "type"}
    if not required.issubset(cols):
        return []

    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT display_name, path, type
        FROM attachments
        WHERE session_id = ?
        ORDER BY rowid
    """,
        (session_id,),
    )
    return [_row_record(row) for row in cursor]


def parse_state_records(lines: List[str]) -> List[Dict[str, JsonValue]]:
    """Decode state JSONL once, retaining object records in source order."""
    records = []
    for line in lines:
        try:
            obj = parse_json(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            records.append(obj)
    return records


def load_state_records(session_id: str) -> List[Dict[str, JsonValue]]:
    """Read optional state input, preserving the missing/unreadable fallback."""
    events_path = COPILOT_STATE_ROOT / session_id / "events.jsonl"
    if not events_path.is_file():
        return []
    try:
        lines = events_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return parse_state_records(lines)


def get_session_models(conn: sqlite3.Connection, session_id: str) -> List[str]:
    """Distinct model ids from the session's usage events, if available.

    Reads assistant_usage_events (Copilot CLI 1.0.90-5); falls back to the
    events.usage_model layout that earlier CLIs are assumed to have written.
    """
    for table, column, order in (
        ("assistant_usage_events", "model", "id"),
        ("events", "usage_model", "timestamp"),
    ):
        cols = _table_columns(conn, table)
        if {"session_id", column, order} <= set(cols):
            break
    else:
        return []

    cursor = conn.cursor()
    cursor.execute(
        f"""
        SELECT {column} AS model
        FROM {table}
        WHERE session_id = ?
          AND {column} IS NOT NULL
          AND {column} != ''
        ORDER BY {order}
    """,
        (session_id,),
    )
    models: List[str] = []
    for row in cursor:
        model = row["model"]
        if isinstance(model, str) and model not in models:
            models.append(model)
    return models


def read_source(
    conn: sqlite3.Connection, session: Dict[str, JsonValue]
) -> CopilotSource:
    """Snapshot required rows and state once; leave connection ownership intact."""
    session_id = session["id"]
    if not isinstance(session_id, str):
        raise TypeError("Copilot session id must be a string")
    return CopilotSource(
        session=dict(session),
        turns=get_session_turns(conn, session_id),
        files=get_session_files(conn, session_id),
        attachments=get_session_attachments(conn, session_id),
        state_records=load_state_records(session_id),
        models=get_session_models(conn, session_id),
    )


def read_image(path_value: str) -> Optional[bytes]:
    """Snapshot an external image; missing/unreadable sources stay unavailable."""
    try:
        return Path(path_value).expanduser().read_bytes()
    except OSError:
        return None


def load_image_inputs(refs: Iterable[Dict[str, JsonValue]]) -> ImageInputs:
    """Read each referenced path once, retaining unresolved paths as well."""
    images: ImageInputs = {}
    for ref in refs:
        path = ref.get("path")
        if isinstance(path, str) and path.strip() and path not in images:
            images[path] = read_image(path)
    return images
