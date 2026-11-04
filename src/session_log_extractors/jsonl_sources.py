"""Shared JSONL object reading, physical line numbers, and diagnostics."""

import json
import sys
from pathlib import Path

from session_log_extractors.json_types import JsonValue, parse_json


def load_records(path: Path) -> list[tuple[int, dict[str, JsonValue]]]:
    """Load object records; reject nonblank input with no valid objects.

    Diagnose skipped malformed/non-object records on stderr, at most five
    individually plus a total. Blank input is a valid empty source.
    """
    entries: list[tuple[int, dict[str, JsonValue]]] = []
    bad = 0
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for lineno, line in enumerate(fh, 1):
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
                entries.append((lineno, obj))
                continue
            bad += 1
            if bad <= 5:
                print(
                    f"warning: {path}: skipping {reason} on line {lineno}",
                    file=sys.stderr,
                )
    if bad > 5:
        print(
            f"warning: {path}: skipped {bad} malformed/non-object records total",
            file=sys.stderr,
        )
    if bad and not entries:
        raise ValueError(
            f"no parseable JSON entries found in {path}; "
            "expected at least one JSON object on a nonblank line"
        )
    return entries
