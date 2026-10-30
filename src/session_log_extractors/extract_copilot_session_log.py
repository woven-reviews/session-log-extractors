#!/usr/bin/env python3
"""Extract GitHub Copilot CLI session transcripts into JSON conversation envelopes.

GitHub Copilot CLI records sessions in a SQLite database at:

    ~/.copilot/session-store.db

The database contains tables for sessions, turns, checkpoints, files, and refs.
This script queries that database to extract session conversations organized by
repository.

This script is mechanical: no LLM, no summarization, and no network access.
User prose is reproduced verbatim; assistant responses are reproduced verbatim.

Copilot records the full permission lifecycle in the per-session events log
(``~/.copilot/session-state/<id>/events.jsonl``), so both approvals and denials
are retained as session records (the tool asked about, the decision, and feedback the
human typed when denying) -- unlike the Claude/Codex logs, where only denials are
observable.

Examples
--------
Every transcript for the current repository, one file per session (the default
when no session selector is given), written into the project root::

    uv run --locked python scripts/extract_copilot_session_log.py

The most recent transcript for the current repository, written to stdout::

    uv run --locked python scripts/extract_copilot_session_log.py --output -

A specific session id, to stdout::

    uv run --locked python scripts/extract_copilot_session_log.py abc123... --output -
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from session_log_extractors import copilot_sources
from session_log_extractors.copilot_interactions import (
    apply_configured_refusal,
    apply_human_response,
    apply_permission_refusal,
    questions_from_schema,
    subagent_summaries,
)
from session_log_extractors.copilot_sources import CopilotSource, ImageInputs
from session_log_extractors.extractor_contract import Extractor
from session_log_extractors.json_types import JsonValue
from session_log_extractors.json_types import json_value as _json_value
from session_log_extractors.json_types import string_value as _string
from session_log_extractors.session_log_envelope import (
    DEFAULT_TOOL_RESULT_MAX_BYTES,
    Envelope,
    assistant_event,
    export_session,
    image_from_base64,
    image_from_bytes,
    media_type_for_ext,
    raw_filename,
    unavailable_image,
    user_event,
    warn_if_oversized,
    write_envelope,
)
from session_log_extractors.session_model import (
    AssistantMessage,
    FileActivity,
    FileActivityProvenance,
    Image,
    Message,
    NativeIdentity,
    PermissionDecision,
    Session,
    ToolCall,
    ToolResult,
    UserMessage,
)
from session_log_extractors.tool_inputs.copilot import input_schema

_NOISE_TAG_BLOCK = re.compile(
    r"<(system-reminder|system-notification|local-command-stdout|local-command-stderr|"
    r"command-stdout|command-stderr|command-name|command-message|command-args|"
    r"task-notification|bash-stdout|bash-stderr)>.*?"
    r"</\1>",
    re.DOTALL | re.IGNORECASE,
)
_NOISE_TAG_LOOSE = re.compile(
    r"</?(system-reminder|system-notification|local-command-stdout|local-command-stderr|"
    r"command-stdout|command-stderr|command-name|command-message|command-args|"
    r"task-notification|bash-stdout|bash-stderr)\b[^>]*/?>",
    re.IGNORECASE,
)
_BASH_INPUT = re.compile(r"<bash-input>(.*?)</bash-input>", re.DOTALL | re.IGNORECASE)
_COPILOT_IMAGE_REF = re.compile(r"\[image:\s*([^\]]+?)\s*\]", re.IGNORECASE)
_DATA_IMAGE_URL = re.compile(r"^data:(image/[A-Za-z0-9.+-]+);base64,(.*)$", re.DOTALL)
_IMAGE_EXT = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/bmp": "bmp",
    "image/svg+xml": "svg",
}


def _decode_arguments(arguments: JsonValue) -> JsonValue:
    if isinstance(arguments, str):
        try:
            return _json_value(json.loads(arguments))
        except json.JSONDecodeError:
            return arguments
    return arguments


def skill_refs_from_call(
    name: str, arguments: object
) -> List[Dict[str, Optional[str]]]:
    """Return skill names and definition paths evidenced by a tool call."""
    args = _decode_arguments(arguments) if isinstance(arguments, str) else arguments
    refs: List[Dict[str, Optional[str]]] = []

    def add(value: str, path: Optional[str] = None) -> None:
        for ref in refs:
            if ref["name"] == value:
                if path and not ref.get("path"):
                    ref["path"] = path
                return
        refs.append({"name": value, "path": path})

    short_name = name.rsplit(".", 1)[-1].lower()
    if short_name in ("skill", "read_skill") and isinstance(args, dict):
        value = args.get("skill") or args.get("name") or args.get("package")
        if isinstance(value, str) and value:
            add(value)

    try:
        blob = (
            json.dumps(args, ensure_ascii=False) if not isinstance(args, str) else args
        )
    except (TypeError, ValueError):
        blob = ""
    skill_path = re.compile(r"(?P<path>[^\s\"']*/(?P<skill>[^/\s\"']+)/SKILL\.md)")
    for match in skill_path.finditer(blob):
        skill = match.group("skill")
        if skill:
            add(skill, match.group("path"))
    return refs


def clean_user_text(text: str) -> str:
    """Strip harness-injected wrappers while keeping the human's prose."""
    if not text:
        return ""
    cleaned = _NOISE_TAG_BLOCK.sub("", text)
    cleaned = _NOISE_TAG_LOOSE.sub("", cleaned)
    cleaned = re.sub(r"\n[ \t]*\n[ \t]*\n+", "\n\n", cleaned)
    return cleaned.strip()


