"""Shared recorded-ID selection rules and streaming identity scan mechanics."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TypeVar

from session_log_extractors.json_types import JsonValue, parse_json

Source = TypeVar("Source")


def select_recorded_id(
    candidates: Iterable[tuple[str, Source]], selector: str
) -> Source | None:
    """Prefer a unique exact ID, otherwise require a unique literal prefix."""
    exact = []
    prefixes = []
    for session_id, source in candidates:
        if session_id == selector:
            exact.append((session_id, source))
        elif session_id.startswith(selector):
            prefixes.append((session_id, source))
    matches = exact or prefixes
    if len(matches) > 1:
        ids = ", ".join(session_id for session_id, _ in matches)
        raise FileNotFoundError(
            f"session selector {selector!r} is ambiguous; recorded IDs: {ids}; "
            "use a unique ID/prefix or resolve duplicate recorded IDs"
        )
    return matches[0][1] if matches else None


def read_recorded_id(
    path: Path, identify: Callable[[dict[str, JsonValue]], str | None]
) -> str | None:
    """Scan all records without retaining content or choosing conflicting IDs.

    Invalid JSON and non-object lines cannot provide identity evidence. Unreadable
    files remain unavailable to ID discovery, as with missing ID evidence.
    """
    session_id = None
    try:
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    record = parse_json(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                recorded_id = identify(record)
                if not recorded_id:
                    continue
                if session_id is not None and recorded_id != session_id:
                    raise ValueError(
                        f"conflicting recorded session IDs in {path}: "
                        f"{session_id!r}, {recorded_id!r}; "
                        "inspect the transcript or select an explicit file path"
                    )
                session_id = recorded_id
    except OSError:
        return None
    return session_id
