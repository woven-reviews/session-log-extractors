#!/usr/bin/env python3
"""Extract Claude Code JSONL transcripts into JSON conversation envelopes.

Transcripts live under ~/.claude/projects/<encoded-project-path>/<session-id>.jsonl.
Non-alphanumeric path characters each become one dash. Extraction is mechanical:
no LLM, summarization, or network access. Recorded Markdown remains message text;
images stay inline as base64, and tool results retain their configured byte cap.

By default, export all sessions in the resolved project directory to the project
root as claude_session_log_raw_<session-id>.json. A session id or --transcript
selects one session; --output PATH writes its JSON there, and --output - writes
JSON to stdout. Diagnostics go to stderr.

Examples::

    uv run --locked python scripts/extract_claude_session_log.py
    uv run --locked python scripts/extract_claude_session_log.py 1a2b3c4d-... --output -
    uv run --locked python scripts/extract_claude_session_log.py --transcript /path/to/abc.jsonl
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from session_log_extractors import session_log_envelope
from session_log_extractors.claude_actions import (
    AGENT_TOOLS,
    FILE_PATH_KEYS,
    FILE_TOOLS,
    FILE_WRITING_TOOLS,
    SKILL_TOOL,
    _attach_actions,
    _attach_user_invocations,
)
from session_log_extractors.claude_authorship import (
    _assistant_authorship,
    _attribute_command_outputs,
    _queued_authorship,
    _user_authorship,
)
from session_log_extractors.claude_decisions import (
    PLAN_TOOLS,
    _attach_decisions,
    parse_permission_denial,
)
from session_log_extractors.claude_questions import (
    ASK_USER_QUESTION,
    _attach_questions,
)
from session_log_extractors.claude_sources import (
    ClaudeSource,
    read_source,
    read_subagent_sources,
    select_project_session,
)
from session_log_extractors.claude_sources import (
    load_entries as load_entries,
)
from session_log_extractors.extractor_contract import Extractor
from session_log_extractors.json_types import JsonValue, string_value
from session_log_extractors.session_log_envelope import (
    DEFAULT_TOOL_RESULT_MAX_BYTES,
    TOOL_RESULT_BYTES_FLAG,
    Envelope,
    assistant_event,
    image_from_base64,
    raw_filename,
    serialize_envelope,
    user_event,
    warn_if_oversized,
    write_envelope,
)
from session_log_extractors.session_model import (
    Event,
    Message,
    NativeIdentity,
    Session,
    SubagentSummary,
    ToolCall,
    ToolResult,
)
from session_log_extractors.tool_inputs.claude import input_schema

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

PROJECTS_ROOT = Path.home() / ".claude" / "projects"

# Tools that prompt the human for a direct answer (pop-up questions, etc.). Their
# tool_result carries the human's selection, so we surface it as a user
# interaction rather than folding it into the generic result notes.
INTERACTION_TOOLS = {ASK_USER_QUESTION}

# Max length for a condensed tool descriptor before it gets an ellipsis.
TOOL_DESC_MAX = 100

# Legacy sidecar-summary cleaning only. Main conversation text is preserved;
# wrapper spelling alone does not establish authorship.
_NOISE_TAG_BLOCK = re.compile(
    r"<(system-reminder|local-command-stdout|local-command-stderr|command-stdout|"
    r"command-stderr|command-name|command-message|command-args|task-notification|"
    r"bash-stdout|bash-stderr)>.*?"
    r"</\1>",
    re.DOTALL | re.IGNORECASE,
)
# Self-closing / unmatched variants and bare opening tags of the same family.
_NOISE_TAG_LOOSE = re.compile(
    r"</?(system-reminder|local-command-stdout|local-command-stderr|command-stdout|"
    r"command-stderr|command-name|command-message|command-args|task-notification|"
    r"bash-stdout|bash-stderr)\b[^>]*/?>",
    re.IGNORECASE,
)

# A `!`-prefixed shell command the human ran in-session is recorded as a user
# message wrapped in <bash-input>…</bash-input>. We surface it as a shell command
# alongside its unchanged prose. Output wrappers also remain in the conversation.
_BASH_INPUT = re.compile(r"<bash-input>(.*?)</bash-input>", re.DOTALL | re.IGNORECASE)

# A slash-command invocation (e.g. `/extract-claude-session-logs`) is recorded as
# a user message wrapping <command-name>…</command-name> (plus command-message and
# command-args). Retain these action fields alongside the original text.
_COMMAND_NAME = re.compile(
    r"<command-name>\s*(.*?)\s*</command-name>", re.DOTALL | re.IGNORECASE
)
_COMMAND_ARGS = re.compile(
    r"<command-args>\s*(.*?)\s*</command-args>", re.DOTALL | re.IGNORECASE
)


# --------------------------------------------------------------------------- #
# Transcript discovery
# --------------------------------------------------------------------------- #


def encode_project_path(path: Path) -> str:
    """Encode a filesystem path the way Claude Code names its project dirs.

    Each character that is not ``[A-Za-z0-9]`` becomes one dash -- runs are
    *not* collapsed, so Windows ``C:\\Users\\me`` encodes as ``C--Users-me``.
    """
    raw = str(path)
    return re.sub(r"[^A-Za-z0-9]", "-", raw)


def _candidate_project_dirs(cwd: Path, strict: bool = False) -> Iterable[Path]:
    """Yield plausible encoded-project dirs for ``cwd``, most specific first.

    Tries the cwd itself and then each parent, so running from a subdirectory of
    the project still resolves to the project's transcript dir. In ``strict``
    mode only the cwd itself is considered -- no parent-walk.
    """
    seen = set()
    bases = [cwd] if strict else [cwd, *cwd.parents]
    for base in bases:
        encoded = encode_project_path(base)
        d = PROJECTS_ROOT / encoded
        if d not in seen:
            seen.add(d)
            yield d


def resolve_project_dir(
    explicit: Optional[str], cwd: Path, strict: bool = False
) -> Optional[Path]:
    """Resolve the encoded project dir.

    If ``explicit`` is given it wins (and need not exist yet -- caller validates).
    Otherwise look for an existing transcript dir matching the cwd or one of its
    parents. Returns ``None`` if nothing matched (caller falls back globally).
    In ``strict`` mode only the exact cwd is matched (no parent-walk).
    """
    if explicit:
        return Path(explicit).expanduser()
    for d in _candidate_project_dirs(cwd, strict=strict):
        if d.is_dir():
            return d
    return None


def _jsonl_files(directory: Path) -> List[Path]:
    if not directory.is_dir():
        return []
    return [p for p in directory.iterdir() if p.is_file() and p.suffix == ".jsonl"]


def newest(paths: Iterable[Path]) -> Optional[Path]:
    paths = list(paths)
    if not paths:
        return None
    return max(paths, key=lambda p: p.stat().st_mtime)


def select_transcript(
    selector: Optional[str],
    transcript: Optional[str],
    project_dir: Optional[str],
    strict: bool = False,
) -> Path:
    """Pick the transcript file to parse, following the documented precedence.

    Precedence:
      1. ``--transcript PATH`` (explicit file).
      2. positional ``selector`` that is itself a path to an existing file.
      3. positional ``selector`` as a case-sensitive recorded ID: unique exact
         match, otherwise unique literal prefix in the resolved project dir.
      4. newest ``.jsonl`` in the resolved project dir.
      5. newest ``.jsonl`` across every project dir (global fallback).

    In ``strict`` mode the project dir must match the cwd exactly (no
    parent-walk) and the global fallback (step 5) is disabled, so the script
    only ever reads sessions started in the current directory.

    Raises ``FileNotFoundError`` with a clear message when nothing resolves.
    """
    cwd = Path.cwd()

    if transcript:
        return _explicit_transcript(transcript)

    direct = _selector_path(selector)
    if direct is not None:
        return direct

    proj = resolve_project_dir(project_dir, cwd, strict=strict)

    if selector:
        picked = select_project_session(selector, proj) if proj is not None else None
        if picked is not None:
            return picked
        where = f" in {proj}" if proj is not None else ""
        raise FileNotFoundError(f"no transcript matching {selector!r}{where}")

    # Newest in the resolved project dir.
    if proj is not None:
        picked = newest(_jsonl_files(proj))
        if picked is not None:
            return picked

    # Strict mode: never reach past the cwd's own project dir.
    if strict:
        raise FileNotFoundError(
            f"no Claude Code transcript for {cwd} under {PROJECTS_ROOT} "
            "(--strict: parent-walk and global fallback disabled)"
        )

    picked = _newest_global_transcript()
    if picked is not None:
        return picked

    raise FileNotFoundError(
        "could not locate any Claude Code transcript "
        f"(looked under {PROJECTS_ROOT}); pass --transcript PATH"
    )


def _explicit_transcript(transcript: str) -> Path:
    path = Path(transcript).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"--transcript path does not exist: {path}")
    return path


def _selector_path(selector: Optional[str]) -> Optional[Path]:
    if selector:
        path = Path(selector).expanduser()
        if path.is_file():
            return path
    return None


def _newest_global_transcript() -> Optional[Path]:
    all_files: List[Path] = []
    if PROJECTS_ROOT.is_dir():
        for directory in PROJECTS_ROOT.iterdir():
            all_files.extend(_jsonl_files(directory))
    return newest(all_files)


# --------------------------------------------------------------------------- #
# Content-block helpers
# --------------------------------------------------------------------------- #


def _get_message(entry: Dict[str, JsonValue]) -> Dict[str, JsonValue]:
    msg = entry.get("message")
    return msg if isinstance(msg, dict) else {}


def _content_blocks(entry: Dict[str, JsonValue]) -> List[JsonValue]:
    """Return the message content as a list of blocks.

    Content may be a plain string (older / simple user messages) or a list of
    typed blocks. Always normalize to a list so callers can iterate.
    """
    content = _get_message(entry).get("content")
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return content
    return []


def _is_tool_result_entry(entry: Dict[str, JsonValue]) -> bool:
    """True if this user-role entry is a tool result, not a human message."""
    for block in _content_blocks(entry):
        if isinstance(block, dict) and block.get("type") == "tool_result":
            return True
    # Some versions stash the result under a top-level toolUseResult and leave
    # content empty; treat those as tool results too.
    if entry.get("toolUseResult") is not None and not _human_text(entry).strip():
        return True
    return False


def _human_text(entry: Dict[str, JsonValue]) -> str:
    """Concatenate the text blocks of a (user or assistant) message."""
    parts: List[str] = []
    for block in _content_blocks(entry):
        if isinstance(block, dict) and block.get("type") == "text":
            txt = block.get("text")
            if isinstance(txt, str):
                parts.append(txt)
        elif isinstance(block, str):
            parts.append(block)
    return "\n".join(parts)


def _image_sources(entry: Dict[str, JsonValue]) -> List[Dict[str, JsonValue]]:
    """Base64 image ``source`` dicts attached to a message, in order."""
    out: List[Dict[str, JsonValue]] = []
    for block in _content_blocks(entry):
        if isinstance(block, dict) and block.get("type") == "image":
            src = block.get("source")
            if (
                isinstance(src, dict)
                and src.get("type") == "base64"
                and isinstance(src.get("data"), str)
            ):
                out.append(src)
    return out


def clean_user_text(text: str) -> str:
    """Strip harness-injected wrappers while keeping the human's prose."""
    if not text:
        return ""
    cleaned = _NOISE_TAG_BLOCK.sub("", text)
    cleaned = _NOISE_TAG_LOOSE.sub("", cleaned)
    # Collapse the blank-line runs that removal can leave behind.
    cleaned = re.sub(r"\n[ \t]*\n[ \t]*\n+", "\n\n", cleaned)
    return cleaned.strip()


