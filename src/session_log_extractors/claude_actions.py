"""Interpret Claude action facts at their canonical, evidenced conversation owners."""

from __future__ import annotations

import re
from typing import Final

from session_log_extractors.claude_authorship import _is_compact_invocation
from session_log_extractors.json_types import JsonValue, json_value
from session_log_extractors.session_model import (
    ActionActivityReference,
    ActionEvidence,
    FileAction,
    FileChangeConfirmation,
    Invocation,
    InvocationDefinition,
    InvocationKind,
    Message,
    RecordedSkillLink,
    Session,
    SubagentTask,
    ToolCall,
    ToolExecutionOutcome,
)

READ_TOOL: Final = "Read"
WRITE_TOOL: Final = "Write"
EDIT_TOOL: Final = "Edit"
NOTEBOOK_EDIT_TOOL: Final = "NotebookEdit"
MULTI_EDIT_TOOL: Final = "MultiEdit"
CREATE_TOOL: Final = "Create"
FILE_WRITING_TOOLS: Final = frozenset(
    {WRITE_TOOL, EDIT_TOOL, NOTEBOOK_EDIT_TOOL, MULTI_EDIT_TOOL, CREATE_TOOL}
)
FILE_TOOLS: Final = FILE_WRITING_TOOLS | {READ_TOOL}
FILE_PATH_KEYS: Final = ("file_path", "path", "notebook_path", "filePath")
SKILL_TOOL: Final = "Skill"
AGENT_TOOLS: Final = frozenset({"Agent", "Task"})
_COMMAND = re.compile(
    r"<(command-name|command-message|command-args)>(.*?)</\1>", re.DOTALL
)
_SKILL_LINK = re.compile(r"\[\$([^\]]+)\]\(([^)]+)\)")
_SKILL_BASE: Final = "Base directory for this skill: "


def _activity(
    message: Message, call_index: int | None = None
) -> ActionActivityReference:
    evidence = [ActionEvidence(field="message.content", value=message.get("text"))]
    activity = ActionActivityReference(message_i=message["i"], evidence=evidence)
    if call_index is not None:
        activity["tool_call_index"] = call_index
        call = (message.get("tool_calls") or [])[call_index]
        evidence[:] = [
            ActionEvidence(field="message.content.tool_use.name", value=call["name"])
        ]
        if "id" in call:
            evidence.append(
                ActionEvidence(field="message.content.tool_use.id", value=call["id"])
            )
    return activity


def _path(payload: object) -> str | None:
    if isinstance(payload, dict):
        for key in FILE_PATH_KEYS:
            value = payload.get(key)
            if isinstance(value, str):
                return value
    return None


def _patch_changes(payload: dict[str, JsonValue]) -> bool:
    patches = payload.get("structuredPatch")
    if not isinstance(patches, list):
        return False
    removed: list[str] = []
    added: list[str] = []
    for patch in patches:
        if not isinstance(patch, dict):
            continue
        lines = patch.get("lines")
        if not isinstance(lines, list):
            continue
        for line in lines:
            if isinstance(line, str) and line.startswith("-"):
                removed.append(line[1:])
            elif isinstance(line, str) and line.startswith("+"):
                added.append(line[1:])
    return removed != added


def _confirms_change(name: str, path: str | None, payload: JsonValue) -> bool:
    if name not in FILE_WRITING_TOOLS or path is None or not isinstance(payload, dict):
        return False
    if _path(payload) != path or payload.get("error"):
        return False
    if name == NOTEBOOK_EDIT_TOOL:
        before, after = payload.get("original_file"), payload.get("updated_file")
        return isinstance(before, str) and isinstance(after, str) and before != after
    if name in {WRITE_TOOL, CREATE_TOOL} and payload.get("type") == "create":
        return isinstance(payload.get("content"), str)
    return _patch_changes(payload)


