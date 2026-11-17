"""Codex filesystem discovery, transcript readers, and optional image assets."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from session_log_extractors.codex_protocol import (
    SESSION_META as SESSION_META,
)
from session_log_extractors.codex_protocol import TURN_CONTEXT
from session_log_extractors.codex_rollouts import (
    CodexRolloutContext,
    CodexRolloutKind,
    recorded_ordinal,
    rollout_context,
)
from session_log_extractors.json_types import JsonValue, parse_json
from session_log_extractors.jsonl_sources import load_records
from session_log_extractors.session_selection import (
    DirectorySelection,
    read_recorded_id,
    select_recorded_id,
    unique_paths,
)
from session_log_extractors.session_selection import (
    newest as newest,
)

CODEX_SESSIONS_ROOT = Path.home() / ".codex" / "sessions"

ImageInputs = Dict[str, Optional[Tuple[bytes, str]]]


@dataclass
class CodexSource:
    """Loaded native records and metadata; safe to interpret without I/O."""

    path: Path
    entries: List[Dict[str, JsonValue]]
    session_id: Optional[str] = None
    cwd: Optional[str] = None
    image_inputs: ImageInputs = field(default_factory=dict)
    rollout: CodexRolloutContext = field(default_factory=CodexRolloutContext)

    @property
    def parent_session_id(self) -> str | None:
        return self.rollout.parent_session_id

    @property
    def history_start_ordinal(self) -> int | None:
        return self.rollout.history_start_ordinal

    @property
    def identifier(self) -> str:
        """Filename label; the recorded session ID remains unchanged."""
        raw = self.session_id or self.path.stem
        ident = re.sub(r"[^A-Za-z0-9_.-]+", "-", raw).strip("-")
        return ident or self.path.stem


def _payload(entry: Dict[str, JsonValue]) -> Dict[str, JsonValue]:
    payload = entry.get("payload")
    return payload if isinstance(payload, dict) else {}


def _jsonl_files(root: Path) -> List[Path]:
    if not root.is_dir():
        return []
    return [p for p in root.rglob("*.jsonl") if p.is_file()]


def _session_metadata_from_file(path: Path) -> Dict[str, JsonValue]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for _, line in zip(range(50), fh, strict=False):
                try:
                    obj = parse_json(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                if obj.get("type") == SESSION_META:
                    return _payload(obj)
    except OSError:
        return {}
    return {}


def session_id_from_file(path: Path) -> Optional[str]:
    sid = _session_metadata_from_file(path).get("id")
    return sid if isinstance(sid, str) else None


def _automatic_transcripts(root: Path) -> List[Path]:
    return [
        path
        for path in _jsonl_files(root)
        if rollout_context(_session_metadata_from_file(path)).kind
        != CodexRolloutKind.GUARDIAN
    ]


def session_cwd_from_file(path: Path) -> Optional[str]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for _, line in zip(range(80), fh, strict=False):
                try:
                    obj = parse_json(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                payload = _payload(obj)
                if obj.get("type") == SESSION_META:
                    cwd = payload.get("cwd")
                    if isinstance(cwd, str) and cwd:
                        return cwd
                if obj.get("type") == TURN_CONTEXT:
                    cwd = payload.get("cwd")
                    if isinstance(cwd, str) and cwd:
                        return cwd
    except OSError:
        return None
    return None


def matching_transcripts(
    root: Path,
    cwd: Path,
    strict: bool = False,
    *,
    selection: DirectorySelection | None = None,
) -> List[Path]:
    """Return every Codex transcript whose recorded cwd overlaps ``cwd``.

    In ``strict`` mode the recorded cwd must equal ``cwd`` exactly (no
    ancestor/descendant overlap).
    """
    selection = selection or DirectorySelection(cwd, strict)
    files = _automatic_transcripts(root)
    if selection.repository is not None:
        files = unique_paths(files)
    return [p for p in files if selection.matches(session_cwd_from_file(p), p)]


def output_identifier(transcript: Path) -> str:
    """Stable filename identifier for one transcript.

    Prefer Codex's session id from ``session_meta``. Fall back to the rollout
    filename stem when older logs do not carry an id.
    """
    return CodexSource(transcript, [], session_id_from_file(transcript)).identifier


def select_transcript(
    selector: Optional[str],
    transcript: Optional[str],
    sessions_root: Optional[str],
    strict: bool = False,
    *,
    repository: bool = False,
) -> Path:
    """Pick a Codex transcript.

    Precedence:
      1. --transcript PATH
      2. positional selector that is an existing file
      3. positional selector as case-sensitive recorded ID (unique exact, then prefix)
      4. newest transcript whose recorded cwd overlaps the current cwd
      5. newest transcript anywhere under ~/.codex/sessions

    In ``strict`` mode step 4 requires an exact cwd match and step 5 (newest
    anywhere) is disabled, so only sessions started in the current directory
    are ever selected.
    """
    if transcript:
        p = Path(transcript).expanduser()
        if not p.is_file():
            raise FileNotFoundError(f"--transcript path does not exist: {p}")
        return p

    if selector:
        p = Path(selector).expanduser()
        if p.is_file():
            return p

    root = Path(sessions_root).expanduser() if sessions_root else CODEX_SESSIONS_ROOT
    if selector:
        return _select_prefix(_jsonl_files(root), selector)

    cwd = Path.cwd()
    selection = DirectorySelection.from_cwd(cwd, strict, repository=repository)
    cwd_matches = matching_transcripts(root, cwd, selection=selection)
    picked = newest(cwd_matches)
    if picked is not None:
        return picked

    if repository:
        raise FileNotFoundError(f"no Codex transcripts in Git repository from {cwd}")
    if strict:
        raise FileNotFoundError(
            f"no Codex transcript for {cwd} under {root} "
            "(--strict: cwd-overlap and newest-anywhere fallback disabled)"
        )

    picked = newest(_automatic_transcripts(root))
    if picked is not None:
        return picked

    raise FileNotFoundError(
        f"could not locate any Codex transcript (looked under {root}); "
        "pass --transcript PATH"
    )


class _CodexSelectionIdentity:
    """Recognize owned metadata while excluding recorded inherited parent history."""

    def __init__(self) -> None:
        self._metadata_seen = False
        self._parent_id: str | None = None
        self._history_boundary: int | None = None

    def identify(self, entry: Dict[str, JsonValue]) -> str | None:
        if entry.get("type") != SESSION_META:
            return None
        payload = _payload(entry)
        session_id = payload.get("id")
        if not self._metadata_seen:
            self._metadata_seen = True
            context = rollout_context(payload)
            self._parent_id = context.parent_session_id
            self._history_boundary = context.history_start_ordinal
        elif self._is_inherited_parent(entry, session_id):
            return None
        return session_id if isinstance(session_id, str) else None

    def _is_inherited_parent(
        self, entry: Dict[str, JsonValue], session_id: JsonValue
    ) -> bool:
        ordinal = recorded_ordinal(entry.get("ordinal"))
        return (
            self._parent_id is not None
            and session_id == self._parent_id
            and self._history_boundary is not None
            and ordinal is not None
            and 0 < ordinal < self._history_boundary
        )


def _select_prefix(files: List[Path], selector: str) -> Path:
    candidates = []
    for path in files:
        session_id = read_recorded_id(path, _CodexSelectionIdentity().identify)
        if session_id is not None:
            candidates.append((session_id, path))
    selected = select_recorded_id(candidates, selector)
    if selected is not None:
        return selected
    raise FileNotFoundError(f"no Codex transcript matching {selector!r}")


def load_entries(path: Path) -> List[Dict[str, JsonValue]]:
    """Load records with the same diagnostics as a complete source read."""
    return [entry for _, entry in load_records(path)]


def read_source(path: Path) -> CodexSource:
    """Read once, retaining metadata's historical physical-line windows."""
    records = load_records(path)
    source = CodexSource(path, [entry for _, entry in records])
    id_seen = False
    for line, entry in records:
        payload = _payload(entry)
        if line <= 50 and entry.get("type") == SESSION_META and not id_seen:
            sid = payload.get("id")
            source.session_id = sid if isinstance(sid, str) else None
            source.rollout = rollout_context(payload)
            id_seen = True
        if line <= 80 and entry.get("type") in (SESSION_META, TURN_CONTEXT):
            cwd = payload.get("cwd")
            if source.cwd is None and isinstance(cwd, str) and cwd:
                source.cwd = cwd
    return source


def read_image(path_value: str) -> Optional[Tuple[bytes, str]]:
    """Missing or unreadable assets become unavailable image inputs."""
    path = Path(path_value).expanduser()
    try:
        return path.read_bytes(), path.suffix.lstrip(".") or "img"
    except OSError:
        return None


def load_image_inputs(refs: Iterable[Dict[str, JsonValue]]) -> ImageInputs:
    """Snapshot referenced path assets, retaining unresolved paths too."""
    images: ImageInputs = {}
    for ref in refs:
        path = ref.get("path")
        if ref.get("type") == "path" and isinstance(path, str) and path not in images:
            images[path] = read_image(path)
    return images
