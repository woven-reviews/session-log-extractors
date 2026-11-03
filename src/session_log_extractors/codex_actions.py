"""Codex file actions and tasks established from structured native evidence."""

import re
from enum import StrEnum
from typing import Final

from session_log_extractors.json_types import JsonValue, json_value, parse_json
from session_log_extractors.session_model import (
    ActionActivityReference,
    ActionEvidence,
    FileAction,
    FileChangeConfirmation,
    Message,
    Session,
    SubagentTask,
    ToolCall,
    ToolExecutionOutcome,
)

APPLY_PATCH: Final = "apply_patch"
SPAWN_AGENT: Final = "spawn_agent"
SUBAGENT_ACTIVITY: Final = "SubAgentActivity"
FILE_CHANGE: Final = "FileChange"
COMMAND_EXECUTION: Final = "CommandExecution"
COMPLETED: Final = "completed"
FAILED: Final = "failed"


class _NativeFileChange(StrEnum):
    ADD = "add"
    UPDATE = "update"
    DELETE = "delete"


_READ_ACTIONS: Final = frozenset(("read", "list_files", "search"))
_PATCH_HEADER = re.compile(r"^\*\*\* (Add|Update|Delete) File: (.+)$", re.MULTILINE)
_MOVE_HEADER = re.compile(r"^\*\*\* Move to: (.+)$", re.MULTILINE)
_DIFF_HUNK = re.compile(r"^@@[^\n]*\n", re.MULTILINE)


def _evidence(field: str, value: JsonValue) -> list[ActionEvidence]:
    return [ActionEvidence(field=field, value=value)]


def _record_evidence(record: dict[str, JsonValue]) -> list[ActionEvidence]:
    evidence = []
    for field in ("type", "id", "call_id", "status", "success", "exit_code"):
        if field in record:
            evidence.extend(_evidence(field, record[field]))
    return evidence


def _outcome(record: dict[str, JsonValue]) -> ToolExecutionOutcome:
    code = record.get("exit_code")
    if record.get("success") is False or record.get("status") == FAILED:
        return ToolExecutionOutcome.FAILED
    if isinstance(code, int) and not isinstance(code, bool):
        return (
            ToolExecutionOutcome.SUCCEEDED if code == 0 else ToolExecutionOutcome.FAILED
        )
    if record.get("success") is True or record.get("status") == COMPLETED:
        return ToolExecutionOutcome.SUCCEEDED
    return ToolExecutionOutcome.UNKNOWN


def _action(
    name: str,
    path: str | None,
    activity: ActionActivityReference,
    evidence: list[ActionEvidence],
    index: int,
) -> FileAction:
    return FileAction(
        ref=f"codex:message/{activity['message_i']}/file/{index}",
        action_name=name,
        path=path,
        execution_outcome=ToolExecutionOutcome.UNKNOWN,
        change=None,
        evidence=evidence,
        activity=activity,
    )


def _patch_text(value: JsonValue) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("patch", "input", "content"):
            text = value.get(key)
            if isinstance(text, str):
                return text
    return ""


def _patch_attempts(call: ToolCall, activity: ActionActivityReference) -> None:
    text = _patch_text(json_value(call.get("input")))
    matches = list(_PATCH_HEADER.finditer(text))
    actions: list[FileAction] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        action = _action(match[1], match[2], activity, _evidence("input", text), index)
        move = _MOVE_HEADER.search(text, match.end(), end)
        if move is not None:
            action["destination_path"] = move[1]
        actions.append(action)
    if actions:
        call["file_actions"] = actions


def _task(call: ToolCall, activity: ActionActivityReference) -> None:
    arguments = call.get("input")
    if not isinstance(arguments, dict):
        return
    task = SubagentTask(
        ref=f"codex:message/{activity['message_i']}/task",
        activity=activity,
        evidence=_evidence("name", call["name"]),
    )
    native_id = call.get("native_id", call.get("id"))
    if native_id is not None:
        task["native_id"] = native_id
    prompt = arguments.get("message")
    description = arguments.get("description")
    agent_type = arguments.get("agent_type")
    agent_id = arguments.get("agent_id")
    if isinstance(prompt, str):
        task["prompt"] = prompt
    if isinstance(description, str):
        task["description"] = description
    if isinstance(agent_type, str):
        task["agent_type"] = agent_type
    if isinstance(agent_id, str):
        task["agent_id"] = agent_id
    task["evidence"].extend(_evidence("input", json_value(arguments)))
    call["subagent_tasks"] = [task]


def populate_call_actions(call: ToolCall, message: Message) -> None:
    """Interpret only native tool payloads; never execute or parse opaque code."""
    activity = ActionActivityReference(
        message_i=message["i"],
        tool_call_index=0,
        evidence=_evidence("call_id", call.get("id")) + _evidence("name", call["name"]),
    )
    name = call["name"].replace("__", ".").rsplit(".", 1)[-1]
    if name == APPLY_PATCH:
        _patch_attempts(call, activity)
    elif name == SPAWN_AGENT:
        _task(call, activity)


def _diff_changes(text: str) -> bool:
    for hunk in _DIFF_HUNK.split(text)[1:]:
        before, after = [], []
        for line in hunk.splitlines(keepends=True):
            if line.startswith(("-", " ")):
                before.append(line[1:])
            if line.startswith(("+", " ")):
                after.append(line[1:])
        if before != after:
            return True
    return False