def _truncate(text: str, limit: int) -> str:
    text = " ".join(text.split())  # flatten whitespace/newlines for one-liners
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"  # ellipsis


def tool_descriptor(name: str, tool_input: object) -> str:
    """Build a short one-line descriptor for a tool_use block."""
    if not isinstance(tool_input, dict):
        return _truncate(str(tool_input), TOOL_DESC_MAX) if tool_input else ""

    # File-oriented tools: show the path.
    for key in FILE_PATH_KEYS:
        if key in tool_input and isinstance(tool_input[key], str):
            return _truncate(str(tool_input[key]), TOOL_DESC_MAX)

    # Shell.
    if "command" in tool_input and isinstance(tool_input["command"], str):
        return _truncate(tool_input["command"], TOOL_DESC_MAX)

    # Common search/agent tools.
    for key in ("pattern", "query", "url", "prompt", "description"):
        if key in tool_input and isinstance(tool_input[key], str):
            return _truncate(str(tool_input[key]), TOOL_DESC_MAX)

    # Fallback: compact JSON of the input.
    try:
        return _truncate(json.dumps(tool_input, ensure_ascii=False), TOOL_DESC_MAX)
    except (TypeError, ValueError):
        return ""


def extract_file_path(tool_input: object) -> Optional[str]:
    if not isinstance(tool_input, dict):
        return None
    for key in FILE_PATH_KEYS:
        val = tool_input.get(key)
        if isinstance(val, str) and val:
            return val
    return None


