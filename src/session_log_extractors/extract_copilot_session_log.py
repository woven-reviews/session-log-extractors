#!/usr/bin/env python3
"""Extract GitHub Copilot CLI session transcripts into JSON conversation envelopes.

GitHub Copilot CLI records sessions in a SQLite database at:

    ~/.copilot/session-store.db

The database contains tables for sessions, turns, checkpoints, files, and refs.
This script queries that database to extract session conversations organized by
repository.

This script is mechanical: no LLM, no summarization, and no network access.
User prose is reproduced verbatim; assistant responses are reproduced verbatim.

Copilot records the full permission lifecycle in the per-session events log
(``~/.copilot/session-state/<id>/events.jsonl``), so both approvals and denials
are surfaced per turn (the tool asked about, the decision, and any feedback the
human typed when denying) -- unlike the Claude/Codex logs, where only denials are
observable.

Examples
--------
Every transcript for the current repository, one file per session (the default
when no session selector is given), written into the project root::

    uv run --locked python scripts/extract_copilot_session_log.py

The most recent transcript for the current repository, written to stdout::

    uv run --locked python scripts/extract_copilot_session_log.py --output -

A specific session id, to stdout::

    uv run --locked python scripts/extract_copilot_session_log.py abc123... --output -
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from session_log_extractors.session_log_envelope import (
    DEFAULT_TOOL_RESULT_MAX_BYTES,
    assistant_event,
    build_envelope,
    image_from_bytes,
    media_type_for_ext,
    raw_filename,
    tool_call,
    unavailable_image,
    user_event,
    warn_if_oversized,
    write_envelope,
)

COPILOT_DB = Path.home() / ".copilot" / "session-store.db"
COPILOT_STATE_ROOT = Path.home() / ".copilot" / "session-state"
PROJECT_ROOT = Path.cwd()

_NOISE_TAG_BLOCK = re.compile(
    r"<(system-reminder|system-notification|local-command-stdout|local-command-stderr|"
    r"command-stdout|command-stderr|command-name|command-message|command-args|"
    r"task-notification|bash-stdout|bash-stderr)>.*?"
    r"</\1>",
    re.DOTALL | re.IGNORECASE,
)
_NOISE_TAG_LOOSE = re.compile(
    r"</?(system-reminder|system-notification|local-command-stdout|local-command-stderr|"
    r"command-stdout|command-stderr|command-name|command-message|command-args|"
    r"task-notification|bash-stdout|bash-stderr)\b[^>]*/?>",
    re.IGNORECASE,
)
_BASH_INPUT = re.compile(r"<bash-input>(.*?)</bash-input>", re.DOTALL | re.IGNORECASE)
_COPILOT_IMAGE_REF = re.compile(r"\[image:\s*([^\]]+?)\s*\]", re.IGNORECASE)
_DATA_IMAGE_URL = re.compile(r"^data:(image/[A-Za-z0-9.+-]+);base64,(.*)$", re.DOTALL)
_IMAGE_EXT = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/bmp": "bmp",
    "image/svg+xml": "svg",
}

TOOL_DESC_MAX = 120
RESULT_NOTE_MAX = 160


def _decode_arguments(arguments: Any) -> Any:
    if isinstance(arguments, str):
        try:
            return json.loads(arguments)
        except json.JSONDecodeError:
            return arguments
    return arguments


def tool_descriptor(name: str, arguments: Any) -> str:
    """Summarize a tool call's most salient argument for a retained tool descriptor."""
    args = _decode_arguments(arguments)
    if not isinstance(args, dict):
        return _truncate(str(args), TOOL_DESC_MAX) if args else ""

    for key in (
        "cmd",
        "command",
        "query",
        "pattern",
        "path",
        "file_path",
        "filePath",
        "url",
        "skill",
    ):
        val = args.get(key)
        if isinstance(val, str) and val:
            return _truncate(val, TOOL_DESC_MAX)

    try:
        return _truncate(json.dumps(args, ensure_ascii=False), TOOL_DESC_MAX)
    except (TypeError, ValueError):
        return ""


def result_note(output: Any) -> str:
    if isinstance(output, str):
        return _truncate(output, RESULT_NOTE_MAX)
    if output is None:
        return ""
    try:
        return _truncate(json.dumps(output, ensure_ascii=False), RESULT_NOTE_MAX)
    except (TypeError, ValueError):
        return ""


