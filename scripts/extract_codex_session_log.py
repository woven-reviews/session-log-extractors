#!/usr/bin/env python3
"""Extract a Codex session transcript into a readable markdown conversation log.

Codex records CLI sessions as JSONL files under:

    ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl

Those logs have a different schema from Claude Code transcripts. Each line is
an event envelope with a top-level ``type`` such as ``session_meta``,
``event_msg``, or ``response_item``. The useful conversation data lives under
``payload``:

* ``event_msg`` / ``payload.type == "user_message"`` starts a real user turn.
* ``response_item`` / ``payload.type == "message"`` and role ``assistant`` is
  assistant text.
* ``response_item`` / ``payload.type == "function_call"`` is a tool call.
* ``response_item`` / ``payload.type == "function_call_output"`` is a tool
  result.

This script is mechanical: no LLM, no summarization, and no network access.
It intentionally ignores base instructions, developer messages, reasoning
payloads, token counts, and encrypted/internal fields.

When the human denies a tool's permission prompt, the decision is surfaced as a
"permission denied" note on the turn (the tool that was denied, plus any steering
message the human typed). Approvals leave no distinct record -- an approved tool
just runs -- so only denials are captured.

Codex Plan mode is identified from the per-turn collaboration-mode metadata.
Plan-mode turns are labeled in the output, and any structured ``update_plan``
calls made during them are rendered in full rather than condensed to tool
bullets. Unlike Claude Code's ``ExitPlanMode`` flow, Codex does not currently
record a separate plan approval result; later approval or steering is captured
as the next ordinary user turn.

Examples
--------
Every transcript for the current project, one file per session::

    python3 scripts/extract_codex_session_log.py

The most recent matching transcript, written to stdout::

    python3 scripts/extract_codex_session_log.py --output -

A specific session, written to one file::

    python3 scripts/extract_codex_session_log.py <session-id> --output session.md
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from session_log_envelope import (
    DEFAULT_TOOL_RESULT_MAX_BYTES,
    assistant_event,
    build_envelope,
    image_from_bytes,
    media_type_for_ext,
    raw_filename,
    tool_call,
    truncate_result,
    unavailable_image,
    user_event,
    write_envelope,
)
from skill_metadata import (
    load_command_details,
    load_skill_details,
    render_skill_lines,
)


CODEX_SESSIONS_ROOT = Path.home() / ".codex" / "sessions"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = PROJECT_ROOT / "codex_session_log.md"

# Pasted images are dumped here so an image-capable reader can describe them.
IMAGE_DUMP_DIR = Path(tempfile.gettempdir()) / "codex_session_images"
_IMAGE_EXT = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
}

TOOL_DESC_MAX = 120
RESULT_NOTE_MAX = 160

# Tools that ask the human for a direct answer. Codex has used both bare and
# namespaced tool names in logs, so matching is suffix-based in build_turns.
INTERACTION_TOOLS = {"request_user_input"}

# Codex can maintain a structured checklist through update_plan in either mode.
# It represents plan output only when the surrounding turn is explicitly marked
# as Plan mode; in Default mode it is an execution-progress tool.
PLAN_TOOLS = {"update_plan"}

_NOISE_TAG_BLOCK = re.compile(
    r"<(system-reminder|local-command-stdout|local-command-stderr|command-stdout|"
    r"command-stderr|command-name|command-message|command-args|task-notification|"
    r"bash-stdout|bash-stderr)>.*?"
    r"</\1>",
    re.DOTALL | re.IGNORECASE,
)
_NOISE_TAG_LOOSE = re.compile(
    r"</?(system-reminder|local-command-stdout|local-command-stderr|command-stdout|"
    r"command-stderr|command-name|command-message|command-args|task-notification|"
    r"bash-stdout|bash-stderr)\b[^>]*/?>",
    re.IGNORECASE,
)
_BASH_INPUT = re.compile(r"<bash-input>(.*?)</bash-input>", re.DOTALL | re.IGNORECASE)
_COMMAND_NAME = re.compile(
    r"<command-name>\s*(.*?)\s*</command-name>", re.DOTALL | re.IGNORECASE
)
_COMMAND_ARGS = re.compile(
    r"<command-args>\s*(.*?)\s*</command-args>", re.DOTALL | re.IGNORECASE
)
_SLASH_COMMAND = re.compile(r"^\s*(/[A-Za-z0-9][\w:.-]*)(?:\s+(.*?))?\s*$", re.DOTALL)
_DATA_IMAGE_URL = re.compile(r"^data:(image/[A-Za-z0-9.+-]+);base64,(.*)$", re.DOTALL)

# When the human denies a tool's permission prompt, the decision is fed back as
# the same canned rejection text the Claude harness emits; in Codex logs the
# orchestrator wraps it as an "[external_agent_tool_result: error]" assistant
# message. Approvals leave no distinct record -- the tool just runs -- so denials
# are the only permission decision we can observe here.
# ponytail: denials only; there is no approval marker in the transcript to parse.
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


def parse_permission_denial(text: Any) -> Optional[str]:
    """If ``text`` is a permission-denial message, return the human's steering
    message (``""`` if they just denied). Return ``None`` otherwise."""
    if not isinstance(text, str):
        return None
    stripped = _EXT_AGENT_PREFIX.sub("", text).lstrip()
    if not stripped.startswith(_PERMISSION_DENIAL_PREFIXES):
        return None
    m = _DENIAL_MESSAGE.search(stripped)
    return m.group(1).strip() if m else ""


def _truncate(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def clean_user_text(text: str) -> str:
    """Strip harness-injected wrappers while keeping the human's prose."""
    if not text:
        return ""
    cleaned = _NOISE_TAG_BLOCK.sub("", text)
    cleaned = _NOISE_TAG_LOOSE.sub("", cleaned)
    cleaned = re.sub(r"\n[ \t]*\n[ \t]*\n+", "\n\n", cleaned)
    return cleaned.strip()


def _parse_ts(ts: Optional[str]):
    if not ts or not isinstance(ts, str):
        return None
    try:
        from datetime import datetime

        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def format_timestamp(ts: Optional[str]) -> str:
    """Render as 'YYYY-MM-DD HH:MM:SS UTC' -- always UTC, never machine-local."""
    from datetime import timezone

    if not ts or not isinstance(ts, str):
        return "(no timestamp)"
    dt = _parse_ts(ts)
    if dt is None:
        return ts
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


