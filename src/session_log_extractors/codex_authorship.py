"""Project recorded Codex content provenance onto unchanged message text."""

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


def _image_delimiters(content: list[JsonValue]) -> set[int]:
    """Native image blocks establish framing context, not human-typed words."""
    delimiters: set[int] = set()
    for index in range(len(content) - 2):
        opening, image, closing = content[index : index + 3]
        text = _text(opening)
        if (
            text is not None
            and text.startswith("<image ")
            and isinstance(image, dict)
            and image.get("type") == "input_image"
            and _text(closing) == "</image>"
        ):
            delimiters.update((index, index + 2))
    return delimiters


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
    content = payload.get("content")
    parts = _text_parts(content) if isinstance(content, list) else []
    if (
        not isinstance(content, list)
        or not parts
        or "\n".join(part for _, _, part in parts) != text
    ):
        return [_authored_range(0, text, "unknown", base, timestamp)]
    metadata = _metadata(payload)
    delimiters = _image_delimiters(content)
    ranges: list[AuthorshipRange] = []
    offset = 0
    for index, block, part in parts:
        kind, provenance, markers = _evidence(metadata, index, len(content))
        if isinstance(block, dict):
            markers = _source_markers(block, f"payload.content[{index}]") + markers
        if part:
            source = "unknown" if index in delimiters else _source(kind, provenance)
            ranges.append(
                _authored_range(offset, part, source, base + markers, timestamp)
            )
        # Existing text serialization joins blocks with newlines. Those inserted
        # separators stay uncovered (unknown), never attributed to a speaker.
        offset += len(part) + 1
    return ranges
