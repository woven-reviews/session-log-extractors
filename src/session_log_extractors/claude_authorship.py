"""Claude-native provenance and exact text boundaries, independent of role."""

import re

from session_log_extractors.json_types import JsonValue, string_value
from session_log_extractors.session_model import (
    AuthorshipMarker,
    AuthorshipRange,
    AuthorshipSource,
    Message,
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


def _is_user_interruption(entry: dict[str, JsonValue]) -> bool:
    """Recognize the captured notice of a deliberate user control action."""
    message = entry.get("message")
    if not isinstance(message, dict):
        return False
    return (
        entry.get("type") == "user"
        and message.get("role") == "user"
        and message.get("content")
        == [{"type": "text", "text": "[Request interrupted by user for tool use]"}]
    )


def _is_user_shell_input(entry: dict[str, JsonValue]) -> bool:
    """Recognize the captured standalone shell-mode input, including its wrapper."""
    message = entry.get("message")
    if not isinstance(message, dict):
        return False
    content = message.get("content")
    if not isinstance(content, str):
        return False
    return (
        entry.get("type") == "user"
        and message.get("role") == "user"
        and content.startswith("<bash-input>")
        and content.endswith("</bash-input>")
        and bool(content[len("<bash-input>") : -len("</bash-input>")].strip())
    )


def _user_action_marker(
    entry: dict[str, JsonValue],
    text: str,
) -> AuthorshipMarker | None:
    if _is_user_interruption(entry):
        return AuthorshipMarker(field="message.content[0].text", value=text)
    if _is_user_shell_input(entry):
        return AuthorshipMarker(field="message.content", value=text)
    return None


def _is_human_input(entry: dict[str, JsonValue]) -> bool:
    return (
        _origin(entry).get("kind") == "human"
        and entry.get("promptSource") in ("typed", "queued")
        and entry.get("turnOrigin") == "human"
    )


def _user_authorship(entry: dict[str, JsonValue], text: str) -> list[AuthorshipRange]:
    """Classify nonempty text using native origins before broad injection flags."""
    markers = _markers(entry)
    origin = _origin(entry)
    user_action = _user_action_marker(entry, text)
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
    elif user_action is not None:
        # Candidate-visible activity includes deliberate user control actions
        # and shell input, preserving their native notices and wrappers.
        source = "candidate"
        markers.append(user_action)
    elif _is_human_input(entry):
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


_SHELL_OUTPUT = re.compile(
    r"<bash-stdout>.*?</bash-stdout><bash-stderr>.*?</bash-stderr>", re.DOTALL
)


def _is_user_shell_output(entry: dict[str, JsonValue]) -> bool:
    message = entry.get("message")
    if not isinstance(message, dict):
        return False
    content = message.get("content")
    return (
        entry.get("type") == "user"
        and message.get("role") == "user"
        and isinstance(content, str)
        and _SHELL_OUTPUT.fullmatch(content) is not None
    )


def _unique_messages_by_id(messages: list[Message]) -> dict[str, Message | None]:
    indexed: dict[str, Message | None] = {}
    for message in messages:
        record_id = message.get("id")
        if isinstance(record_id, str) and record_id:
            indexed[record_id] = None if record_id in indexed else message
    return indexed


def _has_single_source(message: Message, source: AuthorshipSource) -> bool:
    spans = message.get("authorship", [])
    return len(spans) == 1 and spans[0]["source"] == source


def _link_candidate_shell_output(
    entry: dict[str, JsonValue],
    output: Message,
    parent: Message,
) -> None:
    if not _has_single_source(parent, "candidate") or not _has_single_source(
        output, "unknown"
    ):
        return
    text = output.get("text")
    if isinstance(text, str):
        markers = _markers(entry) + [
            AuthorshipMarker(field="parentUuid", value=entry["parentUuid"])
        ]
        output["authorship"] = [_span(entry, text, "candidate", markers)]


def _attribute_shell_outputs(
    entries: list[dict[str, JsonValue]],
    messages: list[Message],
) -> None:
    """Join user-shell output to its recorded candidate input, preserving order."""
    indexed = _unique_messages_by_id(messages)
    shell_ids = set()
    for entry in entries:
        record_id = entry.get("uuid")
        if _is_user_shell_input(entry) and isinstance(record_id, str):
            shell_ids.add(record_id)
    for entry in entries:
        if _is_user_shell_output(entry):
            output_id = string_value(entry.get("uuid")) or ""
            parent_id = string_value(entry.get("parentUuid")) or ""
            output = indexed.get(output_id)
            parent = indexed.get(parent_id) if parent_id in shell_ids else None
            if output is not None and parent is not None:
                _link_candidate_shell_output(entry, output, parent)