def format_elapsed(first_ts: Optional[str], ts: Optional[str]) -> str:
    a, b = _parse_ts(first_ts), _parse_ts(ts)
    if a is None or b is None:
        return ""
    secs = max(0, int((b - a).total_seconds()))
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"+{h}:{m:02d}:{s:02d}"
    if m:
        return f"+{m}:{s:02d}"
    return f"+{s}s"


def date_only(ts: Optional[str]) -> str:
    formatted = format_timestamp(ts)
    if formatted == "(no timestamp)":
        return "unknown"
    return formatted.split(" ")[0]


def _jsonl_files(root: Path) -> List[Path]:
    if not root.is_dir():
        return []
    return [p for p in root.rglob("*.jsonl") if p.is_file()]


def newest(paths: Iterable[Path]) -> Optional[Path]:
    paths = list(paths)
    if not paths:
        return None
    return max(paths, key=lambda p: p.stat().st_mtime)


def load_entries(path: Path) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    bad = 0
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                bad += 1
                if bad <= 5:
                    print(
                        f"warning: skipping malformed JSON on line {lineno}",
                        file=sys.stderr,
                    )
                continue
            if isinstance(obj, dict):
                entries.append(obj)
            else:
                bad += 1
    if bad > 5:
        print(f"warning: skipped {bad} malformed/blank lines total", file=sys.stderr)
    return entries


def _payload(entry: Dict[str, Any]) -> Dict[str, Any]:
    payload = entry.get("payload")
    return payload if isinstance(payload, dict) else {}


def session_id_from_file(path: Path) -> Optional[str]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for _, line in zip(range(50), fh):
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") == "session_meta":
                    sid = _payload(obj).get("id")
                    return sid if isinstance(sid, str) else None
    except OSError:
        return None
    return None


def session_cwd_from_file(path: Path) -> Optional[str]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for _, line in zip(range(80), fh):
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = _payload(obj)
                if obj.get("type") == "session_meta":
                    cwd = payload.get("cwd")
                    if isinstance(cwd, str) and cwd:
                        return cwd
                if obj.get("type") == "turn_context":
                    cwd = payload.get("cwd")
                    if isinstance(cwd, str) and cwd:
                        return cwd
    except OSError:
        return None
    return None


def _paths_overlap(a: Path, b: Path) -> bool:
    try:
        a.resolve().relative_to(b.resolve())
        return True
    except ValueError:
        pass
    try:
        b.resolve().relative_to(a.resolve())
        return True
    except ValueError:
        return False


def _cwd_matches(
    transcript_cwd: Optional[str], cwd: Path, strict: bool = False
) -> bool:
    if not transcript_cwd:
        return False
    rec = Path(transcript_cwd).expanduser()
    if strict:
        return rec.resolve() == cwd.resolve()
    return _paths_overlap(rec, cwd)


def matching_transcripts(root: Path, cwd: Path, strict: bool = False) -> List[Path]:
    """Return every Codex transcript whose recorded cwd overlaps ``cwd``.

    In ``strict`` mode the recorded cwd must equal ``cwd`` exactly (no
    ancestor/descendant overlap).
    """
    return [
        p
        for p in _jsonl_files(root)
        if _cwd_matches(session_cwd_from_file(p), cwd, strict=strict)
    ]


def output_identifier(transcript: Path) -> str:
    """Stable filename identifier for one transcript.

    Prefer Codex's session id from ``session_meta``. Fall back to the rollout
    filename stem when older logs do not carry an id.
    """
    raw = session_id_from_file(transcript) or transcript.stem
    ident = re.sub(r"[^A-Za-z0-9_.-]+", "-", raw).strip("-")
    return ident or transcript.stem


def select_transcript(
    selector: Optional[str],
    transcript: Optional[str],
    sessions_root: Optional[str],
    strict: bool = False,
) -> Path:
    """Pick a Codex transcript.

    Precedence:
      1. --transcript PATH
      2. positional selector that is an existing file
      3. positional selector as filename/session-id prefix
      4. newest transcript whose recorded cwd overlaps the current cwd
      5. newest transcript anywhere under ~/.codex/sessions

    In ``strict`` mode step 4 requires an exact cwd match and step 5 (newest
    anywhere) is disabled, so only sessions started in the current directory
    are ever selected.
    """
    if transcript:
        p = Path(transcript).expanduser()
        if not p.is_file():
            raise FileNotFoundError(f"--transcript path does not exist: {p}")
        return p

    if selector:
        p = Path(selector).expanduser()
        if p.is_file():
            return p

    root = Path(sessions_root).expanduser() if sessions_root else CODEX_SESSIONS_ROOT
    files = _jsonl_files(root)

    if selector:
        matches: List[Path] = []
        for p in files:
            sid = session_id_from_file(p)
            if p.stem.startswith(selector) or (sid and sid.startswith(selector)):
                matches.append(p)
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            joined = "\n  ".join(str(p) for p in matches)
            raise FileNotFoundError(
                f"session selector {selector!r} is ambiguous; matches:\n  {joined}"
            )
        raise FileNotFoundError(f"no Codex transcript matching {selector!r}")

    cwd = Path.cwd()
    cwd_matches = [
        p for p in files if _cwd_matches(session_cwd_from_file(p), cwd, strict=strict)
    ]
    picked = newest(cwd_matches)
    if picked is not None:
        return picked

    if strict:
        raise FileNotFoundError(
            f"no Codex transcript for {cwd} under {root} "
            "(--strict: cwd-overlap and newest-anywhere fallback disabled)"
        )

    picked = newest(files)
    if picked is not None:
        return picked

    raise FileNotFoundError(
        f"could not locate any Codex transcript (looked under {root}); "
        "pass --transcript PATH"
    )


def _content_text(content: Any, text_keys: Tuple[str, ...]) -> str:
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


def user_text_from_event(payload: Dict[str, Any]) -> str:
    text = payload.get("message")
    if isinstance(text, str):
        return clean_user_text(text)
    return ""