def _parse_ts(ts: Optional[str]) -> Optional[datetime]:
    if not ts or not isinstance(ts, str):
        return None
    try:
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    # SQLite writes naive timestamps; treat them as UTC so they compare with aware ones.
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def format_timestamp(ts: Optional[str]) -> str:
    """Render as 'YYYY-MM-DD HH:MM:SS UTC' -- always UTC, never machine-local."""
    if not ts or not isinstance(ts, str):
        return "(no timestamp)"
    dt = _parse_ts(ts)
    if dt is None:
        return ts
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


def date_only(ts: Optional[str]) -> str:
    formatted = format_timestamp(ts)
    if formatted == "(no timestamp)":
        return "unknown"
    return formatted.split(" ")[0]


def _binary_assets(
    events: List[Dict[str, JsonValue]],
) -> Dict[str, tuple[str, JsonValue]]:
    assets: Dict[str, tuple[str, JsonValue]] = {}
    for obj in events:
        asset = obj.get("data")
        if (
            obj.get("type") == "session.binary_asset"
            and isinstance(asset, dict)
            and isinstance(asset.get("assetId"), str)
            and isinstance(asset.get("data"), str)
        ):
            assets[str(asset["assetId"])] = (str(asset["data"]), asset.get("mimeType"))
    return assets


def _tool_result_images(
    result: JsonValue, records: list[dict[str, JsonValue]]
) -> list[Image]:
    if not isinstance(result, dict):
        return []
    blocks = result.get("binaryResultsForLlm")
    if not isinstance(blocks, list):
        return []
    assets = _binary_assets(records)
    images = []
    for block in blocks:
        if not isinstance(block, dict) or block.get("type") != "image":
            continue
        asset_id = _string(block.get("assetId"))
        if asset_id is not None and asset_id in assets:
            data, media_type = assets[asset_id]
            images.append(image_from_base64(data, _string(media_type)))
        else:
            images.append(unavailable_image(asset_id or "missing image asset ID"))
    return images


def _state_attachment(
    item: Dict[str, JsonValue],
    timestamp: JsonValue,
    assets: Dict[str, tuple[str, JsonValue]],
) -> Dict[str, JsonValue]:
    rec = {"timestamp": timestamp}
    for source, target in (
        ("displayName", "display_name"),
        ("path", "path"),
        ("mimeType", "type"),
    ):
        value = item.get(source)
        rec[target] = value if isinstance(value, str) else ""
    for source, target in (
        ("dataUrl", "data_url"),
        ("data", "data"),
        ("mediaType", "media_type"),
    ):
        if isinstance(item.get(source), str):
            rec[target] = item[source]
    asset_id = item.get("assetId")
    if "data" not in rec and isinstance(asset_id, str) and asset_id in assets:
        rec["data"], media_type = assets[asset_id]
        # An asset overrides the attachment's media type, including a missing one.
        rec.pop("media_type", None)
        if isinstance(media_type, str):
            rec["media_type"] = media_type
    return rec


def _attachments_from_events(
    events: List[Dict[str, JsonValue]],
) -> List[Dict[str, JsonValue]]:
    assets = _binary_assets(events)
    attachments: List[Dict[str, JsonValue]] = []
    for obj in events:
        data = obj.get("data")
        if obj.get("type") != "user.message" or not isinstance(data, dict):
            continue
        items = data.get("attachments")
        if isinstance(items, list):
            attachments.extend(
                _state_attachment(item, obj.get("timestamp"), assets)
                for item in items
                if isinstance(item, dict)
            )
    return attachments


def get_state_attachments(session_id: str) -> List[Dict[str, JsonValue]]:
    """Load state attachments for callers needing only this evidence."""
    return _attachments_from_events(copilot_sources.load_state_records(session_id))


def _permission_request(
    record: dict[str, JsonValue], data: dict[str, JsonValue]
) -> PermissionDecision | None:
    request_id = _string(data.get("requestId"))
    if request_id is None:
        return None
    request = data.get("permissionRequest")
    request = request if isinstance(request, dict) else {}
    description = (
        request.get("fullCommandText")
        or request.get("intention")
        or request.get("kind")
    )
    return PermissionDecision(
        tool=_string(description) or "tool",
        request_id=request_id,
        status="pending",
        native_decision=None,
        feedback="",
        ts=_string(record.get("timestamp")),
    )


