#!/usr/bin/env python3
"""Extract a Codex session transcript into a JSON conversation envelope.

Codex records CLI sessions as JSONL files under:

    ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl

Those logs have a different schema from Claude Code transcripts. Each line is
an event envelope with a top-level ``type`` such as ``session_meta``,
``event_msg``, or ``response_item``. The useful conversation data lives under
``payload``:

* ``event_msg`` / ``payload.type == "user_message"`` records an input turn;
  this alone does not establish candidate authorship.
* ``response_item`` / ``payload.type == "message"`` and role ``assistant`` is
  assistant text.
* ``response_item`` / ``payload.type == "function_call"`` is a tool call.
* ``response_item`` / ``payload.type == "function_call_output"`` is a tool
  result.

This script is mechanical: no LLM, no summarization, and no network access.
It intentionally ignores base instructions, developer messages, reasoning
payloads, token counts, and encrypted/internal fields.

When the human denies a tool's permission prompt, the decision is surfaced as a
"permission denied" note on the turn. Current app-server rollouts record the
rejection in the tool result without a feedback message. Approvals leave no
distinct record -- an approved tool just runs -- so only denials are captured.

Codex Plan mode is identified from the per-turn collaboration-mode metadata.
The retained interpretation helpers recognize structured ``update_plan`` calls,
but no current native capture shows that call in Plan mode. Unlike Claude Code's ``ExitPlanMode`` flow, Codex
does not currently record a separate plan approval result; later approval or
steering is captured as the next ordinary user turn.

Examples
--------
Every transcript for the current project, one file per session::

    uv run --locked python scripts/extract_codex_session_log.py

The most recent matching transcript, written to stdout::

    uv run --locked python scripts/extract_codex_session_log.py --output -

A specific session, written to one file::

    uv run --locked python scripts/extract_codex_session_log.py <session-id> --output session.json
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Dict, List, Optional, Tuple, TypedDict

from session_log_extractors import codex_sources
from session_log_extractors.codex_sources import CodexSource, ImageInputs
from session_log_extractors.extractor_contract import Extractor
from session_log_extractors.json_types import (
    JsonValue,
    json_value,
    parse_json,
    string_value,
)
from session_log_extractors.question_types import is_question_option
from session_log_extractors.session_log_envelope import (
    DEFAULT_TOOL_RESULT_MAX_BYTES,
    Envelope,
    assistant_event,
    export_session,
    image_from_bytes,
    media_type_for_ext,
    raw_filename,
    unavailable_image,
    user_event,
    warn_if_oversized,
    write_envelope,
)
from session_log_extractors.session_model import (
    AuthorshipMarker,
    AuthorshipRange,
    Image,
    Message,
    PatchResult,
    PermissionDenial,
    PlanStep,
    PlanUpdate,
    QuestionAnswer,
    Session,
    ToolCall,
    ToolResult,
    UserMessage,
)
from session_log_extractors.tool_inputs.codex import input_schema as codex_input_schema

_IMAGE_EXT = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
}


# Tools that ask the human for a direct answer. Codex has used both bare and
# namespaced tool names in logs, so matching is suffix-based during interpretation.
INTERACTION_TOOLS = {"request_user_input"}

# Retain parsing for update_plan-shaped calls; no current native capture shows
# this tool in Plan mode or Default mode.
PLAN_TOOLS = {"update_plan"}

_BASH_INPUT = re.compile(r"<bash-input>(.*?)</bash-input>", re.DOTALL | re.IGNORECASE)
_COMMAND_NAME = re.compile(
    r"<command-name>\s*(.*?)\s*</command-name>", re.DOTALL | re.IGNORECASE
)
_COMMAND_ARGS = re.compile(
    r"<command-args>\s*(.*?)\s*</command-args>", re.DOTALL | re.IGNORECASE
)
_SLASH_COMMAND = re.compile(r"^\s*(/[A-Za-z0-9][\w:.-]*)(?:\s+(.*?))?\s*$", re.DOTALL)
_DATA_IMAGE_URL = re.compile(r"^data:(image/[A-Za-z0-9.+-]+);base64,(.*)$", re.DOTALL)

# This branch recognizes a harness-style assistant rejection message. The
# current native app-server denial instead appears in a tool result.
_PERMISSION_DENIAL_PREFIXES = (
    "The user doesn't want to proceed with this tool use.",
    "Permission for this tool use was denied.",
)
_EXT_AGENT_PREFIX = re.compile(
    r"^\[external_agent_tool_result[^\]]*\]\s*", re.IGNORECASE
)
_DENIAL_MESSAGE = re.compile(
    r"the user said:\s*\n?(.*?)(?:\n\nNote:|\Z)", re.DOTALL | re.IGNORECASE
)


def parse_permission_denial(text: JsonValue) -> Optional[str]:
    """If ``text`` is a permission-denial message, return the human's steering
    message (``""`` if they just denied). Return ``None`` otherwise."""
    if not isinstance(text, str):
        return None
    stripped = _EXT_AGENT_PREFIX.sub("", text).lstrip()
    if not stripped.startswith(_PERMISSION_DENIAL_PREFIXES):
        return None
    m = _DENIAL_MESSAGE.search(stripped)
    return m.group(1).strip() if m else ""


def _payload(entry: Dict[str, JsonValue]) -> Dict[str, JsonValue]:
    payload = entry.get("payload")
    return payload if isinstance(payload, dict) else {}


def _content_text(content: JsonValue, text_keys: Tuple[str, ...]) -> str:
    parts: List[str] = []
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                for key in text_keys:
                    val = block.get(key)
                    if isinstance(val, str):
                        parts.append(val)
                        break
    return "\n".join(parts)


def assistant_text_from_payload(payload: Dict[str, JsonValue]) -> str:
    return _content_text(payload.get("content"), ("text", "output_text"))


def _image_ref_from_string(value: str) -> Optional[Dict[str, JsonValue]]:
    if value.startswith("data:image/"):
        return {"type": "data_url", "data_url": value}
    if value.startswith(("/", "~")):
        return {"type": "path", "path": value}
    return None


def _image_ref(item: JsonValue) -> dict[str, JsonValue] | None:
    if isinstance(item, str):
        return _image_ref_from_string(item)
    if not isinstance(item, dict):
        return None
    for key in ("image_url", "url", "path", "file_path"):
        value = item.get(key)
        if isinstance(value, str):
            ref = _image_ref_from_string(value)
            if ref:
                return ref
    if isinstance(item.get("data"), str):
        return {
            "type": "base64",
            "data": item["data"],
            "media_type": item.get("media_type") or item.get("mime_type"),
        }
    return None


def image_refs_from_event(payload: Dict[str, JsonValue]) -> List[Dict[str, JsonValue]]:
    """Image refs from Codex event_msg:user_message payloads."""
    refs = []
    for key in ("local_images", "images"):
        items = payload.get(key)
        if isinstance(items, list):
            for item in items:
                ref = (
                    _image_ref(item)
                    if key == "images" or isinstance(item, str)
                    else None
                )
                if ref:
                    refs.append(ref)
    return refs


def image_refs_from_user_message_payload(
    payload: Dict[str, JsonValue],
) -> List[Dict[str, JsonValue]]:
    """Base64 image refs from ``response_item`` user-message content."""
    refs: List[Dict[str, JsonValue]] = []
    content = payload.get("content")
    if not isinstance(content, list):
        return refs

    for block in content:
        if not isinstance(block, dict) or block.get("type") != "input_image":
            continue
        image_url = block.get("image_url")
        if isinstance(image_url, str):
            ref = _image_ref_from_string(image_url)
            if ref:
                refs.append(ref)
    return refs


def _decode_arguments(arguments: JsonValue) -> JsonValue:
    if isinstance(arguments, str):
        try:
            return parse_json(arguments)
        except json.JSONDecodeError:
            return arguments
    return arguments


def _skill_name(name: str, args: object) -> str | None:
    short_name = name.rsplit(".", 1)[-1].lower()
    normalized = name.lower().replace("__", ".")
    if not isinstance(args, dict):
        return None
    keys = ("skill", "name", "package") if short_name in ("skill", "read_skill") else ()
    if normalized.endswith("skills.read"):
        keys = ("package", "skill", "name")
    value = next((args.get(key) for key in keys if args.get(key)), None)
    return value if isinstance(value, str) else None


def skill_refs_from_call(
    name: str, arguments: JsonValue
) -> List[Dict[str, Optional[str]]]:
    """Return skill names and definition paths evidenced by a tool call."""
    args = _decode_arguments(arguments)
    refs: List[Dict[str, Optional[str]]] = []

    def add(value: str, path: Optional[str] = None) -> None:
        for ref in refs:
            if ref["name"] == value:
                if path and not ref.get("path"):
                    ref["path"] = path
                return
        refs.append({"name": value, "path": path})

    value = _skill_name(name, args)
    if value:
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


def skill_names_from_call(name: str, arguments: JsonValue) -> List[str]:
    """Return skill names evidenced by a call (backwards-compatible helper)."""
    return [str(ref["name"]) for ref in skill_refs_from_call(name, arguments)]


def _tool_name(payload: Dict[str, JsonValue]) -> str:
    name = payload.get("name")
    return name if isinstance(name, str) else "tool"


def _is_process_rejection(result: object) -> bool:
    return (
        isinstance(result, str)
        and "exec_command failed: CreateProcess" in result
        and "rejected by user" in result.lower()
    )


def _is_interaction_tool(name: str) -> bool:
    return any(name == tool or name.endswith("." + tool) for tool in INTERACTION_TOOLS)


def _is_plan_tool(name: str) -> bool:
    return any(name == tool or name.endswith("." + tool) for tool in PLAN_TOOLS)


def _collaboration_mode(payload: Dict[str, JsonValue]) -> str:
    """Return the normalized collaboration mode carried by a rollout event."""
    mode = payload.get("collaboration_mode_kind")
    if not isinstance(mode, str):
        collaboration = payload.get("collaboration_mode")
        if isinstance(collaboration, dict):
            mode = collaboration.get("mode")
    return mode.strip().lower() if isinstance(mode, str) else ""


def _plan_update(arguments: JsonValue) -> Optional[PlanUpdate]:
    """Decode one structured update_plan call for first-class rendering."""
    args = _decode_arguments(arguments)
    if not isinstance(args, dict):
        return None
    plan = args.get("plan")
    if not isinstance(plan, list):
        return None
    steps: list[PlanStep] = []
    for item in plan:
        if not isinstance(item, dict) or not isinstance(item.get("step"), str):
            continue
        steps.append(
            {
                "step": str(item["step"]),
                "status": item.get("status", "pending"),
            }
        )
    if not steps:
        return None
    explanation = args.get("explanation")
    return {
        "explanation": explanation if isinstance(explanation, str) else "",
        "steps": steps,
    }


def _decode_jsonish(value: JsonValue) -> JsonValue:
    if isinstance(value, str):
        try:
            return parse_json(value)
        except json.JSONDecodeError:
            return value
    return value


def _value_to_answer_text(value: JsonValue) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        parts = [_value_to_answer_text(v) for v in value]
        return ", ".join(p for p in parts if p)
    if isinstance(value, dict):
        for key in (
            "answer",
            "answers",
            "answer_text",
            "value",
            "label",
            "selected_label",
            "selected",
            "selection",
            "choice",
            "choices",
            "labels",
        ):
            if key in value:
                text = _value_to_answer_text(value[key])
                if text:
                    return text
    return ""


def _extract_answer_from_item(question: Dict[str, JsonValue], item: JsonValue) -> str:
    if not isinstance(item, dict):
        return ""

    qid = question.get("id")
    qtext = question.get("question")
    header = question.get("header")
    item_ids = (
        item.get("id"),
        item.get("question_id"),
        item.get("questionId"),
        item.get("question"),
        item.get("header"),
    )
    if any(marker and marker in item_ids for marker in (qid, qtext, header)):
        return _value_to_answer_text(item)
    return ""


def _answer_in_container(question: Dict[str, JsonValue], container: JsonValue) -> str:
    if isinstance(container, dict):
        for key in (
            question.get("id"),
            question.get("question"),
            question.get("header"),
        ):
            if isinstance(key, str) and key in container:
                text = _value_to_answer_text(container[key])
                if text:
                    return text
        items = list(container.values())
    elif isinstance(container, list):
        items = container
    else:
        return ""
    for item in items:
        text = _extract_answer_from_item(question, item)
        if text:
            return text
    return ""


def _structured_answer_for_question(
    question: Dict[str, JsonValue], output: JsonValue
) -> str:
    output = _decode_jsonish(output)
    if isinstance(output, dict):
        for key in (
            question.get("id"),
            question.get("question"),
            question.get("header"),
        ):
            if isinstance(key, str) and key in output:
                text = _value_to_answer_text(output[key])
                if text:
                    return text
        for key in ("answers", "responses", "selections", "values"):
            text = _answer_in_container(question, output.get(key))
            if text:
                return text
        return _extract_answer_from_item(question, output)
    return _answer_in_container(question, output)


def _flatten_result_text(output: JsonValue) -> str:
    output = _decode_jsonish(output)
    if isinstance(output, str):
        return output
    if isinstance(output, (int, float, bool)):
        return str(output)
    if isinstance(output, list):
        return " ".join(_flatten_result_text(item) for item in output)
    if isinstance(output, dict):
        parts = [
            _flatten_result_text(output[key])
            for key in ("message", "text", "output", "content", "answer", "value")
            if key in output
        ]
        nonempty = [part for part in parts if part]
        if not nonempty:
            nonempty = [
                text
                for value in output.values()
                if (text := _flatten_result_text(value))
            ]
        return " ".join(nonempty)
    return ""


def _parse_chosen(
    question_text: str, options: List[Dict[str, JsonValue]], result_text: str
) -> Tuple[str, List[Dict[str, JsonValue]]]:
    val = ""
    if question_text and result_text:
        marker = f'"{question_text}"="'
        idx = result_text.find(marker)
        if idx != -1:
            rest = result_text[idx + len(marker) :]
            end = rest.find('"')
            val = rest[:end] if end != -1 else rest
    if not val and result_text:
        parts = result_text.split('="', 1)
        if len(parts) == 2:
            tail = parts[1]
            end = tail.find('"')
            val = tail[:end] if end != -1 else tail
    chosen = [
        o
        for o in options
        if isinstance(label := o.get("label"), str) and label and label in val
    ]
    return val, chosen


def option_qa_from_output(
    output: JsonValue, questions: Optional[List[JsonValue]]
) -> List[_OptionAnswer]:
    if not questions:
        return []

    result_text = _flatten_result_text(output)
    out: List[_OptionAnswer] = []
    for q in questions:
        if not isinstance(q, dict):
            continue
        raw_options = q.get("options")
        options = (
            [o for o in raw_options if isinstance(o, dict)]
            if isinstance(raw_options, list)
            else []
        )
        val, chosen = _parse_chosen(
            string_value(q.get("question")) or "", options, result_text
        )
        if not val:
            val = _structured_answer_for_question(q, output)
            chosen = [
                o
                for o in options
                if isinstance(label := o.get("label"), str) and label and label in val
            ]
        out.append(
            {
                "question": q.get("question", ""),
                "header": q.get("header", ""),
                "options": options,
                "chosen_labels": [o.get("label") for o in chosen],
                "answer_text": val,
            }
        )
    return out


def patch_paths(arguments: JsonValue) -> List[str]:
    args = _decode_arguments(arguments)
    if isinstance(args, dict):
        text = args.get("patch") or args.get("input") or args.get("content")
    else:
        text = args
    if not isinstance(text, str):
        return []

    paths: List[str] = []
    for line in text.splitlines():
        m = re.match(r"\*\*\* (?:Add|Update|Delete) File: (.+)", line)
        if m:
            paths.append(m.group(1).strip())
    return paths


def append_unique_path(paths: List[str], seen: set[str], path: str) -> None:
    """Append a changed path, avoiding relative/absolute duplicates."""
    if not path:
        return
    for existing in seen:
        if (
            existing == path
            or existing.endswith("/" + path)
            or path.endswith("/" + existing)
        ):
            return
    seen.add(path)
    paths.append(path)


def envelope_images(
    refs: List[Dict[str, JsonValue]], image_inputs: Optional[ImageInputs] = None
) -> List[Image]:
    """Interpret Codex image refs using supplied bytes as inline base64 entries.

    Codex records images out of band — as a filesystem path, a data URL, or raw
    base64 — so every ref has to be resolved to bytes before it can travel with
    the log. A ref that will not resolve (most often a path whose file has since
    been deleted) is recorded as unavailable rather than dropped: "pasted a
    screenshot we can no longer read" and "pasted nothing" are different facts,
    and only one of them is about the candidate.
    """
    images: List[Image] = []
    for ref in refs:
        decoded = _image_bytes_and_ext(ref, image_inputs)
        if decoded is None:
            images.append(unavailable_image(ref.get("path") or ref.get("type")))
            continue
        raw, ext = decoded
        images.append(image_from_bytes(raw, media_type_for_ext(ext)))
    return images


def _string(value: JsonValue) -> str | None:
    return value if isinstance(value, str) else None


def _copy_identity(
    target: Message | ToolResult | PatchResult, payload: Mapping[str, object]
) -> None:
    value = payload.get("id")
    if isinstance(value, str):
        target["id"] = value
    value = payload.get("parent_id")
    if isinstance(value, str):
        target["parent_id"] = value
    value = payload.get("message_id")
    if isinstance(value, str):
        target["message_id"] = value


def _user_commands(message: UserMessage, raw_text: str) -> None:
    tagged = _COMMAND_NAME.search(raw_text)
    slash = _SLASH_COMMAND.match(message.get("text") or "")
    command, args = "", ""
    if tagged:
        command = tagged[1].strip()
        match = _COMMAND_ARGS.search(raw_text)
        args = match[1].strip() if match else ""
    elif slash:
        command, args = slash[1], (slash[2] or "").strip()
    if command.startswith("/"):
        message["command"] = command
        message["command_args"] = args or None
    commands = [c.strip() for c in _BASH_INPUT.findall(raw_text) if c.strip()]
    if commands:
        message["shell_command"] = "\n".join(commands)


class _OptionAnswer(TypedDict):
    question: JsonValue
    header: JsonValue
    options: list[dict[str, JsonValue]]
    chosen_labels: list[JsonValue]
    answer_text: str


def _questions(call: ToolCall, output: JsonValue) -> list[QuestionAnswer]:
    arguments = call.get("input")
    questions = arguments.get("questions") if isinstance(arguments, dict) else None
    if not _is_interaction_tool(call["name"]) or not isinstance(questions, list):
        return []
    return [
        QuestionAnswer(
            question=str(qa["question"]),
            header=str(qa["header"]),
            options=[o for o in qa["options"] if is_question_option(o)],
            chosen_labels=[str(label) for label in qa["chosen_labels"]],
            answer_text=str(qa["answer_text"]),
        )
        for qa in option_qa_from_output(output, questions)
    ]


def _source_markers(record: dict[str, JsonValue], path: str) -> list[AuthorshipMarker]:
    return [
        AuthorshipMarker(field=f"{path}.{key}", value=record[key])
        for key in ("type", "source")
        if key in record
    ]


def _text_authorship(
    payload: dict[str, JsonValue],
    text: str,
    timestamp: str | None,
    record_type: str,
) -> list[AuthorshipRange]:
    """Retain native evidence without treating input role/type as authorship.

    The captured Codex shapes do not establish who authored input_text, even
    when it resembles a runtime wrapper. Keep its source explicitly unknown.
    """
    if not text:
        return []
    markers = [AuthorshipMarker(field="type", value=record_type)]
    markers.extend(_source_markers(payload, "payload"))
    content = payload.get("content")
    if isinstance(content, list):
        for index, block in enumerate(content):
            if isinstance(block, dict) and any(
                isinstance(block.get(key), str) for key in ("text", "output_text")
            ):
                markers.extend(_source_markers(block, f"payload.content[{index}]"))
    span = AuthorshipRange(start=0, end=len(text), source="unknown", markers=markers)
    if timestamp is not None:
        span["ts"] = timestamp
    return [span]


def _user_message(
    payload: dict[str, JsonValue],
    timestamp: str | None,
    raw_text: str,
    images: list[Image],
    record_type: str,
) -> UserMessage:
    """Preserve user content while retaining identity and command syntax."""
    message = user_event(0, timestamp, raw_text, images)
    _copy_identity(message, payload)
    _user_commands(message, raw_text)
    message["authorship"] = _text_authorship(payload, raw_text, timestamp, record_type)
    return message


def _new_tool_call(payload: dict[str, JsonValue]) -> ToolCall:
    """Decode a native request without changing session state."""
    name = _tool_name(payload)
    arguments = payload.get("arguments")
    if arguments is None:
        arguments = payload.get("input")
    decoded = _decode_arguments(arguments)
    call = ToolCall(
        name=name, input=decoded, input_schema=codex_input_schema(name, decoded)
    )
    call_id = _string(payload.get("call_id"))
    native_id = _string(payload.get("id"))
    if call_id is not None:
        call["id"] = call_id
    if native_id is not None:
        call["native_id"] = native_id
    return call


class _CodexSessionBuilder:
    """One native traversal with ordered canonical messages and ID matching."""

    def __init__(self, source: CodexSource) -> None:
        self.source = source
        self.session = Session(
            harness="codex",
            session_id=source.identifier,
            events=[],
            cwd=source.cwd,
            started_at=first_timestamp(source.entries),
            models=session_models(source.entries),
            skills_used=[],
            files_changed=[],
            unmatched_tool_results=[],
            unsupported_tools=[],
            patch_results=[],
        )
        self.pending_calls: dict[str, ToolCall] = {}
        self.pending_user: UserMessage | None = None
        self.mode = ""
        self.active_user: UserMessage | None = None
        self.files_seen: set[str] = set()

    def append(self, message: Message) -> None:
        message["i"] = len(self.session["events"])
        if self.mode:
            message["collaboration_mode"] = self.mode
        self.session["events"].append(message)

    def flush_user(self) -> None:
        if self.pending_user is not None:
            self.active_user = self.pending_user
            self.append(self.pending_user)
            self.pending_user = None

    def response_user(
        self, payload: dict[str, JsonValue], timestamp: str | None
    ) -> None:
        """Buffer a response item until its event view or the next message arrives."""
        raw_text = _content_text(payload.get("content"), ("text",))
        images = envelope_images(
            image_refs_from_user_message_payload(payload), self.source.image_inputs
        )
        message = _user_message(payload, timestamp, raw_text, images, "response_item")
        self.flush_user()
        if message["text"] or images:
            self.pending_user = message

    def event_user(self, payload: dict[str, JsonValue], timestamp: str | None) -> None:
        """Emit the user event after reconciling its buffered response view."""
        images = envelope_images(
            image_refs_from_event(payload), self.source.image_inputs
        )
        raw_text = _string(payload.get("message")) or ""
        message = _user_message(payload, timestamp, raw_text, images, "event_msg")
        self.merge_pending_user(message)
        if message["text"] or message.get("images") or message.get("command"):
            self.active_user = message
            self.append(message)

    def merge_pending_user(self, message: UserMessage) -> None:
        """Prefer the response view's identity and images for the same input."""
        pending = self.pending_user
        if pending is None:
            return
        if pending["text"] != message["text"]:
            self.flush_user()
            return
        _copy_identity(message, pending)
        if pending.get("images"):
            message["images"] = pending["images"]
        if message["authorship"]:
            markers = message["authorship"][0]["markers"]
            for span in pending.get("authorship", []):
                for marker in span.get("markers", []):
                    if marker not in markers:
                        markers.append(marker)
        self.pending_user = None

    def assistant(self, payload: dict[str, JsonValue], timestamp: str | None) -> None:
        self.flush_user()
        text = assistant_text_from_payload(payload)
        if not text:
            return
        message = assistant_event(0, timestamp, text)
        _copy_identity(message, payload)
        message["authorship"] = _text_authorship(
            payload, text, timestamp, "response_item"
        )
        denial = parse_permission_denial(text)
        if denial is not None:
            # A message without a call ID cannot establish tool ownership.
            message["permission_denial"] = PermissionDenial(
                tool="(tool call)", message=denial, by="user"
            )
        self.append(message)

    def call(self, payload: dict[str, JsonValue], timestamp: str | None) -> None:
        self.flush_user()
        call = _new_tool_call(payload)
        if "id" in call:
            self.pending_calls[call["id"]] = call
        self.call_semantics(call)
        message = assistant_event(0, timestamp, "", [call])
        _copy_identity(message, payload)
        self.append(message)

    def call_semantics(self, call: ToolCall) -> None:
        name, arguments = call["name"], json_value(call.get("input"))
        if self.mode == "plan" and _is_plan_tool(name):
            plan = _plan_update(arguments)
            if plan is not None:
                call["plan_update"] = plan
        for skill in skill_names_from_call(name, arguments):
            if skill not in self.session["skills_used"]:
                self.session["skills_used"].append(skill)
            call["skill"] = skill
        if (
            call["input_schema"] == "unsupported"
            and name not in self.session["unsupported_tools"]
        ):
            self.session["unsupported_tools"].append(name)
        if name.endswith("apply_patch"):
            for path in patch_paths(arguments):
                append_unique_path(self.session["files_changed"], self.files_seen, path)

    def result(self, payload: dict[str, JsonValue], timestamp: str | None) -> None:
        call_id = _string(payload.get("call_id"))
        result = ToolResult(
            tool_call_id=call_id,
            ts=timestamp,
            is_error=payload.get("is_error") is True,
        )
        if "output" in payload:
            result["content"] = payload["output"]
        _copy_identity(result, payload)
        call = self.pending_calls.pop(call_id, None) if call_id is not None else None
        if call is None:
            self.session["unmatched_tool_results"].append(result)
            return
        call["result_record"] = result
        if "output" in payload:
            output = payload["output"]
            # Keep the full native result above and the existing textual JSON
            # interpretation here. Only the exporter applies byte limits.
            call["result"] = None if output is None else _flatten_result_text(output)
            questions = _questions(call, output)
            if questions:
                call["questions"] = questions
            if _is_process_rejection(_flatten_result_text(output)):
                call["permission_denial"] = PermissionDenial(
                    tool=call["name"], message="", by="user"
                )
        if result["is_error"]:
            call["is_error"] = True

    def patch(self, payload: dict[str, JsonValue], timestamp: str | None) -> None:
        changes = payload.get("changes")
        paths = list(changes) if isinstance(changes, dict) else []
        for path in paths:
            append_unique_path(self.session["files_changed"], self.files_seen, path)
        result = PatchResult(
            ts=timestamp,
            paths=paths,
            stdout=payload.get("stdout"),
            stderr=payload.get("stderr"),
        )
        _copy_identity(result, payload)
        self.session["patch_results"].append(result)

    def entry(self, entry: dict[str, JsonValue]) -> None:
        payload = entry.get("payload")
        if not isinstance(payload, dict):
            return
        timestamp = _string(entry.get("timestamp"))
        mode = _collaboration_mode(payload)
        if mode:
            self.mode = mode
            if self.active_user is not None and not self.active_user.get(
                "collaboration_mode"
            ):
                self.active_user["collaboration_mode"] = mode
        if entry.get("type") == "event_msg":
            self.event(payload, timestamp)
        elif entry.get("type") == "response_item":
            self.response(payload, timestamp)

    def event(self, payload: dict[str, JsonValue], timestamp: str | None) -> None:
        if payload.get("type") == "user_message":
            self.event_user(payload, timestamp)
        elif payload.get("type") == "patch_apply_end":
            self.patch(payload, timestamp)
        elif payload.get("type") == "item_completed":
            item = payload.get("item")
            if (
                isinstance(item, dict)
                and item.get("type") == "FileChange"
                and item.get("status") == "completed"
            ):
                self.patch(item, timestamp)

    def response(self, payload: dict[str, JsonValue], timestamp: str | None) -> None:
        kind = payload.get("type")
        if kind == "message":
            if payload.get("role") == "user":
                self.response_user(payload, timestamp)
            elif payload.get("role") == "assistant":
                self.assistant(payload, timestamp)
        elif kind in ("function_call", "custom_tool_call"):
            self.call(payload, timestamp)
        elif kind in ("function_call_output", "custom_tool_call_output"):
            self.result(payload, timestamp)


