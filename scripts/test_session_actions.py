"""Shared action contracts; fixture projections do not adopt native parsing."""

import json
from copy import deepcopy
from pathlib import Path

import pytest
from codex_capture import TRANSCRIPT as CODEX_TRANSCRIPT
from copilot_fixture import native_records

from session_log_extractors.claude_sources import read_source
from session_log_extractors.codex_sources import load_entries
from session_log_extractors.session_log_envelope import export_session
from session_log_extractors.session_model import (
    ActionActivityReference,
    ActionEvidence,
    FileAction,
    FileChangeConfirmation,
    Invocation,
    InvocationDefinition,
    InvocationKind,
    RecordedSkillLink,
    Session,
    SubagentTask,
    ToolCall,
    ToolExecutionOutcome,
)

ROOT = Path(__file__).parent / "fixtures" / "claude" / "2.1.312"
(TRANSCRIPT,) = (ROOT / "native").glob("*.jsonl")


@pytest.mark.parametrize("outcome", list(ToolExecutionOutcome))
def test_execution_outcome_never_implies_a_file_change(outcome):
    # Controlled boundary: a successful no-op and an unknown future action.
    action = FileAction(
        ref="a1",
        action_name="future-action",
        path=None,
        execution_outcome=outcome,
        change=None,
        activity=None,
        evidence=[ActionEvidence(field="example.action", value="future-action")],
    )
    decoded = json.loads(json.dumps(action))
    assert decoded["execution_outcome"] == outcome.value
    assert decoded["change"] is None
    assert decoded["path"] is None
    assert decoded["activity"] is None
    assert "native_id" not in decoded


def _illustrative_session():
    # Explicitly controlled patch evidence: two paths, only one confirmed.
    patch = (
        "*** Begin Patch\n*** Update File: café.py\n-old\n+雪\n"
        "*** Update File: untouched.py\n-old\n+new\n*** End Patch"
    )
    call = ToolCall(name="apply_patch", id="c1", input=patch, result="雪" * 4000)
    evidence = [ActionEvidence(field="example.call.id", value="c1")]
    activity = ActionActivityReference(
        message_i=7, tool_call_index=0, evidence=evidence
    )
    actions = []
    for index, path in enumerate(["café.py", "untouched.py"]):
        actions.append(
            FileAction(
                ref=f"a{index}",
                action_name="apply_patch",
                path=path,
                execution_outcome=ToolExecutionOutcome.SUCCEEDED,
                change=None,
                evidence=[ActionEvidence(field="example.patch", value=patch)],
                activity=activity,
            )
        )
    actions[0]["change"] = FileChangeConfirmation(
        evidence=[ActionEvidence(field="example.result.changed_path", value="café.py")]
    )
    task_prompt = "Full task\n" + "雪" * 5000
    task = SubagentTask(
        ref="t1",
        prompt=task_prompt,
        description="Inspect files",
        activity=None,
        evidence=[
            ActionEvidence(
                field="example.task",
                value={"description": "Inspect files", "prompt": task_prompt},
            )
        ],
    )
    return Session(
        harness="illustrative",
        session_id="s",
        events=[{"i": 7, "role": "assistant", "tool_calls": [call]}],
        file_actions=actions,
        subagent_tasks=[task],
        invocations=[_illustrative_invocation()],
        files_changed=["legacy.py"],
    )


def _illustrative_invocation():
    definition_text = "Full recorded definition\n" + "x" * 9000
    return Invocation(
        ref="v1",
        kind=InvocationKind.SKILL,
        name="review",
        activity=None,
        slash_text="/review café 雪",
        arguments="café 雪",
        skill_links=[
            RecordedSkillLink(
                syntax="[$review](skill://review)", target="skill://review"
            )
        ],
        definitions=[InvocationDefinition(text=definition_text, path="SKILL.md")],
        evidence=[
            ActionEvidence(
                field="example.invocation",
                value={
                    "text": "/review café 雪",
                    "arguments": "café 雪",
                    "link": "[$review](skill://review)",
                    "target": "skill://review",
                    "definition": definition_text,
                    "path": "SKILL.md",
                },
            )
        ],
    )


@pytest.mark.parametrize("limit", [0, 17, 8192, -1])
def test_complete_facts_and_patch_survive_export_policy_without_mutation(limit):
    session = _illustrative_session()
    original = deepcopy(session)

    exported = export_session(session, limit)
    decoded = json.loads(json.dumps(session))

    assert session == original
    assert decoded == original
    assert decoded["file_actions"][0]["change"]["evidence"][0]["value"] == "café.py"
    assert decoded["file_actions"][1]["change"] is None
    assert decoded["file_actions"][0]["activity"]["message_i"] == 7
    for field in ("file_actions", "subagent_tasks", "invocations"):
        assert field not in exported
        assert decoded[field] == original[field]
    (call,) = exported["events"][0]["tool_calls"]
    assert call["input"] == original["events"][0]["tool_calls"][0]["input"]
    assert call["result_truncated"] is (limit >= 0)
    if limit >= 0:
        assert len(call["result"].encode()) <= limit
    else:
        assert call["result"] == original["events"][0]["tool_calls"][0]["result"]
    assert session["files_changed"] == ["legacy.py"]