def skill_refs_from_call(name: str, arguments: Any) -> List[Dict[str, Optional[str]]]:
    """Return skill names and definition paths evidenced by a tool call."""
    args = _decode_arguments(arguments)
    refs: List[Dict[str, Optional[str]]] = []

    def add(value: str, path: Optional[str] = None) -> None:
        for ref in refs:
            if ref["name"] == value:
                if path and not ref.get("path"):
                    ref["path"] = path
                return
        refs.append({"name": value, "path": path})

    short_name = name.rsplit(".", 1)[-1].lower()
    if short_name in ("skill", "read_skill") and isinstance(args, dict):
        value = args.get("skill") or args.get("name") or args.get("package")
        if isinstance(value, str) and value:
            add(value)

    try:
        blob = (
            json.dumps(args, ensure_ascii=False) if not isinstance(args, str) else args
        )
    except (TypeError, ValueError):
        blob = ""
    skill_path = re.compile(r"(?P<path>[^\s\"']*/(?P<skill>[^/\s\"']+)/SKILL\.md)")
    for match in skill_path.finditer(blob):
        skill = match.group("skill")
        if skill:
            add(skill, match.group("path"))
    return refs


def _truncate(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def clean_user_text(text: str) -> str:
    """Strip harness-injected wrappers while keeping the human's prose."""
    if not text:
        return ""
    cleaned = _NOISE_TAG_BLOCK.sub("", text)
    cleaned = _NOISE_TAG_LOOSE.sub("", cleaned)
    cleaned = re.sub(r"\n[ \t]*\n[ \t]*\n+", "\n\n", cleaned)
    return cleaned.strip()


def _parse_ts(ts: Optional[str]):
    if not ts or not isinstance(ts, str):
        return None
    try:
        from datetime import datetime, timezone

        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    # SQLite writes naive timestamps; treat them as UTC so they compare with aware ones.
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def format_timestamp(ts: Optional[str]) -> str:
    """Render as 'YYYY-MM-DD HH:MM:SS UTC' -- always UTC, never machine-local."""
    from datetime import timezone

    if not ts or not isinstance(ts, str):
        return "(no timestamp)"
    dt = _parse_ts(ts)
    if dt is None:
        return ts
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


def date_only(ts: Optional[str]) -> str:
    formatted = format_timestamp(ts)
    if formatted == "(no timestamp)":
        return "unknown"
    return formatted.split(" ")[0]


class Turn:
    def __init__(
        self,
        user_text: str,
        assistant_text: str,
        timestamp: Optional[str],
        turn_index: int,
    ):
        self.user_text = user_text
        self.assistant_text = assistant_text
        self.timestamp = timestamp
        self.turn_index = turn_index
        self.shell_command: Optional[str] = None
        self.tools: List[tuple[str, str]] = []
        self.skills_used: List[str] = []
        self.image_refs: List[Dict[str, str]] = []
        self.permission_decisions: List[Dict[str, Any]] = []
        self.result_notes: List[str] = []
        self.tool_calls: List[Dict[str, Any]] = []

    def add_tool(self, name: str, descriptor: str) -> None:
        self.tools.append((name, descriptor))

    def add_result_note(self, note: str) -> None:
        if note:
            self.result_notes.append(note)

    def add_skill(self, name: str) -> None:
        if name and name not in self.skills_used:
            self.skills_used.append(name)


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


def _table_columns(conn: sqlite3.Connection, name: str) -> set:
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


def matching_sessions(
    conn: sqlite3.Connection, cwd: Path, strict: bool = False
) -> List[Dict[str, Any]]:
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
        session = dict(row)
        if _cwd_matches(session.get("cwd"), cwd, strict=strict):
            sessions.append(session)

    return sessions


def newest_session(
    conn: sqlite3.Connection, cwd: Path, strict: bool = False
) -> Optional[Dict[str, Any]]:
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
        return dict(row) if row else None
    if not sessions:
        return None
    return max(sessions, key=lambda s: s.get("updated_at", s.get("created_at", "")))


def get_session_by_id(
    conn: sqlite3.Connection, session_id: str
) -> Optional[Dict[str, Any]]:
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
    return dict(row) if row else None


def get_session_turns(
    conn: sqlite3.Connection, session_id: str
) -> List[Dict[str, Any]]:
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
    return [dict(row) for row in cursor]


def get_session_checkpoints(
    conn: sqlite3.Connection, session_id: str
) -> List[Dict[str, Any]]:
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
    return [dict(row) for row in cursor]


def get_session_files(
    conn: sqlite3.Connection, session_id: str
) -> List[Dict[str, Any]]:
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
    return [dict(row) for row in cursor]


def get_session_refs(conn: sqlite3.Connection, session_id: str) -> List[Dict[str, Any]]:
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
    return [dict(row) for row in cursor]


def get_session_attachments(
    conn: sqlite3.Connection, session_id: str
) -> List[Dict[str, Any]]:
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
    return [dict(row) for row in cursor]


# Existing source loading and interpretation; remove in QUAL-2516.
def get_state_attachments(session_id: str) -> List[Dict[str, Any]]:  # noqa: C901, PLR0912
    """Retrieve attachment records from the per-session events log if present."""
    events_path = COPILOT_STATE_ROOT / session_id / "events.jsonl"
    if not events_path.is_file():
        return []

    attachments: List[Dict[str, Any]] = []
    try:
        lines = events_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []

    events = []
    for line in lines:
        try:
            events.append(json.loads(line))
        except ValueError:
            continue

    # Copilot logs each attached image's bytes as a session.binary_asset keyed by
    # assetId, so the image survives even after its source file is deleted.
    assets: Dict[str, tuple] = {}
    for obj in events:
        if isinstance(obj, dict) and obj.get("type") == "session.binary_asset":
            asset = obj.get("data")
            if (
                isinstance(asset, dict)
                and isinstance(asset.get("assetId"), str)
                and isinstance(asset.get("data"), str)
            ):
                assets[asset["assetId"]] = (asset["data"], asset.get("mimeType"))

    for obj in events:
        if not isinstance(obj, dict) or obj.get("type") != "user.message":
            continue

        data = obj.get("data")
        if not isinstance(data, dict):
            continue
        event_attachments = data.get("attachments")
        if not isinstance(event_attachments, list):
            continue

        for item in event_attachments:
            if not isinstance(item, dict):
                continue
            display_name = item.get("displayName")
            path_value = item.get("path")
            mime_type = item.get("mimeType")
            data_url = item.get("dataUrl")
            data_b64 = item.get("data")
            media_type = item.get("mediaType")
            asset_id = item.get("assetId")
            if (
                not isinstance(data_b64, str)
                and isinstance(asset_id, str)
                and asset_id in assets
            ):
                data_b64, media_type = assets[asset_id]

            rec: Dict[str, Any] = {
                "display_name": display_name if isinstance(display_name, str) else "",
                "path": path_value if isinstance(path_value, str) else "",
                "type": mime_type if isinstance(mime_type, str) else "",
                "timestamp": obj.get("timestamp"),
            }
            if isinstance(data_url, str):
                rec["data_url"] = data_url
            if isinstance(data_b64, str):
                rec["data"] = data_b64
            if isinstance(media_type, str):
                rec["media_type"] = media_type
            attachments.append(rec)

    return attachments


# Existing native interpretation; remove in QUAL-2519.
def _permissions_from_events(lines: List[str]) -> List[Dict[str, Any]]:  # noqa: C901
    """Join ``permission.requested`` / ``permission.completed`` events into one
    record per decision: the tool asked about, the outcome, and any feedback.

    Unlike Claude/Codex, Copilot records the full permission lifecycle — both
    approvals and denials — so both are captured. A ``permission.requested`` with
    no matching completion (aborted / still pending) is emitted as ``pending``.
    """
    requests: Dict[str, Dict[str, Any]] = {}
    decided: set = set()
    out: List[Dict[str, Any]] = []
    for line in lines:
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        etype = obj.get("type")
        data = obj.get("data")
        if not isinstance(data, dict):
            continue

        if etype == "permission.requested":
            pr = data.get("permissionRequest")
            pr = pr if isinstance(pr, dict) else {}
            rid = data.get("requestId")
            if isinstance(rid, str):
                requests[rid] = {
                    "tool": (
                        pr.get("fullCommandText")
                        or pr.get("intention")
                        or pr.get("kind")
                        or "tool"
                    ),
                    "timestamp": obj.get("timestamp"),
                }
        elif etype == "permission.completed":
            rid = data.get("requestId")
            result = data.get("result")
            result = result if isinstance(result, dict) else {}
            feedback = result.get("feedback")
            req = requests.get(rid, {}) if isinstance(rid, str) else {}
            if isinstance(rid, str):
                decided.add(rid)
            out.append(
                {
                    "tool": req.get("tool", "tool"),
                    "decision": result.get("kind") or "unknown",
                    "feedback": feedback if isinstance(feedback, str) else "",
                    "timestamp": obj.get("timestamp") or req.get("timestamp"),
                }
            )

    for rid, req in requests.items():
        if rid not in decided:
            out.append(
                {
                    "tool": req.get("tool", "tool"),
                    "decision": "pending",
                    "feedback": "",
                    "timestamp": req.get("timestamp"),
                }
            )
    out.sort(key=lambda p: p.get("timestamp") or "")
    return out


def get_state_permissions(session_id: str) -> List[Dict[str, Any]]:
    """Read permission decisions from the per-session events log, if present."""
    events_path = COPILOT_STATE_ROOT / session_id / "events.jsonl"
    if not events_path.is_file():
        return []
    try:
        lines = events_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return _permissions_from_events(lines)


def _skills_from_events(lines: List[str]) -> List[Dict[str, Any]]:
    """Extract Copilot's first-class ``skill.invoked`` events."""
    skills: List[Dict[str, Any]] = []
    for line in lines:
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if not isinstance(obj, dict) or obj.get("type") != "skill.invoked":
            continue
        data = obj.get("data")
        if not isinstance(data, dict):
            continue
        name = data.get("name")
        if isinstance(name, str) and name:
            path = data.get("path")
            skills.append(
                {
                    "name": name,
                    "path": path if isinstance(path, str) else None,
                    "timestamp": obj.get("timestamp"),
                }
            )
    return skills


def get_state_skills(session_id: str) -> List[Dict[str, Any]]:
    """Read invoked skills from the per-session events log, if present."""
    events_path = COPILOT_STATE_ROOT / session_id / "events.jsonl"
    if not events_path.is_file():
        return []
    try:
        lines = events_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return _skills_from_events(lines)


def _result_text(result: Any) -> str:
    """Flatten a ``tool.execution_complete`` result to plain text."""
    if isinstance(result, dict):
        for key in ("detailedContent", "content"):
            val = result.get(key)
            if isinstance(val, str) and val:
                return val
        return ""
    if isinstance(result, str):
        return result
    if result is None:
        return ""
    try:
        return json.dumps(result, ensure_ascii=False)
    except (TypeError, ValueError):
        return ""


def _tools_from_events(lines: List[str]) -> List[Dict[str, Any]]:
    """Join ``tool.execution_start`` / ``tool.execution_complete`` events.

    Copilot records the full tool call (name and arguments) and its result in the
    per-session events log, so both are surfaced -- unlike the DB's
    ``session_files`` table, which only lists edited paths. One record per call,
    ordered by start time.
    """
    starts: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for line in lines:
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        etype = obj.get("type")
        data = obj.get("data")
        if not isinstance(data, dict):
            continue

        if etype == "tool.execution_start":
            call_id = data.get("toolCallId")
            if not isinstance(call_id, str) or not call_id:
                continue
            starts[call_id] = {
                "call_id": call_id,
                "name": data.get("toolName")
                if isinstance(data.get("toolName"), str)
                else "tool",
                "arguments": data.get("arguments"),
                "timestamp": obj.get("timestamp"),
                "success": None,
                "result": None,
            }
            order.append(call_id)
        elif etype == "tool.execution_complete":
            call_id = data.get("toolCallId")
            rec = starts.get(call_id) if isinstance(call_id, str) else None
            if rec is None:
                continue
            rec["success"] = data.get("success")
            rec["result"] = _result_text(data.get("result"))

    return [starts[cid] for cid in order]


def get_state_tools(session_id: str) -> List[Dict[str, Any]]:
    """Read tool calls + results from the per-session events log, if present."""
    events_path = COPILOT_STATE_ROOT / session_id / "events.jsonl"
    if not events_path.is_file():
        return []
    try:
        lines = events_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return _tools_from_events(lines)


def _turn_for(turns: List[Turn], ts: Optional[str]) -> Optional[Turn]:
    """The turn an event at ts belongs to.

    DB turn timestamps mark completion, so that is the first turn ending at or
    after the event; later events go to the last turn, untimed ones to the first.
    """
    if not turns:
        return None
    at = _parse_ts(ts)
    if at is None:
        return turns[0]
    for turn in turns:
        turn_at = _parse_ts(turn.timestamp)
        if turn_at is not None and at <= turn_at:
            return turn
    return turns[-1]


def attribute_tools(turns: List[Turn], tools: List[Dict[str, Any]]) -> None:
    """Fold each tool call under the turn it happened in.

    Populates the tool target, a structured record for the JSON envelope, a
    result note, and any skill evidenced by the call's arguments.
    """
    for tool in tools:
        target = _turn_for(turns, tool.get("timestamp"))
        if target is None:
            continue

        name = tool.get("name") or "tool"
        arguments = tool.get("arguments")
        target.add_tool(str(name), tool_descriptor(str(name), arguments))
        target.tool_calls.append(
            {
                "name": str(name),
                "arguments": arguments,
                "result": tool.get("result"),
                "timestamp": tool.get("timestamp"),
            }
        )

        note = result_note(tool.get("result"))
        if tool.get("success") is False:
            note = f"error: {note}" if note else "error"
        if note:
            target.add_result_note(f"{name}: {note}")

        for skill_ref in skill_refs_from_call(str(name), arguments):
            target.add_skill(str(skill_ref["name"]))


def attribute_skills(turns: List[Turn], skills: List[Dict[str, Any]]) -> None:
    """Fold each skill invocation under the turn it happened in."""
    for skill in skills:
        target = _turn_for(turns, skill.get("timestamp"))
        name = skill.get("name")
        if target is not None and isinstance(name, str):
            target.add_skill(name)


def attribute_permissions(turns: List[Turn], perms: List[Dict[str, Any]]) -> None:
    """Fold each permission decision under the turn it happened in."""
    for p in perms:
        target = _turn_for(turns, p.get("timestamp"))
        if target is not None:
            target.permission_decisions.append(p)


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


def _attachment_lookup(attachments: List[Dict[str, Any]]) -> Dict[str, Dict[str, str]]:
    lookup: Dict[str, Dict[str, str]] = {}
    for item in attachments:
        raw_display = item.get("display_name")
        raw_path = item.get("path")
        has_path = isinstance(raw_path, str) and bool(raw_path.strip())
        has_data_url = isinstance(item.get("data_url"), str)
        has_data = isinstance(item.get("data"), str)
        if not has_path and not has_data_url and not has_data:
            continue

        type_value = item.get("type")
        rec: Dict[str, str] = {
            "type": type_value if isinstance(type_value, str) else ""
        }
        if has_path and isinstance(raw_path, str):
            rec["path"] = raw_path
        if has_data_url and isinstance(item.get("data_url"), str):
            rec["data_url"] = item["data_url"]
        if has_data and isinstance(item.get("data"), str):
            rec["data"] = item["data"]
            media_type = item.get("media_type")
            if isinstance(media_type, str):
                rec["media_type"] = media_type

        if isinstance(raw_display, str) and raw_display.strip():
            lookup.setdefault(raw_display.strip(), rec)

        if has_path and isinstance(raw_path, str):
            lookup.setdefault(Path(raw_path).name, rec)
    return lookup


# Existing native interpretation; remove in QUAL-2519.
def _build_turn_objects(  # noqa: C901, PLR0912, PLR0915
    turns: List[Dict[str, Any]],
    files: List[Dict[str, Any]],
    attachments: List[Dict[str, Any]],
) -> List[Turn]:
    built: List[Turn] = []
    by_turn: Dict[int, List[Dict[str, Any]]] = {}
    attachment_by_name = _attachment_lookup(attachments)
    for file_rec in files:
        idx = file_rec.get("turn_index")
        if isinstance(idx, int):
            by_turn.setdefault(idx, []).append(file_rec)

    for i, raw in enumerate(turns, 1):
        user_msg = clean_user_text(raw.get("user_message") or "")
        asst_msg = (raw.get("assistant_response") or "").strip()
        turn_idx = raw.get("turn_index")
        if not isinstance(turn_idx, int):
            turn_idx = i - 1
        turn = Turn(user_msg, asst_msg, raw.get("timestamp"), turn_idx)

        cmds = [c.strip() for c in _BASH_INPUT.findall(user_msg) if c.strip()]
        if cmds:
            turn.shell_command = "\n".join(cmds)

        for raw_name in _COPILOT_IMAGE_REF.findall(user_msg):
            image_name = raw_name.strip()
            if not image_name:
                continue
            attached = attachment_by_name.get(image_name)
            if attached:
                ref = {"name": image_name}
                for key in ("path", "type", "data_url", "data", "media_type"):
                    if key in attached:
                        ref[key] = attached[key]
                turn.image_refs.append(ref)
            else:
                turn.image_refs.append({"name": image_name})

        for item in by_turn.get(turn.turn_index, []):
            tool_name = item.get("tool_name") or "tool"
            file_path = item.get("file_path") or ""
            turn.add_tool(str(tool_name), str(file_path))

        built.append(turn)

    for attachment in attachments:
        attached_at = _parse_ts(attachment.get("timestamp"))
        if attached_at is None or not built:
            continue
        path = attachment.get("path")
        name = attachment.get("display_name")
        if not isinstance(name, str) or not name:
            name = Path(path).name if isinstance(path, str) else ""
        media_type = attachment.get("type") or attachment.get("media_type")
        data_url = attachment.get("data_url")
        if (
            isinstance(media_type, str)
            and media_type
            and not media_type.startswith("image/")
        ):
            continue
        if not name or not (
            isinstance(media_type, str)
            and media_type.startswith("image/")
            or isinstance(path, str)
            and media_type_for_ext(Path(path).suffix)
            or isinstance(data_url, str)
            and _DATA_IMAGE_URL.match(data_url)
        ):
            continue
        target = _turn_for(built, attachment.get("timestamp"))
        ref = {"name": name}
        for key in ("path", "type", "data_url", "data", "media_type"):
            if attachment.get(key):
                ref[key] = attachment[key]
        # Two different files can share a display name, so match on the image
        # itself; only a ref with no source falls back to its name.
        source = _image_source(ref)
        if any(
            _image_source(r) == source
            if source and _image_source(r)
            else r["name"] == name
            for r in target.image_refs
        ):
            continue
        if len(ref) > 1:
            target.image_refs.append(ref)
    return built


def _image_source(ref: Dict[str, Any]) -> Optional[str]:
    return ref.get("path") or ref.get("data_url") or ref.get("data")


def envelope_images(refs: List[Dict[str, str]]) -> List[Dict[str, Any]]:
    """Resolve Copilot image refs to inline base64 entries.

    Copilot stores image payloads outside DB turns. Image tokens and timestamped
    event attachments both become refs; missing sources remain visible as
    unavailable images.
    """
    images: List[Dict[str, Any]] = []
    for ref in refs:
        decoded = _image_bytes_and_ext(ref)
        if decoded is None:
            images.append(unavailable_image(ref.get("name") or ref.get("path")))
            continue
        raw, ext = decoded
        images.append(image_from_bytes(raw, media_type_for_ext(ext)))
    return images


def build_events(
    turns: List["Turn"],
    tool_result_max_bytes: int = DEFAULT_TOOL_RESULT_MAX_BYTES,
) -> List[Dict[str, Any]]:
    """Produce raw-envelope events from already-built Copilot turns.

    Copilot's store hands us whole turns rather than an event stream, so this
    reads the Turn objects instead of re-walking a transcript.

    When a turn carries structured ``tool_calls`` (recovered from the events log),
    each call becomes its own assistant event stamped with that call's own
    timestamp -- mirroring Codex, where every tool call is a separate event -- and
    its arguments and result are emitted in full (result capped at
    ``tool_result_max_bytes``). Older turns that only have file-edit records fall
    back to a single grouped assistant event with no per-call timestamp or result.
    """
    events: List[Dict[str, Any]] = []
    index = 0

    for turn in turns:
        images = envelope_images(turn.image_refs)
        if turn.user_text.strip() or images:
            events.append(user_event(index, turn.timestamp, turn.user_text, images))
            index += 1

        if turn.tool_calls:
            # One event per tool call, each with its own timestamp, so the stream
            # matches Codex's granularity. Assistant prose (if any) leads.
            if turn.assistant_text.strip():
                events.append(
                    assistant_event(index, turn.timestamp, turn.assistant_text)
                )
                index += 1
            for tc in turn.tool_calls:
                call = tool_call(
                    tc["name"],
                    tc.get("arguments"),
                    tc.get("result"),
                    tool_result_max_bytes,
                )
                events.append(
                    assistant_event(
                        index, tc.get("timestamp") or turn.timestamp, "", [call]
                    )
                )
                index += 1
        else:
            calls = [
                tool_call(name, {"target": target} if target else {})
                for name, target in turn.tools
            ]
            if turn.assistant_text.strip() or calls:
                events.append(
                    assistant_event(index, turn.timestamp, turn.assistant_text, calls)
                )
                index += 1

    return events


def _image_bytes_and_ext(ref: Dict[str, str]) -> Optional[tuple[bytes, str]]:
    data_url = ref.get("data_url")
    if isinstance(data_url, str) and data_url:
        match = _DATA_IMAGE_URL.match(data_url)
        if match:
            media_type, data = match.groups()
            try:
                raw = base64.b64decode(data, validate=False)
            except (ValueError, TypeError):
                return None
            return raw, _IMAGE_EXT.get(media_type, "img")

    data = ref.get("data")
    if isinstance(data, str) and data:
        try:
            raw = base64.b64decode(data, validate=False)
        except (ValueError, TypeError):
            return None
        media_type = ref.get("media_type") or ref.get("type")
        ext = (
            _IMAGE_EXT.get(media_type, "img") if isinstance(media_type, str) else "img"
        )
        return raw, ext

    source_path = ref.get("path")
    if isinstance(source_path, str) and source_path.strip():
        src = Path(source_path).expanduser()
        try:
            raw = src.read_bytes()
        except OSError:
            return None
        ext = src.suffix.lstrip(".")
        if not ext:
            guessed = ref.get("type", "")
            ext = guessed.split("/")[-1] if "/" in guessed else "img"
        return raw, ext

    return None


def output_identifier(session: Dict[str, Any]) -> str:
    """Generate a short identifier for output filenames."""
    session_id = session.get("id", "unknown")
    # Use first 8 chars of session id
    short_id = session_id[:8] if len(session_id) >= 8 else session_id

    date = date_only(session.get("created_at"))
    if date and date != "unknown":
        return f"{date}_{short_id}"
    return short_id


def _unique_identifier(base: str, used: set) -> str:
    ident = base
    suffix = 2
    while ident in used:
        ident = f"{base}-{suffix}"
        suffix += 1
    used.add(ident)
    return ident


def load_session_turns(conn: sqlite3.Connection, session: Dict[str, Any]) -> List[Turn]:
    """Load turns with file, attachment, tool, permission, and skill evidence."""
    session_id = session["id"]
    attachments = get_session_attachments(conn, session_id)
    attachments.extend(get_state_attachments(session_id))
    turns = _build_turn_objects(
        get_session_turns(conn, session_id),
        get_session_files(conn, session_id),
        attachments,
    )
    tools = get_state_tools(session_id)
    if tools:
        # Events carry full calls and results; discard coarser DB file targets.
        for turn in turns:
            turn.tools = []
        attribute_tools(turns, tools)
    attribute_permissions(turns, get_state_permissions(session_id))
    attribute_skills(turns, get_state_skills(session_id))
    return turns


def extract_session(
    conn: sqlite3.Connection,
    session: Dict[str, Any],
    tool_result_max_bytes: int = DEFAULT_TOOL_RESULT_MAX_BYTES,
) -> Optional[Dict[str, Any]]:
    """Build a session envelope without changing its content for display."""
    events = build_events(load_session_turns(conn, session), tool_result_max_bytes)
    if not events:
        return None
    return build_envelope(
        harness="copilot",
        session_id=output_identifier(session),
        events=events,
        cwd=session.get("cwd"),
        started_at=session.get("created_at"),
        models=get_session_models(conn, session["id"]),
        tool_result_max_bytes=tool_result_max_bytes,
    )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Extract GitHub Copilot CLI session logs as JSON.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "session",
        nargs="?",
        metavar="SESSION_ID",
        help="session id or prefix (optional; --output - selects newest for current repo)",
    )
    parser.add_argument(
        "--db",
        metavar="PATH",
        default=None,
        help=f"path to Copilot session database (default: {COPILOT_DB})",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help=(
            "only export sessions started in the current working directory exactly "
            "(no parent/descendant overlap, no newest-anywhere fallback)"
        ),
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help=(
            "extract every session for the current repository into the "
            "project root, one file per session named "
            "copilot_session_log_raw_<identifier>.json (in this mode --output is treated "
            "as the output directory; positional selector is ignored). This is "
            "the default when no session selector is supplied"
        ),
    )
    parser.add_argument(
        "--output",
        metavar="PATH",
        default=None,
        help=(
            "output JSON file (default: copilot_session_log_raw_<identifier>.json "
            f"in the project root, {PROJECT_ROOT}; use '-' for stdout). With --all, "
            "an output directory instead (default: project root)."
        ),
    )
    parser.add_argument(
        "--tool-result-bytes",
        metavar="N",
        type=int,
        default=DEFAULT_TOOL_RESULT_MAX_BYTES,
        help=(
            "cap each tool result in the JSON envelope at N bytes; the original "
            "size is always recorded. Use a negative value for no cap "
            f"(default: {DEFAULT_TOOL_RESULT_MAX_BYTES})"
        ),
    )
    return parser