def _result_block_text(block: Dict[str, JsonValue]) -> str:
    """Flatten a tool_result block's content to a string."""
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            str(c["text"])
            for c in content
            if isinstance(c, dict) and isinstance(c.get("text"), str)
        )
    return ""


def _message_identity(entry: Dict[str, JsonValue]) -> NativeIdentity:
    """Retain native record, parent, and API-message IDs without guessing links."""
    identity = NativeIdentity()
    record_id = entry.get("uuid")
    parent_id = entry.get("parentUuid")
    message_id = _get_message(entry).get("id")
    if isinstance(record_id, str):
        identity["id"] = record_id
    if isinstance(parent_id, str):
        identity["parent_id"] = parent_id
    if isinstance(message_id, str):
        identity["message_id"] = message_id
    return identity


def _attach_identity(message: NativeIdentity, identity: NativeIdentity) -> None:
    if "id" in identity:
        message["id"] = identity["id"]
    if "parent_id" in identity:
        message["parent_id"] = identity["parent_id"]
    if "message_id" in identity:
        message["message_id"] = identity["message_id"]


def _attach_native_result(record: ToolResult, entry: Dict[str, JsonValue]) -> None:
    """Keep alternate result evidence only when its result-block owner is unique."""
    payload = entry.get("toolUseResult")
    if "toolUseResult" in entry:
        record["native_result"] = payload
    denial_kind = entry.get("toolDenialKind")
    if isinstance(denial_kind, str):
        record["native_denial_kind"] = denial_kind


