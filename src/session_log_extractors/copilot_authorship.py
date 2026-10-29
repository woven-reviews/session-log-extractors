"""Interpret Copilot's recorded source evidence and editor context without I/O."""

from dataclasses import dataclass
from pathlib import PurePath
from typing import Literal, TypeAlias

from session_log_extractors.json_types import JsonValue, string_value
from session_log_extractors.session_model import (
    AuthorshipMarker,
    AuthorshipRange,
    AuthorshipSource,
    ContextAttachment,
    SourcePosition,
    SubmittedText,
    TextSelection,
    UserMessage,
)

RecordIndex: TypeAlias = dict[str, dict[str, JsonValue] | None]


def record_index(records: list[dict[str, JsonValue]]) -> RecordIndex:
    """Duplicate native IDs stay ambiguous rather than choosing a later record."""
    indexed: RecordIndex = {}
    for record in records:
        identity = string_value(record.get("id"))
        if identity:
            indexed[identity] = None if identity in indexed else record
    return indexed


def source_markers(
    record: dict[str, JsonValue], prefix: str = ""
) -> list[AuthorshipMarker]:
    markers = [AuthorshipMarker(field=prefix + "type", value=record.get("type"))]
    if prefix and "id" in record:
        markers.append(AuthorshipMarker(field=prefix + "id", value=record["id"]))
    if "parentId" in record:
        markers.append(
            AuthorshipMarker(field=prefix + "parentId", value=record["parentId"])
        )
    data = record.get("data")
    if isinstance(data, dict):
        for key in (
            "source",
            "isAutopilotContinuation",
            "kind",
            "role",
            "authority",
            "name",
            "trigger",
            "model",
            "apiCallId",
            "toolCallId",
            "isUserRequested",
        ):
            if key in data:
                markers.append(
                    AuthorshipMarker(field=f"{prefix}data.{key}", value=data[key])
                )
    return markers


def _original_source(
    data: dict[str, JsonValue], *, agent_scoped: bool
) -> AuthorshipSource:
    source = data.get("source")
    continuation = data.get("isAutopilotContinuation") is True
    if continuation:
        return "harness" if source in (None, "system") else "unknown"
    # The SDK documents omitted source as the runtime's ordinary-human default.
    # An agent-scoped record without source cannot establish human authorship.
    if "source" not in data:
        return "unknown" if agent_scoped else "candidate"
    if source == "user":
        return "candidate"
    if source == "system":
        return "harness"
    if isinstance(source, str) and source.startswith("agent-") and source[6:]:
        return "other_agent"
    return "unknown"


def submitted_text(record: dict[str, JsonValue]) -> SubmittedText | None:
    """Keep original input even when a recorded transformation replaces it."""
    data = record.get("data")
    if not isinstance(data, dict):
        return None
    text = string_value(data.get("content"))
    if text is None:
        return None
    return SubmittedText(
        text=text, authorship=_original_source(data, agent_scoped="agentId" in record)
    )


def _span(
    start: int, end: int, source: AuthorshipSource, markers: list[AuthorshipMarker]
) -> AuthorshipRange:
    return AuthorshipRange(start=start, end=end, source=source, markers=markers)


def user_content(
    record: dict[str, JsonValue], indexed: RecordIndex | None = None
) -> tuple[str, list[AuthorshipRange]]:
    data = record.get("data")
    data = data if isinstance(data, dict) else {}
    original = string_value(data.get("content"))
    transformed = string_value(data.get("transformedContent"))
    text = transformed if transformed is not None else original or ""
    markers = source_markers(record)
    if not text:
        return text, []
    source = _original_source(data, agent_scoped="agentId" in record)
    if transformed is None:
        return text, [_span(0, len(text), source, markers)]
    if original is None:
        source = "harness" if source == "harness" else "unknown"
        return text, [_span(0, len(text), source, markers)]
    if not original:
        return text, [_span(0, len(text), "harness", markers)]
    start = text.find(original)
    if start < 0 or text.find(original, start + 1) >= 0:
        linked = (
            _command_ranges(record, text, indexed, original)
            if source == "candidate"
            else None
        )
        return text, linked if linked is not None else [
            _span(0, len(text), "unknown", markers)
        ]
    end = start + len(original)
    ranges = []
    if start:
        ranges.append(_span(0, start, "harness", markers))
    ranges.append(_span(start, end, source, markers))
    if end < len(text):
        ranges.append(_span(end, len(text), "harness", markers))
    return text, _selection_ranges(text, ranges, data, (start, end))


