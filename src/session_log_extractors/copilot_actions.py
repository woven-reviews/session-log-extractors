"""Interpret Copilot action occurrences and native ownership before export."""

import re
from dataclasses import dataclass

from session_log_extractors.copilot_authorship import (
    RecordIndex,
    linked_skill_invocation,
    record_index,
)
from session_log_extractors.copilot_protocol import (
    CREATE,
    EDIT,
    FILE_TOOLS,
    SKILL,
    SKILL_INVOKED,
    SUBAGENT_STARTED,
    TASK,
    TOOL_EXECUTION_COMPLETE,
    TOOL_EXECUTION_START,
    USER_INVOKED,
    USER_MESSAGE,
)
from session_log_extractors.json_types import JsonValue, json_value, string_value
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


def _evidence(field: str, value: JsonValue) -> list[ActionEvidence]:
    return [ActionEvidence(field=field, value=value)]


@dataclass
class _CallActivity:
    call: ToolCall
    activity: ActionActivityReference
    agent_id: str | None


def _calls(session: Session, native_calls: list[ToolCall]) -> list[_CallActivity]:
    calls = []
    native_call_objects = {id(call) for call in native_calls}
    for message in session["events"]:
        for index, call in enumerate(message.get("tool_calls") or []):
            # Database fallback calls are display-compatible synthetic records,
            # not evidence of conversation ownership for session_files rows.
            if id(call) not in native_call_objects:
                continue
            evidence = _evidence("tool.input", json_value(call.get("input")))
            if "id" in call:
                evidence = _evidence("toolCallId", call["id"])
            activity = ActionActivityReference(
                message_i=message["i"], tool_call_index=index, evidence=evidence
            )
            calls.append(
                _CallActivity(
                    call, activity, call.get("agent_id", message.get("agent_id"))
                )
            )
    return calls


def _unique_completion(
    owner: _CallActivity, records: list[dict[str, JsonValue]]
) -> dict[str, JsonValue] | None:
    matches = []
    call_id = owner.call.get("id")
    if not call_id:
        return None
    for record in records:
        data = record.get("data")
        if (
            record.get("type") == TOOL_EXECUTION_COMPLETE
            and record.get("agentId") == owner.agent_id
            and isinstance(data, dict)
            and data.get("toolCallId") == call_id
        ):
            matches.append(data)
    return matches[0] if len(matches) == 1 else None


def _outcome(completion: dict[str, JsonValue] | None) -> ToolExecutionOutcome:
    if completion is None:
        return ToolExecutionOutcome.UNKNOWN
    success = completion.get("success")
    if isinstance(success, bool):
        return (
            ToolExecutionOutcome.SUCCEEDED if success else ToolExecutionOutcome.FAILED
        )
    return ToolExecutionOutcome.UNKNOWN


def _change(
    name: str, path: str | None, completion: dict[str, JsonValue] | None
) -> FileChangeConfirmation | None:
    if name not in (CREATE, EDIT) or path is None or completion is None:
        return None
    edits = completion.get("fileEdits")
    if not isinstance(edits, list):
        return None
    evidence = []
    for index, edit in enumerate(edits):
        if isinstance(edit, dict) and edit.get("path") == path:
            if edit.get("kind") in (CREATE, EDIT):
                evidence.extend(_evidence(f"completion.fileEdits[{index}]", edit))
    return FileChangeConfirmation(evidence=evidence) if evidence else None


def _file_action(
    session: Session, owner: _CallActivity, completion: dict[str, JsonValue] | None
) -> None:
    call = owner.call
    if call["name"] not in FILE_TOOLS:
        return
    payload = call.get("input")
    payload = payload if isinstance(payload, dict) else {}
    path = string_value(payload.get("path"))
    action = FileAction(
        ref=f"copilot:f{len(session['file_actions'])}",
        action_name=call["name"],
        path=path,
        execution_outcome=_outcome(completion),
        change=_change(call["name"], path, completion),
        evidence=_evidence("tool.input", json_value(call.get("input"))),
        activity=owner.activity,
    )
    if "id" in call:
        action["native_id"] = call["id"]
    if completion is not None and "success" in completion:
        action["evidence"].extend(
            _evidence("completion.success", completion["success"])
        )
    if completion is not None and "fileEdits" in completion:
        action["evidence"].extend(
            _evidence("completion.fileEdits", completion["fileEdits"])
        )
    call.setdefault("file_actions", []).append(action)
    session["file_actions"].append(action)