def _permission_completion(
    record: dict[str, JsonValue],
    data: dict[str, JsonValue],
    request: PermissionDecision | None,
) -> PermissionDecision:
    result = data.get("result")
    result = result if isinstance(result, dict) else {}
    timestamp = _string(record.get("timestamp"))
    if not timestamp and request is not None:
        timestamp = request["ts"]
    return PermissionDecision(
        tool=request["tool"] if request is not None else "tool",
        request_id=_string(data.get("requestId")),
        status="completed",
        native_decision=_string(result.get("kind")),
        feedback=_string(result.get("feedback")) or "",
        ts=timestamp,
    )


def _permissions_from_events(
    events: list[dict[str, JsonValue]],
) -> list[PermissionDecision]:
    """Join permission records by request ID without interpreting native outcomes."""
    requests: dict[str, PermissionDecision] = {}
    completed_ids: set[str] = set()
    decisions: list[PermissionDecision] = []
    for record in events:
        data = record.get("data")
        if not isinstance(data, dict):
            continue
        if record.get("type") == "permission.requested":
            request = _permission_request(record, data)
            request_id = _string(data.get("requestId"))
            if request is not None and request_id is not None:
                requests[request_id] = request
        elif record.get("type") == "permission.completed":
            request_id = _string(data.get("requestId"))
            request = requests.get(request_id) if request_id is not None else None
            decisions.append(_permission_completion(record, data, request))
            if request_id is not None:
                completed_ids.add(request_id)
    decisions.extend(
        request
        for request_id, request in requests.items()
        if request_id not in completed_ids
    )
    return sorted(decisions, key=lambda decision: decision["ts"] or "")


def get_state_permissions(session_id: str) -> list[PermissionDecision]:
    """Read permission lifecycles, preserving native labels without inferred owners."""
    return _permissions_from_events(copilot_sources.load_state_records(session_id))


def _skills_from_events(
    events: List[Dict[str, JsonValue]],
) -> List[Dict[str, JsonValue]]:
    """Extract Copilot's first-class ``skill.invoked`` events."""
    skills: List[Dict[str, JsonValue]] = []
    for obj in events:
        if not isinstance(obj, dict) or obj.get("type") != "skill.invoked":
            continue
        data = obj.get("data")
        if not isinstance(data, dict):
            continue
        name = data.get("name")
        if isinstance(name, str) and name:
            path = data.get("path")
            skills.append(
                {
                    "name": name,
                    "path": path if isinstance(path, str) else None,
                    "timestamp": obj.get("timestamp"),
                }
            )
    return skills


def get_state_skills(session_id: str) -> List[Dict[str, JsonValue]]:
    """Read invoked skills from the per-session events log, if present."""
    return _skills_from_events(copilot_sources.load_state_records(session_id))


def _result_text(result: JsonValue) -> str:
    """Flatten a ``tool.execution_complete`` result to plain text."""
    if isinstance(result, dict):
        for key in ("detailedContent", "content"):
            val = result.get(key)
            if isinstance(val, str) and val:
                return val
        return ""
    if isinstance(result, str):
        return result
    if result is None:
        return ""
    try:
        return json.dumps(result, ensure_ascii=False)
    except (TypeError, ValueError):
        return ""


def _attachment_lookup(
    attachments: List[Dict[str, JsonValue]],
) -> Dict[str, Dict[str, str]]:
    lookup: Dict[str, Dict[str, str]] = {}
    for item in attachments:
        raw_display = item.get("display_name")
        raw_path = item.get("path")
        has_path = isinstance(raw_path, str) and bool(raw_path.strip())
        has_data_url = isinstance(item.get("data_url"), str)
        has_data = isinstance(item.get("data"), str)
        if not has_path and not has_data_url and not has_data:
            continue

        type_value = item.get("type")
        rec: Dict[str, str] = {
            "type": type_value if isinstance(type_value, str) else ""
        }
        if has_path and isinstance(raw_path, str):
            rec["path"] = raw_path
        if has_data_url and isinstance(item.get("data_url"), str):
            rec["data_url"] = str(item["data_url"])
        if has_data and isinstance(item.get("data"), str):
            rec["data"] = str(item["data"])
            media_type = item.get("media_type")
            if isinstance(media_type, str):
                rec["media_type"] = media_type

        if isinstance(raw_display, str) and raw_display.strip():
            lookup.setdefault(raw_display.strip(), rec)

        if has_path and isinstance(raw_path, str):
            lookup.setdefault(Path(raw_path).name, rec)
    return lookup


def envelope_images(
    refs: List[Dict[str, str]], image_inputs: ImageInputs
) -> List[Image]:
    """Resolve Copilot image refs to inline base64 entries.

    Copilot stores image payloads outside DB turns. Image tokens and timestamped
    event attachments both become refs; missing sources remain visible as
    unavailable images.
    """
    images: List[Image] = []
    for ref in refs:
        decoded = _inline_image_bytes(ref)
        if decoded is None and not _has_inline_image(ref):
            raw = image_inputs.get(ref.get("path", ""))
            if raw is not None:
                decoded = (raw, _path_image_ext(ref))
        if decoded is None:
            images.append(unavailable_image(ref.get("name") or ref.get("path")))
            continue
        raw, ext = decoded
        images.append(image_from_bytes(raw, media_type_for_ext(ext)))
    return images