def _ambiguous_call_ids(entries: List[Dict[str, JsonValue]]) -> set[str]:
    counts: Counter[str] = Counter()
    interaction_ids = _interaction_call_ids(entries)
    for entry in entries:
        if entry.get("type") != "assistant" or entry.get("isSidechain") is True:
            continue
        for block in _content_blocks(entry):
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            tool_id = block.get("id")
            if isinstance(tool_id, str) and tool_id:
                counts[tool_id] += 1
    return {
        tool_id
        for tool_id, count in counts.items()
        if count > 1 and tool_id in interaction_ids
    }


def _interaction_call_ids(entries: List[Dict[str, JsonValue]]) -> set[str]:
    """Limit stricter ambiguous-ID handling to observed interaction facts."""
    ids: set[str] = set()
    for entry in entries:
        if entry.get("type") not in {"assistant", "user"}:
            continue
        for block in _content_blocks(entry):
            if not isinstance(block, dict):
                continue
            if (
                block.get("type") == "tool_use"
                and string_value(block.get("name")) in PLAN_TOOLS | INTERACTION_TOOLS
            ):
                value = block.get("id")
            elif (
                block.get("type") == "tool_result"
                and parse_permission_denial(_result_block_text(block)) is not None
            ):
                value = block.get("tool_use_id")
            else:
                continue
            if isinstance(value, str) and value:
                ids.add(value)
    return ids


def _tool_result_record(
    entry: Dict[str, JsonValue], block: Dict[str, JsonValue]
) -> ToolResult:
    tool_id = block.get("tool_use_id")
    record = ToolResult(
        tool_call_id=tool_id if isinstance(tool_id, str) else None,
        ts=string_value(entry.get("timestamp")),
        content=block.get("content"),
        is_error=bool(block.get("is_error")),
    )
    _attach_identity(record, _message_identity(entry))
    return record


