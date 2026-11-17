"""Copilot event-log discovery, lifecycle facts, and source snapshots."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from session_log_extractors.copilot_protocol import (
    ASSISTANT_MESSAGE,
    SESSION_RESUME,
    SESSION_START,
)
from session_log_extractors.json_types import JsonValue, string_value
from session_log_extractors.jsonl_sources import load_jsonl_objects, parse_jsonl_objects
from session_log_extractors.session_selection import (
    RepositoryBoundary,
    cwd_matches,
    select_recorded_id,
    unique_paths,
)

COPILOT_STATE_ROOT = Path.home() / ".copilot" / "session-state"
ImageInputs = Dict[str, Optional[bytes]]


@dataclass(frozen=True)
class CopilotSession:
    """Recorded lifecycle facts used for discovery and the shared session."""

    path: Path
    session_id: str
    cwd: str | None = None
    started_at: str | None = None
    last_activity_at: datetime | None = None


@dataclass
class CopilotSource:
    """Loaded lifecycle facts, ordered native evidence, and optional image bytes."""

    session: CopilotSession
    state_records: List[Dict[str, JsonValue]] = field(default_factory=list)
    models: List[str] = field(default_factory=list)
    image_inputs: ImageInputs = field(default_factory=dict)


def parse_state_records(lines: List[str]) -> List[Dict[str, JsonValue]]:
    """Decode state JSONL once, retaining object records in source order."""
    return parse_jsonl_objects(lines, "Copilot state records")


def load_state_records(session_id: str) -> list[dict[str, JsonValue]]:
    """Read required native records for a known session directory."""
    return load_jsonl_objects(COPILOT_STATE_ROOT / session_id / "events.jsonl")


def _timestamp(value: JsonValue) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _lifecycle_cwd(data: dict[str, JsonValue]) -> str | None:
    context = data.get("context")
    return string_value(context.get("cwd")) if isinstance(context, dict) else None


def _session_facts(path: Path, records: list[dict[str, JsonValue]]) -> CopilotSession:
    session_id = None
    start_cwd = None
    resume_cwd = None
    started_at = None
    activity = []
    for record in records:
        stamp = _timestamp(record.get("timestamp"))
        if stamp is not None:
            activity.append(stamp)
        data = record.get("data")
        if not isinstance(data, dict) or record.get("agentId") is not None:
            continue
        if record.get("type") == SESSION_START:
            identity = string_value(data.get("sessionId"))
            if identity and session_id is not None and identity != session_id:
                raise ValueError(f"conflicting recorded session IDs in {path}")
            session_id = session_id or identity
            start_cwd = start_cwd or _lifecycle_cwd(data)
            started_at = started_at or string_value(data.get("startTime"))
        elif record.get("type") == SESSION_RESUME:
            resume_cwd = resume_cwd or _lifecycle_cwd(data)
    if not session_id:
        raise ValueError(
            f"no recorded Copilot session ID in {path}; expected session.start"
        )
    return CopilotSession(
        path,
        session_id,
        start_cwd or resume_cwd,
        started_at,
        max(activity) if activity else None,
    )


def _used_models(records: list[dict[str, JsonValue]]) -> list[str]:
    """Conversation producer evidence establishes use; configuration does not."""
    models = []
    for record in records:
        data = record.get("data")
        if record.get("type") != ASSISTANT_MESSAGE or not isinstance(data, dict):
            continue
        model = string_value(data.get("model"))
        if model and model not in models:
            models.append(model)
    return models


def read_source(path: Path) -> CopilotSource:
    """Read a required event log; retain complete records and lifecycle evidence."""
    records = load_jsonl_objects(path)
    return CopilotSource(_session_facts(path, records), records, _used_models(records))


def discover_sessions(root: Path) -> list[CopilotSource]:
    """Scan recorded identities; unrelated unavailable candidates are diagnosed."""
    sessions = []
    for path in sorted(root.glob("*/events.jsonl")):
        try:
            source = read_source(path)
        except (OSError, ValueError) as exc:
            print(f"warning: skipping Copilot source {path}: {exc}", file=sys.stderr)
            continue
        sessions.append(source)
    return sessions


def matching_sessions(
    sessions: list[CopilotSource], cwd: Path, strict: bool = False
) -> list[CopilotSource]:
    """Preserve legacy start-cwd matching, using resume cwd only when absent."""
    return [
        source for source in sessions if cwd_matches(source.session.cwd, cwd, strict)
    ]


def repository_sessions(
    sessions: list[CopilotSource], boundary: RepositoryBoundary
) -> list[CopilotSource]:
    """Filter native candidates through an established repository boundary."""
    unique_source_paths = set(unique_paths(source.session.path for source in sessions))
    return [
        source
        for source in sessions
        if source.session.path in unique_source_paths
        and boundary.matches(source.session.cwd, source.session.path)
    ]


def newest_session(
    sessions: list[CopilotSource], cwd: Path, strict: bool = False
) -> CopilotSource | None:
    """Choose the newest repository session, or exact cwd evidence with strict."""
    if strict:
        candidates = matching_sessions(sessions, cwd, strict=True)
    else:
        candidates = repository_sessions(sessions, RepositoryBoundary.discover(cwd))
    return newest_recorded_session(candidates)


def newest_recorded_session(sessions: list[CopilotSource]) -> CopilotSource | None:
    """Choose the most recently active source from an already selected set."""
    if not sessions:
        return None
    return max(
        sessions,
        key=lambda source: (
            source.session.last_activity_at
            or datetime.min.replace(tzinfo=timezone.utc),
            source.session.started_at or "",
            source.session.session_id,
            str(source.session.path),
        ),
    )


def get_session_by_id(
    sessions: list[CopilotSource], session_id: str
) -> CopilotSource | None:
    """Select a case-sensitive exact recorded identity or unique literal prefix."""
    return select_recorded_id(
        ((source.session.session_id, source) for source in sessions), session_id
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