def _inline_image_bytes(ref: Dict[str, str]) -> Optional[tuple[bytes, str]]:
    data_url = ref.get("data_url")
    if isinstance(data_url, str) and data_url:
        match = _DATA_IMAGE_URL.match(data_url)
        if match:
            media_type, data = match.groups()
            try:
                raw = base64.b64decode(data, validate=False)
            except (ValueError, TypeError):
                return None
            return raw, _IMAGE_EXT.get(media_type, "img")

    data = ref.get("data")
    if isinstance(data, str) and data:
        try:
            raw = base64.b64decode(data, validate=False)
        except (ValueError, TypeError):
            return None
        media_type = ref.get("media_type") or ref.get("type")
        ext = (
            _IMAGE_EXT.get(media_type, "img") if isinstance(media_type, str) else "img"
        )
        return raw, ext

    return None


def _has_inline_image(ref: Dict[str, str]) -> bool:
    data_url = ref.get("data_url")
    return bool(
        isinstance(data_url, str)
        and _DATA_IMAGE_URL.match(data_url)
        or isinstance(ref.get("data"), str)
        and ref["data"]
    )


def _path_image_ext(ref: Dict[str, str]) -> str:
    ext = Path(ref["path"]).suffix.lstrip(".")
    if ext:
        return ext
    guessed = ref.get("type", "")
    return guessed.split("/")[-1] if "/" in guessed else "img"


def output_identifier(session: Dict[str, JsonValue]) -> str:
    """Generate a short identifier for output filenames."""
    session_id = session.get("id", "unknown")
    if not isinstance(session_id, str):
        raise TypeError("Copilot session id must be a string")
    # Use first 8 chars of session id
    short_id = session_id[:8] if len(session_id) >= 8 else session_id

    date = date_only(_string(session.get("created_at")))
    if date and date != "unknown":
        return f"{date}_{short_id}"
    return short_id


def _unique_identifier(base: str, used: set[str]) -> str:
    ident = base
    suffix = 2
    while ident in used:
        ident = f"{base}-{suffix}"
        suffix += 1
    used.add(ident)
    return ident


@dataclass(frozen=True)
class CopilotRequest:
    """Selected metadata and a connection whose lifetime belongs to the caller."""

    connection: sqlite3.Connection
    session: dict[str, JsonValue]


def _identity(
    record: dict[str, JsonValue], data: dict[str, JsonValue]
) -> NativeIdentity:
    identity = NativeIdentity()
    for source, target in (("id", "id"), ("parentId", "parent_id")):
        value = _string(record.get(source))
        if value is not None:
            if target == "id":
                identity["id"] = value
            else:
                identity["parent_id"] = value
    if isinstance(data.get("messageId"), str):
        identity["message_id"] = str(data["messageId"])
    if isinstance(data.get("originatingMessageId"), str):
        identity["originating_message_id"] = str(data["originatingMessageId"])
    if isinstance(data.get("interactionId"), str):
        identity["interaction_id"] = str(data["interactionId"])
    if isinstance(data.get("turnId"), str):
        identity["turn_id"] = str(data["turnId"])
    agent_id = _string(record.get("agentId"))
    if agent_id is not None:
        identity["agent_id"] = agent_id
    return identity


def _apply_identity(message: Message | ToolResult, identity: NativeIdentity) -> None:
    """Copy only the named identity fields into the typed message union."""
    if "agent_id" in identity:
        message["agent_id"] = identity["agent_id"]
    if "id" in identity:
        message["id"] = identity["id"]
    if "parent_id" in identity:
        message["parent_id"] = identity["parent_id"]
    if "message_id" in identity:
        message["message_id"] = identity["message_id"]
    if "originating_message_id" in identity:
        message["originating_message_id"] = identity["originating_message_id"]
    if "interaction_id" in identity:
        message["interaction_id"] = identity["interaction_id"]
    if "turn_id" in identity:
        message["turn_id"] = identity["turn_id"]


def _image_refs(
    text: str, attachments: list[dict[str, JsonValue]], *, explicit: bool = True
) -> list[dict[str, str]]:
    """Resolve legacy tokens and explicit attachments within their recorded owner."""
    lookup = _attachment_lookup(attachments)
    refs = [
        {"name": name.strip(), **lookup.get(name.strip(), {})}
        for name in _COPILOT_IMAGE_REF.findall(text)
        if name.strip()
    ]
    for attachment in attachments if explicit else []:
        ref = _attachment_ref(attachment)
        if ref is not None and not any(_same_image(ref, other) for other in refs):
            refs.append(ref)
    return refs