class _ClaudeInterpreter:
    """Interpret ordered messages and associate tool activity by native call ID."""

    def __init__(self, ambiguous_call_ids: set[str]) -> None:
        self.ambiguous_call_ids = ambiguous_call_ids
        self.results_with_alternates: List[ToolResult] = []
        self.question_results: List[ToolResult] = []
        self.events: List[Message] = []
        self.files_changed: List[str] = []
        self.skills_used: List[str] = []
        self.subagent_count = 0
        self.pending_calls: Dict[str, ToolCall] = {}
        self.unsupported_tools: List[str] = []

    def consume(self, entry: Dict[str, JsonValue]) -> None:
        etype = entry.get("type")
        if etype in ("summary", "system"):
            return
        if entry.get("isSidechain") is True:
            if etype == "user" and not _is_tool_result_entry(entry):
                self.subagent_count += 1
            return
        if etype == "user":
            if _is_tool_result_entry(entry):
                self.results(entry)
            else:
                self.user(entry)
        elif etype == "assistant":
            self.assistant(entry)
        elif etype == "attachment":
            self.queued_message(entry)

    def user(self, entry: Dict[str, JsonValue]) -> None:
        text = _human_text(entry)
        images = [
            image_from_base64(str(src["data"]), string_value(src.get("media_type")))
            for src in _image_sources(entry)
        ]
        match = _COMMAND_NAME.search(text)
        command = match.group(1).strip() if match else ""
        command = command if command.startswith("/") else ""
        if not text and not images and not command:
            return
        message = user_event(
            len(self.events), string_value(entry.get("timestamp")), text, images
        )
        _attach_identity(message, _message_identity(entry))
        if text:
            message["authorship"] = _user_authorship(entry, text)
        if command:
            args = _COMMAND_ARGS.search(text)
            message["command"] = command
            message["command_args"] = (args.group(1).strip() if args else "") or None
        _attach_user_invocations(message, entry)
        commands = [c.strip() for c in _BASH_INPUT.findall(text) if c.strip()]
        if commands:
            message["shell_command"] = "\n".join(commands)
        self.events.append(message)

    def queued_message(self, entry: Dict[str, JsonValue]) -> None:
        attachment = entry.get("attachment")
        if (
            not isinstance(attachment, dict)
            or attachment.get("type") != "queued_command"
        ):
            return
        rendered = entry.get("rendered")
        parts: List[str] = []
        for block in rendered if isinstance(rendered, list) else []:
            if isinstance(block, dict):
                content = block.get("content")
                if isinstance(content, str):
                    parts.append(content)
        text = "\n".join(parts) or string_value(attachment.get("prompt")) or ""
        if not text:
            return
        message = Event(
            i=len(self.events),
            ts=string_value(entry.get("timestamp")),
            role=string_value(entry.get("renderedRole")) or "unknown",
            text=text,
            authorship=_queued_authorship(entry, text, attachment),
        )
        _attach_identity(message, _message_identity(entry))
        self.events.append(message)

    def assistant(self, entry: Dict[str, JsonValue]) -> None:
        texts: List[str] = []
        calls: List[ToolCall] = []
        for block in _content_blocks(entry):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text = block.get("text", "")
                if isinstance(text, str) and text.strip():
                    texts.append(text.strip())
            elif block.get("type") == "tool_use":
                calls.append(self.call(block))
        if texts or calls:
            message = assistant_event(
                len(self.events),
                string_value(entry.get("timestamp")),
                "\n\n".join(texts),
                calls,
            )
            _attach_identity(message, _message_identity(entry))
            message["authorship"] = _assistant_authorship(
                entry, message.get("text") or ""
            )
            self.events.append(message)

    def call(self, block: Dict[str, JsonValue]) -> ToolCall:
        name = string_value(block.get("name")) or "tool"
        tool_input = block.get("input")
        # Preserve Claude's explicit null result for unanswered calls.
        schema = input_schema(name, tool_input)
        call = ToolCall(name=name, input=tool_input, input_schema=schema, result=None)
        diagnostic_name = str(name)
        if schema == "unsupported" and diagnostic_name not in self.unsupported_tools:
            self.unsupported_tools.append(diagnostic_name)
        tool_id = block.get("id")
        if isinstance(tool_id, str):
            call["id"] = tool_id
            if tool_id:
                if (
                    tool_id in self.pending_calls
                    and name in FILE_TOOLS | AGENT_TOOLS | {SKILL_TOOL}
                ):
                    self.ambiguous_call_ids.add(tool_id)
                self.pending_calls[tool_id] = call
        self.action(call)
        return call

    def action(self, call: ToolCall) -> None:
        name, tool_input = call["name"], call["input"]
        if not isinstance(tool_input, dict):
            return
        if str(name).lower() == "skill":
            skill = tool_input.get("skill") or tool_input.get("name")
            if isinstance(skill, str) and skill:
                call["skill"] = skill
                if skill not in self.skills_used:
                    self.skills_used.append(skill)
        if name in FILE_WRITING_TOOLS:
            path = extract_file_path(tool_input)
            if path and path not in self.files_changed:
                self.files_changed.append(path)

    def results(self, entry: Dict[str, JsonValue]) -> None:
        blocks = [
            block
            for block in _content_blocks(entry)
            if isinstance(block, dict) and block.get("type") == "tool_result"
        ]
        unmatched: List[ToolResult] = []
        payload = entry.get("toolUseResult")
        ambiguous_payload = (
            len(blocks) > 1
            and isinstance(payload, dict)
            and ("answers" in payload or "annotations" in payload)
        )
        if len(blocks) != 1 and payload is not None:
            record = ToolResult(
                tool_call_id=None,
                ts=string_value(entry.get("timestamp")),
                content=payload,
                is_error=False,
            )
            _attach_identity(record, _message_identity(entry))
            _attach_native_result(record, entry)
            unmatched.append(record)
        for block in blocks:
            record = _tool_result_record(entry, block)
            tool_id = record["tool_call_id"]
            call = (
                self.pending_calls.pop(tool_id, None) if tool_id is not None else None
            )
            if len(blocks) == 1:
                _attach_native_result(record, entry)
                if call is not None and call["name"] == ASK_USER_QUESTION:
                    self.question_results.append(record)
            if tool_id in self.ambiguous_call_ids:
                call = None
            if ambiguous_payload and call is not None:
                self.results_with_alternates.append(record)
            if call is None:
                unmatched.append(record)
            else:
                self.complete_call(call, record, block)
        if unmatched:
            message = Event(
                i=len(self.events),
                role="user",
                text="",
                ts=string_value(entry.get("timestamp")),
                tool_results=unmatched,
            )
            _attach_identity(message, _message_identity(entry))
            self.events.append(message)

    def complete_call(
        self, call: ToolCall, record: ToolResult, block: Dict[str, JsonValue]
    ) -> None:
        call["result_record"] = record
        result_text = _result_block_text(block)
        call["result"] = result_text
        if block.get("is_error"):
            call["is_error"] = True


