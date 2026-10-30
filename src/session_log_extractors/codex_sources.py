"""Codex filesystem discovery, transcript readers, and optional image assets."""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from session_log_extractors.json_types import JsonValue, parse_json

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
    parent_session_id: str | None = None
    history_start_ordinal: int | None = None

    @property
    def identifier(self) -> str:
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


def newest(paths: Iterable[Path]) -> Optional[Path]:
    paths = list(paths)
    if not paths:
        return None
    return max(paths, key=lambda p: p.stat().st_mtime)


def _load_records(path: Path) -> List[Tuple[int, Dict[str, JsonValue]]]:
    entries: List[Tuple[int, Dict[str, JsonValue]]] = []
    bad = 0
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = parse_json(line)
            except json.JSONDecodeError:
                bad += 1
                if bad <= 5:
                    print(
                        f"warning: skipping malformed JSON on line {lineno}",
                        file=sys.stderr,
                    )
                continue
            if isinstance(obj, dict):
                entries.append((lineno, obj))
            else:
                bad += 1
    if bad > 5:
        print(f"warning: skipped {bad} malformed/blank lines total", file=sys.stderr)
    return entries


def session_id_from_file(path: Path) -> Optional[str]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for _, line in zip(range(50), fh, strict=False):
                try:
                    obj = parse_json(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                if obj.get("type") == "session_meta":
                    sid = _payload(obj).get("id")
                    return sid if isinstance(sid, str) else None
    except OSError:
        return None
    return None


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
                if obj.get("type") == "session_meta":
                    cwd = payload.get("cwd")
                    if isinstance(cwd, str) and cwd:
                        return cwd
                if obj.get("type") == "turn_context":
                    cwd = payload.get("cwd")
                    if isinstance(cwd, str) and cwd:
                        return cwd
    except OSError:
        return None
    return None


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


def _cwd_matches(
    transcript_cwd: Optional[str], cwd: Path, strict: bool = False
) -> bool:
    if not transcript_cwd:
        return False
    rec = Path(transcript_cwd).expanduser()
    if strict:
        return rec.resolve() == cwd.resolve()
    return _paths_overlap(rec, cwd)


def matching_transcripts(root: Path, cwd: Path, strict: bool = False) -> List[Path]:
    """Return every Codex transcript whose recorded cwd overlaps ``cwd``.

    In ``strict`` mode the recorded cwd must equal ``cwd`` exactly (no
    ancestor/descendant overlap).
    """
    return [
        p
        for p in _jsonl_files(root)
        if _cwd_matches(session_cwd_from_file(p), cwd, strict=strict)
    ]


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
) -> Path:
    """Pick a Codex transcript.

    Precedence:
      1. --transcript PATH
      2. positional selector that is an existing file
      3. positional selector as filename/session-id prefix
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
    files = _jsonl_files(root)

    if selector:
        return _select_prefix(files, selector)

    cwd = Path.cwd()
    cwd_matches = [
        p for p in files if _cwd_matches(session_cwd_from_file(p), cwd, strict=strict)
    ]
    picked = newest(cwd_matches)
    if picked is not None:
        return picked

    if strict:
        raise FileNotFoundError(
            f"no Codex transcript for {cwd} under {root} "
            "(--strict: cwd-overlap and newest-anywhere fallback disabled)"
        )

    picked = newest(files)
    if picked is not None:
        return picked

    raise FileNotFoundError(
        f"could not locate any Codex transcript (looked under {root}); "
        "pass --transcript PATH"
    )


def _select_prefix(files: List[Path], selector: str) -> Path:
    matches: List[Path] = []
    for p in files:
        sid = session_id_from_file(p)
        if p.stem.startswith(selector) or (sid and sid.startswith(selector)):
            matches.append(p)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        joined = "\n  ".join(str(p) for p in matches)
        raise FileNotFoundError(
            f"session selector {selector!r} is ambiguous; matches:\n  {joined}"
        )
    raise FileNotFoundError(f"no Codex transcript matching {selector!r}")


def load_entries(path: Path) -> List[Dict[str, JsonValue]]:
    """Load records with the same diagnostics as a complete source read."""
    return [entry for _, entry in _load_records(path)]


def read_source(path: Path) -> CodexSource:
    """Read once, retaining metadata's historical physical-line windows."""
    records = _load_records(path)
    source = CodexSource(path, [entry for _, entry in records])
    id_seen = False
    for line, entry in records:
        payload = _payload(entry)
        if line <= 50 and entry.get("type") == "session_meta" and not id_seen:
            sid = payload.get("id")
            source.session_id = sid if isinstance(sid, str) else None
            parent = payload.get("parent_thread_id")
            source.parent_session_id = parent if isinstance(parent, str) else None
            boundary = payload.get("subagent_history_start_ordinal")
            if isinstance(boundary, int) and not isinstance(boundary, bool):
                source.history_start_ordinal = boundary
            id_seen = True
        if line <= 80 and entry.get("type") in ("session_meta", "turn_context"):
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
