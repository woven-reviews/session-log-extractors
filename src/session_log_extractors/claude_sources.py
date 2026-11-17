"""Filesystem readers and recorded inputs for Claude interpretation.

Images are inline source blocks in ``entries``; no external asset lookup is
needed. ``path`` retains transcript identity and its sidecar relationship.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

from session_log_extractors.json_types import JsonValue, parse_json
from session_log_extractors.jsonl_sources import load_jsonl_objects
from session_log_extractors.session_selection import (
    RepositoryBoundary,
    read_recorded_id,
    select_recorded_id,
    unique_paths,
)

load_entries = load_jsonl_objects


def session_cwd(entries: Iterable[Dict[str, JsonValue]]) -> str | None:
    """Use the first nonempty recorded session directory, never tool inputs."""
    for entry in entries:
        cwd = entry.get("cwd")
        if isinstance(cwd, str) and cwd:
            return cwd
    return None


def _recorded_objects(lines: Iterable[str]) -> Iterator[Dict[str, JsonValue]]:
    for line in lines:
        try:
            entry = parse_json(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            yield entry


def recorded_session_cwd(path: Path) -> str | None:
    """Read only until the first recorded directory, leaving the rest unparsed.

    Discovery inspects every store, so malformed records are diagnosed when a
    selected source is fully read rather than for each unrelated candidate.
    """
    with path.open("r", encoding="utf-8", errors="replace") as lines:
        return session_cwd(_recorded_objects(lines))


def repository_transcripts(
    projects_root: Path,
    boundary: RepositoryBoundary,
    *,
    project_dir: Path | None = None,
) -> list[Path]:
    """Read directory evidence across stores, excluding nested subagent logs.

    An explicit project store limits discovery, but its encoded name never
    establishes ownership. Resolve aliases before reading to export each source
    once even when multiple stores point at the same transcript.
    """
    pattern = "*.jsonl" if project_dir is not None else "*/*.jsonl"
    root = project_dir if project_dir is not None else projects_root
    matches = []
    for path in unique_paths(sorted(root.glob(pattern))):
        if not path.is_file():
            continue
        try:
            recorded_cwd = recorded_session_cwd(path)
        except OSError as exc:
            print(f"warning: skipping Claude source {path}: {exc}", file=sys.stderr)
            continue
        if boundary.matches(recorded_cwd, path):
            matches.append(path)
    return matches


@dataclass
class ClaudeSource:
    """Recorded entries and optional sidecar metadata, ready to interpret."""

    path: Path
    entries: List[Dict[str, JsonValue]]
    metadata: Dict[str, JsonValue] = field(default_factory=dict)


def _read_metadata(path: Path) -> Dict[str, JsonValue]:
    if not path.is_file():
        return {}
    try:
        metadata = parse_json(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return metadata if isinstance(metadata, dict) else {}


def read_source(path: Path, *, read_metadata: bool = False) -> ClaudeSource:
    """Read entries and, for nonempty sidecars only, optional agent metadata.

    Transcript read errors propagate. Missing, unreadable or invalid metadata
    falls back to an empty mapping. Main JSON extraction never loads metadata.
    """
    entries = load_entries(path)
    metadata = (
        _read_metadata(path.with_suffix(".meta.json"))
        if read_metadata and entries
        else {}
    )
    return ClaudeSource(path, entries, metadata)


def read_subagent_sources(transcript: Path) -> List[ClaudeSource]:
    """Read sibling sidecars in filename order, retaining their source paths."""
    directory = transcript.parent / transcript.stem / "subagents"
    if not directory.is_dir():
        return []
    return [
        read_source(path, read_metadata=True)
        for path in sorted(directory.iterdir())
        if path.is_file() and path.suffix == ".jsonl"
    ]


def _recorded_id(entry: Dict[str, JsonValue]) -> str | None:
    session_id = entry.get("sessionId")
    return session_id if isinstance(session_id, str) else None


def select_project_session(selector: str, project: Path) -> Path | None:
    """Select by recorded ID among top-level project transcripts only."""
    if not project.is_dir():
        return None
    candidates = []
    # Child transcripts share their parent's sessionId. Recursing into their
    # directories would incorrectly make a single parent session ambiguous.
    for path in sorted(project.glob("*.jsonl")):
        if path.is_file():
            session_id = read_recorded_id(path, _recorded_id)
            if session_id is not None:
                candidates.append((session_id, path))
    return select_recorded_id(candidates, selector)