def user_shell_output_source(
    message: UserMessage, record: dict[str, JsonValue]
) -> AuthorshipSource:
    """Called only after a unique, same-agent shell call-ID join is established."""
    data = record.get("data")
    if not isinstance(data, dict):
        return "unknown"
    if "isUserRequested" in data:
        return "candidate" if data["isUserRequested"] is True else "unknown"
    parent = string_value(record.get("parentId"))
    return "candidate" if parent and message.get("id") == parent else "unknown"


def _has_model_evidence(
    record: dict[str, JsonValue], data: dict[str, JsonValue]
) -> bool:
    if record.get("type") != "assistant.message":
        return False
    if "source" in data or data.get("role") not in (None, "assistant"):
        return False
    model = string_value(data.get("model"))
    call = string_value(data.get("apiCallId"))
    return bool(model and model != "<synthetic>" and call)


def assistant_authorship(record: dict[str, JsonValue]) -> list[AuthorshipRange]:
    """Native model and provider response ID establish a model contribution."""
    data = record.get("data")
    data = data if isinstance(data, dict) else {}
    text = string_value(data.get("content")) or ""
    if not text:
        return []
    source: AuthorshipSource = (
        "other_agent" if _has_model_evidence(record, data) else "unknown"
    )
    span = _span(0, len(text), source, source_markers(record))
    if "timestamp" in record:
        span["ts"] = string_value(record["timestamp"])
    return [span]


@dataclass(frozen=True)
class _UserCommand:
    name: str
    arguments: str
    body: str
    invocation: dict[str, JsonValue]


def _linked_invocation(
    record: dict[str, JsonValue], indexed: RecordIndex
) -> dict[str, JsonValue] | None:
    parent = string_value(record.get("parentId"))
    visited: set[str] = set()
    while parent and parent not in visited:
        visited.add(parent)
        ancestor = indexed.get(parent)
        if ancestor is None or ancestor.get("agentId") != record.get("agentId"):
            return None
        if ancestor.get("type") == "skill.invoked":
            return ancestor
        if ancestor.get("type") not in ("hook.start", "hook.end"):
            return None
        parent = string_value(ancestor.get("parentId"))
    return None


def _user_command(
    record: dict[str, JsonValue], indexed: RecordIndex | None, original: str
) -> _UserCommand | None:
    if indexed is None:
        return None
    invocation = _linked_invocation(record, indexed)
    if invocation is None:
        return None
    invoked = invocation.get("data")
    if not isinstance(invoked, dict):
        return None
    command, _, arguments = original.partition(" ")
    name = string_value(invoked.get("name"))
    body = string_value(invoked.get("content"))
    if invoked.get("trigger") != "user-invoked" or not name or not body:
        return None
    if command != "/" + name:
        return None
    return _UserCommand(name, arguments, body, invocation)


def _unique_bounds(text: str, body: str) -> tuple[int, int] | None:
    start = text.find(body)
    if not body or start < 0 or text.find(body, start + 1) >= 0:
        return None
    return start, start + len(body)


def _command_ranges(
    record: dict[str, JsonValue], text: str, indexed: RecordIndex | None, original: str
) -> list[AuthorshipRange] | None:
    command = _user_command(record, indexed, original)
    if command is None:
        return None
    notice = f'The user explicitly invoked the "/{command.name}" skill. Follow its instructions now.'
    invocation_bounds = _unique_bounds(text, notice)
    body_bounds = _unique_bounds(text, command.body)
    if (
        invocation_bounds is None
        or body_bounds is None
        or invocation_bounds[1] > body_bounds[0]
    ):
        return None
    windows = [invocation_bounds]
    if command.arguments:
        arguments = _unique_bounds(text, command.arguments)
        if arguments is None or arguments[0] < body_bounds[1]:
            return None
        windows.append(arguments)
    markers = source_markers(record) + source_markers(command.invocation, "invocation.")
    ranges = []
    cursor = 0
    for start, end in windows:
        if start > cursor:
            ranges.append(_span(cursor, start, "harness", markers))
        ranges.append(_span(start, end, "candidate", markers))
        cursor = end
    if cursor < len(text):
        ranges.append(_span(cursor, len(text), "harness", markers))
    return ranges


