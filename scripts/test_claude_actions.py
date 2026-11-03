"""Production action facts; controlled boundaries supplement the native capture."""

from copy import deepcopy
from pathlib import Path

import pytest

from session_log_extractors.claude_sources import ClaudeSource, read_source
from session_log_extractors.extract_claude_session_log import interpret_source
from session_log_extractors.session_log_envelope import export_session

NATIVE = Path(__file__).parent / "fixtures/claude/2.1.312/native"
TRANSCRIPT = next(NATIVE.glob("*.jsonl"))


def _session(entries):
    session = interpret_source(ClaudeSource(Path("controlled.jsonl"), entries))
    assert session is not None
    return session


def _call(name, payload, call_id="call"):
    return {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "tool_use", "name": name, "id": call_id, "input": payload}
            ]
        },
    }


def _result(payload, *, failed=False):
    return {
        "type": "user",
        "toolUseResult": payload,
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "call",
                    "content": "File updated",
                    "is_error": failed,
                }
            ]
        },
    }


@pytest.mark.parametrize("limit", [0, 13, -1])
def test_native_actions_tasks_and_invocations_survive_production_export(limit):
    source = read_source(TRANSCRIPT)
    session = interpret_source(source)
    assert session is not None
    original = deepcopy(session)

    exported = export_session(session, limit)

    actions = []
    tasks = []
    invocations = []
    for message in exported["events"]:
        invocations.extend(message.get("invocations", []))
        for call in message.get("tool_calls", []):
            actions.extend(call.get("file_actions", []))
            for task in call.get("subagent_tasks", []):
                assert task["prompt"] == call["input"]["prompt"]
                assert task["description"] == call["input"]["description"]
                assert task["agent_type"] == call["input"]["subagent_type"]
                assert isinstance(task["agent_id"], str) and task["agent_id"]
                tasks.append(task)
            invocations.extend(call.get("invocations", []))
    names = set()
    confirmed = set()
    for action in actions:
        names.add(action["action_name"])
        if action["change"] is not None:
            confirmed.add(action["action_name"])
    assert names == {"Read", "Write", "Edit", "NotebookEdit"}
    assert confirmed == {"Write", "Edit", "NotebookEdit"}
    assert tasks
    skills = []
    commands = []
    for invocation in invocations:
        if invocation["kind"] == "skill":
            assert invocation["definitions"][0]["text"]
            skills.append(invocation)
        else:
            commands.append(invocation)
    assert len(skills) >= 2
    assert skills[0]["ref"] != skills[1]["ref"]
    assert commands
    assert session == original


@pytest.mark.parametrize(
    "name", ["Read", "Write", "Edit", "NotebookEdit", "MultiEdit", "Create"]
)
@pytest.mark.parametrize("result_kind", ["missing", "success", "failure"])
def test_controlled_attempts_never_confirm_from_success_or_prose(name, result_kind):
    # MultiEdit/Create lack current native coverage; opaque extra input survives.
    payload = {"file_path": " ./café 雪.py ", "extra": {"nested": [None, "full"]}}
    entries = [_call(name, payload)]
    if result_kind != "missing":
        entries.append(_result({"success": True}, failed=result_kind == "failure"))
    session = _session(entries)

    exported = export_session(session, 0)

    (message,) = exported["events"]
    (call,) = message["tool_calls"]
    (action,) = call["file_actions"]
    expected_outcomes = {
        "missing": "unknown",
        "success": "succeeded",
        "failure": "failed",
    }
    assert action["execution_outcome"] == expected_outcomes[result_kind]
    assert action["change"] is None
    assert action["path"] == payload["file_path"]
    assert call["input"] == payload
    assert (
        session["file_actions"][0]
        is session["events"][0]["tool_calls"][0]["file_actions"][0]
    )


def test_duplicate_call_ids_leave_results_unassociated():
    entries = [
        _call("Edit", {"file_path": "a"}),
        _call("Edit", {"file_path": "b"}),
        _result({"filePath": "b", "structuredPatch": [{"lines": ["-x", "+y"]}]}),
    ]
    session = _session(entries)

    for action in session["file_actions"]:
        assert action["execution_outcome"] == "unknown"
        assert action["change"] is None
    assert session["events"][-1]["tool_results"]