def user_text_from_response_item(payload: Dict[str, Any]) -> str:
    """Text from Codex's user-role response item, used for initial prompts."""
    return clean_user_text(_content_text(payload.get("content"), ("text",)).strip())


def is_startup_context(text: str) -> bool:
    """Codex sends its plugin and workspace context as a user-role item."""
    return (
        text.startswith("<recommended_plugins>")
        and "# AGENTS.md instructions" in text
        and "<environment_context>" in text
    )


def assistant_text_from_payload(payload: Dict[str, Any]) -> str:
    return _content_text(payload.get("content"), ("text", "output_text")).strip()


def _image_ref_from_string(value: str) -> Optional[Dict[str, Any]]:
    if value.startswith("data:image/"):
        return {"type": "data_url", "data_url": value}
    if value.startswith("/") or value.startswith("~"):
        return {"type": "path", "path": value}
    return None


def image_refs_from_event(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Image refs from Codex ``event_msg:user_message`` payloads."""
    refs: List[Dict[str, Any]] = []

    local_images = payload.get("local_images")
    if isinstance(local_images, list):
        for item in local_images:
            if isinstance(item, str):
                ref = _image_ref_from_string(item)
                if ref:
                    refs.append(ref)

    images = payload.get("images")
    if isinstance(images, list):
        for item in images:
            if isinstance(item, str):
                ref = _image_ref_from_string(item)
                if ref:
                    refs.append(ref)
            elif isinstance(item, dict):
                url = item.get("image_url") or item.get("url")
                if isinstance(url, str):
                    ref = _image_ref_from_string(url)
                    if ref:
                        refs.append(ref)
                        continue

                path = item.get("path") or item.get("file_path")
                if isinstance(path, str):
                    ref = _image_ref_from_string(path)
                    if ref:
                        refs.append(ref)
                        continue

                data = item.get("data")
                if isinstance(data, str):
                    refs.append(
                        {
                            "type": "base64",
                            "data": data,
                            "media_type": item.get("media_type")
                            or item.get("mime_type"),
                        }
                    )

    return refs


def image_refs_from_user_message_payload(
    payload: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Base64 image refs from ``response_item`` user-message content."""
    refs: List[Dict[str, Any]] = []
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


def _decode_arguments(arguments: Any) -> Any:
    if isinstance(arguments, str):
        try:
            return json.loads(arguments)
        except json.JSONDecodeError:
            return arguments
    return arguments


def tool_descriptor(name: str, arguments: Any) -> str:
    args = _decode_arguments(arguments)
    if not isinstance(args, dict):
        return _truncate(str(args), TOOL_DESC_MAX) if args else ""

    for key in ("cmd", "command", "query", "pattern", "path", "file_path", "url"):
        val = args.get(key)
        if isinstance(val, str) and val:
            return _truncate(val, TOOL_DESC_MAX)

    # multi_tool_use.parallel stores nested recipient calls.
    tool_uses = args.get("tool_uses")
    if isinstance(tool_uses, list):
        names = []
        for item in tool_uses:
            if isinstance(item, dict) and isinstance(item.get("recipient_name"), str):
                names.append(item["recipient_name"])
        if names:
            return _truncate(", ".join(names), TOOL_DESC_MAX)

    try:
        return _truncate(json.dumps(args, ensure_ascii=False), TOOL_DESC_MAX)
    except (TypeError, ValueError):
        return ""


def skill_refs_from_call(name: str, arguments: Any) -> List[Dict[str, Optional[str]]]:
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

    short_name = name.rsplit(".", 1)[-1].lower()

    if short_name in ("skill", "read_skill"):
        if isinstance(args, dict):
            value = args.get("skill") or args.get("name") or args.get("package")
            if isinstance(value, str) and value:
                add(value)

    normalized_name = name.lower().replace("__", ".")
    if normalized_name.endswith("skills.read") and isinstance(args, dict):
        value = args.get("package") or args.get("skill") or args.get("name")
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


def skill_names_from_call(name: str, arguments: Any) -> List[str]:
    """Return skill names evidenced by a call (backwards-compatible helper)."""
    return [str(ref["name"]) for ref in skill_refs_from_call(name, arguments)]


def result_note(output: Any) -> str:
    if isinstance(output, str):
        return _truncate(output, RESULT_NOTE_MAX)
    try:
        return _truncate(json.dumps(output, ensure_ascii=False), RESULT_NOTE_MAX)
    except (TypeError, ValueError):
        return ""


def _is_interaction_tool(name: str) -> bool:
    return any(name == tool or name.endswith("." + tool) for tool in INTERACTION_TOOLS)


def _is_plan_tool(name: str) -> bool:
    return any(name == tool or name.endswith("." + tool) for tool in PLAN_TOOLS)


def _collaboration_mode(payload: Dict[str, Any]) -> str:
    """Return the normalized collaboration mode carried by a rollout event."""
    mode = payload.get("collaboration_mode_kind")
    if not isinstance(mode, str):
        collaboration = payload.get("collaboration_mode")
        if isinstance(collaboration, dict):
            mode = collaboration.get("mode")
    return mode.strip().lower() if isinstance(mode, str) else ""


def _plan_update(arguments: Any) -> Optional[Dict[str, Any]]:
    """Decode one structured update_plan call for first-class rendering."""
    args = _decode_arguments(arguments)
    if not isinstance(args, dict) or not isinstance(args.get("plan"), list):
        return None
    steps = []
    for item in args["plan"]:
        if not isinstance(item, dict) or not isinstance(item.get("step"), str):
            continue
        steps.append(
            {
                "step": item["step"],
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


def _decode_jsonish(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _value_to_answer_text(value: Any) -> str:
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


def _extract_answer_from_item(question: Dict[str, Any], item: Any) -> str:
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


def _structured_answer_for_question(question: Dict[str, Any], output: Any) -> str:
    output = _decode_jsonish(output)
    qid = question.get("id")
    qtext = question.get("question")
    header = question.get("header")

    if isinstance(output, dict):
        for key in (qid, qtext, header):
            if isinstance(key, str) and key in output:
                text = _value_to_answer_text(output[key])
                if text:
                    return text

        for container_key in ("answers", "responses", "selections", "values"):
            container = output.get(container_key)
            if isinstance(container, dict):
                for key in (qid, qtext, header):
                    if isinstance(key, str) and key in container:
                        text = _value_to_answer_text(container[key])
                        if text:
                            return text
                for value in container.values():
                    text = _extract_answer_from_item(question, value)
                    if text:
                        return text
            elif isinstance(container, list):
                for item in container:
                    text = _extract_answer_from_item(question, item)
                    if text:
                        return text

        text = _extract_answer_from_item(question, output)
        if text:
            return text

    if isinstance(output, list):
        for item in output:
            text = _extract_answer_from_item(question, item)
            if text:
                return text

    return ""


def _flatten_result_text(output: Any) -> str:
    output = _decode_jsonish(output)
    if isinstance(output, str):
        return output
    if isinstance(output, (int, float, bool)):
        return str(output)
    if isinstance(output, list):
        return " ".join(_flatten_result_text(item) for item in output)
    if isinstance(output, dict):
        parts: List[str] = []
        for key in ("message", "text", "output", "content", "answer", "value"):
            if key in output:
                text = _flatten_result_text(output[key])
                if text:
                    parts.append(text)
        if not parts:
            for value in output.values():
                text = _flatten_result_text(value)
                if text:
                    parts.append(text)
        return " ".join(parts)
    return ""


def _parse_chosen(
    question_text: str, options: List[Dict[str, Any]], result_text: str
) -> Tuple[str, List[Dict[str, Any]]]:
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
    chosen = [o for o in options if o.get("label") and o["label"] in val]
    return val, chosen


def option_qa_from_output(
    output: Any, questions: Optional[List[Any]]
) -> List[Dict[str, Any]]:
    if not questions:
        return []

    result_text = _flatten_result_text(output)
    out: List[Dict[str, Any]] = []
    for q in questions:
        if not isinstance(q, dict):
            continue
        options = [o for o in q.get("options", []) if isinstance(o, dict)]
        val, chosen = _parse_chosen(q.get("question", ""), options, result_text)
        if not val:
            val = _structured_answer_for_question(q, output)
            chosen = [o for o in options if o.get("label") and o["label"] in val]
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


def patch_paths(arguments: Any) -> List[str]:
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


def append_unique_path(paths: List[str], seen: set, path: str) -> None:
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


class Turn:
    def __init__(
        self, user_text: str, timestamp: Optional[str], collaboration_mode: str = ""
    ):
        self.user_text = user_text
        self.timestamp = timestamp
        self.collaboration_mode = collaboration_mode
        self.images: List[Dict[str, Any]] = []
        self.shell_command: Optional[str] = None
        self.command: Optional[str] = None
        self.command_args: Optional[str] = None
        self.command_details: Dict[str, Any] = {}
        self.assistant_text_blocks: List[str] = []
        self.tool_bullets: List[str] = []
        self.skills_used: List[str] = []
        self.skill_details: Dict[str, Dict[str, Any]] = {}
        self.result_notes: List[str] = []
        self.option_qas: List[Dict[str, Any]] = []
        self.permission_denials: List[Dict[str, Any]] = []  # denied tool + message
        self.plan_updates: List[Dict[str, Any]] = []

    def add_assistant_text(self, text: str) -> None:
        if text and text.strip():
            self.assistant_text_blocks.append(text.strip())

    def add_tool(self, name: str, descriptor: str) -> None:
        if descriptor:
            self.tool_bullets.append(f"- {name} -> {descriptor}")
        else:
            self.tool_bullets.append(f"- {name}")

    def add_skill(self, name: str, path: Optional[str] = None) -> None:
        if name and name not in self.skills_used:
            self.skills_used.append(name)
        if name and (
            name not in self.skill_details or (path and not self.skill_details[name])
        ):
            self.skill_details[name] = load_skill_details(name, path, PROJECT_ROOT)

    def add_command(self, name: str, args: str = "") -> None:
        self.command = name
        self.command_args = args or None
        self.command_details = load_command_details(name, PROJECT_ROOT)

    def add_result_note(self, note: str) -> None:
        if note:
            self.result_notes.append(note)


def build_turns(entries: List[Dict[str, Any]]) -> Tuple[List[Turn], List[str]]:
    turns: List[Turn] = []
    files_changed: List[str] = []
    files_seen = set()
    current: Optional[Turn] = None
    call_names: Dict[str, str] = {}
    pending_questions: Dict[str, List[Any]] = {}
    pending_plan_calls = set()
    pending_user_images: List[Dict[str, Any]] = []
    pending_response_user: Optional[Tuple[str, Optional[str], List[Dict[str, Any]]]] = None
    last_tool = ""  # most recent tool call, to name a denied permission
    pending_mode = ""

    def start_user_turn(
        text: str,
        timestamp: Optional[str],
        images: List[Dict[str, Any]],
        raw_text: Optional[str] = None,
    ) -> None:
        nonlocal current, last_tool
        raw_text = raw_text or text
        tagged_command = _COMMAND_NAME.search(raw_text)
        slash_command = _SLASH_COMMAND.match(text)
        command = ""
        command_args = ""
        if tagged_command:
            command = tagged_command.group(1).strip()
            args_match = _COMMAND_ARGS.search(raw_text)
            command_args = args_match.group(1).strip() if args_match else ""
        elif slash_command:
            command = slash_command.group(1)
            command_args = (slash_command.group(2) or "").strip()
        if command and not command.startswith("/"):
            command = ""
        if not text and not images and not command:
            return
        current = Turn(text, timestamp, pending_mode)
        current.images = images
        if command:
            current.user_text = ""
            current.add_command(command, command_args)
        cmds = [c.strip() for c in _BASH_INPUT.findall(text) if c.strip()]
        if cmds:
            current.shell_command = "\n".join(cmds)
        turns.append(current)
        last_tool = ""

    def flush_pending_response_user() -> None:
        nonlocal pending_response_user
        if pending_response_user is not None:
            start_user_turn(*pending_response_user)
            pending_response_user = None

    for entry in entries:
        etype = entry.get("type")
        payload = _payload(entry)
        ptype = payload.get("type")

        mode = _collaboration_mode(payload)
        if mode:
            pending_mode = mode

        if etype == "turn_context":
            # Context describes the next/active task but is not conversational.
            if current is not None and mode and not current.collaboration_mode:
                current.collaboration_mode = mode
            continue

        if etype == "event_msg":
            if ptype == "user_message":
                text = user_text_from_event(payload)
                response_images: List[Dict[str, Any]] = []
                if pending_response_user is not None:
                    response_images = pending_response_user[2]
                    pending_response_user = None
                images = response_images or pending_user_images or image_refs_from_event(payload)
                pending_user_images = []
                start_user_turn(
                    text,
                    entry.get("timestamp"),
                    images,
                    payload.get("message")
                    if isinstance(payload.get("message"), str)
                    else text,
                )
                continue

            if ptype == "patch_apply_end":
                changes = payload.get("changes")
                if isinstance(changes, dict):
                    for fp in changes:
                        if isinstance(fp, str):
                            append_unique_path(files_changed, files_seen, fp)
                if current is not None:
                    stdout = payload.get("stdout")
                    stderr = payload.get("stderr")
                    note = result_note(stdout or stderr)
                    if note:
                        current.add_result_note("apply_patch: " + note)
                continue

        if etype != "response_item":
            continue

        if ptype == "message":
            role = payload.get("role")
            if role == "user":
                text = user_text_from_response_item(payload)
                images = image_refs_from_user_message_payload(payload)
                if text and not is_startup_context(text):
                    flush_pending_response_user()
                    pending_response_user = (text, entry.get("timestamp"), images)
                else:
                    pending_user_images = images
                continue
            if role == "assistant":
                flush_pending_response_user()
                if current is None:
                    current = Turn("", entry.get("timestamp"), pending_mode)
                    turns.append(current)
                text = assistant_text_from_payload(payload)
                message = parse_permission_denial(text)
                if message is not None:
                    current.permission_denials.append(
                        {"tool": last_tool or "(tool call)", "message": message}
                    )
                else:
                    current.add_assistant_text(text)
            # Ignore developer/system/user context response_items. Real user turns
            # are represented by event_msg:user_message above.
            continue

        if ptype in ("function_call", "custom_tool_call"):
            flush_pending_response_user()
            if current is None:
                current = Turn("", entry.get("timestamp"), pending_mode)
                turns.append(current)
            name = (
                payload.get("name") if isinstance(payload.get("name"), str) else "tool"
            )
            last_tool = name
            call_id = payload.get("call_id")
            if isinstance(call_id, str):
                call_names[call_id] = name
            arguments = payload.get("arguments")
            if arguments is None:
                arguments = payload.get("input")
            if current.collaboration_mode == "plan" and _is_plan_tool(name):
                plan = _plan_update(arguments)
                if plan:
                    current.plan_updates.append(plan)
                    if isinstance(call_id, str):
                        pending_plan_calls.add(call_id)
                    continue
            current.add_tool(name, tool_descriptor(name, arguments))
            for skill_ref in skill_refs_from_call(name, arguments):
                current.add_skill(str(skill_ref["name"]), skill_ref.get("path"))
            args = _decode_arguments(arguments)

            if isinstance(call_id, str) and _is_interaction_tool(name):
                qs = args.get("questions") if isinstance(args, dict) else None
                if isinstance(qs, list):
                    pending_questions[call_id] = qs

            if name.endswith("apply_patch") or name == "apply_patch":
                for fp in patch_paths(arguments):
                    append_unique_path(files_changed, files_seen, fp)
            continue

        if ptype in ("function_call_output", "custom_tool_call_output"):
            if current is None:
                continue
            call_id = payload.get("call_id")
            if isinstance(call_id, str) and call_id in pending_plan_calls:
                pending_plan_calls.remove(call_id)
                continue
            prefix = ""
            if isinstance(call_id, str) and call_id in call_names:
                prefix = f"{call_names[call_id]}: "
            output = payload.get("output")
            questions = (
                pending_questions.get(call_id) if isinstance(call_id, str) else None
            )
            qas = option_qa_from_output(output, questions)
            if qas:
                current.option_qas.extend(qas)
                continue
            note = result_note(output)
            if note:
                current.add_result_note(prefix + note)
            continue

    flush_pending_response_user()
    return turns, files_changed


def envelope_images(refs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Resolve Codex image refs to inline base64 entries.

    Codex records images out of band — as a filesystem path, a data URL, or raw
    base64 — so every ref has to be resolved to bytes before it can travel with
    the log. A ref that will not resolve (most often a path whose file has since
    been deleted) is recorded as unavailable rather than dropped: "pasted a
    screenshot we can no longer read" and "pasted nothing" are different facts,
    and only one of them is about the candidate.
    """
    images: List[Dict[str, Any]] = []
    for ref in refs:
        decoded = _image_bytes_and_ext(ref)
        if decoded is None:
            images.append(unavailable_image(ref.get("path") or ref.get("type")))
            continue
        raw, ext = decoded
        images.append(image_from_bytes(raw, media_type_for_ext(ext)))
    return images


def build_events(
    entries: List[Dict[str, Any]],
    tool_result_max_bytes: int = DEFAULT_TOOL_RESULT_MAX_BYTES,
) -> List[Dict[str, Any]]:
    """Walk Codex entries in order, producing raw-envelope events.

    A second pass alongside ``build_turns``, deliberately not a refactor of it,
    so the markdown stays byte-for-byte what it was.
    """
    events: List[Dict[str, Any]] = []
    pending_calls: Dict[str, Dict[str, Any]] = {}
    pending_user_images: List[Dict[str, Any]] = []
    pending_response_user: Optional[Tuple[str, Optional[str], List[Dict[str, Any]]]] = None
    index = 0

    def flush_pending_response_user() -> None:
        nonlocal pending_response_user, index
        if pending_response_user is None:
            return
        text, timestamp, refs = pending_response_user
        events.append(user_event(index, timestamp, text, envelope_images(refs)))
        index += 1
        pending_response_user = None

    for entry in entries:
        etype = entry.get("type")
        payload = _payload(entry)
        ptype = payload.get("type")

        if etype == "event_msg" and ptype == "user_message":
            text = user_text_from_event(payload)
            response_refs: List[Dict[str, Any]] = []
            if pending_response_user is not None:
                response_refs = pending_response_user[2]
                pending_response_user = None
            refs = response_refs or pending_user_images or image_refs_from_event(payload)
            pending_user_images = []
            images = envelope_images(refs)
            if not text and not images:
                continue
            events.append(user_event(index, entry.get("timestamp"), text, images))
            index += 1
            continue

        if etype != "response_item":
            continue

        if ptype == "message":
            role = payload.get("role")
            if role == "user":
                text = user_text_from_response_item(payload)
                refs = image_refs_from_user_message_payload(payload)
                if text and not is_startup_context(text):
                    flush_pending_response_user()
                    pending_response_user = (text, entry.get("timestamp"), refs)
                else:
                    pending_user_images = refs
                continue
            if role == "assistant":
                flush_pending_response_user()
                text = assistant_text_from_payload(payload)
                if not text.strip():
                    continue
                events.append(assistant_event(index, entry.get("timestamp"), text))
                index += 1
            continue

        if ptype in ("function_call", "custom_tool_call"):
            flush_pending_response_user()
            name = (
                payload.get("name") if isinstance(payload.get("name"), str) else "tool"
            )
            arguments = payload.get("arguments")
            if arguments is None:
                arguments = payload.get("input")
            call = tool_call(
                name, _decode_arguments(arguments), None, tool_result_max_bytes
            )
            events.append(assistant_event(index, entry.get("timestamp"), "", [call]))
            index += 1
            call_id = payload.get("call_id")
            if isinstance(call_id, str):
                pending_calls[call_id] = call
            continue

        if ptype in ("function_call_output", "custom_tool_call_output"):
            call = pending_calls.pop(payload.get("call_id"), None)
            if call is None:
                continue
            call.update(
                truncate_result(
                    _flatten_result_text(payload.get("output")), tool_result_max_bytes
                )
            )
            continue

    flush_pending_response_user()
    return events


def write_raw_envelope(
    transcript: Path,
    entries: List[Dict[str, Any]],
    out_dir: Path,
    tool_result_max_bytes: int = DEFAULT_TOOL_RESULT_MAX_BYTES,
) -> Optional[Path]:
    """Write the raw envelope for one Codex transcript."""
    events = build_events(entries, tool_result_max_bytes)
    if not events:
        return None

    session_id = output_identifier(transcript)
    envelope = build_envelope(
        harness="codex",
        session_id=session_id,
        events=events,
        cwd=session_cwd_from_file(transcript),
        started_at=first_timestamp(entries),
        models=session_models(entries),
        tool_result_max_bytes=tool_result_max_bytes,
    )
    out_path = out_dir / raw_filename("codex", session_id)
    write_envelope(out_path, envelope)
    return out_path


def _image_bytes_and_ext(ref: Dict[str, Any]) -> Optional[Tuple[bytes, str]]:
    kind = ref.get("type")

    if kind == "data_url":
        data_url = ref.get("data_url")
        if not isinstance(data_url, str):
            return None
        match = _DATA_IMAGE_URL.match(data_url)
        if not match:
            return None
        media_type, data = match.groups()
        try:
            raw = base64.b64decode(data, validate=False)
        except (ValueError, TypeError):
            return None
        return raw, _IMAGE_EXT.get(media_type, "img")

    if kind == "base64":
        data = ref.get("data")
        if not isinstance(data, str):
            return None
        try:
            raw = base64.b64decode(data, validate=False)
        except (ValueError, TypeError):
            return None
        media_type = ref.get("media_type")
        return raw, _IMAGE_EXT.get(media_type, "img") if isinstance(
            media_type, str
        ) else "img"

    if kind == "path":
        path_value = ref.get("path")
        if not isinstance(path_value, str):
            return None
        path = Path(path_value).expanduser()
        try:
            raw = path.read_bytes()
        except OSError:
            return None
        ext = path.suffix.lstrip(".") or "img"
        return raw, ext

    return None


def dump_images(
    turns: List[Turn], session_id: str, dump_dir: Path = IMAGE_DUMP_DIR
) -> List[Path]:
    """Write each turn's pasted images to ``dump_dir`` and append a marker.

    The marker text matches the Claude extractor so an image-capable describe
    pass can replace it with ``[Image: ... Text: ...]``.
    """
    written: List[Path] = []
    for ti, turn in enumerate(turns, 1):
        for n, ref in enumerate(turn.images, 1):
            decoded = _image_bytes_and_ext(ref)
            if decoded is None:
                continue
            raw, ext = decoded
            path = dump_dir / f"{session_id}_turn{ti}_img{n}.{ext}"
            dump_dir.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
            written.append(path)
            turn.user_text = (
                f"{turn.user_text}\n\n[Image dumped to `{path}` — description pending]"
            ).strip()
    return written


def first_timestamp(entries: List[Dict[str, Any]]) -> Optional[str]:
    for entry in entries:
        ts = entry.get("timestamp")
        if isinstance(ts, str) and ts:
            return ts
    return None


def session_models(entries: List[Dict[str, Any]]) -> List[str]:
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


def derive_project_label(transcript: Path, entries: List[Dict[str, Any]]) -> str:
    for entry in entries:
        payload = _payload(entry)
        if entry.get("type") in ("session_meta", "turn_context"):
            cwd = payload.get("cwd")
            if isinstance(cwd, str) and cwd:
                sid = payload.get("id")
                if isinstance(sid, str) and sid:
                    return f"{cwd}  (session: {sid})"
                return cwd
    return str(transcript)


def _derive_context_line(turns: List[Turn]) -> str:
    for turn in turns:
        if turn.command:
            return f'a Codex session starting with command: "{turn.command}"'
        if turn.user_text.strip():
            first = _truncate(turn.user_text, 80)
            return f'a Codex session starting with: "{first}"'
    return "a Codex session"


def _quote(text: str) -> List[str]:
    """Blockquote text so embedded markdown remains inside the log section."""
    return [f"> {line}" if line.strip() else ">" for line in text.splitlines()]


def _render_plan_update(plan: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    if plan.get("explanation"):
        out.extend(_quote(plan["explanation"]))
    for item in plan.get("steps", []):
        status = item.get("status", "pending")
        mark = "x" if status == "completed" else " "
        out.append(f"- [{mark}] {item.get('step', '')} _({status})_")
    return out


def render_summary(turns: List[Turn], first_ts: Optional[str]) -> List[str]:
    """Render the up-front recap of every real user input."""
    out: List[str] = ["## Summary - user inputs", ""]
    input_turns = [
        (i, t) for i, t in enumerate(turns, 1) if t.user_text.strip() or t.command
    ]
    if not input_turns:
        out += ["_(No user inputs in this transcript.)_", ""]
        return out

    prev_ts: Optional[str] = None
    for i, turn in input_turns:
        elapsed = format_elapsed(first_ts, turn.timestamp) or "+?"
        delta = format_elapsed(prev_ts, turn.timestamp) if prev_ts else "+0s"
        out.append(
            f"### Turn {i} - {format_timestamp(turn.timestamp)} "
            f"({elapsed}, delta {delta})"
        )
        out.append("")
        if turn.command:
            head = f"Invoked command: **{turn.command}**"
            if turn.command_args:
                head += f" `{turn.command_args}`"
            out.append(head)
            out.append("")
            out.extend(
                render_skill_lines(turn.command, turn.command_details, kind="Command")
            )
        elif turn.shell_command:
            out.append("Ran shell command:")
            out.append("")
            out.append("```sh")
            out.append(turn.shell_command)
            out.append("```")
        else:
            for line in turn.user_text.splitlines():
                out.append(f"> {line}" if line.strip() else ">")
        out.append("")
        if turn.collaboration_mode == "plan":
            out.append("_Codex Plan mode turn._")
            out.append("")

        for skill in turn.skills_used:
            out.extend(render_skill_lines(skill, turn.skill_details.get(skill)))
            out.append("")

        if turn.plan_updates:
            out.append(
                f"_Structured plan updated {len(turn.plan_updates)} "
                f"time{'s' if len(turn.plan_updates) != 1 else ''}; latest state:_"
            )
            out.extend(_render_plan_update(turn.plan_updates[-1]))
            out.append("")

        for qa in turn.option_qas:
            label = qa["header"] or "Question"
            out.append(f"_Answered via options ({label}):_")
            if qa["question"]:
                out.append(f"> {qa['question']}")
            for option in qa["options"]:
                mark = "x" if option.get("label") in qa["chosen_labels"] else " "
                row = f"- [{mark}] **{option.get('label', '')}**"
                if option.get("description"):
                    row += f" - {option['description']}"
                out.append(row)
            if not qa["chosen_labels"] and qa["answer_text"]:
                out.append(f"- [x] _(custom answer)_ {qa['answer_text']}")
            out.append("")

        for d in turn.permission_denials:
            out.append(f"_Denied permission:_ **{d['tool']}**")
            if d["message"]:
                for line in d["message"].splitlines():
                    out.append(f"> {line}" if line.strip() else ">")
            out.append("")

        prev_ts = turn.timestamp
    return out


def render(
    turns: List[Turn],
    files_changed: List[str],
    project_label: str,
    first_ts: Optional[str],
    models: Optional[List[str]] = None,
) -> str:
    out: List[str] = []
    out.append("# Codex Session Conversation Log")
    out.append("")
    out.append(
        f"A turn-by-turn log of the conversation for {_derive_context_line(turns)}."
    )
    out.append(f"Project: {project_label}. Date: {date_only(first_ts)}.")
    if models:
        out.append(f"Model: {', '.join(models)}.")
    out.append("")
    out.append("---")

    if not turns:
        out.append("")
        out.append("_(No conversational turns found in this Codex transcript.)_")
        out.append("")
        return "\n".join(out)

    out.append("")
    out.extend(render_summary(turns, first_ts))
    out.append("---")
    out.append("")
    out.append("# Full turn-by-turn detail")

    for i, turn in enumerate(turns, 1):
        out.append("")
        elapsed = format_elapsed(first_ts, turn.timestamp)
        header = f"## Turn {i} - {format_timestamp(turn.timestamp)}"
        if elapsed:
            header += f" ({elapsed} into session)"
        out.append(header)
        out.append("")
        if turn.collaboration_mode == "plan":
            out.append("**Mode:** Plan")
            out.append("")
        if turn.command:
            head = f"**User invoked command:** {turn.command}"
            if turn.command_args:
                head += f" `{turn.command_args}`"
            out.append(head)
            out.append("")
            out.extend(
                render_skill_lines(
                    turn.command, turn.command_details, indent="  ", kind="Command"
                )
            )
        elif turn.shell_command:
            out.append("**User ran shell command:**")
            out.append("")
            out.append("```sh")
            out.append(turn.shell_command)
            out.append("```")
        elif turn.user_text.strip():
            out.append(f"**User:** {turn.user_text}")
        else:
            out.append("**User:** _(no user text - pre-conversation activity)_")
        out.append("")

        if turn.assistant_text_blocks:
            assistant_body = "\n\n".join(turn.assistant_text_blocks)
            out.append(f"**Assistant:** {assistant_body}")
        elif turn.tool_bullets or turn.plan_updates:
            out.append("**Assistant:**")
        else:
            out.append("**Assistant:** _(no response captured)_")

        if turn.tool_bullets:
            out.append("")
            out.extend(turn.tool_bullets)

        if turn.skills_used:
            out.append("")
            for skill in turn.skills_used:
                out.extend(
                    render_skill_lines(
                        skill, turn.skill_details.get(skill), indent="  "
                    )
                )

        if turn.result_notes:
            notes = "; ".join(turn.result_notes[:6])
            extra = len(turn.result_notes) - 6
            if extra > 0:
                notes += f"; (+{extra} more results)"
            out.append("")
            out.append(f"  _results:_ {_truncate(notes, 500)}")

        if turn.plan_updates:
            for number, plan in enumerate(turn.plan_updates, 1):
                out.append("")
                heading = "**Assistant updated the Plan mode plan:**"
                if len(turn.plan_updates) > 1:
                    heading = f"**Assistant updated the Plan mode plan ({number}):**"
                out.append(heading)
                out.append("")
                out.extend(_render_plan_update(plan))

        if turn.option_qas:
            out.append("")
            for qa in turn.option_qas:
                chosen = (
                    ", ".join(c for c in qa["chosen_labels"] if c)
                    or qa["answer_text"]
                    or "(no selection)"
                )
                q = qa["header"] or _truncate(qa["question"], 80)
                out.append(f"  _user chose:_ {q} -> {chosen}")

        if turn.permission_denials:
            out.append("")
            for d in turn.permission_denials:
                line = f"  _user denied permission:_ {d['tool']}"
                if d["message"]:
                    line += f' -> "{_truncate(d["message"], 160)}"'
                out.append(line)

        out.append("")
        out.append("---")

    if files_changed:
        out.append("")
        out.append("## Files changed during the session")
        out.append("")
        out.append("```")
        out.extend(files_changed)
        out.append("```")

    out.append("")
    return "\n".join(out)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="extract_codex_session_log.py",
        description=(
            "Extract a Codex CLI session JSONL transcript into a markdown "
            "conversation log. Mechanical extraction only; no LLM or network."
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
            "codex_session_log_<session-id>.md (in this mode --output is treated "
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
            "root); for a selected single session, the markdown file (default: "
            f"{DEFAULT_OUTPUT}). Use '-' for stdout and the newest single session"
        ),
    )
    parser.add_argument(
        "--raw",
        dest="raw",
        action="store_true",
        default=True,
        help=(
            "also write the raw envelope codex_session_log_raw_<session-id>.json "
            "alongside the markdown, carrying the full conversation with pasted "
            "images inlined as base64 (default: on)"
        ),
    )
    parser.add_argument(
        "--no-raw",
        dest="raw",
        action="store_false",
        help="skip the raw envelope and write only the markdown log",
    )
    parser.add_argument(
        "--raw-tool-result-bytes",
        metavar="N",
        type=int,
        default=DEFAULT_TOOL_RESULT_MAX_BYTES,
        help=(
            "cap each tool result in the raw envelope at N bytes; the original "
            "size is always recorded. Use a negative value for no cap "
            f"(default: {DEFAULT_TOOL_RESULT_MAX_BYTES})"
        ),
    )
    return parser


def render_transcript(
    transcript: Path,
    raw_output: Optional[Path] = None,
    tool_result_max_bytes: int = DEFAULT_TOOL_RESULT_MAX_BYTES,
) -> str:
    """Parse one Codex transcript file and return its rendered markdown.

    When ``raw_output`` is given, the raw envelope is also written there.
    """
    entries = load_entries(transcript)
    if not entries:
        raise ValueError(f"no parseable JSON entries found in {transcript}")
    turns, files_changed = build_turns(entries)
    dump_images(turns, output_identifier(transcript))
    markdown = render(
        turns=turns,
        files_changed=files_changed,
        project_label=derive_project_label(transcript, entries),
        first_ts=first_timestamp(entries),
        models=session_models(entries),
    )
    if raw_output is not None:
        raw_path = write_raw_envelope(
            transcript, entries, raw_output, tool_result_max_bytes
        )
        if raw_path is not None:
            print(f"  wrote {raw_path}", file=sys.stderr)
    return markdown


def _unique_identifier(base: str, used: set) -> str:
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
    raw: bool = True,
    tool_result_max_bytes: int = DEFAULT_TOOL_RESULT_MAX_BYTES,
) -> int:
    """Extract every matching Codex transcript, one markdown file each."""
    root = Path(sessions_root).expanduser() if sessions_root else CODEX_SESSIONS_ROOT
    cwd = Path.cwd()
    files = sorted(
        matching_transcripts(root, cwd, strict=strict),
        key=lambda p: (p.stat().st_mtime, str(p)),
    )
    if not files:
        print(
            f"error: no Codex transcripts for {cwd} under {root}",
            file=sys.stderr,
        )
        return 1

    out_dir = Path(output).expanduser() if output else PROJECT_ROOT
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"extracting {len(files)} Codex session(s) from {root}", file=sys.stderr)

    written = 0
    used_ids = set()
    for transcript in files:
        ident = _unique_identifier(output_identifier(transcript), used_ids)
        try:
            markdown = render_transcript(
                transcript,
                raw_output=out_dir if raw else None,
                tool_result_max_bytes=tool_result_max_bytes,
            )
        except (OSError, ValueError) as exc:
            print(f"  skip {ident}: {exc}", file=sys.stderr)
            continue

        out_path = out_dir / f"codex_session_log_{ident}.md"
        try:
            out_path.write_text(markdown, encoding="utf-8")
        except OSError as exc:
            print(f"  error writing {out_path}: {exc}", file=sys.stderr)
            continue

        written += 1
        print(f"  wrote {out_path}", file=sys.stderr)

    print(f"done: {written}/{len(files)} session(s) written", file=sys.stderr)
    return 0 if written else 1


def main(argv: Optional[List[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)

    default_all = not args.session and not args.transcript and args.output != "-"
    if args.all or default_all:
        out = None if args.output == "-" else args.output
        return _extract_all(
            args.sessions_root,
            out,
            strict=args.strict,
            raw=args.raw,
            tool_result_max_bytes=args.raw_tool_result_bytes,
        )

    try:
        transcript = select_transcript(
            selector=args.session,
            transcript=args.transcript,
            sessions_root=args.sessions_root,
            strict=args.strict,
        )
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"using transcript: {transcript}", file=sys.stderr)

    # The envelope is a file, so there is nowhere to put it when the markdown is
    # going to stdout. Write it next to the markdown otherwise.
    if not args.raw or args.output == "-":
        raw_output = None
    elif args.output is None:
        raw_output = DEFAULT_OUTPUT.parent
    else:
        raw_output = Path(args.output).expanduser().parent

    try:
        markdown = render_transcript(
            transcript,
            raw_output=raw_output,
            tool_result_max_bytes=args.raw_tool_result_bytes,
        )
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.output == "-":
        sys.stdout.write(markdown)
        if not markdown.endswith("\n"):
            sys.stdout.write("\n")
    else:
        out_path = (
            DEFAULT_OUTPUT if args.output is None else Path(args.output).expanduser()
        )
        try:
            out_path.write_text(markdown, encoding="utf-8")
        except OSError as exc:
            print(f"error: could not write {out_path}: {exc}", file=sys.stderr)
            return 1
        print(f"wrote {out_path}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