def _task(session: Session, owner: _CallActivity, records: RecordIndex) -> None:
    call = owner.call
    if call["name"] != TASK:
        return
    payload = call.get("input")
    payload = payload if isinstance(payload, dict) else {}
    task = SubagentTask(
        ref=f"copilot:t{len(session['subagent_tasks'])}",
        evidence=_evidence("tool.input", json_value(call.get("input"))),
        activity=owner.activity,
    )
    if "id" in call:
        task["native_id"] = call["id"]
    for key in ("prompt", "description", "agent_type"):
        value = string_value(payload.get(key))
        if value is not None:
            task[key] = value
    children = _task_children(owner, records)
    if len(children) == 1:
        child = children[0]
        identity = string_value(child.get("agentId"))
        if identity is not None:
            task["agent_id"] = identity
        data = child.get("data")
        if isinstance(data, dict) and "agent_type" not in task:
            agent_type = string_value(data.get("agentType"))
            if agent_type is not None:
                task["agent_type"] = agent_type
        task["evidence"].extend(_evidence("subagent.started", child))
    call.setdefault("subagent_tasks", []).append(task)
    session["subagent_tasks"].append(task)


def _task_children(
    owner: _CallActivity, records: RecordIndex
) -> list[dict[str, JsonValue]]:
    children: list[dict[str, JsonValue]] = []
    if not owner.call.get("id"):
        return children
    for record in records.values():
        if record is None or record.get("type") != SUBAGENT_STARTED:
            continue
        data = record.get("data")
        parent = records.get(string_value(record.get("parentId")) or "")
        if not isinstance(data, dict) or parent is None:
            continue
        parent_data = parent.get("data")
        if (
            parent.get("type") == TOOL_EXECUTION_START
            and parent.get("agentId") == owner.agent_id
            and isinstance(parent_data, dict)
            and parent_data.get("toolCallId") == owner.call.get("id")
            and data.get("toolCallId") == owner.call.get("id")
        ):
            children.append(record)
    return children


def _new_invocation(session: Session, name: str | None) -> Invocation:
    invocation = Invocation(
        ref=f"copilot:i{len(session['invocations'])}",
        kind=InvocationKind.SKILL,
        name=name,
        evidence=[],
        activity=None,
    )
    session["invocations"].append(invocation)
    return invocation


def _skill_call(session: Session, owner: _CallActivity) -> None:
    call = owner.call
    if call["name"] != SKILL:
        return
    payload = call.get("input")
    name = string_value(payload.get("skill")) if isinstance(payload, dict) else None
    invocation = _new_invocation(session, name)
    invocation["activity"] = owner.activity
    invocation["evidence"] = _evidence("tool.input", json_value(payload))
    if "id" in call:
        invocation["native_id"] = call["id"]
    call.setdefault("invocations", []).append(invocation)


def _linked_call(
    record: dict[str, JsonValue], records: RecordIndex, calls: list[_CallActivity]
) -> _CallActivity | None:
    parent = records.get(string_value(record.get("parentId")) or "")
    if parent is None or parent.get("type") != TOOL_EXECUTION_COMPLETE:
        return None
    data = parent.get("data")
    if (
        not isinstance(data, dict)
        or not string_value(data.get("toolCallId"))
        or parent.get("agentId") != record.get("agentId")
    ):
        return None
    matches = []
    for owner in calls:
        if (
            owner.call.get("id") == data.get("toolCallId")
            and owner.agent_id == record.get("agentId")
            and owner.call["name"] == SKILL
        ):
            matches.append(owner)
    return matches[0] if len(matches) == 1 else None


