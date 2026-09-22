#!/usr/bin/env python3
"""Extract a Claude Code session transcript into a readable markdown conversation log.

Claude Code records each working session as a JSONL file (one JSON object per
line) under ``~/.claude/projects/<encoded-project-path>/<session-id>.jsonl``.
The project path is "encoded" by replacing every run of non-alphanumeric
characters with a single dash, e.g.::

    /Users/jane/work/app  ->  -Users-jane-work-app

This script reads one of those transcripts and produces a turn-by-turn markdown
log with per-turn timestamps. It is a *mechanical* extractor: there is no LLM,
no summarization, and no network access. User prose is reproduced verbatim;
assistant text is reproduced verbatim; tool calls are condensed to a single
bullet each (tool name + a short descriptor such as the file path or command).

A "turn" is a real user message together with everything the assistant did in
response, up to the next real user message. Tool results come back as
``user``-role entries whose content is ``tool_result`` blocks -- those are *not*
real user messages and do not start a new turn; they are folded (condensed) into
the preceding assistant activity. Subagent / sidechain entries
(``isSidechain == true``) and meta entries (``isMeta == true``) are kept out of
the main flow.

When the human denies a tool's permission prompt, the harness records a canned
error tool_result; those are surfaced as "permission denied" decisions (the tool
that was denied, plus any steering message the human typed). Approvals leave no
distinct record -- an approved tool just runs -- so only denials are captured.

Plan mode is the exception: a plan is presented via the ``ExitPlanMode`` tool and
the human's approve/reject decision comes back as that tool's result. Both the
plan text and the decision (including a plan the human edited in the approval
dialog, or the steering message they typed when rejecting) are surfaced.

Standard library only; works on Python 3.8+.

Examples
--------
Most recent transcript for the current project, written to ``session_log.md``
in the project root (always, regardless of where you run it from)::

    python3 scripts/extract_claude_session_log.py

A specific session id, to stdout::

    python3 scripts/extract_claude_session_log.py 1a2b3c4d-... --output -

An explicit transcript file::

    python3 scripts/extract_claude_session_log.py --transcript ~/.claude/projects/-Users-me-app/abc.jsonl
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
    image_from_base64,
    raw_filename,
    tool_call,
    truncate_result,
    user_event,
    write_envelope,
)
from skill_metadata import (
    load_command_details,
    load_skill_details,
    render_skill_lines,
)

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

PROJECTS_ROOT = Path.home() / ".claude" / "projects"

# This script lives at ``<project_root>/scripts/extract_claude_session_log.py``, so the
# project root is two levels up. The log is always written here by default,
# regardless of the current working directory.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = PROJECT_ROOT / "session_log.md"

# Pasted images are dumped here so an image-capable reader can describe them.
IMAGE_DUMP_DIR = Path(tempfile.gettempdir()) / "claude_session_images"
_IMAGE_EXT = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
}

# Tool calls whose inputs touch files we want to collect for "Files changed".
FILE_WRITING_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit", "Create"}

# Tools that prompt the human for a direct answer (pop-up questions, etc.). Their
# tool_result carries the human's selection, so we surface it as a user
# interaction rather than folding it into the generic result notes.
INTERACTION_TOOLS = {"AskUserQuestion"}

# Plan mode: the assistant presents a plan via ExitPlanMode and the human either
# approves it (optionally after editing it in the approval dialog) or rejects it
# with a steering message. Both the plan and the decision are surfaced.
PLAN_TOOLS = {"ExitPlanMode", "exit_plan_mode"}

# Keys that, across tool versions, hold a target file path.
FILE_PATH_KEYS = ("file_path", "path", "notebook_path", "filePath")

# Max length for a condensed tool descriptor before it gets an ellipsis.
TOOL_DESC_MAX = 100
# Max length for a condensed tool-result note.
RESULT_NOTE_MAX = 120

# Patterns for harness noise embedded in user-message text. These wrappers are
# injected by the CLI, not typed by the human, so we strip them while keeping
# the surrounding human prose.
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
# rather than as prose. (The matching <bash-stdout>/<bash-stderr> output arrives
# as a separate user message and is stripped as noise above.)
_BASH_INPUT = re.compile(r"<bash-input>(.*?)</bash-input>", re.DOTALL | re.IGNORECASE)

# A slash-command invocation (e.g. `/extract-claude-session-logs`) is recorded as
# a user message wrapping <command-name>…</command-name> (plus command-message and
# command-args). clean_user_text strips those tags to nothing, so we pull the
# command name from the raw text *before* cleaning and surface it as its own turn.
_COMMAND_NAME = re.compile(
    r"<command-name>\s*(.*?)\s*</command-name>", re.DOTALL | re.IGNORECASE
)
_COMMAND_ARGS = re.compile(
    r"<command-args>\s*(.*?)\s*</command-args>", re.DOTALL | re.IGNORECASE
)

# When the human denies a permission prompt, the harness feeds the assistant a
# canned tool_result (is_error) whose content opens with one of these preambles.
# An approval leaves no distinct record — the tool just runs — so denials are the
# only permission *decision* we can observe.
# ponytail: denials only; there is no approval marker in the transcript to parse.
_PERMISSION_DENIAL_PREFIXES = (
    "The user doesn't want to proceed with this tool use.",
    "Permission for this tool use was denied.",
)
# Optional steering message the human typed when denying, e.g.
# "…the user said:\nWe need a new branch for this". A plan sent back in plan mode
# uses a different lead-in ("…reason for the rejection: …") for the same thing.
# Captured up to the trailing "Note:" hint block (or end of string).
_DENIAL_MESSAGE = re.compile(
    r"(?:the user said|reason for the rejection):\s*\n?(.*?)(?:\n\nNote:|\Z)",
    re.DOTALL | re.IGNORECASE,
)


def parse_permission_denial(content: str) -> Optional[str]:
    """If ``content`` is a permission-denial tool_result, return the human's
    steering message (``""`` if they just denied). Return ``None`` otherwise."""
    if not isinstance(content, str) or not content.startswith(
        _PERMISSION_DENIAL_PREFIXES
    ):
        return None
    m = _DENIAL_MESSAGE.search(content)
    return m.group(1).strip() if m else ""


# An approved plan comes back as a tool_result opening with this preamble. When
# the human edited the plan in the approval dialog, the *edited* text follows an
# "## Approved Plan (edited by user):" heading — that version is what the
# assistant actually works from, so it supersedes the proposed one.
_PLAN_APPROVAL_PREFIX = "User has approved your plan"
_PLAN_EDITED = re.compile(
    r"^##\s*Approved Plan \(edited by user\):\s*\n(.*)\Z", re.DOTALL | re.MULTILINE
)


def parse_plan_decision(content: str) -> Tuple[str, str, str]:
    """Classify an ExitPlanMode tool_result.

    Returns ``(decision, edited_plan, message)`` where decision is
    ``"approved"`` / ``"rejected"`` / ``"unknown"``, ``edited_plan`` is the
    human's edited plan text (``""`` if they approved as-is), and ``message`` is
    any steering message they typed when rejecting.
    """
    if not isinstance(content, str):
        return "unknown", "", ""
    if content.startswith(_PLAN_APPROVAL_PREFIX):
        m = _PLAN_EDITED.search(content)
        return "approved", m.group(1).strip() if m else "", ""
    denial = parse_permission_denial(content)
    if denial is not None:
        return "rejected", "", denial
    return "unknown", "", ""


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
      3. positional ``selector`` treated as a session id, looked up in the
         resolved project dir (with or without the ``.jsonl`` suffix).
      4. newest ``.jsonl`` in the resolved project dir.
      5. newest ``.jsonl`` across every project dir (global fallback).

    In ``strict`` mode the project dir must match the cwd exactly (no
    parent-walk) and the global fallback (step 5) is disabled, so the script
    only ever reads sessions started in the current directory.

    Raises ``FileNotFoundError`` with a clear message when nothing resolves.
    """
    cwd = Path.cwd()

    if transcript:
        p = Path(transcript).expanduser()
        if not p.is_file():
            raise FileNotFoundError(f"--transcript path does not exist: {p}")
        return p

    # A positional selector that points directly at a file.
    if selector:
        as_path = Path(selector).expanduser()
        if as_path.is_file():
            return as_path

    proj = resolve_project_dir(project_dir, cwd, strict=strict)

    # A positional selector treated as a session id within the project dir.
    if selector and proj is not None:
        for name in (selector, f"{selector}.jsonl"):
            candidate = proj / name
            if candidate.is_file():
                return candidate
        # Allow a prefix match on the session id (ids are long UUIDs).
        prefix_matches = [p for p in _jsonl_files(proj) if p.stem.startswith(selector)]
        if len(prefix_matches) == 1:
            return prefix_matches[0]
        if len(prefix_matches) > 1:
            joined = "\n  ".join(str(p) for p in prefix_matches)
            raise FileNotFoundError(
                f"session id {selector!r} is ambiguous; matches:\n  {joined}"
            )

    # Newest in the resolved project dir.
    if proj is not None:
        picked = newest(_jsonl_files(proj))
        if picked is not None:
            return picked

    # If a selector was given but never resolved, that is an error -- do not
    # silently fall through to an unrelated transcript.
    if selector:
        where = f" in {proj}" if proj is not None else ""
        raise FileNotFoundError(f"no transcript matching {selector!r}{where}")

    # Strict mode: never reach past the cwd's own project dir.
    if strict:
        raise FileNotFoundError(
            f"no Claude Code transcript for {cwd} under {PROJECTS_ROOT} "
            "(--strict: parent-walk and global fallback disabled)"
        )

    # Global fallback: newest .jsonl across all project dirs.
    all_files: List[Path] = []
    if PROJECTS_ROOT.is_dir():
        for d in PROJECTS_ROOT.iterdir():
            all_files.extend(_jsonl_files(d))
    picked = newest(all_files)
    if picked is not None:
        return picked

    raise FileNotFoundError(
        "could not locate any Claude Code transcript "
        f"(looked under {PROJECTS_ROOT}); pass --transcript PATH"
    )