def _attachment_ref(attachment: dict[str, JsonValue]) -> dict[str, str] | None:
    path = _string(attachment.get("path")) or ""
    name = _string(attachment.get("display_name")) or Path(path).name
    mime = attachment.get("type") or attachment.get("media_type")
    data_url = attachment.get("data_url")
    if isinstance(mime, str) and mime and not mime.startswith("image/"):
        return None
    has_image_type = isinstance(mime, str) and mime.startswith("image/")
    has_image_extension = bool(media_type_for_ext(Path(path).suffix))
    has_image_data_url = isinstance(data_url, str) and bool(
        _DATA_IMAGE_URL.match(data_url)
    )
    if not name or not (has_image_type or has_image_extension or has_image_data_url):
        return None
    ref = {"name": name}
    for key in ("path", "type", "data_url", "data", "media_type"):
        value = attachment.get(key)
        if isinstance(value, str) and value:
            ref[key] = value
    return ref


def _same_image(left: dict[str, str], right: dict[str, str]) -> bool:
    def source(ref: dict[str, str]) -> str | None:
        return ref.get("path") or ref.get("data_url") or ref.get("data")

    a, b = source(left), source(right)
    return a == b if a and b else left["name"] == right["name"]


def _user_message(
    text: str, timestamp: str | None, refs: list[dict[str, str]], source: CopilotSource
) -> UserMessage:
    message = user_event(
        0, timestamp, clean_user_text(text), envelope_images(refs, source.image_inputs)
    )
    commands = [
        command.strip() for command in _BASH_INPUT.findall(text) if command.strip()
    ]
    if commands:
        message["shell_command"] = "\n".join(commands)
    return message


def _new_call(name: str, arguments: JsonValue) -> ToolCall:
    payload = _decode_arguments(arguments)
    return ToolCall(
        name=name, input=payload, input_schema=input_schema(name, payload), result=None
    )