def _linked_message(
    record: dict[str, JsonValue], records: RecordIndex, session: Session
) -> Message | None:
    data = record.get("data")
    if not isinstance(data, dict) or data.get("trigger") != USER_INVOKED:
        return None
    matches = []
    for message in session["events"]:
        native = records.get(message.get("id", ""))
        if native is not None and native.get("type") == USER_MESSAGE:
            if linked_skill_invocation(native, records) is record:
                matches.append(message)
    return matches[0] if len(matches) == 1 else None


def _definition(invocation: Invocation, data: dict[str, JsonValue]) -> None:
    content = string_value(data.get("content"))
    if content is None:
        return
    definition = InvocationDefinition(text=content)
    path = string_value(data.get("path"))
    if path is not None:
        definition["path"] = path
    invocation.setdefault("definitions", []).append(definition)


def _native_invocation(
    session: Session,
    record: dict[str, JsonValue],
    records: RecordIndex,
    calls: list[_CallActivity],
) -> None:
    data = record.get("data")
    if not isinstance(data, dict):
        return
    owner = _linked_call(record, records, calls)
    message = _linked_message(record, records, session)
    if owner is not None:
        invocation = owner.call["invocations"][0]
        if invocation["name"] is None:
            invocation["name"] = string_value(data.get("name"))
    else:
        invocation = _new_invocation(session, string_value(data.get("name")))
    if message is not None:
        invocation["activity"] = ActionActivityReference(
            message_i=message["i"],
            evidence=_evidence("user.message.id", json_value(message.get("id")))
            + _evidence("skill.invoked.id", json_value(record.get("id"))),
        )
        message.setdefault("invocations", []).append(invocation)
    invocation["evidence"].extend(_evidence(SKILL_INVOKED, record))
    native_id = string_value(record.get("id"))
    if native_id is not None:
        invocation["native_id"] = native_id
    _definition(invocation, data)


_SKILL_LINK = re.compile(r"\[\$[^\]\n]+\]\(([^)\n]+)\)")


def _slash_invocation(session: Session, message: Message) -> None:
    submitted = message.get("submitted")
    text = submitted["text"] if submitted is not None else ""
    if not text.startswith("/"):
        return
    command, separator, arguments = text.partition(" ")
    invocations = message.get("invocations", [])
    if len(invocations) == 1 and invocations[0]["name"] == command[1:]:
        invocation = invocations[0]
    else:
        invocation = _new_invocation(session, command[1:])
        invocation["kind"] = InvocationKind.COMMAND
        invocation["activity"] = ActionActivityReference(
            message_i=message["i"], evidence=_evidence("submitted.text", text)
        )
        message.setdefault("invocations", []).append(invocation)
    invocation["slash_text"] = text
    invocation["arguments"] = arguments if separator else ""
    invocation["evidence"].extend(_evidence("submitted.text", text))
    links = []
    for match in _SKILL_LINK.finditer(text):
        links.append(RecordedSkillLink(syntax=match.group(), target=match.group(1)))
    if links:
        invocation["skill_links"] = links


def _database_actions(session: Session, files: list[dict[str, JsonValue]]) -> None:
    for row in files:
        session["file_actions"].append(
            FileAction(
                ref=f"copilot:f{len(session['file_actions'])}",
                action_name=string_value(row.get("tool_name")) or "tool",
                path=string_value(row.get("file_path")),
                execution_outcome=ToolExecutionOutcome.UNKNOWN,
                change=None,
                evidence=_evidence("session_files", row),
                activity=None,
            )
        )


def _unique_call_scopes(calls: list[_CallActivity]) -> set[tuple[str | None, str]]:
    counts: dict[tuple[str | None, str], int] = {}
    for owner in calls:
        call_id = owner.call.get("id")
        if call_id:
            scope = owner.agent_id, call_id
            counts[scope] = counts.get(scope, 0) + 1
    return {scope for scope, count in counts.items() if count == 1}


def _completion_owner(
    record: dict[str, JsonValue], calls: list[_CallActivity]
) -> _CallActivity | None:
    data = record.get("data")
    if not isinstance(data, dict) or not string_value(data.get("toolCallId")):
        return None
    matches = []
    for owner in calls:
        if owner.call.get("id") == data["toolCallId"] and owner.agent_id == record.get(
            "agentId"
        ):
            matches.append(owner)
    return matches[0] if len(matches) == 1 else None