# --------------------------------------------------------------------------- #
# JSONL loading
# --------------------------------------------------------------------------- #


def load_entries(path: Path) -> List[Dict[str, Any]]:
    """Load a JSONL transcript, skipping blank/malformed lines.

    A single bad line never aborts the run; it is reported to stderr (rate
    limited) and skipped.
    """
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
        print(
            f"warning: skipped {bad} malformed/blank lines total",
            file=sys.stderr,
        )
    return entries


# --------------------------------------------------------------------------- #
# Content-block helpers
# --------------------------------------------------------------------------- #


def _get_message(entry: Dict[str, Any]) -> Dict[str, Any]:
    msg = entry.get("message")
    return msg if isinstance(msg, dict) else {}


def _content_blocks(entry: Dict[str, Any]) -> List[Any]:
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


def _is_tool_result_entry(entry: Dict[str, Any]) -> bool:
    """True if this user-role entry is a tool result, not a human message."""
    for block in _content_blocks(entry):
        if isinstance(block, dict) and block.get("type") == "tool_result":
            return True
    # Some versions stash the result under a top-level toolUseResult and leave
    # content empty; treat those as tool results too.
    if entry.get("toolUseResult") is not None and not _human_text(entry).strip():
        return True
    return False


def _human_text(entry: Dict[str, Any]) -> str:
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


