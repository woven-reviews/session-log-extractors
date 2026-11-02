"""Project recorded Codex content provenance onto unchanged message text."""

import re

from session_log_extractors.json_types import JsonValue
from session_log_extractors.session_model import (
    AuthorshipMarker,
    AuthorshipRange,
    AuthorshipSource,
)

_META = "payload.internal_chat_message_metadata_passthrough"
_KIND_SOURCES: dict[str, AuthorshipSource] = {
    "user.text": "candidate",
    "agents_md.instructions": "harness",
    "environments.environment_context": "harness",
    "plugins.recommendations": "harness",
    "skills.selected_skill_instructions": "harness",
    "generic.turn_aborted": "harness",
    "images.preparation_error": "harness",
    "daemon_recovery.internal_context": "harness",
    "shell.user_command": "harness",
}
_PROVENANCE_SOURCES: dict[str, AuthorshipSource] = {
    "user": "candidate",
    "harness": "harness",
    "agents_md": "harness",
    "skills": "harness",
}


def _source_markers(record: dict[str, JsonValue], path: str) -> list[AuthorshipMarker]:
    return [
        AuthorshipMarker(field=f"{path}.{key}", value=record[key])
        for key in ("type", "source")
        if key in record
    ]


def _text(block: JsonValue) -> str | None:
    if isinstance(block, str):
        return block
    if isinstance(block, dict):
        for key in ("text", "output_text"):
            value = block.get(key)
            if isinstance(value, str):
                return value
    return None


def _metadata(payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
    value = payload.get("internal_chat_message_metadata_passthrough")
    return value if isinstance(value, dict) else {}


def _text_parts(content: list[JsonValue]) -> list[tuple[int, JsonValue, str]]:
    parts: list[tuple[int, JsonValue, str]] = []
    for index, block in enumerate(content):
        text = _text(block)
        if text is not None:
            parts.append((index, block, text))
    return parts


def _authored_range(
    start: int,
    text: str,
    source: AuthorshipSource,
    markers: list[AuthorshipMarker],
    timestamp: str | None,
) -> AuthorshipRange:
    span = AuthorshipRange(
        start=start, end=start + len(text), source=source, markers=markers
    )
    if timestamp is not None:
        span["ts"] = timestamp
    return span


def _evidence(
    metadata: dict[str, JsonValue], index: int, count: int
) -> tuple[JsonValue, JsonValue, list[AuthorshipMarker]]:
    values: list[JsonValue] = []
    markers: list[AuthorshipMarker] = []
    for key in ("content_item_kinds", "content_item_metadata"):
        value = metadata.get(key)
        path = f"{_META}.{key}"
        if isinstance(value, list) and len(value) == count:
            value = value[index]
            path += f"[{index}]"
            if key == "content_item_metadata" and isinstance(value, dict):
                if "provenance" in value:
                    value = value["provenance"]
                    path += ".provenance"
        if key in metadata:
            markers.append(AuthorshipMarker(field=path, value=value))
        values.append(value)
    return values[0], values[1], markers


def _source(kind: JsonValue, provenance: JsonValue) -> AuthorshipSource:
    """Require understood, consistent evidence; role never supplies an author."""
    kind_source = _KIND_SOURCES.get(kind) if isinstance(kind, str) else None
    if kind is not None and kind_source is None:
        return "unknown"
    provenance_source = None
    if provenance is not None:
        native_type = provenance.get("type") if isinstance(provenance, dict) else None
        provenance_source = (
            _PROVENANCE_SOURCES.get(native_type)
            if isinstance(native_type, str)
            else None
        )
        if provenance_source is None:
            return "unknown"
    if kind_source and provenance_source and kind_source != provenance_source:
        return "unknown"
    return kind_source or provenance_source or "unknown"


def _block_source(
    payload: dict[str, JsonValue],
    block: JsonValue,
    kind: JsonValue,
    provenance: JsonValue,
) -> AuthorshipSource:
    if payload.get("type") != "agent_message":
        source = _source(kind, provenance)
        if source != "unknown":
            return source
        # The recorded assistant/output_text pair identifies a runtime response.
        # Codex's content-kind label "unknown" does not erase that actor evidence.
        if (
            payload.get("type") == "message"
            and payload.get("role") == "assistant"
            and isinstance(block, dict)
            and block.get("type") == "output_text"
            and kind in (None, "unknown")
            and provenance is None
        ):
            return "harness"
        return "unknown"
    author, recipient = payload.get("author"), payload.get("recipient")
    if (
        isinstance(author, str)
        and author
        and isinstance(recipient, str)
        and recipient
        and recipient != author
        and isinstance(provenance, dict)
        and provenance.get("type") == "tool"
        and provenance.get("namespace") == "collaboration"
    ):
        return "other_agent"
    return "unknown"


def _shell_activity_range(
    span: AuthorshipRange, text: str, kind: JsonValue
) -> AuthorshipRange:
    """Attribute command and outcome only from the native shell-activity schema."""
    if kind != "shell.user_command" or span["source"] != "harness":
        return span
    match = re.match(
        r"\A<user_shell_command>\s*<command>\n(.*?)\n</command>\s*<result>",
        text,
        re.DOTALL,
    )
    if match is None:
        span["source"] = "unknown"
        return span
    # Command and result are parts of the same native candidate-run activity.
    # Preserve the complete record so filtering does not hide its outcome.
    span["source"] = "candidate"
    return span


def message_authorship(
    payload: dict[str, JsonValue],
    text: str,
    timestamp: str | None,
    record_type: str,
) -> list[AuthorshipRange]:
    """Retain block boundaries, exact markers, and recorded content timestamps."""
    if not text:
        return []
    base = [AuthorshipMarker(field="type", value=record_type)]
    base.extend(_source_markers(payload, "payload"))
    if payload.get("type") == "agent_message":
        for key in ("author", "recipient"):
            if key in payload:
                base.append(
                    AuthorshipMarker(field=f"payload.{key}", value=payload[key])
                )
    content = payload.get("content")
    parts = _text_parts(content) if isinstance(content, list) else []
    if (
        not isinstance(content, list)
        or not parts
        or "\n".join(part for _, _, part in parts) != text
    ):
        return [_authored_range(0, text, "unknown", base, timestamp)]
    metadata = _metadata(payload)
    ranges: list[AuthorshipRange] = []
    offset = 0
    for index, block, part in parts:
        kind, provenance, markers = _evidence(metadata, index, len(content))
        if isinstance(block, dict):
            markers = _source_markers(block, f"payload.content[{index}]") + markers
        if part:
            source = _block_source(payload, block, kind, provenance)
            span = _authored_range(offset, part, source, base + markers, timestamp)
            ranges.append(_shell_activity_range(span, part, kind))
        # Existing text serialization joins blocks with newlines. Those inserted
        # separators stay uncovered (unknown), never attributed to a speaker.
        offset += len(part) + 1
    return ranges