def _completion_edits(
    session: Session, record: dict[str, JsonValue], owner: _CallActivity | None
) -> None:
    data = record.get("data")
    if not isinstance(data, dict):
        return
    edits = data.get("fileEdits")
    if not isinstance(edits, list):
        return
    for index, edit in enumerate(edits):
        if not isinstance(edit, dict):
            continue
        path = string_value(edit.get("path"))
        name = string_value(edit.get("kind"))
        if name is None:
            continue
        if owner is not None and _has_file_path(owner.call, path):
            continue
        evidence = _evidence(f"completion.fileEdits[{index}]", edit)
        confirmation = None
        if path is not None and name in (CREATE, EDIT):
            confirmation = FileChangeConfirmation(evidence=evidence)
        action = FileAction(
            ref=f"copilot:f{len(session['file_actions'])}",
            action_name=name,
            path=path,
            execution_outcome=_outcome(data),
            change=confirmation,
            evidence=evidence,
            activity=owner.activity if owner is not None else None,
        )
        native_id = string_value(record.get("id"))
        if native_id is not None:
            action["native_id"] = native_id
        if owner is not None:
            owner.call.setdefault("file_actions", []).append(action)
        session["file_actions"].append(action)


def _has_file_path(call: ToolCall, path: str | None) -> bool:
    return any(action["path"] == path for action in call.get("file_actions", []))


def _orphan_task(session: Session, record: dict[str, JsonValue]) -> None:
    data = record.get("data")
    if not isinstance(data, dict):
        return
    task = SubagentTask(
        ref=f"copilot:t{len(session['subagent_tasks'])}",
        evidence=_evidence(SUBAGENT_STARTED, record),
        activity=None,
    )
    native_id = string_value(record.get("id"))
    if native_id is not None:
        task["native_id"] = native_id
    identity = string_value(record.get("agentId"))
    if identity is not None:
        task["agent_id"] = identity
    description = string_value(data.get("agentDescription"))
    if description is not None:
        task["description"] = description
    agent_type = string_value(data.get("agentType"))
    if agent_type is not None:
        task["agent_type"] = agent_type
    session["subagent_tasks"].append(task)


def _remaining_action_facts(
    session: Session,
    native_records: list[dict[str, JsonValue]],
    calls: list[_CallActivity],
    records: RecordIndex,
) -> None:
    linked_children = set()
    for owner in calls:
        if owner.call["name"] == TASK:
            children = _task_children(owner, records)
            if len(children) == 1:
                linked_children.add(id(children[0]))
    for record in native_records:
        if record.get("type") == TOOL_EXECUTION_COMPLETE:
            completion_owner = _completion_owner(record, calls)
            if (
                completion_owner is not None
                and _unique_completion(completion_owner, native_records) is None
            ):
                completion_owner = None
            _completion_edits(session, record, completion_owner)
        elif (
            record.get("type") == SUBAGENT_STARTED and id(record) not in linked_children
        ):
            _orphan_task(session, record)


def interpret_actions(
    session: Session,
    native_records: list[dict[str, JsonValue]],
    files: list[dict[str, JsonValue]],
    native_calls: list[ToolCall],
) -> None:
    """Attach established facts and derive indexes from those same records."""
    session["file_actions"] = []
    session["subagent_tasks"] = []
    session["invocations"] = []
    records = record_index(native_records)
    calls = _calls(session, native_calls)
    unique_scopes = _unique_call_scopes(calls)
    for owner in calls:
        unique = (owner.agent_id, owner.call.get("id", "")) in unique_scopes
        completion = _unique_completion(owner, native_records) if unique else None
        _file_action(session, owner, completion)
        _task(session, owner, records if unique else {})
        _skill_call(session, owner)
    for record in native_records:
        if record.get("type") == SKILL_INVOKED:
            _native_invocation(session, record, records, calls)
    for message in session["events"]:
        _slash_invocation(session, message)
    unique_calls = []
    for owner in calls:
        if (owner.agent_id, owner.call.get("id", "")) in unique_scopes:
            unique_calls.append(owner)
    _remaining_action_facts(session, native_records, unique_calls, records)
    _database_actions(session, files)