class _CopilotSessionBuilder:
    """Assemble one session, joining tool activity by recorded identities."""

    def __init__(self, source: CopilotSource):
        self.source = source
        self.events: list[Message] = []
        self.calls: dict[str, ToolCall] = {}
        self.messages: dict[str, AssistantMessage] = {}
        self.unmatched: list[ToolResult] = []
        self.user_shells: dict[str, UserMessage] = {}

    def user_message(
        self, record: dict[str, JsonValue], data: dict[str, JsonValue]
    ) -> None:
        text = _string(data.get("content")) or ""
        refs = self.user_image_refs(record, data)
        # Native content is human input; only the DB fallback needs legacy cleanup.
        message = user_event(
            0,
            _string(record.get("timestamp")),
            text,
            envelope_images(refs, self.source.image_inputs),
        )
        if text.startswith("/"):
            command, _, arguments = text.partition(" ")
            message["command"] = command
            message["command_args"] = arguments or None
        _apply_identity(message, _identity(record, data))
        self.events.append(message)

    def user_image_refs(
        self, record: dict[str, JsonValue], data: dict[str, JsonValue]
    ) -> list[dict[str, str]]:
        # Only this message's attachments belong here; assets may occur anywhere.
        assets = _binary_assets(self.source.state_records)
        items = data.get("attachments")
        attachments = []
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    attachments.append(
                        _state_attachment(item, record.get("timestamp"), assets)
                    )

        text = _string(data.get("content")) or ""
        if attachments:
            return _image_refs(text, attachments)
        return _image_refs(text, self.source.attachments, explicit=False)

    def assistant_message(
        self, record: dict[str, JsonValue], data: dict[str, JsonValue]
    ) -> None:
        text = _string(data.get("content")) or ""
        message = assistant_event(0, _string(record.get("timestamp")), text)
        requests = data.get("toolRequests")
        if isinstance(requests, list):
            for request in requests:
                if isinstance(request, dict):
                    message.setdefault("tool_calls", []).append(self.request(request))
        _apply_identity(message, _identity(record, data))
        self.messages[str(record.get("id"))] = message
        self.events.append(message)

    def request(self, data: dict[str, JsonValue]) -> ToolCall:
        call = _new_call(
            _string(data.get("name")) or "tool", _json_value(data.get("arguments"))
        )
        if call["name"] == "ask_user":
            call["questions"] = questions_from_schema(call.get("input"))
        call_id = _string(data.get("toolCallId"))
        if call_id:
            call["id"] = call_id
            self.calls[call_id] = call
        return call

    def start(self, record: dict[str, JsonValue], data: dict[str, JsonValue]) -> None:
        call_id = _string(data.get("toolCallId"))
        if not call_id:
            return
        call = self.calls.get(call_id)
        if call is None:
            call = self.recover_request(record, data)
        call["execution"] = {
            **_identity(record, data),
            "ts": _string(record.get("timestamp")),
            "name": _string(data.get("toolName")) or "tool",
            "input": _decode_arguments(data.get("arguments")),
        }

    def recover_request(
        self, record: dict[str, JsonValue], data: dict[str, JsonValue]
    ) -> ToolCall:
        """Keep a start without a request, attaching only to a recorded parent."""
        call = self.request({**data, "name": data.get("toolName")})
        parent = self.messages.get(str(record.get("parentId")))
        if parent is not None:
            parent.setdefault("tool_calls", []).append(call)
            return call

        message = assistant_event(0, _string(record.get("timestamp")), "", [call])
        _apply_identity(message, _identity(record, {}))
        self.events.append(message)
        return call

    def complete(
        self, record: dict[str, JsonValue], data: dict[str, JsonValue]
    ) -> None:
        call_id = _string(data.get("toolCallId"))
        result: ToolResult = {
            "tool_call_id": call_id,
            "ts": _string(record.get("timestamp")),
            "content": _json_value(data.get("result")),
            "is_error": data.get("success") is False,
        }
        _apply_identity(result, _identity(record, data))
        success = data.get("success")
        shell = data.get("shellExecution")
        exit_code = shell.get("exitCode") if isinstance(shell, dict) else None
        if isinstance(exit_code, int) and not isinstance(exit_code, bool):
            result["is_error"] |= exit_code != 0
        if call_id in self.user_shells:
            self.complete_user_shell(self.user_shells[call_id], result, data)
            return
        call = self.calls.get(call_id) if call_id else None
        if call is None or "result_record" in call:
            self.unmatched.append(result)
            return
        if isinstance(success, bool):
            call["native_success"] = success
        if isinstance(exit_code, int) and not isinstance(exit_code, bool):
            call["shell_exit_code"] = exit_code
        images = _tool_result_images(data.get("result"), self.source.state_records)
        if images:
            call["result_images"] = images
        call["result_record"] = result
        call["result"] = _result_text(data.get("result"))
        if "error" in data:
            call["error"] = data["error"]
        apply_configured_refusal(call)
        if result["is_error"]:
            call["is_error"] = True

    def complete_user_shell(
        self, message: UserMessage, result: ToolResult, data: dict[str, JsonValue]
    ) -> None:
        if "shell_result" in message:
            self.unmatched.append(result)
            return
        message["shell_result"] = result
        command = message["shell_command"]
        output = _result_text(data.get("result"))
        message["text"] = command
        message["authorship"] = []
        if command:
            message["authorship"].append(
                {"start": 0, "end": len(command), "source": "candidate"}
            )
        if output:
            message["text"] = command + "\n" + output
            message["authorship"].append(
                {
                    "start": len(command) + 1,
                    "end": len(command) + 1 + len(output),
                    "source": "harness",
                }
            )

    def human_response(
        self, record: dict[str, JsonValue], data: dict[str, JsonValue]
    ) -> None:
        call = self.calls.get(str(data.get("toolCallId")))
        if call is not None:
            apply_human_response(call, data)

    def permission_completion(
        self, record: dict[str, JsonValue], data: dict[str, JsonValue]
    ) -> None:
        call = self.calls.get(str(data.get("toolCallId")))
        if call is not None:
            apply_permission_refusal(call, data)

    def user_shell(
        self, record: dict[str, JsonValue], data: dict[str, JsonValue]
    ) -> None:
        arguments = data.get("arguments")
        if not isinstance(arguments, dict):
            return
        command = _string(arguments.get("command"))
        call_id = _string(data.get("toolCallId"))
        if command is None or call_id is None:
            return
        message = user_event(0, _string(record.get("timestamp")), command)
        message["shell_command"] = command
        _apply_identity(message, _identity(record, data))
        self.user_shells[call_id] = message
        self.events.append(message)

    def build(self) -> list[Message]:
        handlers = {
            "user.message": self.user_message,
            "tool.user_requested": self.user_shell,
            "human_response.recorded": self.human_response,
            "permission.completed": self.permission_completion,
            "assistant.message": self.assistant_message,
            "tool.execution_start": self.start,
            "tool.execution_complete": self.complete,
        }
        for record in self.source.state_records:
            data = record.get("data")
            handler = handlers.get(str(record.get("type")))
            if handler is not None and isinstance(data, dict):
                handler(record, data)
        return self.events


def _database_events(
    source: CopilotSource, *, include_files: bool = True
) -> list[Message]:
    events: list[Message] = []
    for index, row in enumerate(source.turns):
        timestamp = _string(row.get("timestamp"))
        text = _string(row.get("user_message")) or ""
        user = _user_message(
            text,
            timestamp,
            _image_refs(text, source.attachments, explicit=False),
            source,
        )
        if user["text"] or user.get("images"):
            events.append(user)
        turn_index = row.get("turn_index", index)
        calls = []
        if include_files:
            calls = [
                _file_call(file)
                for file in source.files
                if file.get("turn_index") == turn_index
            ]
        text = (_string(row.get("assistant_response")) or "").strip()
        if text or calls:
            events.append(assistant_event(0, timestamp, text, calls))
    return events


def _file_call(file: dict[str, JsonValue]) -> ToolCall:
    """Translate the database's tool name and target without parsing display text."""
    name = str(file.get("tool_name") or "tool")
    target = file.get("file_path")
    return _new_call(name, {"target": str(target)} if target else {})


def _file_events(source: CopilotSource) -> list[Message]:
    """Keep DB file fallback as explicit activity without guessing native owners."""
    events: list[Message] = []
    for file in source.files:
        timestamp = _string(file.get("first_seen_at"))
        events.append(assistant_event(0, timestamp, "", [_file_call(file)]))
    return events