def interpret_source(source: CodexSource) -> Session | None:
    """Interpret supplied native data without I/O or export policy."""
    interpreter = _CodexSessionBuilder(source)
    for entry in source.entries:
        interpreter.entry(entry)
    interpreter.flush_user()
    session = interpreter.session
    return (
        session
        if session["events"]
        or session["unmatched_tool_results"]
        or session["patch_results"]
        else None
    )


def build_raw_envelope(
    source: CodexSource, tool_result_max_bytes: int = DEFAULT_TOOL_RESULT_MAX_BYTES
) -> Envelope | None:
    """Compatibility entry point; production uses the public adapter."""
    session = interpret_source(source)
    return (
        export_session(session, tool_result_max_bytes)
        if session and session["events"]
        else None
    )


class CodexExtractor:
    """Public Codex reader/interpreter adapter."""

    def read_source(self, request: Path) -> CodexSource:
        return read_source(request)

    def interpret_source(self, source: CodexSource) -> Session | None:
        return interpret_source(source)


EXTRACTOR: Extractor[Path, CodexSource] = CodexExtractor()


def _decode_image(
    data: JsonValue, media_type: JsonValue
) -> Optional[Tuple[bytes, str]]:
    if not isinstance(data, str):
        return None
    try:
        raw = base64.b64decode(data, validate=False)
    except (ValueError, TypeError):
        return None
    ext = _IMAGE_EXT.get(media_type, "img") if isinstance(media_type, str) else "img"
    return raw, ext