CAP_FLAG = "--tool-result-bytes"
NO_EVENTS_NOTE = "no exportable events; no JSON written"


def _extract_all(
    conn: sqlite3.Connection,
    output: Optional[str],
    strict: bool = False,
    tool_result_max_bytes: int = DEFAULT_TOOL_RESULT_MAX_BYTES,
) -> int:
    """Extract every matching Copilot session, one JSON file each."""
    sessions = matching_sessions(conn, Path.cwd(), strict=strict)
    if not sessions:
        print(f"error: no Copilot sessions for {Path.cwd()}", file=sys.stderr)
        return 1
    sessions.sort(key=lambda s: s.get("created_at", ""))
    out_dir = Path(output).expanduser() if output else PROJECT_ROOT
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"extracting {len(sessions)} Copilot session(s)", file=sys.stderr)
    written = 0
    used_ids = set()
    for session in sessions:
        ident = _unique_identifier(output_identifier(session), used_ids)
        out_path = out_dir / raw_filename("copilot", ident)
        try:
            envelope = extract_session(conn, session, tool_result_max_bytes)
            if envelope is None:
                print(f"  skip {ident}: {NO_EVENTS_NOTE}", file=sys.stderr)
                continue
            write_envelope(out_path, envelope, CAP_FLAG)
        except (OSError, ValueError) as exc:
            print(f"  skip {ident}: {exc}", file=sys.stderr)
            continue
        written += 1
        print(f"  wrote {out_path}", file=sys.stderr)
    print(f"done: {written}/{len(sessions)} session(s) written", file=sys.stderr)
    return 0 if written else 1


