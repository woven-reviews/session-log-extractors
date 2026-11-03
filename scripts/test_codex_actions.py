"""Codex actions retain native evidence through production JSON export."""

import json
from copy import deepcopy

import pytest
from codex_capture import TRANSCRIPT

from session_log_extractors.codex_sources import CodexSource, load_entries
from session_log_extractors.extract_codex_session_log import interpret_source
from session_log_extractors.session_log_envelope import export_session


def _interpret(entries):
    session = interpret_source(CodexSource(TRANSCRIPT, entries))
    assert session is not None
    return session


def _facts(session, field):
    facts = []
    for message in session["events"]:
        facts.extend(message.get(field, []))
        for call in message.get("tool_calls") or []:
            facts.extend(call.get(field, []))
    return facts


def _call(name, arguments, call_id="call"):
    return {
        "type": "response_item",
        "payload": {
            "type": "function_call",
            "name": name,
            "call_id": call_id,
            "arguments": json.dumps(arguments),
        },
    }


def _result(output, call_id="call", **fields):
    return {
        "type": "response_item",
        "payload": {
            "type": "function_call_output",
            "call_id": call_id,
            "output": output,
            **fields,
        },
    }


def _patch(changes, **fields):
    # Controlled legacy completion: the native capture has no direct patch call.
    return {
        "type": "event_msg",
        "payload": {"type": "patch_apply_end", "changes": changes, **fields},
    }


@pytest.mark.parametrize("limit", [0, 17, -1])
def test_native_file_changes_and_tasks_survive_json_caps(limit):
    entries = load_entries(TRANSCRIPT)
    recorded_changes = []
    spawn = None
    for entry in entries:
        payload = entry.get("payload", {})
        item = payload.get("item", {})
        if item.get("type") == "FileChange":
            recorded_changes.append(item)
        if payload.get("name") == "spawn_agent":
            spawn = payload
    assert recorded_changes, "Native file-change completion evidence is required"
    assert spawn is not None, "A real subagent request is required"
    session = _interpret(entries)
    before = deepcopy(session)

    envelope = export_session(session, limit)

    assert session == before
    actions = _facts(envelope, "file_actions")
    changes_by_id = {}
    for action in actions:
        if action.get("native_id") in changes_by_id:
            changes_by_id[action["native_id"]].append(action)
        else:
            changes_by_id[action.get("native_id")] = [action]
    for recorded in recorded_changes:
        actual = changes_by_id[recorded["id"]]
        assert len(actual) == len(recorded["changes"])
        for action in actual:
            native_change = recorded["changes"][action["path"]]
            assert action["action_name"] == native_change["type"]
            assert action["execution_outcome"] == "succeeded"
            assert action["change"]["evidence"]
            owner = envelope["events"][action["activity"]["message_i"]]
            assert owner["role"] == "harness"
            assert "tool_call_index" not in action["activity"]
            assert action in owner["file_actions"]
    (task,) = _facts(envelope, "subagent_tasks")
    arguments = json.loads(spawn["arguments"])
    assert task["prompt"] == arguments["message"]
    assert isinstance(task["prompt"], str) and task["prompt"]
    assert task["agent_id"]
    call = envelope["events"][task["activity"]["message_i"]]["tool_calls"][0]
    model_call = session["events"][task["activity"]["message_i"]]["tool_calls"][0]
    assert model_call["id"] == spawn["call_id"]
    assert call["input"] == arguments
    for field in ("file_actions", "subagent_tasks", "invocations"):
        assert field not in envelope


def test_patch_attempts_keep_moves_and_complete_input_without_confirmation():
    patch = "*** Begin Patch\n*** Add File: a\n+café\n*** Update File: b\n*** Move to: c\n@@\n-x\n+y\n*** Delete File: d\n*** End Patch"
    session = _interpret([_call("functions.apply_patch", {"patch": patch})])

    envelope = export_session(session, 0)

    actions = _facts(envelope, "file_actions")
    actual = []
    for action in actions:
        actual.append((action["action_name"], action["path"], action["change"]))
        assert action["execution_outcome"] == "unknown"
    assert actual == [("Add", "a", None), ("Update", "b", None), ("Delete", "d", None)]
    assert actions[1]["destination_path"] == "c"
    assert envelope["events"][0]["tool_calls"][0]["input"] == {"patch": patch}


@pytest.mark.parametrize("output", ["Success", "", {"exit_code": 0}])
def test_success_alone_never_confirms_a_patch(output):
    session = _interpret(
        [
            _call("apply_patch", "*** Update File: a\n@@\n-x\n+y"),
            _result(output),
        ]
    )

    actions = _facts(export_session(session, 0), "file_actions")

    assert len(actions) == 1
    assert actions[0]["change"] is None