def _image_bytes_and_ext(
    ref: Dict[str, JsonValue], image_inputs: Optional[ImageInputs] = None
) -> Optional[Tuple[bytes, str]]:
    kind = ref.get("type")
    if kind == "data_url":
        data_url = ref.get("data_url")
        match = _DATA_IMAGE_URL.match(data_url) if isinstance(data_url, str) else None
        return _decode_image(match[2], match[1]) if match else None
    if kind == "base64":
        return _decode_image(ref.get("data"), ref.get("media_type"))
    if kind == "path" and isinstance(ref.get("path"), str):
        return (image_inputs or {}).get(str(ref["path"]))
    return None


def first_timestamp(entries: List[Dict[str, JsonValue]]) -> Optional[str]:
    for entry in entries:
        ts = string_value(entry.get("timestamp"))
        if isinstance(ts, str) and ts:
            return ts
    return None


def session_models(entries: List[Dict[str, JsonValue]]) -> List[str]:
    """Distinct model ids from turn_context payloads, in first-seen order.

    Codex records the model on each ``turn_context`` entry; a session can
    switch models mid-run, so return all of them.
    """
    models: List[str] = []
    for entry in entries:
        model = _payload(entry).get("model")
        if isinstance(model, str) and model and model not in models:
            models.append(model)
    return models