def _selection_ranges(
    text: str,
    ranges: list[AuthorshipRange],
    data: dict[str, JsonValue],
    original: tuple[int, int],
) -> list[AuthorshipRange]:
    attachments = data.get("attachments")
    if not isinstance(attachments, list):
        return ranges
    for index, item in enumerate(attachments):
        if not isinstance(item, dict) or item.get("type") != "selection":
            continue
        selected = string_value(item.get("text"))
        if not selected:
            continue
        start = text.find(selected)
        if start < 0 or text.find(selected, start + 1) >= 0:
            continue
        end = start + len(selected)
        if start < original[1] and original[0] < end:
            continue
        evidence = AuthorshipMarker(
            field=f"data.attachments[{index}].type", value="selection"
        )
        ranges = _overlay_selection(ranges, start, end, evidence)
    return ranges


def _overlay_selection(
    ranges: list[AuthorshipRange], start: int, end: int, evidence: AuthorshipMarker
) -> list[AuthorshipRange]:
    result = []
    for span in ranges:
        if (
            span["source"] != "harness"
            or not span["start"] <= start < end <= span["end"]
        ):
            result.append(span)
            continue
        markers = span.get("markers", [])
        if span["start"] < start:
            result.append(_span(span["start"], start, "harness", markers))
        result.append(_span(start, end, "ide", [*markers, evidence]))
        if end < span["end"]:
            result.append(_span(end, span["end"], "harness", markers))
    return result


def _position(data: dict[str, JsonValue]) -> SourcePosition:
    position = SourcePosition()
    remaining = dict(data)
    for key in ("line", "character"):
        value = data.get(key)
        if isinstance(value, int):
            if key == "line":
                position["line"] = value
            else:
                position["character"] = value
            remaining.pop(key)
    if remaining:
        position["unrecognized_fields"] = remaining
    return position


def _selection(data: dict[str, JsonValue]) -> TextSelection:
    selection = TextSelection()
    remaining = dict(data)
    for key in ("start", "end"):
        value = data.get(key)
        if isinstance(value, dict):
            if key == "start":
                selection["start"] = _position(value)
            else:
                selection["end"] = _position(value)
            remaining.pop(key)
    if remaining:
        selection["unrecognized_fields"] = remaining
    return selection


_TextField: TypeAlias = Literal[
    "kind",
    "display_name",
    "path",
    "file_path",
    "text",
    "mime_type",
    "media_type",
    "asset_id",
    "data_url",
    "data",
]
_IntegerField: TypeAlias = Literal["mention_index", "byte_length"]


def _context_attachment(item: dict[str, JsonValue]) -> ContextAttachment:
    attachment = ContextAttachment()
    remaining = dict(item)
    fields: tuple[tuple[str, _TextField], ...] = (
        ("type", "kind"),
        ("displayName", "display_name"),
        ("path", "path"),
        ("filePath", "file_path"),
        ("text", "text"),
        ("mimeType", "mime_type"),
        ("mediaType", "media_type"),
        ("assetId", "asset_id"),
        ("dataUrl", "data_url"),
        ("data", "data"),
    )
    for native, target in fields:
        text = string_value(item.get(native))
        if text is not None:
            attachment[target] = text
            remaining.pop(native)
    value = item.get("selection")
    if isinstance(value, dict):
        attachment["selection"] = _selection(value)
        remaining.pop("selection")
    integer_fields: tuple[tuple[str, _IntegerField], ...] = (
        ("mentionIndex", "mention_index"),
        ("byteLength", "byte_length"),
    )
    for native, numeric_target in integer_fields:
        index = item.get(native)
        if isinstance(index, int):
            attachment[numeric_target] = index
            remaining.pop(native)
    if "taggedFilesEntry" in item:
        attachment["tagged_files_entry"] = item["taggedFilesEntry"]
        remaining.pop("taggedFilesEntry")
    if remaining:
        attachment["unrecognized_fields"] = remaining
    return attachment


def _is_image(item: dict[str, JsonValue]) -> bool:
    mime = (
        string_value(item.get("mimeType")) or string_value(item.get("mediaType")) or ""
    )
    path = string_value(item.get("path")) or ""
    url = string_value(item.get("dataUrl")) or ""
    if mime:
        return mime.startswith("image/")
    return url.startswith("data:image/") or PurePath(path).suffix.lower() in (
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".bmp",
        ".svg",
    )


def context_attachments(data: dict[str, JsonValue]) -> list[ContextAttachment]:
    """Retain non-image context; images already have a canonical representation."""
    items = data.get("attachments")
    if not isinstance(items, list):
        return []
    return [
        _context_attachment(item)
        for item in items
        if isinstance(item, dict) and not _is_image(item)
    ]