def build_events(
    entries: List[Dict[str, JsonValue]],
    tool_result_max_bytes: int = DEFAULT_TOOL_RESULT_MAX_BYTES,
) -> List[Message]:
    """Compatibility export helper; native interpretation is shared with the CLI."""
    envelope = build_raw_envelope(Path("session.jsonl"), entries, tool_result_max_bytes)
    return envelope["events"]


def first_timestamp(entries: List[Dict[str, JsonValue]]) -> Optional[str]:
    for entry in entries:
        ts = string_value(entry.get("timestamp"))
        if isinstance(ts, str) and ts:
            return ts
    return None


def session_models(entries: List[Dict[str, JsonValue]]) -> List[str]:
    """Distinct model ids used by assistant messages, in first-seen order.

    A session can switch models mid-run (e.g. /model), so return all of them.
    """
    models: List[str] = []
    for entry in entries:
        model = _get_message(entry).get("model")
        # Synthetic/no-model assistant turns report "<synthetic>"; skip those.
        if (
            isinstance(model, str)
            and model
            and model != "<synthetic>"
            and model not in models
        ):
            models.append(model)
    return models


def session_cwd(entries: List[Dict[str, JsonValue]]) -> Optional[str]:
    """The working directory the session ran in, from the first entry carrying it."""
    for entry in entries:
        cwd = entry.get("cwd")
        if isinstance(cwd, str) and cwd:
            return cwd
    return None


def build_raw_envelope(
    transcript: Path,
    entries: List[Dict[str, JsonValue]],
    tool_result_max_bytes: int = DEFAULT_TOOL_RESULT_MAX_BYTES,
) -> Envelope:
    """Build the normal JSON envelope, including sessions with no events."""
    session = EXTRACTOR.interpret_source(ClaudeSource(transcript, entries))
    return session_log_envelope.export_session(session, tool_result_max_bytes)


def _recorded_session_id(source: ClaudeSource) -> str:
    identifiers: set[str] = set()
    for entry in source.entries:
        identifier = entry.get("sessionId")
        if isinstance(identifier, str) and identifier:
            identifiers.add(identifier)
    if len(identifiers) > 1:
        conflicting = ", ".join(repr(value) for value in sorted(identifiers))
        raise ValueError(f"Conflicting Claude session IDs: {conflicting}")
    return next(iter(identifiers), source.path.stem)


def interpret_source(
    source: ClaudeSource,
) -> Session:
    """Interpret supplied records, inline images and source identity without I/O."""
    adapter = _ClaudeInterpreter(_ambiguous_call_ids(source.entries))
    for entry in source.entries:
        adapter.consume(entry)
    _attribute_command_outputs(source.entries, adapter.events)
    _attach_questions(
        adapter.events, adapter.results_with_alternates, adapter.question_results
    )
    _attach_decisions(adapter.events)
    session = Session(
        harness="claude",
        session_id=_recorded_session_id(source),
        events=adapter.events,
        skills_used=adapter.skills_used,
        subagent_count=adapter.subagent_count,
        unsupported_tools=adapter.unsupported_tools,
        files_changed=adapter.files_changed,
        cwd=session_cwd(source.entries),
        started_at=first_timestamp(source.entries),
        models=session_models(source.entries),
    )
    _attach_actions(session, source.entries)
    return session


class ClaudeExtractor(Extractor[Path, ClaudeSource]):
    """Claude implementation of the shared public extraction stages."""

    def read_source(self, request: Path) -> ClaudeSource:
        return read_source(request)

    def interpret_source(self, source: ClaudeSource) -> Session:
        return interpret_source(source)


