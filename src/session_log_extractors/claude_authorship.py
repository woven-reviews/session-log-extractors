"""Claude-native provenance and exact text boundaries, independent of role."""

from session_log_extractors.json_types import JsonValue, string_value
from session_log_extractors.session_model import (
    AuthorshipMarker,
    AuthorshipRange,
    AuthorshipSource,
)


def _origin(record: dict[str, JsonValue]) -> dict[str, JsonValue]:
    origin = record.get("origin")
    return origin if isinstance(origin, dict) else {}


def _markers(record: dict[str, JsonValue], prefix: str = "") -> list[AuthorshipMarker]:
    markers = [
        AuthorshipMarker(field=prefix + field, value=record[field])
        for field in (
            "type",
            "isMeta",
            "isCompactSummary",
            "sourceToolUseID",
            "promptSource",
            "turnOrigin",
            "userType",
            "source_uuid",
            "delivery_id",
            "commandMode",
            "humanTurn",
        )
        if field in record
    ]
    origin = _origin(record)
    markers.extend(
        AuthorshipMarker(field=prefix + "origin." + field, value=origin[field])
        for field in (
            "kind",
            "from",
            "senderTaskId",
            "name",
            "handback",
            "producer",
            "runId",
        )
        if field in origin
    )
    return markers


def _span(
    record: dict[str, JsonValue],
    text: str,
    source: AuthorshipSource,
    markers: list[AuthorshipMarker],
) -> AuthorshipRange:
    span = AuthorshipRange(start=0, end=len(text), source=source, markers=markers)
    if "timestamp" in record:
        span["ts"] = string_value(record["timestamp"])
    return span


def _split_content(
    text: str,
    body: JsonValue,
    content: AuthorshipRange,
    wrapper: AuthorshipRange,
) -> list[AuthorshipRange]:
    """Recorded original content must occur exactly once; no tag heuristics."""
    start = text.find(body) if isinstance(body, str) and body else -1
    if start < 0 or not isinstance(body, str) or text.find(body, start + 1) >= 0:
        unknown = wrapper.copy()
        unknown["source"] = "unknown"
        return [unknown]
    end = start + len(body)
    spans = []
    if start:
        prefix = wrapper.copy()
        prefix["end"] = start
        spans.append(prefix)
    content["start"], content["end"] = start, end
    spans.append(content)
    if end < len(text):
        suffix = wrapper.copy()
        suffix["start"] = end
        spans.append(suffix)
    return spans


def _user_authorship(entry: dict[str, JsonValue], text: str) -> list[AuthorshipRange]:
    """Classify nonempty text using native origins before broad injection flags."""
    markers = _markers(entry)
    origin = _origin(entry)
    if origin.get("kind") == "peer":
        return _split_content(
            text,
            origin.get("body"),
            _span(entry, text, "other_agent", markers),
            _span(entry, text, "harness", markers),
        )
    if (
        entry.get("isMeta") is True
        or entry.get("isCompactSummary") is True
        or origin.get("kind") == "task-notification"
        or entry.get("promptSource") == "system"
        or entry.get("turnOrigin") in ("task_notification", "scheduled")
    ):
        source: AuthorshipSource = "harness"
    elif (
        origin.get("kind") == "human"
        and entry.get("promptSource") in ("typed", "queued")
        and entry.get("turnOrigin") == "human"
    ):
        source = "candidate"
    else:
        source = "unknown"
    return [_span(entry, text, source, markers)]


def _queued_authorship(
    entry: dict[str, JsonValue],
    text: str,
    attachment: dict[str, JsonValue],
) -> list[AuthorshipRange]:
    """Separate queued human input from its recorded rendered wrapper."""
    markers = _markers(entry) + _markers(attachment, "attachment.")
    wrapper = _span(entry, text, "harness", markers)
    if (
        _origin(attachment).get("kind") == "human"
        and attachment.get("humanTurn") is True
    ):
        return _split_content(
            text,
            attachment.get("prompt"),
            _span(attachment, text, "candidate", markers),
            wrapper,
        )
    wrapper["source"] = "unknown"
    return [wrapper]
