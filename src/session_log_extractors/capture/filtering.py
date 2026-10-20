"""Allowlist filters that run on the candidate's machine before anything is committed.

Everything here is an allowlist: an entry type, block type, or field that is not
named is dropped, so a harness adding a new field can lose data but never leak it.
Each ``filter_*`` function takes the transcript text and returns the filtered text.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from typing import Any

from .secrets import (
    RESULT_MARKER,
    clip_result,
    looks_secret,
    mentions_env_file,
    redact_lines,
    strip_injected_tags,
)

RESULT_LIMIT = 1024
LONG_RESULT_LIMIT = 8192
# Tools whose results carry the candidate's own decisions (plan edits, answers), which
# are graded, so they keep more than a generic result.
LONG_RESULT_TOOLS = {"ExitPlanMode", "exit_plan_mode", "AskUserQuestion"}
IMAGE_LIMIT_CHARS = 1_000_000
IMAGE_MARKER = "[image removed: too large]"
STUB = {"type": "capture_removed", "reason": "possible secret"}

CLAUDE_TYPES = {"user", "assistant"}
CLAUDE_KEYS = ("type", "timestamp", "sessionId", "cwd", "version")


def _loads(raw: str) -> dict[str, Any] | None:
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _dumps(entries: list[dict[str, Any]]) -> str:
    return "".join(
        json.dumps(e, separators=(",", ":"), ensure_ascii=False) + "\n" for e in entries
    )


def entries(text: str) -> Iterator[dict[str, Any]]:
    for raw in text.splitlines():
        entry = _loads(raw)
        if entry is not None:
            yield entry


def _scrub_text(text: str) -> str:
    return redact_lines(strip_injected_tags(text))


def _redact_value(value: Any) -> Any:
    """Redact credential-shaped lines in every string inside a JSON-like value."""
    if isinstance(value, str):
        return redact_lines(value)
    if isinstance(value, dict):
        return {k: _redact_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_value(v) for v in value]
    return value


def _image_ok(data: Any) -> bool:
    return isinstance(data, str) and len(data) <= IMAGE_LIMIT_CHARS


# ---- Claude -------------------------------------------------------------


class _ToolState:
    """Which tool each call id belongs to, and which calls touched an env file."""

    def __init__(self) -> None:
        self.names: dict[str, str] = {}
        self.sensitive: set[str] = set()

    def note_call(self, block: dict[str, Any]) -> None:
        call_id = block.get("id")
        if not isinstance(call_id, str):
            return
        self.names[call_id] = str(block.get("name", ""))
        if mentions_env_file(block.get("input")):
            self.sensitive.add(call_id)

    def limit_for(self, call_id: str) -> int:
        return (
            LONG_RESULT_LIMIT
            if self.names.get(call_id) in LONG_RESULT_TOOLS
            else RESULT_LIMIT
        )


def _claude_result_parts(content: Any) -> list[Any]:
    if isinstance(content, list):
        return content
    return [{"type": "text", "text": content}] if isinstance(content, str) else []


def _claude_result_part(
    part: Any, call_id: str, state: _ToolState
) -> dict[str, Any] | None:
    if not isinstance(part, dict):
        return None
    if part.get("type") == "image":
        return _claude_image(part)
    if part.get("type") != "text":
        return None
    if call_id in state.sensitive:
        return {"type": "text", "text": RESULT_MARKER}
    return {
        "type": "text",
        "text": clip_result(str(part.get("text", "")), state.limit_for(call_id)),
    }


def _claude_image(part: dict[str, Any]) -> dict[str, Any]:
    source = part.get("source")
    if isinstance(source, dict) and _image_ok(source.get("data")):
        return part
    return {"type": "text", "text": IMAGE_MARKER}


def _claude_tool_result(block: dict[str, Any], state: _ToolState) -> dict[str, Any]:
    call_id = str(block.get("tool_use_id", ""))
    parts = [
        _claude_result_part(p, call_id, state)
        for p in _claude_result_parts(block.get("content"))
    ]
    kept = [p for p in parts if p is not None]
    # A plain-string result stays a string, as the harness wrote it.
    content: Any = kept
    if not isinstance(block.get("content"), list):
        content = kept[0]["text"] if kept else ""
    out = {"type": "tool_result", "tool_use_id": call_id, "content": content}
    if block.get("is_error") is True:
        out["is_error"] = True
    return out


def _claude_block(block: Any, state: _ToolState) -> dict[str, Any] | None:
    if isinstance(block, str):
        block = {"type": "text", "text": block}
    if not isinstance(block, dict):
        return None
    kind = block.get("type")
    if kind == "text":
        text = _scrub_text(str(block.get("text", "")))
        return {"type": "text", "text": text} if text.strip() else None
    if kind == "tool_use":
        state.note_call(block)
        return {
            **{k: block[k] for k in ("type", "id", "name") if k in block},
            "input": _redact_value(block.get("input")),
        }
    if kind == "tool_result":
        return _claude_tool_result(block, state)
    if kind == "image":
        return _claude_image(block)
    return None


def _claude_content(content: Any, state: _ToolState) -> Any:
    if isinstance(content, str):
        text = _scrub_text(content)
        return text if text.strip() else None
    if not isinstance(content, list):
        return None
    blocks = [_claude_block(b, state) for b in content]
    kept = [b for b in blocks if b is not None]
    return kept or None


def _tool_use_result(entry: dict[str, Any], state: _ToolState) -> Any:
    """Keep the structured result only for decision tools, under the long limit."""
    value = entry.get("toolUseResult")
    message = entry.get("message")
    blocks = message.get("content") if isinstance(message, dict) else None
    if value is None or not isinstance(blocks, list):
        return None
    ids = [
        b.get("tool_use_id")
        for b in blocks
        if isinstance(b, dict) and b.get("type") == "tool_result"
    ]
    if not any(state.names.get(i) in LONG_RESULT_TOOLS for i in ids):
        return None
    dumped = json.dumps(value)
    if len(dumped.encode("utf-8")) > LONG_RESULT_LIMIT or looks_secret(dumped):
        return None
    return value


def _claude_entry(entry: dict[str, Any], state: _ToolState) -> dict[str, Any] | None:
    if (
        entry.get("type") not in CLAUDE_TYPES
        or entry.get("isMeta")
        or entry.get("isSidechain")
    ):
        return None
    message = entry.get("message")
    if not isinstance(message, dict):
        return None
    content = _claude_content(message.get("content"), state)
    if content is None:
        return None
    out = {k: entry[k] for k in CLAUDE_KEYS if k in entry}
    out["message"] = {
        **{k: message[k] for k in ("role",) if k in message},
        "content": content,
    }
    tool_result = _tool_use_result(entry, state)
    if tool_result is not None:
        out["toolUseResult"] = tool_result
    return out


def filter_claude(text: str) -> str:
    """Filter a Claude Code transcript (main session or one subagent file)."""
    state = _ToolState()
    kept = [_claude_entry(e, state) for e in entries(text)]
    return _dumps([e for e in kept if e is not None])


# ---- Codex --------------------------------------------------------------

CODEX_INJECTED_PREFIXES = (
    "<environment_context>",
    "# AGENTS.md instructions",
    "<user_instructions>",
)
CODEX_ROLES = {"user", "assistant"}
CODEX_EVENT_TYPES = {
    "user_message",
    "agent_message",
    "task_started",
    "task_complete",
    "turn_aborted",
    "item_completed",
    "patch_apply_end",
    "exec_command_end",
}
CODEX_RESULT_KEYS = ("stdout", "stderr", "aggregated_output", "formatted_output")


def _codex_output_text(output: Any) -> str:
    if isinstance(output, str):
        return output
    if isinstance(output, dict):
        return _codex_output_text(output.get("output", output.get("content")))
    if isinstance(output, list):
        return "\n".join(
            str(part.get("text", ""))
            for part in output
            if isinstance(part, dict) and part.get("type") == "input_text"
        )
    return ""


def _codex_output(payload: dict[str, Any], sensitive: set[str]) -> dict[str, Any]:
    call_id = str(payload.get("call_id", ""))
    output = payload.get("output")
    if call_id in sensitive:
        text = RESULT_MARKER
    else:
        text = clip_result(_codex_output_text(output), RESULT_LIMIT)
    if isinstance(output, list):
        parts = [
            p for p in output if isinstance(p, dict) and p.get("type") == "input_text"
        ]
        shaped: Any = [
            {
                "type": "input_text",
                "text": clip_result(str(p.get("text", "")), RESULT_LIMIT),
            }
            for p in parts
        ]
        if text == RESULT_MARKER:
            shaped = [{"type": "input_text", "text": RESULT_MARKER}]
        return {"type": payload["type"], "call_id": call_id, "output": shaped}
    return {"type": payload["type"], "call_id": call_id, "output": text}


def _codex_call(payload: dict[str, Any], sensitive: set[str]) -> dict[str, Any]:
    if mentions_env_file(payload.get("arguments")) or mentions_env_file(
        payload.get("input")
    ):
        sensitive.add(str(payload.get("call_id", "")))
    out = {k: payload[k] for k in ("type", "name", "call_id") if k in payload}
    for key in ("arguments", "input"):
        if key in payload:
            out[key] = _redact_value(payload[key])
    return out


def _codex_part(part: Any) -> dict[str, Any] | None:
    """One message content part: text is scrubbed, images are kept if small enough."""
    if not isinstance(part, dict):
        return None
    if part.get("type") == "input_image":
        if _image_ok(part.get("image_url")):
            return {"type": "input_image", "image_url": part["image_url"]}
        return {"type": "input_text", "text": IMAGE_MARKER}
    return {"type": part.get("type"), "text": _scrub_text(str(part.get("text", "")))}


def _codex_message(payload: dict[str, Any]) -> dict[str, Any] | None:
    if payload.get("role") not in CODEX_ROLES:
        return None
    parts = []
    for raw in payload.get("content") or []:
        if str(raw.get("text", "") if isinstance(raw, dict) else "").startswith(
            CODEX_INJECTED_PREFIXES
        ):
            return None
        part = _codex_part(raw)
        if part is not None:
            parts.append(part)
    return (
        {"type": "message", "role": payload["role"], "content": parts}
        if parts
        else None
    )


def _codex_images(images: Any) -> list[str]:
    if not isinstance(images, list):
        return []
    return [i if _image_ok(i) else IMAGE_MARKER for i in images if isinstance(i, str)]


def _codex_change(change: Any) -> Any:
    if not isinstance(change, dict):
        return {}
    out = {k: v for k, v in change.items() if k != "content"}
    if "content" in change:
        out["content"] = clip_result(str(change["content"]), RESULT_LIMIT)
    return out


def _codex_item(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {}
    out = {k: item[k] for k in ("type", "id") if k in item}
    if item.get("type") == "FileChange" and isinstance(item.get("changes"), dict):
        out["changes"] = {path: _codex_change(c) for path, c in item["changes"].items()}
    return out


def _codex_event(payload: dict[str, Any]) -> dict[str, Any] | None:
    kind = payload.get("type")
    if kind not in CODEX_EVENT_TYPES:
        return None
    out: dict[str, Any] = {"type": kind}
    if isinstance(payload.get("message"), str):
        out["message"] = _scrub_text(payload["message"])
    if "images" in payload:
        out["images"] = _codex_images(payload["images"])
    if "collaboration_mode_kind" in payload:
        out["collaboration_mode_kind"] = payload["collaboration_mode_kind"]
    if "item" in payload:
        out["item"] = _codex_item(payload["item"])
    for key in CODEX_RESULT_KEYS:
        if isinstance(payload.get(key), str):
            out[key] = clip_result(payload[key], RESULT_LIMIT)
    return out


def _codex_response_item(
    payload: dict[str, Any], sensitive: set[str]
) -> dict[str, Any] | None:
    kind = payload.get("type")
    if kind == "message":
        return _codex_message(payload)
    if kind in ("function_call", "custom_tool_call"):
        return _codex_call(payload, sensitive)
    if kind in ("function_call_output", "custom_tool_call_output"):
        return _codex_output(payload, sensitive)
    return None


def _codex_payload(
    etype: Any, payload: dict[str, Any], sensitive: set[str]
) -> dict[str, Any] | None:
    if etype == "session_meta":
        return {k: payload[k] for k in ("id", "cwd", "cli_version") if k in payload}
    if etype == "turn_context":
        return {
            k: payload[k]
            for k in ("cwd", "model", "collaboration_mode")
            if k in payload
        }
    if etype == "response_item":
        return _codex_response_item(payload, sensitive)
    if etype == "event_msg":
        return _codex_event(payload)
    return None


def filter_codex(text: str) -> str:
    """Filter a Codex rollout transcript."""
    sensitive: set[str] = set()
    kept = []
    for entry in entries(text):
        payload = entry.get("payload")
        filtered = (
            _codex_payload(entry.get("type"), payload, sensitive)
            if isinstance(payload, dict)
            else None
        )
        if filtered is not None:
            kept.append(
                {
                    "timestamp": entry.get("timestamp"),
                    "type": entry["type"],
                    "payload": filtered,
                }
            )
    return _dumps(kept)


# ---- Copilot ------------------------------------------------------------

COPILOT_TOOL_RESULT_KEYS = ("success", "shellExecution")


def _copilot_start(data: dict[str, Any]) -> dict[str, Any]:
    out = {
        k: data[k] for k in ("sessionId", "copilotVersion", "startTime") if k in data
    }
    context = data.get("context")
    if isinstance(context, dict) and "cwd" in context:
        out["context"] = {"cwd": context["cwd"]}
    return out


def _copilot_user_message(data: dict[str, Any]) -> dict[str, Any]:
    out = {k: data[k] for k in ("messageId", "attachments") if k in data}
    if isinstance(data.get("content"), str):
        out["content"] = _scrub_text(data["content"])
    return out


def _copilot_request(request: Any) -> dict[str, Any]:
    if not isinstance(request, dict):
        return {}
    out = {k: request[k] for k in ("toolCallId", "name") if k in request}
    out["arguments"] = _redact_value(request.get("arguments"))
    return out


def _copilot_assistant_message(data: dict[str, Any]) -> dict[str, Any]:
    out = {k: data[k] for k in ("messageId", "model", "turnId") if k in data}
    if isinstance(data.get("content"), str):
        out["content"] = _scrub_text(data["content"])
    out["toolRequests"] = [_copilot_request(r) for r in data.get("toolRequests") or []]
    return out


def _copilot_tool_start(data: dict[str, Any], sensitive: set[str]) -> dict[str, Any]:
    if mentions_env_file(data.get("arguments")):
        sensitive.add(str(data.get("toolCallId", "")))
    out = {k: data[k] for k in ("toolCallId", "toolName", "turnId") if k in data}
    out["arguments"] = _redact_value(data.get("arguments"))
    return out


def _copilot_result_text(result: Any) -> str | None:
    if isinstance(result, str):
        return result
    if isinstance(result, dict) and isinstance(result.get("content"), str):
        return result["content"]
    return None


def _copilot_tool_complete(data: dict[str, Any], sensitive: set[str]) -> dict[str, Any]:
    call_id = str(data.get("toolCallId", ""))
    out = {
        k: data[k]
        for k in ("toolCallId", "turnId", *COPILOT_TOOL_RESULT_KEYS)
        if k in data
    }
    text = _copilot_result_text(data.get("result"))
    if text is not None:
        out["result"] = {
            "content": RESULT_MARKER
            if call_id in sensitive
            else clip_result(text, RESULT_LIMIT)
        }
    return out


def _copilot_asset(data: dict[str, Any]) -> dict[str, Any]:
    out = {
        k: data[k] for k in ("assetId", "type", "mimeType", "byteLength") if k in data
    }
    out["data"] = data["data"] if _image_ok(data.get("data")) else IMAGE_MARKER
    return out


def _copilot_data(
    kind: Any, data: dict[str, Any], sensitive: set[str]
) -> dict[str, Any] | None:
    plain: dict[str, tuple[str, ...]] = {
        "assistant.turn_start": ("turnId",),
        "assistant.turn_end": ("turnId",),
        "session.model_change": ("newModel", "source"),
        "permission.requested": ("requestId", "toolName", "fullCommandText"),
        "permission.completed": ("requestId", "result"),
        "skill.invoked": ("name", "path", "source"),
    }
    handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
        "session.start": _copilot_start,
        "session.resume": _copilot_start,
        "user.message": _copilot_user_message,
        "assistant.message": _copilot_assistant_message,
        "session.binary_asset": _copilot_asset,
    }
    if kind in plain:
        return {k: data[k] for k in plain[kind] if k in data}
    if kind in handlers:
        return handlers[kind](data)
    if kind == "tool.execution_start":
        return _copilot_tool_start(data, sensitive)
    if kind == "tool.execution_complete":
        return _copilot_tool_complete(data, sensitive)
    return None


def filter_copilot_events(text: str) -> str:
    """Filter a Copilot ``events.jsonl``."""
    sensitive: set[str] = set()
    kept = []
    for entry in entries(text):
        data = entry.get("data") if isinstance(entry.get("data"), dict) else {}
        filtered = _copilot_data(entry.get("type"), data, sensitive)
        if filtered is not None:
            kept.append(
                {
                    "type": entry["type"],
                    "timestamp": entry.get("timestamp"),
                    "data": filtered,
                }
            )
    return _dumps(kept)


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


def filter_copilot_dump(dump: dict[str, Any]) -> dict[str, Any]:
    """Keep only the columns the Copilot extractor reads, with text lines redacted."""
    out: dict[str, Any] = {}
    for table, columns in COPILOT_DB_COLUMNS.items():
        rows = dump.get(table)
        if isinstance(rows, list):
            out[table] = [
                _redact_value({c: row[c] for c in columns if c in row})
                for row in rows
                if isinstance(row, dict)
            ]
    return out


def drop_line(text: str, line_number: int) -> str:
    """Replace one 1-indexed JSONL line with a stub, used when GitHub flags that line."""
    lines = text.splitlines()
    if not 1 <= line_number <= len(lines):
        return text
    lines[line_number - 1] = json.dumps(STUB, separators=(",", ":"))
    return "\n".join(lines) + "\n"
