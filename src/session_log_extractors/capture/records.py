"""Structure the capture code relies on: JSONL parsing and the Copilot database layout.

Nothing here decides what is private. That is the job of the filter plug-in
(``session_filter``); this module only reads records and names the tables and columns
the Copilot extractor needs.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any


def entries(text: str) -> Iterator[dict[str, Any]]:
    """Each line of a JSONL ``text`` that parses to an object; anything else is skipped."""
    for raw in text.splitlines():
        try:
            value = json.loads(raw)
        except ValueError:
            continue
        if isinstance(value, dict):
            yield value


COPILOT_DB_COLUMNS = {
    "sessions": (
        "id",
        "cwd",
        "repository",
        "branch",
        "summary",
        "created_at",
        "updated_at",
    ),
    "turns": (
        "session_id",
        "turn_index",
        "user_message",
        "assistant_response",
        "timestamp",
    ),
    "checkpoints": (
        "session_id",
        "checkpoint_number",
        "title",
        "overview",
        "created_at",
    ),
    "session_files": (
        "session_id",
        "file_path",
        "tool_name",
        "turn_index",
        "first_seen_at",
    ),
    "session_refs": ("session_id", "ref_type", "ref_value", "turn_index", "created_at"),
    "attachments": ("session_id", "display_name", "path", "type"),
    # The extractor reads the models a session used from here.
    "assistant_usage_events": ("id", "session_id", "turn_index", "model"),
}