def _image_sources(entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Base64 image ``source`` dicts attached to a message, in order."""
    out: List[Dict[str, Any]] = []
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


def tool_descriptor(name: str, tool_input: Any) -> str:
    """Build a short one-line descriptor for a tool_use block."""
    if not isinstance(tool_input, dict):
        return _truncate(str(tool_input), TOOL_DESC_MAX) if tool_input else ""

    # File-oriented tools: show the path.
    for key in FILE_PATH_KEYS:
        if key in tool_input and isinstance(tool_input[key], str):
            return _truncate(tool_input[key], TOOL_DESC_MAX)

    # Shell.
    if "command" in tool_input and isinstance(tool_input["command"], str):
        return _truncate(tool_input["command"], TOOL_DESC_MAX)

    # Common search/agent tools.
    for key in ("pattern", "query", "url", "prompt", "description"):
        if key in tool_input and isinstance(tool_input[key], str):
            return _truncate(tool_input[key], TOOL_DESC_MAX)

    # Fallback: compact JSON of the input.
    try:
        return _truncate(json.dumps(tool_input, ensure_ascii=False), TOOL_DESC_MAX)
    except (TypeError, ValueError):
        return ""


def extract_file_path(tool_input: Any) -> Optional[str]:
    if not isinstance(tool_input, dict):
        return None
    for key in FILE_PATH_KEYS:
        val = tool_input.get(key)
        if isinstance(val, str) and val:
            return val
    return None


def result_note(entry: Dict[str, Any]) -> str:
    """A terse note about a tool result (error flag / short output)."""
    is_error = False
    text_bits: List[str] = []
    for block in _content_blocks(entry):
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        if block.get("is_error"):
            is_error = True
        content = block.get("content")
        if isinstance(content, str):
            text_bits.append(content)
        elif isinstance(content, list):
            for c in content:
                if isinstance(c, dict) and isinstance(c.get("text"), str):
                    text_bits.append(c["text"])

    if not text_bits:
        tur = entry.get("toolUseResult")
        if isinstance(tur, str):
            text_bits.append(tur)
        elif isinstance(tur, dict):
            for k in ("stdout", "stderr", "output", "content"):
                v = tur.get(k)
                if isinstance(v, str) and v.strip():
                    text_bits.append(v)
                    break

    blob = _truncate(" ".join(text_bits), RESULT_NOTE_MAX)
    if is_error:
        return f"error: {blob}" if blob else "error"
    return blob


def _result_block_text(block: Dict[str, Any]) -> str:
    """Flatten a tool_result block's content to a string."""
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            c["text"]
            for c in content
            if isinstance(c, dict) and isinstance(c.get("text"), str)
        )
    return ""


def _parse_chosen(
    question_text: str, options: List[Dict[str, Any]], result_text: str
) -> Tuple[str, List[Dict[str, Any]]]:
    """Pull the chosen answer for one question out of the result string.

    The AskUserQuestion result is self-describing, e.g.
    ``Your questions have been answered: "<question>"="<label>", "<q2>"="<l2>".``
    For multiSelect the value is comma-joined labels; for an "Other" answer the
    value is free text matching no option. We locate the value by anchoring on
    the exact question text, then mark every option whose label appears in it.
    """
    val = ""
    if question_text and result_text:
        marker = f'"{question_text}"="'
        idx = result_text.find(marker)
        if idx != -1:
            rest = result_text[idx + len(marker) :]
            end = rest.find('"')
            val = rest[:end] if end != -1 else rest
    if not val and result_text:
        # Single-question fallback: first value after an '="'.
        parts = result_text.split('="', 1)
        if len(parts) == 2:
            tail = parts[1]
            end = tail.find('"')
            val = tail[:end] if end != -1 else tail
    chosen = [o for o in options if o.get("label") and o["label"] in val]
    return val, chosen