def _confirmation(
    record: dict[str, JsonValue], outcome: ToolExecutionOutcome, path: str
) -> FileChangeConfirmation | None:
    if outcome != ToolExecutionOutcome.SUCCEEDED:
        return None
    kind = record.get("type")
    diff = record.get("unified_diff")
    # Content can record an empty creation. Updates require actual diff lines or
    # a recorded move; a completed command/status alone proves no modification.
    changed = (
        (kind == _NativeFileChange.ADD and isinstance(record.get("content"), str))
        or kind == _NativeFileChange.DELETE
        or (isinstance(diff, str) and _diff_changes(diff))
        or (
            kind == _NativeFileChange.UPDATE
            and isinstance(record.get("move_path"), str)
            and record["move_path"] != path
        )
    )
    return (
        FileChangeConfirmation(evidence=_evidence(f"changes.{path}", record))
        if changed
        else None
    )


def populate_file_event(
    message: Message, record: dict[str, JsonValue], call: ToolCall | None
) -> None:
    """Attach independently recorded file evidence to its own canonical event."""
    activity = ActionActivityReference(
        message_i=message["i"], evidence=_record_evidence(record)
    )
    outcome = _outcome(record)
    changes = record.get("changes")
    actions: list[FileAction] = []
    if isinstance(changes, dict):
        for path, value in changes.items():
            change = value if isinstance(value, dict) else {}
            name = change.get("type")
            action = _action(
                name if isinstance(name, str) else FILE_CHANGE,
                path,
                activity,
                _evidence(f"changes.{path}", value) + _record_evidence(record),
                len(actions),
            )
            action["execution_outcome"] = outcome
            action["change"] = _confirmation(change, outcome, path)
            move = change.get("move_path")
            if isinstance(move, str):
                action["destination_path"] = move
            native_id = record.get("id")
            if isinstance(native_id, str):
                action["native_id"] = native_id
            actions.append(action)
    actions.extend(_parsed_reads(record, activity))
    retained = []
    for action in actions:
        if call is None or not _attach_file_completion(call, action, record):
            retained.append(action)
    if retained:
        message["file_actions"] = retained


def _parsed_reads(
    record: dict[str, JsonValue], activity: ActionActivityReference
) -> list[FileAction]:
    parsed = record.get("parsed_cmd")
    actions: list[FileAction] = []
    if not isinstance(parsed, list):
        return actions
    for command in parsed:
        if not isinstance(command, dict):
            continue
        name, path = command.get("type"), command.get("path")
        if not isinstance(name, str) or name not in _READ_ACTIONS:
            continue
        action = _action(
            name,
            path if isinstance(path, str) else None,
            activity,
            _evidence("parsed_cmd", command) + _record_evidence(record),
            len(actions),
        )
        action["execution_outcome"] = _outcome(record)
        native_id = record.get("id")
        if isinstance(native_id, str):
            action["native_id"] = native_id
        actions.append(action)
    return actions


def _attach_file_completion(
    call: ToolCall, completion: FileAction, record: dict[str, JsonValue]
) -> bool:
    """Enrich a unique matching path only after the caller proves call ownership."""
    matches = []
    for attempt in call.get("file_actions", []):
        if attempt["path"] == completion["path"]:
            matches.append(attempt)
    if len(matches) != 1:
        return False
    attempt = matches[0]
    attempt["evidence"].extend(completion["evidence"] + _record_evidence(record))
    attempt["execution_outcome"] = completion["execution_outcome"]
    attempt["change"] = completion["change"]
    if "destination_path" in completion:
        attempt["destination_path"] = completion["destination_path"]
    if "native_id" in completion:
        attempt["native_id"] = completion["native_id"]
    return True


def populate_action_results(session: Session) -> None:
    """Complete facts using ID-matched results, and build internal indexes."""
    for message in session["events"]:
        for call in message.get("tool_calls") or []:
            result = call.get("result_record")
            if result is not None:
                _call_result(call, result.get("content"), result["is_error"])
    for message in session["events"]:
        session.setdefault("file_actions", []).extend(message.get("file_actions", []))
        for call in message.get("tool_calls") or []:
            session.setdefault("file_actions", []).extend(call.get("file_actions", []))
            session.setdefault("subagent_tasks", []).extend(
                call.get("subagent_tasks", [])
            )


def _call_result(call: ToolCall, output: JsonValue, is_error: bool) -> None:
    decoded = output
    if isinstance(output, str) and output.startswith("{"):
        try:
            decoded = parse_json(output)
        except ValueError:
            pass
    for action in call.get("file_actions", []):
        if is_error:
            action["execution_outcome"] = ToolExecutionOutcome.FAILED
        elif isinstance(decoded, dict):
            action["execution_outcome"] = _outcome(decoded)
    for task in call.get("subagent_tasks", []):
        identity = decoded.get("agent_id") if isinstance(decoded, dict) else None
        if isinstance(identity, str):
            task["agent_id"] = identity
            task["evidence"].extend(_evidence("result.agent_id", identity))


def populate_subagent_identity(call: ToolCall, record: dict[str, JsonValue]) -> None:
    """A native SubAgentActivity ID equals its originating call ID."""
    for task in call.get("subagent_tasks", []):
        identity = record.get("agent_thread_id")
        if isinstance(identity, str):
            task["agent_id"] = identity
            task["evidence"].extend(_evidence("SubAgentActivity", record))
