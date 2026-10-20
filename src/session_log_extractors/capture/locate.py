"""Find a harness's session files from a hook payload and build the filtered snapshot.

A hook payload names the session (and, for Claude and Copilot, a transcript path).
Each locator reads only files that belong to that one session, runs them through the
allowlist filters, and returns them under the branch layout:

    session_logs/claude/<session-id>.jsonl
    session_logs/claude/<session-id>/subagents/<agent-file>.jsonl
    session_logs/codex/<session-id>.jsonl
    session_logs/copilot/<session-id>/events.jsonl
    session_logs/copilot/<session-id>/copilot_db.json
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import filtering

SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
COPILOT_TABLES = tuple(filtering.COPILOT_DB_COLUMNS)


@dataclass
class Snapshot:
    """The filtered files for one session and what we know about its origin."""

    harness: str
    session_id: str
    files: dict[str, str] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)


def inside(root: Path, candidate: str | Path | None) -> bool:
    """True when ``candidate`` resolves to ``root`` or somewhere beneath it."""
    if not candidate:
        return False
    try:
        Path(candidate).resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        return False
    return True


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _first_value(text: str, key: str) -> Any:
    return next((e[key] for e in filtering.entries(text) if key in e), None)


def _payload_id(payload: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and SAFE_ID.match(value):
            return value
    return None


def _reported(payload: dict[str, Any]) -> dict[str, Any]:
    transcript = payload.get("transcript_path", payload.get("transcriptPath"))
    return {
        "transcript_path_reported": transcript,
        "hook_event": payload.get("hook_event_name", payload.get("hookEventName")),
    }


def claude_snapshot(payload: dict[str, Any], root: Path) -> Snapshot | None:
    """Claude Code: the hook's ``transcript_path`` plus its sibling subagent transcripts."""
    session_id = _payload_id(payload, "session_id", "sessionId")
    transcript = payload.get("transcript_path")
    if not session_id or not isinstance(transcript, str):
        return None
    path = Path(transcript)
    text = _read(path)
    if text is None or not inside(root, payload.get("cwd")):
        return None
    snap = Snapshot(
        "claude",
        session_id,
        meta={**_reported(payload), "harness_version": _first_value(text, "version")},
    )
    snap.files[f"session_logs/claude/{session_id}.jsonl"] = filtering.filter_claude(
        text
    )
    for agent in sorted((path.with_suffix("") / "subagents").glob("agent-*.jsonl")):
        agent_text = _read(agent)
        if agent_text is not None:
            snap.files[f"session_logs/claude/{session_id}/subagents/{agent.name}"] = (
                filtering.filter_claude(agent_text)
            )
    return snap


def codex_home(home: Path) -> Path:
    return (
        Path(os.environ["CODEX_HOME"])
        if os.environ.get("CODEX_HOME")
        else home / ".codex"
    )


def _codex_rollout(session_id: str, sessions: Path) -> Path | None:
    matches = sorted(sessions.glob(f"**/rollout-*{session_id}*.jsonl"))
    return matches[-1] if matches else None


def _codex_meta(text: str) -> dict[str, Any]:
    for entry in filtering.entries(text):
        if entry.get("type") == "session_meta":
            payload = entry.get("payload")
            return payload if isinstance(payload, dict) else {}
    return {}


def codex_snapshot(payload: dict[str, Any], root: Path, home: Path) -> Snapshot | None:
    """Codex: the hook's transcript path if given, else the rollout file for the session id."""
    session_id = _payload_id(payload, "session_id", "sessionId")
    reported = payload.get("transcript_path")
    path = Path(reported) if isinstance(reported, str) else None
    if path is None and session_id:
        path = _codex_rollout(session_id, codex_home(home) / "sessions")
    text = _read(path) if path else None
    if text is None or not inside(root, payload.get("cwd")):
        return None
    meta = _codex_meta(text)
    session_id = session_id or _payload_id(meta, "id")
    if not session_id:
        return None
    snap = Snapshot(
        "codex",
        session_id,
        meta={**_reported(payload), "harness_version": meta.get("cli_version")},
    )
    snap.files[f"session_logs/codex/{session_id}.jsonl"] = filtering.filter_codex(text)
    return snap


def copilot_home(home: Path) -> Path:
    return (
        Path(os.environ["COPILOT_HOME"])
        if os.environ.get("COPILOT_HOME")
        else home / ".copilot"
    )


def copilot_db_dump(db_path: Path, session_id: str) -> dict[str, Any]:
    """Rows for one session from Copilot's session store, read-only, allowlisted columns only."""
    if not db_path.exists():
        return {}
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return filtering.filter_copilot_dump(
            {t: _table_rows(conn, t, session_id) for t in COPILOT_TABLES}
        )
    except sqlite3.Error:
        return {}
    finally:
        conn.close()


def _table_rows(
    conn: sqlite3.Connection, table: str, session_id: str
) -> list[dict[str, Any]]:
    columns = filtering.COPILOT_DB_COLUMNS[table]
    present = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    wanted = [c for c in columns if c in present]
    if not wanted:
        return []
    key = "id" if table == "sessions" else "session_id"
    if key not in present:
        return []
    sql = f"SELECT {', '.join(wanted)} FROM {table} WHERE {key} = ? ORDER BY rowid"
    return [dict(row) for row in conn.execute(sql, (session_id,))]


def copilot_snapshot(
    payload: dict[str, Any], root: Path, home: Path
) -> Snapshot | None:
    """Copilot: events.jsonl located from the session id, plus a dump of its database rows.

    The hook's ``transcriptPath`` is recorded as reported, not trusted: the docs do not
    say what it points to.
    """
    session_id = _payload_id(payload, "sessionId", "session_id")
    if not session_id or not inside(root, payload.get("cwd")):
        return None
    base = copilot_home(home)
    text = _read(base / "session-state" / session_id / "events.jsonl")
    if text is None:
        return None
    version = _first_copilot_version(text)
    snap = Snapshot(
        "copilot", session_id, meta={**_reported(payload), "harness_version": version}
    )
    prefix = f"session_logs/copilot/{session_id}"
    snap.files[f"{prefix}/events.jsonl"] = filtering.filter_copilot_events(text)
    dump = copilot_db_dump(base / "session-store.db", session_id)
    if dump:
        snap.files[f"{prefix}/copilot_db.json"] = (
            json.dumps(dump, separators=(",", ":"), ensure_ascii=False) + "\n"
        )
    return snap


def _first_copilot_version(text: str) -> Any:
    for entry in filtering.entries(text):
        data = entry.get("data")
        if isinstance(data, dict) and "copilotVersion" in data:
            return data["copilotVersion"]
    return None


def detect_harness(payload: dict[str, Any]) -> str | None:
    """Guess the harness from the payload shape when ``--harness`` was not given."""
    if "transcriptPath" in payload or "sessionId" in payload:
        return "copilot"
    if (
        isinstance(payload.get("transcript_path"), str)
        and "/.codex/" in payload["transcript_path"]
    ):
        return "codex"
    if "transcript_path" in payload:
        return "claude"
    return None


def build_snapshot(
    harness: str, payload: dict[str, Any], root: Path, home: Path
) -> Snapshot | None:
    if harness == "claude":
        return claude_snapshot(payload, root)
    if harness == "codex":
        return codex_snapshot(payload, root, home)
    if harness == "copilot":
        return copilot_snapshot(payload, root, home)
    return None
