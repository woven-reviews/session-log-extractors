"""Filesystem readers and recorded inputs for Claude interpretation.

Images are inline source blocks in ``entries``; no external asset lookup is
needed. ``path`` retains transcript identity and its sidecar relationship.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List


@dataclass
class ClaudeSource:
    """Recorded entries and optional sidecar metadata, ready to interpret."""

    path: Path
    entries: List[Dict[str, Any]]
    metadata: Dict[str, Any] = field(default_factory=dict)


def load_entries(path: Path) -> List[Dict[str, Any]]:
    """Load a JSONL transcript, skipping blank/malformed lines.

    A single bad line never aborts the run; it is reported to stderr (rate
    limited) and skipped.
    """
    entries: List[Dict[str, Any]] = []
    bad = 0
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                bad += 1
                if bad <= 5:
                    print(
                        f"warning: skipping malformed JSON on line {lineno}",
                        file=sys.stderr,
                    )
                continue
            if isinstance(obj, dict):
                entries.append(obj)
            else:
                bad += 1
    if bad > 5:
        print(
            f"warning: skipped {bad} malformed/blank lines total",
            file=sys.stderr,
        )
    return entries


def _read_metadata(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        metadata = json.loads(path.read_text("utf-8"))
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