def _user(text, native_id="message", **fields):
    return {
        "type": "user",
        "uuid": native_id,
        "message": {"role": "user", "content": text},
        **fields,
    }


@pytest.mark.parametrize("name", ["Edit", "MultiEdit", "Write", "Create"])
@pytest.mark.parametrize(
    "patch", [[], [{"lines": ["-same", "+same"]}], [{"lines": ["-before", "+after"]}]]
)
def test_controlled_patch_confirmation_requires_actual_path_change(name, patch):
    # Same-path structured hunks supplement MultiEdit/Create capture gaps.
    payload = {"filePath": "file", "structuredPatch": patch}
    session = _session([_call(name, {"file_path": "file"}), _result(payload)])

    (action,) = session["file_actions"]
    expected_confirmation = patch == [{"lines": ["-before", "+after"]}]
    assert (action["change"] is not None) is expected_confirmation
    if expected_confirmation:
        assert action["change"]["evidence"][0]["value"] == payload


@pytest.mark.parametrize(
    "payload",
    [
        None,
        "Updated",
        {"filePath": "different", "structuredPatch": [{"lines": ["+x"]}]},
        {"filePath": "file", "error": "failed", "structuredPatch": [{"lines": ["+x"]}]},
        {"filePath": "file", "structuredPatch": [None, {}, {"lines": "opaque"}]},
        {"filePath": "file", "structuredPatch": [{"lines": [None, " context"]}]},
    ],
)
def test_controlled_incomplete_change_evidence_stays_unconfirmed(payload):
    session = _session([_call("Edit", {"file_path": "file"}), _result(payload)])

    assert session["file_actions"][0]["change"] is None


@pytest.mark.parametrize("payload", [None, {}, {"file_path": None}, {"path": ""}])
def test_missing_and_empty_paths_remain_distinct(payload):
    session = _session([_call("Create", payload)])

    expected_path = "" if payload == {"path": ""} else None
    assert session["file_actions"][0]["path"] == expected_path


@pytest.mark.parametrize(
    "payload",
    [
        {"notebook_path": "file", "original_file": "a", "updated_file": "a"},
        {"notebook_path": "file", "original_file": None, "updated_file": "b"},
        {"notebook_path": "file", "original_file": "a", "updated_file": "b"},
    ],
)
def test_controlled_notebook_confirmation_requires_distinct_complete_versions(payload):
    session = _session(
        [_call("NotebookEdit", {"notebook_path": "file"}), _result(payload)]
    )

    expected_confirmation = (
        payload["original_file"] == "a" and payload["updated_file"] == "b"
    )
    assert (session["file_actions"][0]["change"] is not None) is expected_confirmation


def test_command_occurrences_preserve_whitespace_links_and_linked_definitions():
    # Controlled command wrapper and explicit native parent link; no disk loading.
    text = (
        "<command-name> /review </command-name>\n"
        "<command-message>[$review](skill://review/SKILL.md)</command-message>\n"
        "<command-args>  café 雪  </command-args>"
    )
    definition = "  Full recorded instructions\n\n"
    session = _session(
        [
            _user(text, "first"),
            _user(
                definition,
                "definition",
                parentUuid="first",
                isMeta=True,
                turnCompanion=True,
            ),
            _user(text, "second"),
            _user("[$review](skill://review/SKILL.md)", "link-only"),
            _user("<skill><name>review</name>Unlinked definition</skill>", "unlinked"),
        ]
    )
    original = deepcopy(session)

    exported = export_session(session, 0)

    first, second = session["invocations"]
    assert first["slash_text"] == " /review "
    assert first["arguments"] == "  café 雪  "
    assert first["skill_links"] == [
        {
            "syntax": "[$review](skill://review/SKILL.md)",
            "target": "skill://review/SKILL.md",
        }
    ]
    assert first["definitions"] == [{"text": definition}]
    assert "definitions" not in second
    assert first["ref"] != second["ref"]
    assert session == original
    assert exported["events"][0]["invocations"] == [first]
    for index in (1, 3, 4):
        assert "invocations" not in exported["events"][index]