@pytest.mark.parametrize("kind", list(InvocationKind))
def test_unsupported_invocations_and_missing_task_text_remain_explicit(kind):
    evidence = [
        ActionEvidence(
            field="example.opaque", value={"nested": [None, {"future": True}]}
        )
    ]
    invocation = Invocation(
        ref="v", kind=kind, name=None, evidence=evidence, activity=None
    )
    task = SubagentTask(ref="t", evidence=evidence, activity=None)

    decoded_invocation, decoded_task = json.loads(json.dumps([invocation, task]))

    assert decoded_invocation["name"] is None
    assert decoded_invocation["kind"] == kind.value
    assert decoded_invocation["evidence"] == evidence
    assert "prompt" not in decoded_task
    assert "description" not in decoded_task
    assert "definitions" not in decoded_invocation
    assert decoded_task["activity"] is None


def _claude_calls():
    calls = []
    for record in read_source(TRANSCRIPT).entries:
        content = record.get("message", {}).get("content")
        if record.get("type") != "assistant" or not isinstance(content, list):
            continue
        for block in content:
            if block.get("type") == "tool_use":
                calls.append((block["name"], block["input"], block["id"]))
    return calls


def _native_calls(harness):
    if harness == "claude":
        return _claude_calls()
    calls = []
    if harness == "codex":
        for record in load_entries(CODEX_TRANSCRIPT):
            payload = record.get("payload", {})
            if (
                payload.get("type") == "function_call"
                and payload.get("name") == "spawn_agent"
            ):
                calls.append(
                    (
                        payload["name"],
                        json.loads(payload["arguments"]),
                        payload["call_id"],
                    )
                )
    else:
        for record in native_records():
            if record["type"] == "tool.execution_start":
                data = record["data"]
                calls.append((data["toolName"], data["arguments"], data["toolCallId"]))
    return calls


@pytest.mark.parametrize(
    ("harness", "tool", "prompt_field", "id_field"),
    [
        ("claude", "Agent", "prompt", "tool_use.id"),
        ("codex", "spawn_agent", "message", "payload.call_id"),
        ("copilot", "task", "prompt", "data.toolCallId"),
    ],
)
def test_native_tasks_preserve_full_recorded_prompt_and_nested_input(
    harness, tool, prompt_field, id_field
):
    tasks = []
    for name, payload, call_id in _native_calls(harness):
        if name != tool:
            continue
        prompt = payload[prompt_field]
        assert isinstance(prompt, str) and prompt
        evidence = [ActionEvidence(field=id_field, value=call_id)]
        task = SubagentTask(
            ref=f"t{len(tasks)}",
            prompt=prompt,
            evidence=evidence,
            activity=ActionActivityReference(
                message_i=0, tool_call_index=0, evidence=evidence
            ),
        )
        if "description" in payload:
            task["description"] = payload["description"]
        call = ToolCall(name=name, id=call_id, input=payload)
        session = Session(
            harness=harness,
            session_id="s",
            events=[{"i": 0, "role": "assistant", "tool_calls": [call]}],
            subagent_tasks=[task],
        )

        decoded = json.loads(json.dumps(session))
        exported = export_session(session, 0)

        assert decoded["subagent_tasks"][0]["prompt"] == payload[prompt_field]
        assert exported["events"][0]["tool_calls"][0]["input"] == payload
        assert (
            decoded["subagent_tasks"][0]["activity"]["evidence"][0]["value"] == call_id
        )
        tasks.append(task)
    assert tasks, "Capture must contain a task with a recorded prompt"


def test_native_copilot_skill_keeps_recorded_definition_without_external_loading():
    invocations = []
    for record in native_records():
        if record["type"] != "skill.invoked":
            continue
        data = record["data"]
        invocation = Invocation(
            ref=f"v{len(invocations)}",
            native_id=record["id"],
            kind=InvocationKind.SKILL,
            name=data["name"],
            activity=None,
            definitions=[InvocationDefinition(text=data["content"], path=data["path"])],
            evidence=[ActionEvidence(field="skill.invoked.data", value=data)],
        )
        decoded = json.loads(json.dumps(invocation))
        assert decoded["definitions"][0]["text"] == data["content"]
        assert decoded["evidence"][0]["value"] == data
        assert "slash_text" not in decoded
        invocations.append(invocation)
    assert invocations, "Capture must supply recorded skill definitions"


