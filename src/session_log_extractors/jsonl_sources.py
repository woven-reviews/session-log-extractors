"""Read object-based JSONL transcripts with bounded source diagnostics."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Dict, Iterable, List

from session_log_extractors.json_types import JsonValue, parse_json

_WARNING_LIMIT = 5


def load_jsonl_objects(path: Path) -> List[Dict[str, JsonValue]]:
    """Load object records; reject nonblank input with no valid objects.

    Diagnose skipped malformed/non-object records on stderr, at most five
    individually plus a total. Blank input is a valid empty source.
    """
    with path.open("r", encoding="utf-8", errors="replace") as lines:
        return parse_jsonl_objects(lines, str(path))


def parse_jsonl_objects(
    lines: Iterable[str], source: str
) -> List[Dict[str, JsonValue]]:
    """Retain ordered objects and diagnose malformed records at their source."""
    entries: List[Dict[str, JsonValue]] = []
    bad = 0
    for lineno, line in enumerate(lines, 1):
        line = line.strip()
        if not line:
            continue
        try:
            obj = parse_json(line)
        except json.JSONDecodeError:
            obj = None
            reason = "malformed JSON"
        else:
            reason = "non-object JSON"
        if isinstance(obj, dict):
            entries.append(obj)
            continue
        bad += 1
        if bad <= _WARNING_LIMIT:
            print(
                f"warning: {source}: skipping {reason} on line {lineno}",
                file=sys.stderr,
            )
    if bad > _WARNING_LIMIT:
        print(
            f"warning: {source}: skipped {bad} malformed/non-object records total",
            file=sys.stderr,
        )
    if bad and not entries:
        raise ValueError(
            f"no parseable JSON entries found in {source}; "
            "expected at least one JSON object on a nonblank line"
        )
    return entries