@pytest.mark.parametrize(
    "relationship",
    [
        "missing",
        "duplicate-parent",
        "duplicate-source",
        "multiple-invocations",
        "not-meta",
        "not-companion",
    ],
)
def test_ambiguous_or_unmarked_definitions_do_not_enrich_invocations(relationship):
    text = "<command-name>/review</command-name><command-args></command-args>"
    entries = [_user(text, "parent")]
    fields = {"isMeta": True, "turnCompanion": True, "parentUuid": "parent"}
    if relationship == "missing":
        fields["parentUuid"] = "absent"
    elif relationship == "duplicate-parent":
        entries.append(_user(text, "parent"))
    elif relationship == "not-meta":
        fields["isMeta"] = False
    elif relationship == "not-companion":
        fields["turnCompanion"] = False
    elif relationship == "multiple-invocations":
        # Deliberately duplicated command tags must not collapse into one fact.
        entries[0] = _user(text + text, "parent")
    definition = _user("Recorded definition", "definition", **fields)
    entries.append(definition)
    if relationship == "duplicate-source":
        entries.append(deepcopy(definition))
    session = _session(entries)

    for invocation in session["invocations"]:
        assert "definitions" not in invocation


@pytest.mark.parametrize("name", ["Agent", "Task", "Skill"])
@pytest.mark.parametrize(
    "payload", [None, {}, {"prompt": "", "description": "", "skill": "", "args": ""}]
)
def test_empty_and_absent_task_and_skill_fields_are_not_substituted(name, payload):
    session = _session([_call(name, payload)])

    if name == "Skill":
        (invocation,) = session["invocations"]
        assert invocation["name"] == ("" if payload else None)
        assert ("arguments" in invocation) is bool(payload)
    else:
        (task,) = session["subagent_tasks"]
        assert ("prompt" in task) is bool(payload)
        assert ("description" in task) is bool(payload)
        assert "agent_id" not in task