def _select_session(conn: sqlite3.Connection, args: argparse.Namespace):
    if args.session:
        session = get_session_by_id(conn, args.session)
        if session is None:
            print(f"error: no session found matching {args.session!r}", file=sys.stderr)
        return session
    session = newest_session(conn, Path.cwd(), strict=args.strict)
    if session is None:
        print(
            f"error: no Copilot sessions for {Path.cwd()}"
            + (" (--strict: exact cwd match only)" if args.strict else ""),
            file=sys.stderr,
        )
    return session


def _extract_selected(conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    session = _select_session(conn, args)
    if session is None:
        return 1
    print(f"using session: {session['id']}", file=sys.stderr)
    try:
        envelope = extract_session(conn, session, args.tool_result_bytes)
        if envelope is None:
            print(f"skip {session['id']}: {NO_EVENTS_NOTE}", file=sys.stderr)
            return 1
        if args.output == "-":
            payload = json.dumps(envelope, ensure_ascii=False)
            warn_if_oversized("stdout", payload, CAP_FLAG)
            sys.stdout.write(payload + "\n")
        else:
            out_path = (
                Path(args.output).expanduser()
                if args.output
                else PROJECT_ROOT / raw_filename("copilot", output_identifier(session))
            )
            write_envelope(out_path, envelope, CAP_FLAG)
            print(f"wrote {out_path}", file=sys.stderr)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    db_path = Path(args.db).expanduser() if args.db else COPILOT_DB
    try:
        conn = get_db_connection(db_path)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        if args.all or (not args.session and args.output != "-"):
            out = None if args.output == "-" else args.output
            return _extract_all(conn, out, args.strict, args.tool_result_bytes)
        return _extract_selected(conn, args)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