EXTRACTOR = ClaudeExtractor()


# --------------------------------------------------------------------------- #
# Subagents (current format: a sibling <session-id>/subagents/agent-*.jsonl dir)
# --------------------------------------------------------------------------- #


def summarize_subagent(path: Path) -> Optional[SubagentSummary]:
    """Read and summarize a sidecar; retained for existing callers."""
    return summarize_subagent_source(read_source(path, read_metadata=True))


def summarize_subagent_source(source: ClaudeSource) -> Optional[SubagentSummary]:
    """Condense supplied sidecar records into task / tools / result without I/O.

    No Task tool-use id links sidecars to main records; attribution remains
    timestamp based. The optional metadata supplies only ``agentType``.
    """
    entries = source.entries
    if not entries:
        return None

    # The spawning prompt is the first real (non-tool-result) user message.
    task = ""
    for e in entries:
        if e.get("type") == "user" and not _is_tool_result_entry(e):
            t = clean_user_text(_human_text(e))
            if t:
                task = t
                break

    tools, result = _subagent_activity(entries)
    return {
        "agent_type": string_value(source.metadata.get("agentType")) or "subagent",
        "start_ts": first_timestamp(entries),
        "task": task,
        "tool_count": len(tools),
        "tool_names": sorted(set(tools)),
        "result": result,
    }


def _subagent_activity(entries: List[Dict[str, JsonValue]]) -> Tuple[List[str], str]:
    tools: List[str] = []
    result = ""
    for entry in entries:
        if entry.get("type") != "assistant":
            continue
        for block in _content_blocks(entry):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                tools.append(string_value(block.get("name")) or "tool")
            elif block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str) and text.strip():
                    result = text.strip()
    return tools, result


def load_subagents(transcript: Path) -> List[SubagentSummary]:
    """Summarize every subagent transcript for this session, oldest first."""
    subs = [
        summary
        for source in read_subagent_sources(transcript)
        if (summary := summarize_subagent_source(source)) is not None
    ]
    subs.sort(key=lambda summary: summary.get("start_ts") or "")
    return subs


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def build_arg_parser(project_root: Optional[Path] = None) -> argparse.ArgumentParser:
    project_root = Path.cwd() if project_root is None else project_root
    p = argparse.ArgumentParser(
        prog="extract_claude_session_log.py",
        description=(
            "Extract a Claude Code session transcript (JSONL) into a JSON "
            "conversation envelope. Mechanical extraction "
            "only — no LLM, no summarization. Empty sessions produce events: []. "
            "Skipped malformed/non-object records are diagnosed on stderr. "
            "Exit 1 on input/output failure, including any batch failure."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Selection precedence:\n"
            "  --transcript PATH  >  positional file path  >  positional session id\n"
            "  >  newest .jsonl in the resolved project dir  >  newest .jsonl anywhere.\n\n"
            "The project dir is resolved from the current working directory by\n"
            "encoding its path (non-alphanumerics -> '-') under ~/.claude/projects/."
        ),
    )
    p.add_argument(
        "session",
        nargs="?",
        help="case-sensitive recorded ID (unique exact, then literal prefix) OR transcript path",
    )
    p.add_argument(
        "--transcript",
        metavar="PATH",
        help="explicit path to a .jsonl transcript (overrides positional selection)",
    )
    p.add_argument(
        "--project-dir",
        metavar="PATH",
        help=(
            "override the encoded project dir under ~/.claude/projects/ "
            "(default: resolved from the current working directory)"
        ),
    )
    p.add_argument(
        "--strict",
        action="store_true",
        help=(
            "only read sessions started in the current directory: disable the "
            "parent-dir walk and the newest-anywhere global fallback"
        ),
    )
    p.add_argument(
        "--all",
        action="store_true",
        help=(
            "extract every session in the resolved project dir, one file per "
            "session named claude_session_log_raw_<session-id>.json "
            "(in this mode --output is "
            "treated as the output directory; positional selector is ignored). "
            "This is the default unless a session id, --transcript, or "
            "'--output -' selects a single session."
        ),
    )
    p.add_argument(
        "--output",
        metavar="PATH",
        default=None,
        help=(
            "output JSON file (default: claude_session_log_raw_<session-id>.json "
            f"in the project root, {project_root}; use '-' for stdout). "
            "With --all, an output "
            "directory instead (default: project root)."
        ),
    )
    p.add_argument(
        TOOL_RESULT_BYTES_FLAG,
        metavar="N",
        type=int,
        default=DEFAULT_TOOL_RESULT_MAX_BYTES,
        help=(
            "cap each textual tool result in the JSON envelope at N bytes; the original "
            "size is always recorded. Use a negative value for no cap "
            f"(default: {DEFAULT_TOOL_RESULT_MAX_BYTES})"
        ),
    )
    return p