def _native_tool_records(source):
    recorded_calls = {}
    recorded_results = {}
    for entry in source.entries:
        if (
            entry.get("type") not in {"assistant", "user"}
            or entry.get("isSidechain") is True
        ):
            continue
        content = entry.get("message", {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if block.get("type") == "tool_use":
                recorded_calls[block["id"]] = block
            elif block.get("type") == "tool_result":
                recorded_results[block["tool_use_id"]] = (entry, block)
    return recorded_calls, recorded_results


def test_native_inputs_results_and_fact_owners_retain_recorded_content():
    source = read_source(TRANSCRIPT)
    recorded_calls, recorded_results = _native_tool_records(source)
    session = interpret_source(source)
    assert session is not None
    exported = export_session(session, -1)

    for message, exported_message in zip(
        session["events"], exported["events"], strict=True
    ):
        for index, call in enumerate(message.get("tool_calls", [])):
            native_call = recorded_calls[call["id"]]
            assert call["input"] == native_call["input"]
            assert (
                exported_message["tool_calls"][index]["input"] == native_call["input"]
            )
            if call["id"] in recorded_results:
                entry, block = recorded_results[call["id"]]
                assert call["result_record"]["content"] == block["content"]
                if "toolUseResult" in entry:
                    assert (
                        call["result_record"]["native_result"] == entry["toolUseResult"]
                    )
            for field in ("file_actions", "subagent_tasks", "invocations"):
                for fact in call.get(field, []):
                    assert fact["activity"]["message_i"] == message["i"]
                    assert fact["activity"]["tool_call_index"] == index
                    assert fact["native_id"] == native_call["id"]


def test_skill_definition_uses_explicit_tool_id_over_unrelated_parent():
    entries = [
        _call("Skill", {"name": "review", "args": "  "}),
        _user("<command-name>/other</command-name>", "parent"),
        _user(
            "Base directory for this skill: /recorded/path\n\nFull instructions",
            "definition",
            sourceToolUseID="call",
            parentUuid="parent",
            isMeta=True,
            turnCompanion=True,
        ),
    ]
    session = _session(entries)

    skill, command = session["invocations"]
    assert skill["name"] == "review"
    assert skill["arguments"] == "  "
    assert skill["definitions"][0]["path"] == "/recorded/path"
    assert "definitions" not in command


@pytest.mark.parametrize("source_id", ["missing", "duplicate"])
def test_skill_definitions_with_missing_or_ambiguous_call_ids_remain_content(source_id):
    entries = [_call("Skill", {"skill": "review"})]
    if source_id == "duplicate":
        entries.append(_call("Skill", {"skill": "review"}))
    entries.append(
        _user(
            "Full definition",
            "definition",
            sourceToolUseID=source_id if source_id == "missing" else "call",
            isMeta=True,
            turnCompanion=True,
        )
    )
    session = _session(entries)

    for invocation in session["invocations"]:
        assert "definitions" not in invocation
    assert session["events"][-1]["text"] == "Full definition"


def test_plain_links_definitions_and_quoted_commands_do_not_create_invocations():
    entries = [
        _user("[$review](skill://review)", "link"),
        _user("<skill><name>review</name>Definition</skill>", "definition"),
        _user("An example: <command-name>/review</command-name>", "quoted"),
        _user("<command-message>review</command-message>", "no-name"),
        _user("<command-name>review</command-name>", "no-slash"),
    ]
    session = _session(entries)

    assert session["invocations"] == []
    assert len(session["events"]) == len(entries)


@pytest.mark.parametrize("entrypoint", ["cli", "sdk", None])
def test_plain_compact_requires_recorded_cli_context(entrypoint):
    # Current capture records both plain and wrapped compact invocations.
    session = _session([_user("/compact", entrypoint=entrypoint)])

    assert len(session["invocations"]) == (1 if entrypoint == "cli" else 0)
    if entrypoint == "cli":
        (invocation,) = session["invocations"]
        assert invocation["slash_text"] == "/compact"
        assert invocation["name"] == "compact"
        assert "arguments" not in invocation


def test_legacy_task_uses_the_agent_input_schema_without_filtering():
    # Controlled legacy name; the capture supplies the Agent vocabulary.
    payload = {
        "prompt": "Complete task",
        "description": "",
        "subagent_type": "Explore",
        "future": {"nested": [None, True]},
    }
    session = _session([_call("Task", payload)])

    (message,) = session["events"]
    (call,) = message["tool_calls"]
    assert call["input_schema"] == "Task"
    assert call["input"] == payload
    assert call["subagent_tasks"][0]["prompt"] == payload["prompt"]
    assert call["subagent_tasks"][0]["agent_type"] == payload["subagent_type"]


def test_error_result_cannot_confirm_a_structured_patch():
    result = {"filePath": "file", "structuredPatch": [{"lines": ["-x", "+y"]}]}
    session = _session(
        [_call("Edit", {"file_path": "file"}), _result(result, failed=True)]
    )

    (action,) = session["file_actions"]
    assert action["execution_outcome"] == "failed"
    assert action["change"] is None


def test_multiple_result_blocks_do_not_assign_the_shared_native_patch():
    first = _call("Edit", {"file_path": "file"})
    second = _call("Edit", {"file_path": "other"}, "other-call")
    patch = {"filePath": "file", "structuredPatch": [{"lines": ["-x", "+y"]}]}
    result = _result(patch)
    result["message"]["content"].append(
        {"type": "tool_result", "tool_use_id": "other-call", "content": "Updated"}
    )
    session = _session([first, second, result])

    for action in session["file_actions"]:
        assert action["execution_outcome"] == "succeeded"
        assert action["change"] is None
    (unmatched_result,) = session["events"][-1]["tool_results"]
    assert unmatched_result["native_result"] == patch