def test_explicit_completion_confirms_only_its_path_and_retains_native_event():
    session = _interpret(
        [
            _call("apply_patch", "*** Update File: a\n*** Update File: b"),
            _patch(
                {"a": {"type": "update", "unified_diff": "@@ -1 +1 @@\n-x\n+y\n"}},
                call_id="call",
                success=True,
            ),
        ]
    )

    actions = _facts(export_session(session, 0), "file_actions")

    assert len(actions) == 2
    assert actions[0]["path"] == "a"
    assert actions[0]["change"]["evidence"]
    assert actions[1]["path"] == "b"
    assert actions[1]["change"] is None
    assert actions[0]["activity"]["tool_call_index"] == 0


@pytest.mark.parametrize("fields", [{}, {"success": False}, {"success": True}])
def test_missing_failure_and_noop_evidence_cannot_confirm_changes(fields):
    session = _interpret(
        [_patch({"a": {"type": "update", "unified_diff": ""}}, **fields)]
    )

    (action,) = _facts(export_session(session, 0), "file_actions")

    assert action["change"] is None


def test_task_prompt_and_description_are_independent_and_result_ids_match():
    prompt = "Full task café " * 500
    description = "A separately recorded description"
    session = _interpret(
        [
            _call(
                "collaboration.spawn_agent",
                {
                    "message": prompt,
                    "description": description,
                    "agent_type": "worker",
                    "other": {"keep": [1, 2]},
                },
            ),
            _result({"agent_id": "child", "nested": {"keep": [3, 4]}}),
        ]
    )

    envelope = export_session(session, 0)

    (task,) = _facts(envelope, "subagent_tasks")
    assert task["prompt"] == prompt
    assert task["description"] == description
    assert task["agent_id"] == "child"
    assert task["agent_type"] == "worker"
    call = envelope["events"][0]["tool_calls"][0]
    assert call["input"]["other"] == {"keep": [1, 2]}
    model_call = session["events"][0]["tool_calls"][0]
    assert model_call["result_record"]["content"]["nested"] == {"keep": [3, 4]}


def test_opaque_shell_and_javascript_do_not_create_actions_or_tasks():
    session = _interpret(
        [
            _call(
                "exec",
                'await tools.apply_patch("*** Add File: invented"); spawn_agent({message:"guess"})',
            ),
            _call("exec_command", {"cmd": "cat /tmp/foo/SKILL.md; rm x"}, "other"),
        ]
    )

    envelope = export_session(session, 0)

    assert not _facts(envelope, "file_actions")
    assert not _facts(envelope, "subagent_tasks")
    assert not _facts(envelope, "invocations")


@pytest.mark.parametrize("success", [True, False])
def test_controlled_move_completion_requires_success_and_keeps_destination(success):
    session = _interpret(
        [
            _call("apply_patch", {"content": "*** Update File: a\n*** Move to: b"}),
            _patch(
                {"a": {"type": "update", "move_path": "b"}},
                call_id="call",
                success=success,
            ),
        ]
    )

    (action,) = _facts(export_session(session, 0), "file_actions")

    assert action["destination_path"] == "b"
    assert (action["change"] is not None) is success
    assert action["execution_outcome"] == ("succeeded" if success else "failed")


def test_unmatched_and_ambiguous_paths_keep_completion_on_native_event():
    session = _interpret(
        [
            _call("apply_patch", "*** Update File: a\n*** Update File: a"),
            _patch(
                {"a": {"type": "delete"}, "other": {"type": "future", "unknown": [1]}},
                call_id="call",
                success=True,
            ),
        ]
    )

    envelope = export_session(session, 0)

    actions = _facts(envelope, "file_actions")
    assert len(actions) == 4
    assert actions[0]["change"] is None
    assert actions[1]["change"] is None
    assert actions[2]["change"]["evidence"]
    assert actions[3]["action_name"] == "future"
    assert actions[3]["change"] is None
    assert envelope["events"][1]["role"] == "harness"


@pytest.mark.parametrize("arguments", [None, {}, {"patch": 42}, 42, "no headers"])
def test_malformed_patch_payloads_remain_complete_without_invented_actions(arguments):
    session = _interpret([_call("apply_patch", arguments)])

    envelope = export_session(session, 0)

    assert not _facts(envelope, "file_actions")
    assert envelope["events"][0]["tool_calls"][0]["input"] == arguments


@pytest.mark.parametrize(
    "arguments",
    [
        None,
        {"message": None, "description": "Only description"},
        {"agent_id": "known-child"},
    ],
)
def test_missing_task_fields_are_not_substituted(arguments):
    session = _interpret([_call("spawn_agent", arguments)])

    tasks = _facts(export_session(session, 0), "subagent_tasks")

    if arguments is None:
        assert tasks == []
    else:
        (task,) = tasks
        assert "prompt" not in task
        if "description" in arguments:
            assert task["description"] == arguments["description"]
        else:
            assert task["agent_id"] == arguments["agent_id"]
            assert "description" not in task


@pytest.mark.parametrize("output", ["{not json", "failure", {"exit_code": 1}])
def test_error_results_never_confirm_patch_actions(output):
    session = _interpret(
        [
            _call("apply_patch", "*** Add File: a\n+attempt"),
            _result(output, is_error=True),
        ]
    )

    (action,) = _facts(export_session(session, 0), "file_actions")

    assert action["execution_outcome"] == "failed"
    assert action["change"] is None