def extract_transcript(
    transcript: Path,
    tool_result_max_bytes: int = DEFAULT_TOOL_RESULT_MAX_BYTES,
) -> Envelope:
    """Read one valid transcript into its JSON envelope, including empty sessions."""
    source = EXTRACTOR.read_source(transcript)
    session = EXTRACTOR.interpret_source(source)
    if session["unsupported_tools"]:
        print(
            "warning: unsupported tool input schemas: "
            + ", ".join(session["unsupported_tools"])
            + "; recorded payloads preserved",
            file=sys.stderr,
        )
    return session_log_envelope.export_session(session, tool_result_max_bytes)


def _extract_all(
    project_dir: Optional[Path],
    output: Optional[str],
    strict: bool = False,
    tool_result_max_bytes: int = DEFAULT_TOOL_RESULT_MAX_BYTES,
    *,
    project_root: Optional[Path] = None,
) -> int:
    """Extract every transcript in the project dir, one JSON file each.

    The session id (the ``.jsonl`` stem) is the per-file identifier. Files are
    written to ``output`` (treated as a directory) or the project root.
    """
    cwd = Path.cwd()
    project_root = cwd if project_root is None else project_root
    proj = project_dir or resolve_project_dir(None, cwd, strict=strict)
    if proj is None or not proj.is_dir():
        where = (
            proj if proj is not None else f"(none matched cwd under {PROJECTS_ROOT})"
        )
        print(f"error: no project transcript dir found: {where}", file=sys.stderr)
        return 1

    files = sorted(_jsonl_files(proj), key=lambda p: p.stat().st_mtime)
    if not files:
        print(f"error: no .jsonl transcripts in {proj}", file=sys.stderr)
        return 1

    out_dir = Path(output).expanduser() if output else project_root
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(
            f"error: could not create output directory {out_dir}: {exc}",
            file=sys.stderr,
        )
        return 1
    print(f"extracting {len(files)} session(s) from {proj}", file=sys.stderr)

    written = 0
    for transcript in files:
        session_id = transcript.stem
        try:
            envelope = extract_transcript(transcript, tool_result_max_bytes)
        except (OSError, ValueError) as exc:
            print(f"  skip {session_id} ({transcript}): {exc}", file=sys.stderr)
            continue
        out_path = out_dir / raw_filename("claude", session_id)
        try:
            write_envelope(out_path, envelope, TOOL_RESULT_BYTES_FLAG)
        except OSError as exc:
            print(f"  error writing {out_path}: {exc}", file=sys.stderr)
            continue
        written += 1
        print(f"  wrote {out_path}", file=sys.stderr)

    print(f"done: {written}/{len(files)} session(s) written", file=sys.stderr)
    return 0 if written == len(files) else 1


def main(
    argv: Optional[List[str]] = None, *, project_root: Optional[Path] = None
) -> int:
    project_root = Path.cwd() if project_root is None else project_root
    args = build_arg_parser(project_root).parse_args(argv)

    # all-sessions is the default; a session id, --transcript or stdout opts out
    if args.all or not (args.session or args.transcript or args.output == "-"):
        proj = Path(args.project_dir).expanduser() if args.project_dir else None
        out = None if args.output == "-" else args.output
        return _extract_all(
            proj,
            out,
            strict=args.strict,
            tool_result_max_bytes=args.tool_result_bytes,
            project_root=project_root,
        )

    try:
        transcript = select_transcript(
            selector=args.session,
            transcript=args.transcript,
            project_dir=args.project_dir,
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

    if args.output == "-":
        payload = serialize_envelope(envelope)
        sys.stdout.write(payload)
        warn_if_oversized("stdout JSON", payload, TOOL_RESULT_BYTES_FLAG)
    else:
        out_path = (
            project_root / raw_filename("claude", transcript.stem)
            if args.output is None
            else Path(args.output).expanduser()
        )
        try:
            write_envelope(out_path, envelope, TOOL_RESULT_BYTES_FLAG)
        except OSError as exc:
            print(f"error: could not write {out_path}: {exc}", file=sys.stderr)
            return 1
        print(f"wrote {out_path}", file=sys.stderr)

    return 0
