#!/usr/bin/env python3
"""Build the raw session-log envelope shared by all three session-log extractors.

The markdown log each extractor writes is a *condensed* view: tool calls collapse
to one bullet, tool results collapse to a short note, and pasted images are
dumped to a temp file and replaced with a text marker. That is the right shape
for scoring, but it means the image bytes and the agent's actual output never
leave the machine the session ran on.

This module builds the second artifact — a normalized JSON envelope carrying the
full conversation with images inlined as base64, so a grader can examine what the
candidate actually pasted and what the agent actually did.

One schema serves all three harnesses. That matters because they store images
very differently: Claude Code already has them inline as base64 in the
transcript, Codex records filesystem paths, and Copilot keeps them in a SQLite
``attachments`` table. Each extractor resolves its own images to bytes and hands
them here, so everything downstream sees one shape.

The envelope is deliberately *not* a copy of the harness's own transcript. A
verbatim copy would carry three incompatible schemas downstream and, for Codex
and Copilot, would not carry the images at all.

Standard library only; works on Python 3.8+.
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

SCHEMA = "raw-session-log/1"

# Tool results are the single biggest contributor to transcript size — a handful
# of whole-file Reads dwarfs every prompt in the session. The conversation is
# what a grader needs, so results are kept but capped, with the original size
# recorded so a truncation is never mistaken for a short result.
DEFAULT_TOOL_RESULT_MAX_BYTES = 4096

# Qualified stores solution files inside a single Mongo document, and MongoDB
# caps a BSON document at 16 MB. The drag-and-drop upload allows session logs up
# to 10 MB; warn well before that so an oversized envelope is noticed here rather
# than as a failed upload.
SIZE_WARN_BYTES = 8 * 1024 * 1024

# Extension by media type, mirroring the _IMAGE_EXT maps in the extractors.
_MEDIA_TYPE_BY_EXT = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "bmp": "image/bmp",
    "svg": "image/svg+xml",
}


def raw_filename(harness: str, session_id: str) -> str:
    """Filename for a harness's raw envelope.

    Every harness gets an explicit prefix, including Claude — unlike the markdown
    log, whose Claude variant is unprefixed (``session_log_<id>.md``) and must
    stay that way: the consumer that selects it matches only ``codex_`` and
    ``copilot_`` prefixes, so renaming it would make it unselectable.
    """
    return f"{harness}_session_log_raw_{session_id}.json"


def media_type_for_ext(ext: Optional[str]) -> Optional[str]:
    """Best-effort media type from a file extension, for image refs that only
    carried a path. Returns None when the extension is unknown, which is honest:
    a wrong media type renders as a broken image downstream."""
    if not ext:
        return None
    return _MEDIA_TYPE_BY_EXT.get(ext.lower().lstrip("."))


def image_from_bytes(raw: bytes, media_type: Optional[str]) -> Dict[str, Any]:
    """An inline image entry from decoded bytes (Codex / Copilot path)."""
    return {
        "media_type": media_type or "application/octet-stream",
        "data": base64.b64encode(raw).decode("ascii"),
        "bytes": len(raw),
    }


def image_from_base64(data: str, media_type: Optional[str]) -> Dict[str, Any]:
    """An inline image entry from an already-base64 payload (Claude path).

    Claude Code stores pasted images as base64 in the transcript, so re-encoding
    would be a pointless decode/encode round trip. The byte count is still
    reported from the decoded length so it means the same thing across harnesses;
    if the payload does not decode, the entry is marked unavailable rather than
    carrying a size we cannot stand behind.
    """
    try:
        size = len(base64.b64decode(data, validate=False))
    except (ValueError, TypeError):
        return unavailable_image("undecodable base64 payload")
    return {
        "media_type": media_type or "application/octet-stream",
        "data": data,
        "bytes": size,
    }


def unavailable_image(ref: Any) -> Dict[str, Any]:
    """An image the extractor knew about but could not resolve to bytes.

    Recorded rather than skipped: a session where the candidate pasted a
    screenshot whose source file has since been deleted is materially different
    from one where they pasted nothing, and silently dropping it erases that.
    """
    return {"unavailable": True, "ref": ref if isinstance(ref, str) else repr(ref)}


def truncate_result(
    text: Any, max_bytes: int = DEFAULT_TOOL_RESULT_MAX_BYTES
) -> Dict[str, Any]:
    """Cap a tool result, recording its original size.

    Truncation is done on UTF-8 bytes (the budget that matters for file size) but
    backed off to a character boundary so the result stays valid UTF-8.
    """
    if text is None:
        return {"result": None, "result_bytes": 0, "result_truncated": False}
    if not isinstance(text, str):
        text = str(text)

    encoded = text.encode("utf-8")
    total = len(encoded)
    if max_bytes < 0 or total <= max_bytes:
        return {"result": text, "result_bytes": total, "result_truncated": False}

    clipped = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return {"result": clipped, "result_bytes": total, "result_truncated": True}


def user_event(
    index: int,
    timestamp: Optional[str],
    text: str,
    images: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    event: Dict[str, Any] = {
        "i": index,
        "ts": timestamp,
        "role": "user",
        "text": text or "",
    }
    if images:
        event["images"] = images
    return event


def assistant_event(
    index: int,
    timestamp: Optional[str],
    text: str,
    tool_calls: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    event: Dict[str, Any] = {
        "i": index,
        "ts": timestamp,
        "role": "assistant",
        "text": text or "",
    }
    if tool_calls:
        event["tool_calls"] = tool_calls
    return event


def tool_call(
    name: str,
    tool_input: Any,
    result: Any = None,
    max_bytes: int = DEFAULT_TOOL_RESULT_MAX_BYTES,
) -> Dict[str, Any]:
    """One tool call with its input verbatim and its result capped.

    The input is kept in full: it is usually small, and it is the part that shows
    what the agent was actually asked to do.
    """
    call: Dict[str, Any] = {"name": name, "input": _jsonable(tool_input)}
    call.update(truncate_result(result, max_bytes))
    return call


def build_envelope(
    harness: str,
    session_id: str,
    events: List[Dict[str, Any]],
    cwd: Optional[str] = None,
    started_at: Optional[str] = None,
    models: Optional[List[str]] = None,
    tool_result_max_bytes: int = DEFAULT_TOOL_RESULT_MAX_BYTES,
) -> Dict[str, Any]:
    return {
        "schema": SCHEMA,
        "harness": harness,
        "session_id": session_id,
        "cwd": cwd,
        "started_at": started_at,
        "models": models or [],
        "truncation": {"tool_result_max_bytes": tool_result_max_bytes},
        "events": events,
    }


def write_envelope(path: Path, envelope: Dict[str, Any]) -> int:
    """Write the envelope and return its size in bytes.

    Warns above SIZE_WARN_BYTES so an oversized file is caught while the author
    is still looking at the terminal, not when the upload is rejected.
    """
    payload = json.dumps(envelope, ensure_ascii=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")

    size = len(payload.encode("utf-8"))
    if size > SIZE_WARN_BYTES:
        print(
            f"  warning: {path.name} is {_human_size(size)} — session-log uploads "
            f"are capped at 10 MB; consider a lower --raw-tool-result-bytes",
            file=sys.stderr,
        )
    return size


def _human_size(size: int) -> str:
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size} B"


def _jsonable(value: Any) -> Any:
    """Coerce a tool input to something json.dumps can handle.

    Tool inputs come straight from a transcript, so they are normally plain JSON
    already. This is a guard against a harness stashing something exotic, so one
    odd tool call cannot fail the whole export.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return repr(value)