def build_arg_parser(project_root: Optional[Path] = None) -> argparse.ArgumentParser:
    project_root = Path.cwd() if project_root is None else project_root
    parser = argparse.ArgumentParser(
        prog="extract_codex_session_log.py",
        description=(
            "Extract a Codex CLI session JSONL transcript into a JSON "
            "conversation envelope. Mechanical extraction only; no LLM or network."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "With no session selector, all matching project sessions are exported.\n"
            "Single-session selection precedence:\n"
            "  --transcript PATH > positional file path > positional session id/prefix\n"
            "  > newest transcript whose recorded cwd overlaps the current cwd\n"
            "  > newest transcript anywhere under ~/.codex/sessions.\n"
            "Use --output - without a selector to print only that newest session."
        ),
    )
    parser.add_argument(
        "session",
        nargs="?",
        help="session id/prefix OR path to a Codex .jsonl transcript",
    )
    parser.add_argument(
        "--transcript",
        metavar="PATH",
        help="explicit path to a .jsonl transcript (overrides positional selection)",
    )
    parser.add_argument(
        "--sessions-root",
        metavar="PATH",
        help="override Codex sessions root (default: ~/.codex/sessions)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help=(
            "only read sessions started in the current directory: require an "
            "exact recorded-cwd match (no overlap) and disable the "
            "newest-anywhere global fallback"
        ),
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help=(
            "extract every Codex session whose recorded cwd overlaps the current "
            "working directory, one file per session named "
            "codex_session_log_raw_<session-id>.json (in this mode --output is treated "
            "as the output directory; positional selector is ignored). This is "
            "the default when no session selector is supplied"
        ),
    )
    parser.add_argument(
        "--output",
        metavar="PATH",
        default=None,
        help=(
            "for all-session extraction, the output directory (default: project "
            "root); for a selected single session, the JSON file (default: "
            f"codex_session_log_raw_<session-id>.json in the project root, {project_root}). "
            "Use '-' for stdout and the newest single session"
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
NO_EVENTS_NOTE = "no exportable events (no user or assistant messages); no JSON written"


def read_source(path: Path) -> CodexSource:
    """Load native records, metadata, and optional path image inputs."""
    source = codex_sources.read_source(path)
    refs = []
    for entry in source.entries:
        payload = _payload(entry)
        if entry.get("type") == "event_msg" and payload.get("type") == "user_message":
            refs.extend(image_refs_from_event(payload))
        if entry.get("type") == "response_item" and payload.get("role") == "user":
            refs.extend(image_refs_from_user_message_payload(payload))
    source.image_inputs = codex_sources.load_image_inputs(refs)
    return source


def extract_transcript(
    transcript: Path,
    tool_result_max_bytes: int = DEFAULT_TOOL_RESULT_MAX_BYTES,
) -> Envelope | None:
    """Read one transcript directly into its JSON envelope."""
    return _extract_source(EXTRACTOR.read_source(transcript), tool_result_max_bytes)


def _extract_source(source: CodexSource, tool_result_max_bytes: int) -> Envelope | None:
    if not source.entries:
        raise ValueError(f"no parseable JSON entries found in {source.path}")
    session = EXTRACTOR.interpret_source(source)
    if session and session["unsupported_tools"]:
        print(
            "warning: unsupported tool input schemas: "
            + ", ".join(session["unsupported_tools"]),
            file=sys.stderr,
        )
    return (
        export_session(session, tool_result_max_bytes)
        if session and session["events"]
        else None
    )


def _unique_identifier(base: str, used: set[str]) -> str:
    ident = base
    suffix = 2
    while ident in used:
        ident = f"{base}-{suffix}"
        suffix += 1
    used.add(ident)
    return ident


def _extract_all(
    sessions_root: Optional[str],
    output: Optional[str],
    strict: bool = False,
    tool_result_max_bytes: int = DEFAULT_TOOL_RESULT_MAX_BYTES,
    *,
    project_root: Optional[Path] = None,
) -> int:
    """Extract every matching Codex transcript, one JSON file each."""
    root = (
        Path(sessions_root).expanduser()
        if sessions_root
        else codex_sources.CODEX_SESSIONS_ROOT
    )
    cwd = Path.cwd()
    project_root = cwd if project_root is None else project_root
    files = sorted(
        codex_sources.matching_transcripts(root, cwd, strict=strict),
        key=lambda p: (p.stat().st_mtime, str(p)),
    )
    if not files:
        print(
            f"error: no Codex transcripts for {cwd} under {root}",
            file=sys.stderr,
        )
        return 1

    out_dir = Path(output).expanduser() if output else project_root
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"extracting {len(files)} Codex session(s) from {root}", file=sys.stderr)

    written = 0
    used_ids: set[str] = set()
    for transcript in files:
        ident = CodexSource(transcript, []).identifier
        try:
            source = EXTRACTOR.read_source(transcript)
            ident = source.identifier
            envelope = _extract_source(source, tool_result_max_bytes)
        except (OSError, ValueError) as exc:
            ident = _unique_identifier(ident, used_ids)
            print(f"  skip {ident}: {exc}", file=sys.stderr)
            continue

        ident = _unique_identifier(ident, used_ids)
        if envelope is None:
            print(f"  skip {ident}: {NO_EVENTS_NOTE}", file=sys.stderr)
            continue
        out_path = out_dir / raw_filename("codex", ident)
        try:
            write_envelope(out_path, envelope, CAP_FLAG)
        except OSError as exc:
            print(f"  error writing {out_path}: {exc}", file=sys.stderr)
            continue

        written += 1
        print(f"  wrote {out_path}", file=sys.stderr)

    print(f"done: {written}/{len(files)} session(s) written", file=sys.stderr)
    return 0 if written else 1


def main(
    argv: Optional[List[str]] = None, *, project_root: Optional[Path] = None
) -> int:
    project_root = Path.cwd() if project_root is None else project_root
    args = build_arg_parser(project_root).parse_args(argv)

    default_all = not args.session and not args.transcript and args.output != "-"
    if args.all or default_all:
        out = None if args.output == "-" else args.output
        return _extract_all(
            args.sessions_root,
            out,
            strict=args.strict,
            tool_result_max_bytes=args.tool_result_bytes,
            project_root=project_root,
        )

    try:
        transcript = codex_sources.select_transcript(
            selector=args.session,
            transcript=args.transcript,
            sessions_root=args.sessions_root,
            strict=args.strict,
        )
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"using transcript: {transcript}", file=sys.stderr)

    try:
        envelope = extract_transcript(transcript, args.tool_result_bytes)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if envelope is None:
        print(f"note: {transcript.stem}: {NO_EVENTS_NOTE}", file=sys.stderr)
        return 0
    if args.output == "-":
        payload = json.dumps(envelope, ensure_ascii=False)
        print(payload)
        warn_if_oversized("stdout JSON", payload, CAP_FLAG)
    else:
        out_path = (
            project_root / raw_filename("codex", envelope["session_id"])
            if args.output is None
            else Path(args.output).expanduser()
        )
        try:
            write_envelope(out_path, envelope, CAP_FLAG)
        except OSError as exc:
            print(f"error: could not write {out_path}: {exc}", file=sys.stderr)
            return 1
        print(f"wrote {out_path}", file=sys.stderr)

    return 0