def _file_action(call: ToolCall, activity: ActionActivityReference) -> FileAction:
    result = call.get("result_record")
    outcome = ToolExecutionOutcome.UNKNOWN
    change: FileChangeConfirmation | None = None
    path = _path(call.get("input"))
    if result is not None:
        outcome = (
            ToolExecutionOutcome.FAILED
            if result["is_error"]
            else ToolExecutionOutcome.SUCCEEDED
        )
        payload = result.get("native_result")
        if not result["is_error"] and _confirms_change(call["name"], path, payload):
            change = FileChangeConfirmation(
                evidence=[ActionEvidence(field="toolUseResult", value=payload)]
            )
    action = FileAction(
        ref=f"claude.file.{activity['message_i']}.{activity['tool_call_index']}",
        action_name=call["name"],
        path=path,
        execution_outcome=outcome,
        change=change,
        activity=activity,
        evidence=[
            ActionEvidence(field="tool_use.input", value=json_value(call.get("input")))
        ],
    )
    if "id" in call:
        action["native_id"] = call["id"]
    return action


def _task(call: ToolCall, activity: ActionActivityReference) -> SubagentTask:
    payload = call.get("input")
    task = SubagentTask(
        ref=f"claude.task.{activity['message_i']}.{activity['tool_call_index']}",
        activity=activity,
        evidence=[ActionEvidence(field="tool_use.input", value=json_value(payload))],
    )
    if "id" in call:
        task["native_id"] = call["id"]
    if isinstance(payload, dict):
        for key in ("prompt", "description"):
            value = payload.get(key)
            if isinstance(value, str):
                if key == "prompt":
                    task["prompt"] = value
                else:
                    task["description"] = value
        agent_type = payload.get("subagent_type")
        if isinstance(agent_type, str):
            task["agent_type"] = agent_type
    result = call.get("result_record", {}).get("native_result")
    agent_id = result.get("agentId") if isinstance(result, dict) else None
    if isinstance(agent_id, str):
        task["agent_id"] = agent_id
        task["evidence"].append(
            ActionEvidence(field="toolUseResult.agentId", value=agent_id)
        )
    return task


def _skill(call: ToolCall, activity: ActionActivityReference) -> Invocation:
    payload = call.get("input")
    invocation = Invocation(
        ref=f"claude.invocation.{activity['message_i']}.{activity['tool_call_index']}",
        kind=InvocationKind.SKILL,
        name=None,
        activity=activity,
        evidence=[ActionEvidence(field="tool_use.input", value=json_value(payload))],
    )
    if "id" in call:
        invocation["native_id"] = call["id"]
    if isinstance(payload, dict):
        name = payload.get("skill", payload.get("name"))
        if isinstance(name, str):
            invocation["name"] = name
        args = payload.get("args")
        if isinstance(args, str):
            invocation["arguments"] = args
    return invocation


def _command_invocation(
    message: Message, fields: dict[str, str], index: int
) -> Invocation | None:
    slash = fields.get("command-name")
    if slash is None or not slash.strip().startswith("/"):
        return None
    invocation = Invocation(
        ref=f"claude.invocation.{message['i']}.{index}",
        kind=InvocationKind.COMMAND,
        name=slash.strip()[1:],
        slash_text=slash,
        activity=_activity(message),
        evidence=[ActionEvidence(field="message.content", value=message.get("text"))],
    )
    if "command-args" in fields:
        invocation["arguments"] = fields["command-args"]
    links = []
    for content in fields.values():
        for match in _SKILL_LINK.finditer(content):
            links.append(
                RecordedSkillLink(syntax=match.group(0), target=match.group(2))
            )
    if links:
        invocation["skill_links"] = links
    return invocation


def _message_invocations(message: Message) -> list[Invocation]:
    text = message.get("text") or ""
    matches = list(_COMMAND.finditer(text))
    # A complete native wrapper, not an example embedded in prose.
    if not matches or _COMMAND.sub("", text).strip():
        return []
    groups: list[dict[str, str]] = [{}]
    for match in matches:
        key, value = match.group(1), match.group(2)
        if key in groups[-1]:
            groups.append({})
        groups[-1][key] = value
    invocations = []
    for index, fields in enumerate(groups):
        invocation = _command_invocation(message, fields, index)
        if invocation is not None:
            invocations.append(invocation)
    return invocations