def option_qa_from_result(
    entry: Dict[str, Any], pending_questions: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """Build structured Q/options/choice records for an AskUserQuestion result.

    Returns [] for any tool_result that isn't a tracked pop-up question.
    """
    out: List[Dict[str, Any]] = []
    for block in _content_blocks(entry):
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        qid = block.get("tool_use_id")
        questions = pending_questions.get(qid) if isinstance(qid, str) else None
        if not questions:
            continue
        result_text = _result_block_text(block)
        for q in questions:
            if not isinstance(q, dict):
                continue
            options = [o for o in q.get("options", []) if isinstance(o, dict)]
            val, chosen = _parse_chosen(q.get("question", ""), options, result_text)
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


def apply_plan_decisions(
    entry: Dict[str, Any], pending_plans: Dict[str, Dict[str, Any]]
) -> bool:
    """Record the human's approve/reject decision on any plan in this result.

    ``pending_plans`` maps a tool_use id to the plan record already attached to
    the turn, so filling it in here updates the rendered log. Returns True when
    the entry was a plan decision (and so should not also be folded in as a
    generic result note or permission denial).
    """
    handled = False
    for block in _content_blocks(entry):
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        tuid = block.get("tool_use_id")
        record = pending_plans.pop(tuid, None) if isinstance(tuid, str) else None
        if record is None:
            continue
        decision, edited, message = parse_plan_decision(_result_block_text(block))
        record["decision"] = decision
        # The approval result always carries an "edited by user" copy of the
        # plan, edited or not — only keep it when it actually differs.
        record["edited_plan"] = (
            edited
            if edited and edited.strip() != (record["plan"] or "").strip()
            else ""
        )
        record["message"] = message
        handled = True
    return handled


def permission_denials_from_result(
    entry: Dict[str, Any], tool_uses: Dict[str, str]
) -> List[Dict[str, Any]]:
    """Build a record for each permission the human denied in this tool_result.

    ``tool_uses`` maps a tool_use id to its rendered label ("Name — descriptor")
    so we can name the tool that was denied. Returns [] when nothing was denied.
    """
    out: List[Dict[str, Any]] = []
    for block in _content_blocks(entry):
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        message = parse_permission_denial(_result_block_text(block))
        if message is None:
            continue
        tuid = block.get("tool_use_id")
        out.append(
            {
                "tool": tool_uses.get(tuid, "tool")
                if isinstance(tuid, str)
                else "tool",
                "message": message,
            }
        )
    return out


# --------------------------------------------------------------------------- #
# Timestamp formatting
# --------------------------------------------------------------------------- #


def _parse_ts(ts: Optional[str]):
    """Parse an ISO-8601 timestamp to a datetime, or None if unparseable."""
    if not ts or not isinstance(ts, str):
        return None
    try:
        from datetime import datetime

        # Python's fromisoformat dislikes a trailing 'Z' before 3.11.
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def format_timestamp(ts: Optional[str]) -> str:
    """Render an ISO-8601 timestamp as 'YYYY-MM-DD HH:MM:SS UTC'.

    Always UTC so logs extracted on different machines are comparable.
    Falls back to the raw string, then to '(no timestamp)'.
    """
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
    """Time since session start, as '+H:MM:SS' / '+M:SS' / '+Ns'. '' if unknown."""
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
    if formatted in ("(no timestamp)",):
        return "unknown"
    return formatted.split(" ")[0]


# --------------------------------------------------------------------------- #
# Turn assembly
# --------------------------------------------------------------------------- #


class Turn:
    """One human message and the assistant activity that followed it."""

    def __init__(self, user_text: str, timestamp: Optional[str]):
        self.user_text = user_text
        self.timestamp = timestamp
        self.images: List[Dict[str, Any]] = []  # base64 source dicts, in order
        # Set when the user's input was a `!`-prefixed shell command.
        self.shell_command: Optional[str] = None
        # Set when the user's input was a `/`-prefixed slash command.
        self.command: Optional[str] = None
        self.command_args: Optional[str] = None
        self.command_details: Dict[str, Any] = {}
        self.assistant_text_blocks: List[str] = []
        self.tool_bullets: List[str] = []  # already-rendered "- Name — desc"
        self.skills_used: List[str] = []
        self.skill_details: Dict[str, Dict[str, Any]] = {}
        self.result_notes: List[str] = []
        self.option_qas: List[
            Dict[str, Any]
        ] = []  # AskUserQuestion: Q + options + choice
        self.permission_denials: List[Dict[str, Any]] = []  # denied tool + message
        self.plans: List[Dict[str, Any]] = []  # plan mode: plan text + decision
        self.subagents: List[Dict[str, Any]] = []  # folded subagent summaries
        self.subagent_count = 0

    def add_assistant_text(self, text: str) -> None:
        if text and text.strip():
            self.assistant_text_blocks.append(text.strip())

    def add_tool(self, name: str, descriptor: str) -> None:
        if descriptor:
            self.tool_bullets.append(f"- {name} — {descriptor}")
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
    """Walk entries in order, producing turns and the changed-files list."""
    turns: List[Turn] = []
    files_changed: List[str] = []
    files_seen = set()
    current: Optional[Turn] = None
    pending_subagents = 0  # sidechain entries seen since the last real user msg
    pending_questions: Dict[str, Any] = {}  # tool_use id -> AskUserQuestion questions
    pending_plans: Dict[str, Dict[str, Any]] = {}  # tool_use id -> plan record
    tool_uses: Dict[str, str] = {}  # tool_use id -> "Name — descriptor" label

    for entry in entries:
        etype = entry.get("type")

        # Meta and summary entries never participate in the main flow.
        if entry.get("isMeta") is True:
            continue
        if etype in ("summary", "system"):
            continue

        # Sidechain (subagent) entries are kept out of the flow; we only count
        # the user-prompt that *spawns* a subagent so we can note it.
        if entry.get("isSidechain") is True:
            if etype == "user" and not _is_tool_result_entry(entry):
                pending_subagents += 1
            continue

        if etype == "user":
            if _is_tool_result_entry(entry):
                # Fold the result into the current turn, condensed.
                if current is not None:
                    if apply_plan_decisions(entry, pending_plans):
                        continue
                    denials = permission_denials_from_result(entry, tool_uses)
                    qas = option_qa_from_result(entry, pending_questions)
                    if denials:
                        current.permission_denials.extend(denials)
                    if qas:
                        current.option_qas.extend(qas)
                    elif not denials:
                        note = result_note(entry)
                        if note:
                            current.add_result_note(note)
                continue
            # A real human message: start a new turn.
            raw = _human_text(entry)
            text = clean_user_text(raw)
            images = _image_sources(entry)
            cmd_match = _COMMAND_NAME.search(raw)
            command = cmd_match.group(1).strip() if cmd_match else ""
            if not command.startswith("/"):  # only slash commands, not stray tags
                command = ""
            # An entry that becomes empty after stripping harness noise is not a
            # meaningful turn on its own; skip it -- unless it carried images or
            # was a slash-command invocation.
            if not text and not images and not command:
                continue
            current = Turn(text, entry.get("timestamp"))
            current.images = images
            if command:
                args_match = _COMMAND_ARGS.search(raw)
                current.add_command(
                    command, args_match.group(1).strip() if args_match else ""
                )
            cmds = [c.strip() for c in _BASH_INPUT.findall(text) if c.strip()]
            if cmds:
                current.shell_command = "\n".join(cmds)
            if pending_subagents:
                current.subagent_count += pending_subagents
                pending_subagents = 0
            turns.append(current)

        elif etype == "assistant":
            if current is None:
                # Assistant activity before any human turn (rare); make a stub.
                current = Turn("", entry.get("timestamp"))
                turns.append(current)
            for block in _content_blocks(entry):
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    current.add_assistant_text(block.get("text", ""))
                elif btype == "tool_use":
                    name = block.get("name", "tool")
                    tool_input = block.get("input")
                    if name in PLAN_TOOLS:
                        # Rendered as its own plan block, not a tool bullet.
                        record = {
                            "plan": (tool_input or {}).get("plan", "")
                            if isinstance(tool_input, dict)
                            else "",
                            "decision": "no decision recorded",
                            "edited_plan": "",
                            "message": "",
                        }
                        current.plans.append(record)
                        if isinstance(block.get("id"), str):
                            pending_plans[block["id"]] = record
                        continue
                    descriptor = tool_descriptor(name, tool_input)
                    current.add_tool(name, descriptor)
                    if str(name).lower() == "skill" and isinstance(tool_input, dict):
                        skill_name = tool_input.get("skill") or tool_input.get("name")
                        if isinstance(skill_name, str):
                            current.add_skill(skill_name)
                    if isinstance(block.get("id"), str):
                        tool_uses[block["id"]] = (
                            f"{name} — {descriptor}" if descriptor else name
                        )
                    if name in INTERACTION_TOOLS and isinstance(block.get("id"), str):
                        qs = (
                            tool_input.get("questions")
                            if isinstance(tool_input, dict)
                            else None
                        )
                        if isinstance(qs, list):
                            pending_questions[block["id"]] = qs
                    if name in FILE_WRITING_TOOLS:
                        fp = extract_file_path(tool_input)
                        if fp and fp not in files_seen:
                            files_seen.add(fp)
                            files_changed.append(fp)
        # Unknown types are ignored silently (robust to schema additions).

    # If subagents were spawned at the very end with no following user turn,
    # attribute them to the last real turn.
    if pending_subagents and turns:
        turns[-1].subagent_count += pending_subagents

    return turns, files_changed


def build_events(
    entries: List[Dict[str, Any]],
    tool_result_max_bytes: int = DEFAULT_TOOL_RESULT_MAX_BYTES,
) -> List[Dict[str, Any]]:
    """Walk entries in order, producing raw-envelope events.

    A deliberate second pass over the same entries ``build_turns`` consumes,
    rather than a refactor of it. The two outputs answer different questions —
    the markdown condenses for scoring, this keeps the detail for examination —
    and keeping them separate is what guarantees the markdown is unchanged.

    Entry filtering matches ``build_turns`` exactly (meta, summary/system, and
    sidechain entries are skipped) so events and turns describe the same
    conversation. User text is the harness-stripped prose, i.e. what the human
    actually typed: the ``<system-reminder>`` blocks the CLI injects are neither
    the candidate's words nor useful context, and they dwarf everything else.
    """
    events: List[Dict[str, Any]] = []
    pending_calls: Dict[str, Dict[str, Any]] = {}  # tool_use id -> call awaiting result
    index = 0

    for entry in entries:
        etype = entry.get("type")

        if entry.get("isMeta") is True:
            continue
        if etype in ("summary", "system"):
            continue
        if entry.get("isSidechain") is True:
            continue

        if etype == "user":
            if _is_tool_result_entry(entry):
                # Attach each result to the call it answers. Results arrive as
                # their own user-role entry, well after the assistant event was
                # emitted, so the call dict is mutated in place.
                for block in _content_blocks(entry):
                    if (
                        not isinstance(block, dict)
                        or block.get("type") != "tool_result"
                    ):
                        continue
                    call = pending_calls.pop(block.get("tool_use_id"), None)
                    if call is None:
                        continue
                    call.update(
                        truncate_result(
                            _result_block_text(block), tool_result_max_bytes
                        )
                    )
                    if block.get("is_error"):
                        call["is_error"] = True
                continue

            raw = _human_text(entry)
            text = clean_user_text(raw)
            images = [
                image_from_base64(src.get("data", ""), src.get("media_type"))
                for src in _image_sources(entry)
            ]

            # A slash command cleans to nothing (its text lives entirely in
            # harness tags), but invoking one is a real thing the human did.
            cmd_match = _COMMAND_NAME.search(raw)
            command = cmd_match.group(1).strip() if cmd_match else ""
            if command.startswith("/") and not text:
                args_match = _COMMAND_ARGS.search(raw)
                args = args_match.group(1).strip() if args_match else ""
                text = f"{command} {args}".strip()

            if not text and not images:
                continue
            events.append(user_event(index, entry.get("timestamp"), text, images))
            index += 1

        elif etype == "assistant":
            text_parts: List[str] = []
            calls: List[Dict[str, Any]] = []
            ids: List[Optional[str]] = []

            for block in _content_blocks(entry):
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    chunk = block.get("text", "")
                    if isinstance(chunk, str) and chunk.strip():
                        text_parts.append(chunk.strip())
                elif btype == "tool_use":
                    call = tool_call(
                        block.get("name", "tool"),
                        block.get("input"),
                        None,
                        tool_result_max_bytes,
                    )
                    calls.append(call)
                    ids.append(
                        block.get("id") if isinstance(block.get("id"), str) else None
                    )

            if not text_parts and not calls:
                continue
            events.append(
                assistant_event(
                    index, entry.get("timestamp"), "\n\n".join(text_parts), calls
                )
            )
            index += 1
            for call, tool_id in zip(calls, ids):
                if tool_id:
                    pending_calls[tool_id] = call

    return events


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def _derive_context_line(turns: List[Turn]) -> str:
    """A brief context line from the first human message, if usable."""
    for t in turns:
        if t.user_text.strip():
            first = _truncate(t.user_text, 80)
            return f"a Claude Code working session starting with: “{first}”"
    return "a Claude Code working session"


def _quote(text: str) -> List[str]:
    """Blockquote a block of text so embedded markdown can't break the log."""
    return [f"> {line}" if line.strip() else ">" for line in text.splitlines()]


def render_summary(turns: List[Turn], first_ts: Optional[str]) -> List[str]:
    """The up-front recap: every turn that carried real user input.

    Turns with no user text (pre-conversation / assistant-only activity) are
    omitted — they required no input. User prose is reproduced in full. When the
    input was a pop-up choice, all presented options are listed with the chosen
    one(s) checked.
    """
    out: List[str] = ["## Summary — user inputs", ""]
    input_turns = [
        (i, t)
        for i, t in enumerate(turns, 1)
        if t.user_text.strip() or t.command or t.plans
    ]
    if not input_turns:
        out += ["_(No user inputs in this transcript.)_", ""]
        return out

    prev_ts: Optional[str] = None
    for i, turn in input_turns:
        elapsed = format_elapsed(first_ts, turn.timestamp) or "+?"
        delta = format_elapsed(prev_ts, turn.timestamp) if prev_ts else "+0s"
        out.append(
            f"### Turn {i} · {format_timestamp(turn.timestamp)} ({elapsed}, Δ{delta})"
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
        for skill in turn.skills_used:
            out.extend(render_skill_lines(skill, turn.skill_details.get(skill)))
            out.append("")
        for qa in turn.option_qas:
            label = qa["header"] or "Question"
            out.append(f"_Answered via options ({label}):_")
            if qa["question"]:
                out.append(f"> {qa['question']}")
            for o in qa["options"]:
                mark = "x" if o.get("label") in qa["chosen_labels"] else " "
                row = f"- [{mark}] **{o.get('label', '')}**"
                if o.get("description"):
                    row += f" — {o['description']}"
                out.append(row)
            if not qa["chosen_labels"] and qa["answer_text"]:
                out.append(f"- [x] _(custom answer)_ {qa['answer_text']}")
            out.append("")
        for p in turn.plans:
            out.append(
                f"_Plan presented for approval — user **{p['decision']}**_"
                + (" _(after editing it)_" if p["edited_plan"] else "")
            )
            out.append("> _(full plan text in Turn %d below)_" % i)
            if p["message"]:
                out.append("")
                out.append("_Feedback the user gave:_")
                out.extend(_quote(p["message"]))
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
    out.append("# Session Conversation Log")
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
        out.append("_(No conversational turns found in this transcript.)_")
        out.append("")
        return "\n".join(out)

    # Summary first, then the full detail.
    out.append("")
    out.extend(render_summary(turns, first_ts))
    out.append("---")
    out.append("")
    out.append("# Full turn-by-turn detail")

    for i, turn in enumerate(turns, 1):
        out.append("")
        elapsed = format_elapsed(first_ts, turn.timestamp)
        header = f"## Turn {i} · {format_timestamp(turn.timestamp)}"
        if elapsed:
            header += f" ({elapsed} into session)"
        out.append(header)
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
            out.append("**User:** _(no user text — pre-conversation activity)_")
        out.append("")

        assistant_chunks: List[str] = []
        if turn.assistant_text_blocks:
            assistant_chunks.append("\n\n".join(turn.assistant_text_blocks))
        body = "\n\n".join(c for c in assistant_chunks if c)
        if body:
            out.append(f"**Assistant:** {body}")
        elif turn.tool_bullets or turn.plans:
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
            # Keep results compact: one summarizing line.
            notes = "; ".join(turn.result_notes[:6])
            extra = len(turn.result_notes) - 6
            if extra > 0:
                notes += f"; (+{extra} more results)"
            out.append("")
            out.append(f"  _results:_ {_truncate(notes, 400)}")

        for p in turn.plans:
            out.append("")
            out.append("**Assistant presented a plan for approval:**")
            out.append("")
            out.extend(_quote(p["plan"] or "_(empty plan)_"))
            out.append("")
            out.append(f"  _user {p['decision']} the plan_")
            if p["message"]:
                # Verbatim, not truncated: when a plan is sent back, what the
                # human typed is the highest-signal input in the session.
                out.append("")
                out.append("  _feedback the user gave:_")
                out.append("")
                out.extend(_quote(p["message"]))
            if p["edited_plan"]:
                out.append("")
                out.append("  _user edited the plan before approving; final version:_")
                out.append("")
                out.extend(_quote(p["edited_plan"]))

        if turn.option_qas:
            out.append("")
            for qa in turn.option_qas:
                chosen = (
                    ", ".join(c for c in qa["chosen_labels"] if c)
                    or qa["answer_text"]
                    or "(no selection)"
                )
                q = qa["header"] or _truncate(qa["question"], 80)
                out.append(f"  _user chose:_ {q} → {chosen}")

        if turn.permission_denials:
            out.append("")
            for d in turn.permission_denials:
                line = f"  _user denied permission:_ {d['tool']}"
                if d["message"]:
                    line += f" → “{_truncate(d['message'], 160)}”"
                out.append(line)

        for s in turn.subagents:
            out.append("")
            label = s["agent_type"]
            if s["task"]:
                out.append(f"  _subagent ({label}):_ {_truncate(s['task'], 160)}")
            else:
                out.append(f"  _subagent ({label})_")
            detail = f"    → {s['tool_count']} tool call(s)"
            if s["tool_names"]:
                detail += f" ({', '.join(s['tool_names'])})"
            if s["result"]:
                detail += f"; result: {_truncate(s['result'], 200)}"
            out.append(detail)

        if turn.subagent_count:
            out.append("")
            out.append(f"_(spawned {turn.subagent_count} subagent(s))_")

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


def derive_project_label(
    transcript: Path, entries: Optional[List[Dict[str, Any]]] = None
) -> str:
    """Best-effort human-readable project label.

    Prefer the real project path: transcript entries carry a ``cwd`` field, which
    is the accurate working directory. Fall back to de-dashing the encoded dir
    name (lossy — the encoding collapses '/' and '_' to '-', so the original
    separators can't be recovered).
    """
    encoded = transcript.parent.name
    # Accurate path from the transcript itself, when available.
    if entries:
        for entry in entries:
            cwd = entry.get("cwd")
            if isinstance(cwd, str) and cwd:
                return f"{cwd}  (dir: {encoded})" if encoded else cwd
    # Encoded dirs typically begin with a leading dash (from the leading '/').
    decoded = "/" + encoded.lstrip("-").replace("-", "/") if encoded else encoded
    if encoded:
        return f"{decoded}  (dir: {encoded})"
    return str(transcript.parent)


def first_timestamp(entries: List[Dict[str, Any]]) -> Optional[str]:
    for entry in entries:
        ts = entry.get("timestamp")
        if isinstance(ts, str) and ts:
            return ts
    return None


def session_models(entries: List[Dict[str, Any]]) -> List[str]:
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


def session_cwd(entries: List[Dict[str, Any]]) -> Optional[str]:
    """The working directory the session ran in, from the first entry carrying it."""
    for entry in entries:
        cwd = entry.get("cwd")
        if isinstance(cwd, str) and cwd:
            return cwd
    return None


def write_raw_envelope(
    transcript: Path,
    entries: List[Dict[str, Any]],
    out_dir: Path,
    tool_result_max_bytes: int = DEFAULT_TOOL_RESULT_MAX_BYTES,
) -> Optional[Path]:
    """Write the raw envelope for one transcript. Returns the path, or None if
    the session held no events worth exporting."""
    events = build_events(entries, tool_result_max_bytes)
    if not events:
        return None

    session_id = transcript.stem
    envelope = build_envelope(
        harness="claude",
        session_id=session_id,
        events=events,
        cwd=session_cwd(entries),
        started_at=first_timestamp(entries),
        models=session_models(entries),
        tool_result_max_bytes=tool_result_max_bytes,
    )
    out_path = out_dir / raw_filename("claude", session_id)
    write_envelope(out_path, envelope)
    return out_path


def dump_images(
    turns: List[Turn], session_id: str, dump_dir: Path = IMAGE_DUMP_DIR
) -> List[Path]:
    """Write each turn's pasted images to ``dump_dir`` and append a marker to the
    turn text pointing at the file. Descriptions are filled in later by an
    image-capable reader (see the extract command); the script only dumps.

    Returns the paths written. The marker text is stable so a describe pass can
    find it: ``[Image dumped to `<path>` — description pending]``.
    """
    written: List[Path] = []
    for ti, turn in enumerate(turns, 1):
        for n, src in enumerate(turn.images, 1):
            try:
                raw = base64.b64decode(src.get("data", ""), validate=False)
            except (ValueError, TypeError):
                continue
            ext = _IMAGE_EXT.get(src.get("media_type", ""), "img")
            path = dump_dir / f"{session_id}_turn{ti}_img{n}.{ext}"
            dump_dir.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
            written.append(path)
            turn.user_text = (
                f"{turn.user_text}\n\n[Image dumped to `{path}` — description pending]"
            ).strip()
    return written


# --------------------------------------------------------------------------- #
# Subagents (current format: a sibling <session-id>/subagents/agent-*.jsonl dir)
# --------------------------------------------------------------------------- #


def summarize_subagent(path: Path) -> Optional[Dict[str, Any]]:
    """Condense one subagent transcript into task / tools / result.

    There is no reliable link from the main transcript to a subagent file (no
    Task tool_use id, no back-reference), so we attribute by time elsewhere. The
    only sidecar metadata is ``agentType`` in ``agent-<id>.meta.json``.
    """
    entries = load_entries(path)
    if not entries:
        return None

    agent_type = "subagent"
    meta_path = path.parent / (path.stem + ".meta.json")
    if meta_path.is_file():
        try:
            agent_type = json.loads(meta_path.read_text("utf-8")).get(
                "agentType", agent_type
            )
        except (OSError, ValueError):
            pass

    # The spawning prompt is the first real (non-tool-result) user message.
    task = ""
    for e in entries:
        if e.get("type") == "user" and not _is_tool_result_entry(e):
            t = clean_user_text(_human_text(e))
            if t:
                task = t
                break

    # Tool calls and the final assistant text, in one pass.
    tools: List[str] = []
    result = ""
    for e in entries:
        if e.get("type") != "assistant":
            continue
        for b in _content_blocks(e):
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use":
                tools.append(b.get("name", "tool"))
            elif b.get("type") == "text":
                txt = b.get("text")
                if isinstance(txt, str) and txt.strip():
                    result = txt.strip()  # keep the last one

    return {
        "agent_type": agent_type,
        "start_ts": first_timestamp(entries),
        "task": task,
        "tool_count": len(tools),
        "tool_names": sorted(set(tools)),
        "result": result,
    }


def load_subagents(transcript: Path) -> List[Dict[str, Any]]:
    """Summarize every subagent transcript for this session, oldest first."""
    d = transcript.parent / transcript.stem / "subagents"
    subs: List[Dict[str, Any]] = []
    if d.is_dir():
        for p in sorted(d.iterdir()):
            if p.is_file() and p.suffix == ".jsonl":
                s = summarize_subagent(p)
                if s:
                    subs.append(s)
    subs.sort(key=lambda s: s.get("start_ts") or "")
    return subs


def attribute_subagents(turns: List[Turn], subs: List[Dict[str, Any]]) -> None:
    """Fold each subagent under the latest turn that started at or before it."""
    parsed = [(_parse_ts(t.timestamp), t) for t in turns]
    for s in subs:
        st = _parse_ts(s.get("start_ts"))
        target: Optional[Turn] = None
        if st is not None:
            for ts, t in parsed:  # turns are in order; last match wins
                if ts is not None and ts <= st:
                    target = t
        if target is None and turns:
            target = turns[0]
        if target is not None:
            target.subagents.append(s)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="extract_claude_session_log.py",
        description=(
            "Extract a Claude Code session transcript (JSONL) into a markdown "
            "conversation log with per-turn timestamps. Mechanical extraction "
            "only — no LLM, no summarization."
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
        help="session id (full or unique prefix) OR a path to a .jsonl transcript",
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
            "session named session_log_<session-id>.md (in this mode --output is "
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
            "output markdown file (default: session_log.md in the project root, "
            f"{DEFAULT_OUTPUT}; use '-' for stdout). With --all, an output "
            "directory instead (default: project root)."
        ),
    )
    p.add_argument(
        "--raw",
        dest="raw",
        action="store_true",
        default=True,
        help=(
            "also write the raw envelope claude_session_log_raw_<session-id>.json "
            "alongside the markdown, carrying the full conversation with pasted "
            "images inlined as base64 (default: on)"
        ),
    )
    p.add_argument(
        "--no-raw",
        dest="raw",
        action="store_false",
        help="skip the raw envelope and write only the markdown log",
    )
    p.add_argument(
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
    return p


def render_transcript(
    transcript: Path,
    project_label_override: Optional[str],
    raw_output: Optional[Path] = None,
    tool_result_max_bytes: int = DEFAULT_TOOL_RESULT_MAX_BYTES,
) -> str:
    """Parse one transcript file and return its rendered markdown.

    When ``raw_output`` is given, the raw envelope is also written to that
    directory. Entries are parsed once and fed to both passes.

    Raises ``OSError`` if the file can't be read and ``ValueError`` if it holds
    no parseable entries.
    """
    entries = load_entries(transcript)
    if not entries:
        raise ValueError(f"no parseable JSON entries found in {transcript}")
    turns, files_changed = build_turns(entries)
    dump_images(turns, transcript.stem)
    subagents = load_subagents(transcript)
    attribute_subagents(turns, subagents)
    project_label = project_label_override or derive_project_label(transcript, entries)
    markdown = render(
        turns=turns,
        files_changed=files_changed,
        project_label=project_label,
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


def _extract_all(
    project_dir: Optional[Path],
    output: Optional[str],
    strict: bool = False,
    raw: bool = True,
    tool_result_max_bytes: int = DEFAULT_TOOL_RESULT_MAX_BYTES,
) -> int:
    """Extract every transcript in the project dir, one markdown file each.

    The session id (the ``.jsonl`` stem) is the per-file identifier. Files are
    written to ``output`` (treated as a directory) or the project root.
    """
    cwd = Path.cwd()
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

    out_dir = Path(output).expanduser() if output else PROJECT_ROOT
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"extracting {len(files)} session(s) from {proj}", file=sys.stderr)

    written = 0
    for transcript in files:
        session_id = transcript.stem
        try:
            markdown = render_transcript(
                transcript,
                None,
                raw_output=out_dir if raw else None,
                tool_result_max_bytes=tool_result_max_bytes,
            )
        except (OSError, ValueError) as exc:
            print(f"  skip {session_id}: {exc}", file=sys.stderr)
            continue
        out_path = out_dir / f"session_log_{session_id}.md"
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

    # all-sessions is the default; a session id, --transcript or stdout opts out
    if args.all or not (args.session or args.transcript or args.output == "-"):
        proj = Path(args.project_dir).expanduser() if args.project_dir else None
        out = None if args.output == "-" else args.output
        return _extract_all(
            proj,
            out,
            strict=args.strict,
            raw=args.raw,
            tool_result_max_bytes=args.raw_tool_result_bytes,
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
            args.project_dir if args.project_dir else None,
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