def test_structured_read_events_preserve_unknown_paths_errors_and_native_order():
    # Boundary mutation of a native CommandExecution, without parsing shell text.
    entries = load_entries(TRANSCRIPT)
    selected = None
    for entry in entries:
        item = entry.get("payload", {}).get("item", {})
        if (
            item.get("type") == "CommandExecution"
            and item.get("parsed_cmd", [{}])[0].get("type") == "read"
        ):
            selected = entry
            break
    assert selected is not None, "Native structured read evidence is required"
    record = deepcopy(selected)
    item = record["payload"]["item"]
    item["exit_code"] = 1
    item["parsed_cmd"] = [
        None,
        {"type": "read", "path": None},
        {"type": ["unknown"]},
        {"type": "unknown", "cmd": "rm a"},
    ]
    session = _interpret([record])

    (action,) = _facts(export_session(session, 0), "file_actions")

    assert action["action_name"] == "read"
    assert action["path"] is None
    assert action["execution_outcome"] == "failed"
    assert action["change"] is None


def test_native_conversation_content_and_relative_order_survive_enrichment():
    session = _interpret(load_entries(TRANSCRIPT))
    envelope = export_session(session, 17)
    recorded_order = []
    exported_order = []
    for message in session["events"]:
        recorded_order.append(
            (message["role"], message.get("text"), message.get("images"))
        )
    for message in envelope["events"]:
        exported_order.append(
            (message["role"], message.get("text"), message.get("images"))
        )

    assert exported_order == recorded_order
    assert session["file_actions"] == _facts(session, "file_actions")
    assert session["subagent_tasks"] == _facts(session, "subagent_tasks")


def test_successful_identical_diff_is_a_noop_not_a_confirmed_change():
    diff = "--- a\n+++ a\n@@ -1 +1 @@\n-same\n+same\n"
    session = _interpret(
        [_patch({"a": {"type": "update", "unified_diff": diff}}, success=True)]
    )

    (action,) = _facts(export_session(session, 0), "file_actions")

    assert action["execution_outcome"] == "succeeded"
    assert action["change"] is None


def test_same_path_without_call_id_cannot_confirm_an_earlier_attempt():
    session = _interpret(
        [
            _call("apply_patch", "*** Delete File: same"),
            _patch({"same": {"type": "delete"}}, success=True),
        ]
    )

    actions = _facts(export_session(session, 0), "file_actions")

    assert len(actions) == 2
    assert actions[0]["change"] is None
    assert actions[1]["change"]["evidence"]
    assert actions[0]["activity"]["message_i"] != actions[1]["activity"]["message_i"]


def test_completed_patch_with_conflicting_call_ids_does_not_borrow_ownership():
    session = _interpret(
        [
            _call("apply_patch", "*** Delete File: same", "first"),
            _call("apply_patch", "*** Delete File: same", "second"),
            _patch({"same": {"type": "delete"}}, call_id="first", success=True),
        ]
    )

    actions = _facts(export_session(session, 0), "file_actions")

    assert len(actions) == 2
    assert actions[0]["change"]["evidence"]
    assert actions[1]["change"] is None


def test_subagent_identity_after_result_still_matches_the_recorded_call_id():
    entries = load_entries(TRANSCRIPT)
    call = None
    result = None
    activity = None
    for entry in entries:
        payload = entry.get("payload", {})
        if payload.get("name") == "spawn_agent":
            call = entry
    assert call is not None, "Native spawn_agent is required"
    for entry in entries:
        payload = entry.get("payload", {})
        item = payload.get("item", {})
        if (
            item.get("type") == "SubAgentActivity"
            and item.get("id") == call["payload"]["call_id"]
        ):
            activity = entry
        if (
            payload.get("type") == "function_call_output"
            and payload.get("call_id") == call["payload"]["call_id"]
        ):
            result = entry
    assert result is not None and activity is not None
    session = _interpret([call, result, activity])

    (task,) = _facts(export_session(session, 0), "subagent_tasks")

    assert task["agent_id"] == activity["payload"]["item"]["agent_thread_id"]
    assert task["evidence"][-1]["value"] == activity["payload"]["item"]


def test_patch_paths_keep_recorded_whitespace_and_unicode():
    source_path = " café file "
    destination_path = " moved file "
    patch = "*** Update File: " + source_path + "\n*** Move to: " + destination_path
    session = _interpret([_call("apply_patch", patch)])

    (action,) = _facts(export_session(session, 0), "file_actions")

    assert action["path"] == source_path
    assert action["destination_path"] == destination_path


def test_successful_move_to_same_path_is_not_confirmation():
    session = _interpret(
        [_patch({"a": {"type": "update", "move_path": "a"}}, success=True)]
    )

    (action,) = _facts(export_session(session, 0), "file_actions")

    assert action["change"] is None