def read_source(
    conn: sqlite3.Connection, session: Dict[str, JsonValue]
) -> CopilotSource:
    """Snapshot native records and referenced external image bytes."""
    source = copilot_sources.read_source(conn, session)
    attachments = source.attachments + _attachments_from_events(source.state_records)
    source.image_inputs = copilot_sources.load_image_inputs(attachments)
    return source


def _file_activity(source: CopilotSource) -> list[FileActivity]:
    activity = []
    for file in source.files:
        index = file.get("turn_index")
        activity.append(
            FileActivity(
                file_path=_string(file.get("file_path")) or "",
                tool_name=_string(file.get("tool_name")) or "tool",
                provenance=FileActivityProvenance(
                    record_type="copilot.session_files",
                    turn_index=index if isinstance(index, int) else None,
                    first_seen_at=_string(file.get("first_seen_at")),
                ),
            )
        )
    return activity


def _apply_database_fallbacks(
    source: CopilotSource, builder: _CopilotSessionBuilder
) -> list[Message]:
    """Supplement partial native activity without guessing message ownership."""
    events = builder.events
    has_native_messages = any(
        record.get("type") in ("user.message", "assistant.message")
        for record in source.state_records
    )
    if not has_native_messages:
        events = _database_events(source, include_files=not builder.calls) + events
    if not builder.calls and events and any(event.get("id") for event in events):
        events.extend(_file_events(source))
    return events


def _session_skills(source: CopilotSource, calls: list[ToolCall]) -> list[str]:
    """Combine recorded skill invocations and tool evidence in encounter order."""
    skills = [str(skill["name"]) for skill in _skills_from_events(source.state_records)]
    for call in calls:
        for ref in skill_refs_from_call(call["name"], call.get("input")):
            if ref["name"] and ref["name"] not in skills:
                skills.append(ref["name"])
    return list(dict.fromkeys(skills))


def interpret_source(source: CopilotSource) -> Session | None:
    """Interpret complete supplied activity without I/O or export limits."""
    builder = _CopilotSessionBuilder(source)
    builder.build()
    conversation = _apply_database_fallbacks(source, builder)
    events = [
        event
        for event in conversation
        if event.get("text") or event.get("images") or event.get("tool_calls")
    ]
    if not events:
        return None
    for index, event in enumerate(events):
        event["i"] = index

    calls = [call for event in events for call in event.get("tool_calls") or []]
    unsupported_tools = {
        call["name"] for call in calls if call.get("input_schema") == "unsupported"
    }
    return Session(
        harness="copilot",
        session_id=output_identifier(source.session),
        events=events,
        cwd=_string(source.session.get("cwd")),
        started_at=_string(source.session.get("created_at")),
        models=source.models,
        skills_used=_session_skills(source, calls),
        subagent_count=len(subagent_summaries(source.state_records)),
        unmatched_tool_results=builder.unmatched,
        file_activity=_file_activity(source),
        unsupported_tools=sorted(unsupported_tools),
        permission_decisions=_permissions_from_events(source.state_records),
    )


class CopilotExtractor:
    """Public Copilot stage adapter; connection ownership stays with orchestration."""

    def read_source(self, request: CopilotRequest) -> CopilotSource:
        return read_source(request.connection, request.session)

    def interpret_source(self, source: CopilotSource) -> Session | None:
        return interpret_source(source)


EXTRACTOR: Extractor[CopilotRequest, CopilotSource] = CopilotExtractor()


def build_raw_envelope(
    source: CopilotSource,
    tool_result_max_bytes: int = DEFAULT_TOOL_RESULT_MAX_BYTES,
) -> Optional[Envelope]:
    """Apply output policy only at the shared exporter boundary."""
    session = EXTRACTOR.interpret_source(source)
    return None if session is None else export_session(session, tool_result_max_bytes)


def extract_session(
    conn: sqlite3.Connection,
    session: Dict[str, JsonValue],
    tool_result_max_bytes: int = DEFAULT_TOOL_RESULT_MAX_BYTES,
) -> Optional[Envelope]:
    """Run the public stages, preserving caller-owned connection lifetime."""
    source = EXTRACTOR.read_source(CopilotRequest(conn, session))
    return build_raw_envelope(source, tool_result_max_bytes)