def _attach_user_invocations(message: Message, entry: dict[str, JsonValue]) -> None:
    invocations = _message_invocations(message)
    text = message.get("text")
    if not invocations and _is_compact_invocation(entry) and text == "/compact":
        invocation = _command_invocation(message, {"command-name": text}, 0)
        if invocation is not None:
            invocation["evidence"].append(
                ActionEvidence(field="entrypoint", value=entry["entrypoint"])
            )
            invocations.append(invocation)
    if invocations:
        message["invocations"] = invocations


def _enrich_definitions(
    events: list[Message], entries: list[dict[str, JsonValue]]
) -> None:
    messages: dict[str, list[Message]] = {}
    calls: dict[str, list[ToolCall]] = {}
    sources: dict[str, list[dict[str, JsonValue]]] = {}
    for message in events:
        native_id = message.get("id")
        if native_id is not None:
            messages.setdefault(native_id, []).append(message)
        for call in message.get("tool_calls") or []:
            if "id" in call:
                calls.setdefault(call["id"], []).append(call)
    for entry in entries:
        source_id = entry.get("uuid")
        if isinstance(source_id, str):
            sources.setdefault(source_id, []).append(entry)
    for native_id, owners in messages.items():
        records = sources.get(native_id, [])
        if len(owners) == 1 and len(records) == 1:
            _attach_definition(owners[0], records[0], messages, calls)


def _attach_definition(
    message: Message,
    entry: dict[str, JsonValue],
    messages: dict[str, list[Message]],
    calls: dict[str, list[ToolCall]],
) -> None:
    if entry.get("isMeta") is not True or entry.get("turnCompanion") is not True:
        return
    text = message.get("text")
    if not isinstance(text, str):
        return
    tool_id = entry.get("sourceToolUseID")
    parent_id = entry.get("parentUuid")
    targets: list[Invocation] = []
    field = "sourceToolUseID" if isinstance(tool_id, str) else "parentUuid"
    value = tool_id if isinstance(tool_id, str) else parent_id
    if isinstance(tool_id, str):
        owners = calls.get(tool_id, [])
        if len(owners) == 1:
            targets = owners[0].get("invocations", [])
    elif isinstance(parent_id, str):
        parents = messages.get(parent_id, [])
        if len(parents) == 1:
            targets = parents[0].get("invocations", [])
    if len(targets) != 1:
        return
    definition = InvocationDefinition(text=text)
    if text.startswith(_SKILL_BASE):
        definition["path"] = text[len(_SKILL_BASE) :].split("\n", 1)[0]
    targets[0].setdefault("definitions", []).append(definition)
    targets[0]["evidence"].append(ActionEvidence(field=field, value=value))


def _attach_actions(session: Session, entries: list[dict[str, JsonValue]]) -> None:
    """Establish facts on native owners; session lists index those same records."""
    files: list[FileAction] = []
    tasks: list[SubagentTask] = []
    invocations: list[Invocation] = []
    for message in session["events"]:
        invocations.extend(message.get("invocations", []))
        for index, call in enumerate(message.get("tool_calls") or []):
            activity = _activity(message, index)
            if call["name"] in FILE_TOOLS:
                action = _file_action(call, activity)
                call["file_actions"] = [action]
                files.append(action)
            elif call["name"] in AGENT_TOOLS:
                task = _task(call, activity)
                call["subagent_tasks"] = [task]
                tasks.append(task)
            elif call["name"] == SKILL_TOOL:
                invocation = _skill(call, activity)
                call["invocations"] = [invocation]
                invocations.append(invocation)
    _enrich_definitions(session["events"], entries)
    session["file_actions"] = files
    session["subagent_tasks"] = tasks
    session["invocations"] = invocations
