"""Claude-native provenance and exact text boundaries, independent of role."""

import re
from typing import Literal, TypeAlias

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
            "entrypoint",
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


def _native_user_text(entry: dict[str, JsonValue]) -> str | None:
    """Read standalone string content from a native user-message record."""
    message = entry.get("message")
    if (
        entry.get("type") != "user"
        or not isinstance(message, dict)
        or message.get("role") != "user"
    ):
        return None
    content = message.get("content")
    return content if isinstance(content, str) else None


def _is_user_shell_input(entry: dict[str, JsonValue]) -> bool:
    """Recognize the captured standalone shell-mode input, including its wrapper."""
    content = _native_user_text(entry)
    return (
        content is not None
        and content.startswith("<bash-input>")
        and content.endswith("</bash-input>")
        and bool(content[len("<bash-input>") : -len("</bash-input>")].strip())
    )


_COMMAND_INVOCATION = re.compile(
    r"\s*<command-message>[^<]*</command-message>\s*"
    r"<command-name>/[^<\s]+</command-name>"
    r"(?:\s*<command-args>.*?</command-args>)?\s*",
    re.DOTALL,
)


def _is_human_command_invocation(entry: dict[str, JsonValue]) -> bool:
    content = _native_user_text(entry)
    return (
        content is not None
        and _origin(entry).get("kind") == "human"
        and entry.get("turnOrigin") == "human"
        and _COMMAND_INVOCATION.fullmatch(content) is not None
    )


_LOCAL_CONTROL_INVOCATION = re.compile(
    r"\s*<command-name>/(?P<command>model|compact)</command-name>\s*"
    r"<command-message>(?P=command)</command-message>\s*"
    r"<command-args>.*?</command-args>\s*",
    re.DOTALL,
)


def _local_control_name(entry: dict[str, JsonValue]) -> str | None:
    content = _native_user_text(entry)
    match = (
        _LOCAL_CONTROL_INVOCATION.fullmatch(content) if content is not None else None
    )
    return match.group("command") if match is not None else None


def _is_model_invocation(entry: dict[str, JsonValue]) -> bool:
    return _local_control_name(entry) == "model"


def _is_compact_invocation(entry: dict[str, JsonValue]) -> bool:
    return entry.get("entrypoint") == "cli" and (
        _native_user_text(entry) == "/compact"
        or _local_control_name(entry) == "compact"
    )


def _user_action_marker(
    entry: dict[str, JsonValue],
    text: str,
) -> AuthorshipMarker | None:
    if _is_user_interruption(entry):
        return AuthorshipMarker(field="message.content[0].text", value=text)
    if (
        _is_user_shell_input(entry)
        or _is_human_command_invocation(entry)
        or _is_model_invocation(entry)
        or _is_compact_invocation(entry)
    ):
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
        # and command input, preserving their native notices and wrappers.
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
_LOCAL_COMMAND_OUTPUT = re.compile(
    r"<local-command-stdout>.*?</local-command-stdout>", re.DOTALL
)
_OutputKind: TypeAlias = Literal["shell", "local"]


def _input_output_kind(entry: dict[str, JsonValue]) -> _OutputKind | None:
    if _is_user_shell_input(entry):
        return "shell"
    if _is_model_invocation(entry):
        return "local"
    return None


def _output_kind(entry: dict[str, JsonValue]) -> _OutputKind | None:
    content = _native_user_text(entry)
    if content is None:
        return None
    if _SHELL_OUTPUT.fullmatch(content) is not None:
        return "shell"
    if _LOCAL_COMMAND_OUTPUT.fullmatch(content) is not None:
        return "local"
    return None


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


def _link_candidate_output(
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


def _attribute_command_outputs(
    entries: list[dict[str, JsonValue]],
    messages: list[Message],
) -> None:
    """Join supported command results to candidate inputs by recorded parent ID."""
    indexed = _unique_messages_by_id(messages)
    command_ids: dict[str, _OutputKind] = {}
    for entry in entries:
        record_id = entry.get("uuid")
        kind = _input_output_kind(entry)
        if kind is not None and isinstance(record_id, str):
            command_ids[record_id] = kind
    for entry in entries:
        kind = _output_kind(entry)
        if kind is None:
            continue
        output_id = string_value(entry.get("uuid")) or ""
        parent_id = string_value(entry.get("parentUuid")) or ""
        output = indexed.get(output_id)
        parent = indexed.get(parent_id) if command_ids.get(parent_id) == kind else None
        if output is not None and parent is not None:
            _link_candidate_output(entry, output, parent)