def _confirmed_claude_edits():
    edits = {}
    for name, payload, call_id in _claude_calls():
        if name == "Edit":
            edits[call_id] = payload
    interactions = []
    for record in read_source(TRANSCRIPT).entries:
        result = record.get("toolUseResult")
        if not isinstance(result, dict) or not result.get("structuredPatch"):
            continue
        for block in record["message"]["content"]:
            call_id = block.get("tool_use_id")
            if call_id not in edits or block.get("is_error"):
                continue
            interactions.append((edits[call_id], record, block))
    assert interactions, "Capture must contain ID-matched edits with structured patches"
    return interactions


def test_native_claude_edit_confirmation_retains_complete_edit_and_patch_evidence():
    for payload, record, block in _confirmed_claude_edits():
        result = record["toolUseResult"]
        call_id = block["tool_use_id"]
        # Native ID match proves the result belongs to this Edit; the
        # structured patch and its path establish the modification.
        assert result["filePath"] == payload["file_path"]
        evidence = [
            ActionEvidence(field="tool_use.id", value=call_id),
            ActionEvidence(field="tool_result.tool_use_id", value=block["tool_use_id"]),
        ]
        action = FileAction(
            ref="a1",
            action_name="Edit",
            path=payload["file_path"],
            execution_outcome=ToolExecutionOutcome.SUCCEEDED,
            change=FileChangeConfirmation(
                evidence=[ActionEvidence(field="toolUseResult", value=result)]
            ),
            evidence=[ActionEvidence(field="tool_use.input", value=payload)],
            activity=ActionActivityReference(
                message_i=0, tool_call_index=0, evidence=evidence
            ),
        )
        call = ToolCall(name="Edit", id=call_id, input=payload, result=block["content"])
        session = Session(
            harness="claude",
            session_id="s",
            events=[{"i": 0, "role": "assistant", "tool_calls": [call]}],
            file_actions=[action],
        )

        exported = export_session(session, 0)
        decoded = json.loads(json.dumps(session))

        assert exported["events"][0]["tool_calls"][0]["input"] == payload
        assert decoded["file_actions"][0]["change"]["evidence"][0]["value"] == result


@pytest.mark.parametrize("arguments", ["", "  café 雪 --mode=full  "])
def test_command_text_and_message_ownership_preserve_recorded_arguments(arguments):
    slash_text = "/review " + arguments
    evidence = [ActionEvidence(field="example.message.text", value=slash_text)]
    invocation = Invocation(
        ref="v1",
        kind=InvocationKind.COMMAND,
        name="review",
        slash_text=slash_text,
        arguments=arguments,
        evidence=evidence,
        activity=ActionActivityReference(message_i=3, evidence=evidence),
    )
    session = Session(
        harness="illustrative",
        session_id="s",
        events=[{"i": 3, "role": "user", "text": slash_text}],
        invocations=[invocation],
    )

    decoded = json.loads(json.dumps(session))

    (actual,) = decoded["invocations"]
    assert actual["kind"] == "command"
    assert actual["slash_text"] == slash_text
    assert actual["arguments"] == arguments
    assert actual["activity"]["message_i"] == decoded["events"][0]["i"]
    assert "tool_call_index" not in actual["activity"]


def test_unsupported_nested_input_and_result_remain_complete_alongside_facts():
    # Controlled future payload: recognition is unsupported, retention is exact.
    payload = {"operations": [{"path": "café.py", "unknown": [None, {"text": "雪"}]}]}
    native_result = {"future": {"output": [False, None, {"body": "雪" * 9000}]}}
    evidence = [ActionEvidence(field="example.request.input", value=payload)]
    action = FileAction(
        ref="a1",
        action_name="future-tool",
        path=None,
        execution_outcome=ToolExecutionOutcome.UNKNOWN,
        change=None,
        evidence=evidence,
        activity=ActionActivityReference(
            message_i=0,
            tool_call_index=0,
            evidence=evidence,
        ),
    )
    call = ToolCall(
        name="future-tool",
        input=payload,
        input_schema="unsupported",
        result=json.dumps(native_result),
        result_record={
            "tool_call_id": None,
            "ts": None,
            "is_error": False,
            "content": native_result,
        },
    )
    session = Session(
        harness="illustrative",
        session_id="s",
        events=[{"i": 0, "role": "assistant", "tool_calls": [call]}],
        file_actions=[action],
        unsupported_tools=["future-tool"],
    )
    original = deepcopy(session)

    exported = export_session(session, 0)

    assert session == original
    assert (
        session["events"][0]["tool_calls"][0]["result_record"]["content"]
        == native_result
    )
    assert exported["events"][0]["tool_calls"][0]["input"] == payload
    assert session["unsupported_tools"] == ["future-tool"]
    assert session["file_actions"][0]["change"] is None