def build_arg_parser(project_root: Optional[Path] = None) -> argparse.ArgumentParser:
    project_root = Path.cwd() if project_root is None else project_root
    parser = argparse.ArgumentParser(
        description="Extract GitHub Copilot CLI session logs as JSON.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "session",
        nargs="?",
        metavar="SESSION_ID",
        help="session id or prefix (optional; --output - selects newest for current repo)",
    )
    parser.add_argument(
        "--db",
        metavar="PATH",
        default=None,
        help=f"path to Copilot session database (default: {copilot_sources.COPILOT_DB})",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help=(
            "only export sessions started in the current working directory exactly "
            "(no parent/descendant overlap, no newest-anywhere fallback)"
        ),
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help=(
            "extract every session for the current repository into the "
            "project root, one file per session named "
            "copilot_session_log_raw_<identifier>.json (in this mode --output is treated "
            "as the output directory; positional selector is ignored). This is "
            "the default when no session selector is supplied"
        ),
    )
    parser.add_argument(
        "--output",
        metavar="PATH",
        default=None,
        help=(
            "output JSON file (default: copilot_session_log_raw_<identifier>.json "
            f"in the project root, {project_root}; use '-' for stdout). With --all, "
            "an output directory instead (default: project root)."
        ),
    )
    parser.add_argument(
        "--tool-result-bytes",
        metavar="N",
        type=int,
        default=DEFAULT_TOOL_RESULT_MAX_BYTES,
        help=(
            "cap each tool result in the JSON envelope at N bytes; the original "
            "size is always recorded. Use a negative value for no cap "
            f"(default: {DEFAULT_TOOL_RESULT_MAX_BYTES})"
        ),
    )
    return parser


CAP_FLAG = "--tool-result-bytes"
NO_EVENTS_NOTE = "no exportable events; no JSON written"


def _extract_all(
    conn: sqlite3.Connection,
    output: Optional[str],
    strict: bool = False,
    tool_result_max_bytes: int = DEFAULT_TOOL_RESULT_MAX_BYTES,
    *,
    project_root: Optional[Path] = None,
) -> int:
    """Extract every matching Copilot session, one JSON file each."""
    project_root = Path.cwd() if project_root is None else project_root
    sessions = copilot_sources.matching_sessions(conn, Path.cwd(), strict=strict)
    if not sessions:
        print(f"error: no Copilot sessions for {Path.cwd()}", file=sys.stderr)
        return 1
    sessions.sort(key=lambda s: _string(s.get("created_at")) or "")
    out_dir = Path(output).expanduser() if output else project_root
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"extracting {len(sessions)} Copilot session(s)", file=sys.stderr)
    written = 0
    used_ids: set[str] = set()
    for session in sessions:
        ident = _unique_identifier(output_identifier(session), used_ids)
        out_path = out_dir / raw_filename("copilot", ident)
        try:
            envelope = extract_session(conn, session, tool_result_max_bytes)
            if envelope is None:
                print(f"  skip {ident}: {NO_EVENTS_NOTE}", file=sys.stderr)
                continue
            write_envelope(out_path, envelope, CAP_FLAG)
        except (OSError, ValueError) as exc:
            print(f"  skip {ident}: {exc}", file=sys.stderr)
            continue
        written += 1
        print(f"  wrote {out_path}", file=sys.stderr)
    print(f"done: {written}/{len(sessions)} session(s) written", file=sys.stderr)
    return 0 if written else 1


def _select_session(
    conn: sqlite3.Connection, args: argparse.Namespace
) -> Optional[Dict[str, JsonValue]]:
    if args.session:
        session = copilot_sources.get_session_by_id(conn, args.session)
        if session is None:
            print(f"error: no session found matching {args.session!r}", file=sys.stderr)
        return session
    session = copilot_sources.newest_session(conn, Path.cwd(), strict=args.strict)
    if session is None:
        print(
            f"error: no Copilot sessions for {Path.cwd()}"
            + (" (--strict: exact cwd match only)" if args.strict else ""),
            file=sys.stderr,
        )
    return session


def _extract_selected(
    conn: sqlite3.Connection, args: argparse.Namespace, project_root: Path
) -> int:
    session = _select_session(conn, args)
    if session is None:
        return 1
    print(f"using session: {session['id']}", file=sys.stderr)
    try:
        envelope = extract_session(conn, session, args.tool_result_bytes)
        if envelope is None:
            print(f"skip {session['id']}: {NO_EVENTS_NOTE}", file=sys.stderr)
            return 1
        if args.output == "-":
            payload = json.dumps(envelope, ensure_ascii=False)
            warn_if_oversized("stdout", payload, CAP_FLAG)
            sys.stdout.write(payload + "\n")
        else:
            out_path = (
                Path(args.output).expanduser()
                if args.output
                else project_root / raw_filename("copilot", output_identifier(session))
            )
            write_envelope(out_path, envelope, CAP_FLAG)
            print(f"wrote {out_path}", file=sys.stderr)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def main(
    argv: Optional[List[str]] = None, *, project_root: Optional[Path] = None
) -> int:
    project_root = Path.cwd() if project_root is None else project_root
    args = build_arg_parser(project_root).parse_args(argv)
    db_path = Path(args.db).expanduser() if args.db else copilot_sources.COPILOT_DB
    try:
        conn = copilot_sources.get_db_connection(db_path)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        if args.all or (not args.session and args.output != "-"):
            out = None if args.output == "-" else args.output
            return _extract_all(
                conn,
                out,
                args.strict,
                args.tool_result_bytes,
                project_root=project_root,
            )
        return _extract_selected(conn, args, project_root)
    finally:
        conn.close()
