"""Copilot action facts through production interpretation and JSON export."""

from copy import deepcopy

import pytest
from copilot_fixture import native_source, recorded_calls

from session_log_extractors import extract_copilot_session_log as extractor
from session_log_extractors.extract_copilot_session_log import interpret_source
from session_log_extractors.session_log_envelope import export_session


def _exported_calls(session, envelope):
    calls = {}
    for original, exported in zip(session["events"], envelope["events"], strict=True):
        for call, exported_call in zip(
            original.get("tool_calls", []), exported.get("tool_calls", []), strict=True
        ):
            if "id" in call:
                calls[call["id"]] = exported_call
    return calls


@pytest.mark.parametrize("limit", [0, 17, -1])
def test_native_file_actions_preserve_paths_outcomes_and_modification_evidence(limit):
    source = native_source()
    session = interpret_source(source)
    assert session is not None
    before = deepcopy(session)

    envelope = export_session(session, limit)

    assert session == before
    calls = _exported_calls(session, envelope)
    completions = {}
    for record in source.state_records:
        if record["type"] == "tool.execution_complete":
            completions[record["data"]["toolCallId"]] = record["data"]
    names = set()
    failed_creates = []
    confirmed = []
    for action in session["file_actions"]:
        if action["activity"] is None:
            assert action["change"] is None
            continue
        call = calls[action["native_id"]]
        names.add(action["action_name"])
        assert action in call["file_actions"]
        assert action["path"] == call["input"].get("path")
        result = completions.get(action["native_id"], {})
        expected = "unknown"
        if isinstance(result.get("success"), bool):
            expected = "succeeded" if result["success"] else "failed"
        assert action["execution_outcome"] == expected
        if action["change"] is not None:
            confirmed.append(action)
            _assert_native_change(action, result)
        if call["name"] == "view" or expected == "failed":
            assert action["change"] is None
        if call["name"] == "create" and expected == "failed":
            failed_creates.append(action)
    assert names == {"view", "create", "edit", "glob", "grep"}
    assert failed_creates, "Capture must exercise failed creates"
    assert confirmed, "Capture must exercise per-path fileEdits"
    assert "file_actions" not in envelope


def _assert_native_change(action, result):
    matching = []
    for edit in result["fileEdits"]:
        if edit["path"] == action["path"]:
            matching.append(edit)
    assert matching
    assert action["change"]["evidence"]


@pytest.mark.parametrize("limit", [0, 17, -1])
def test_native_tasks_and_repeated_skill_invocations_survive_caps(limit):
    source = native_source()
    session = interpret_source(source)
    assert session is not None
    calls = recorded_calls(session)
    envelope = export_session(session, limit)
    exported = _exported_calls(session, envelope)

    assert len(session["subagent_tasks"]) == 1, "Capture must exercise a task"
    (task,) = session["subagent_tasks"]
    call = calls[task["native_id"]]
    assert task["prompt"] == call["input"]["prompt"]
    assert task["description"] == call["input"]["description"]
    assert isinstance(task["prompt"], str) and task["prompt"]
    assert task["agent_type"] == call["input"]["agent_type"]
    children = []
    for record in source.state_records:
        if record["type"] == "subagent.started":
            children.append(record)
    assert len(children) == 1
    assert task["agent_id"] == children[0]["agentId"]
    assert exported[call["id"]]["subagent_tasks"] == [task]
    skill_calls = []
    for call in calls.values():
        if call["name"] == "skill":
            skill_calls.append(call)
    assert len(skill_calls) == 2, "Capture must exercise repeated skill calls"
    for call in skill_calls:
        (invocation,) = exported[call["id"]]["invocations"]
        assert invocation["name"] == call["input"]["skill"]
        assert invocation["kind"] == "skill"
    assert (
        skill_calls[0]["invocations"][0]["ref"]
        != skill_calls[1]["invocations"][0]["ref"]
    )
    assert "subagent_tasks" not in envelope
    assert "invocations" not in envelope


def test_native_slash_invocation_enriches_one_occurrence_with_linked_definition():
    source = native_source()
    session = interpret_source(source)
    assert session is not None
    definitions = {}
    for record in source.state_records:
        if record["type"] == "skill.invoked":
            definitions[record["id"]] = record["data"]
    slashes = []
    for message in export_session(session, 0)["events"]:
        if message.get("submitted", {}).get("text", "").startswith("/"):
            slashes.append(message)
    assert len(slashes) == 1, "Capture must exercise a slash invocation"
    (message,) = slashes
    (invocation,) = message["invocations"]
    original = message["submitted"]["text"]
    assert invocation["slash_text"] == original
    assert invocation["arguments"] == original.partition(" ")[2]
    assert invocation["kind"] == "skill"
    definition = definitions[invocation["native_id"]]
    assert invocation["definitions"] == [
        {"text": definition["content"], "path": definition["path"]}
    ]
    assert len(session["invocations"]) == 3


def test_database_file_facts_remain_unassociated_without_confirming_changes():
    source = native_source()
    source.state_records = []
    session = interpret_source(source)
    assert session is not None

    assert len(session["file_actions"]) == len(source.files)
    for action, row in zip(session["file_actions"], source.files, strict=True):
        assert action["action_name"] == row["tool_name"]
        assert action["path"] == row["file_path"]
        assert action["activity"] is None
        assert action["change"] is None
        assert action["execution_outcome"] == "unknown"
    for message in export_session(session, 0)["events"]:
        for call in message.get("tool_calls", []):
            assert "file_actions" not in call


def _tool_source(name):
    """Narrow a native occurrence without changing its recorded tool payloads."""
    source = native_source()
    selected = None
    for record in source.state_records:
        if record["type"] != "assistant.message":
            continue
        for request in record["data"].get("toolRequests", []):
            if request["name"] == name:
                selected = record, request
                break
        if selected is not None:
            break
    assert selected is not None, f"Capture must supply {name}"
    owner, request = selected
    owner["data"]["toolRequests"] = [request]
    records = [owner]
    for record in source.state_records:
        if record["data"].get("toolCallId") == request["toolCallId"]:
            records.append(record)
    source.state_records = records
    source.files = []
    source.turns = []
    return source, request


@pytest.mark.parametrize(
    "evidence", [None, [], "diff --git", [{"path": "/other", "kind": "edit"}]]
)
def test_success_and_diff_text_do_not_confirm_unmatched_paths(evidence):
    source, request = _tool_source("edit")
    for record in source.state_records:
        if record["type"] == "tool.execution_complete":
            # Retain the native diff-looking result, replacing only fileEdits.
            record["data"]["success"] = True
            record["data"]["fileEdits"] = evidence
    session = interpret_source(source)
    assert session is not None

    action = session["file_actions"][0]
    assert action["path"] == request["arguments"]["path"]
    assert action["execution_outcome"] == "succeeded"
    assert action["change"] is None


def test_completion_preserves_additional_paths_without_confirming_other_attempts():
    source, request = _tool_source("edit")
    for record in source.state_records:
        if record["type"] == "tool.execution_complete":
            record["data"]["fileEdits"] = [{"path": "/other", "kind": "edit"}]
    session = interpret_source(source)
    assert session is not None

    attempted, changed = session["file_actions"]
    assert attempted["path"] == request["arguments"]["path"]
    assert attempted["change"] is None
    assert changed["path"] == "/other"
    assert changed["change"] is not None
    assert changed["activity"] == attempted["activity"]


def test_unmatched_completion_preserves_modifications_with_unknown_ownership():
    source, _ = _tool_source("edit")
    selected = []
    for record in source.state_records:
        if record["type"] == "tool.execution_complete":
            selected.append(record)
    source.state_records = selected
    session = interpret_source(source)
    assert session is not None

    (action,) = session["file_actions"]
    assert action["activity"] is None
    assert action["change"] is not None
    assert session["unmatched_tool_results"]
    assert "file_actions" not in export_session(session, 0)


def test_orphan_subagent_start_preserves_identity_without_invented_parent():
    source, _ = _tool_source("task")
    selected = []
    for record in source.state_records:
        if record["type"] == "subagent.started":
            selected.append(record)
    source.state_records = selected
    session = interpret_source(source)
    assert session is not None

    (task,) = session["subagent_tasks"]
    assert task["activity"] is None
    assert task["agent_id"] == selected[0]["agentId"]
    assert task["agent_type"] == selected[0]["data"]["agentType"]
    assert task["description"] == selected[0]["data"]["agentDescription"]
    assert "prompt" not in task


@pytest.mark.parametrize(
    "boundary",
    ["missing", "duplicate", "cross-agent", "missing-success", "nonboolean-success"],
)
def test_ambiguous_or_absent_completions_preserve_unknown_outcomes(boundary):
    source, _ = _tool_source("edit")
    completions = []
    for record in source.state_records:
        if record["type"] == "tool.execution_complete":
            completions.append(record)
    assert len(completions) == 1
    (completion,) = completions
    if boundary == "missing":
        source.state_records.remove(completion)
    elif boundary == "duplicate":
        source.state_records.append(deepcopy(completion))
    elif boundary == "cross-agent":
        completion["agentId"] = "another-agent"
    elif boundary == "missing-success":
        completion["data"].pop("success")
    else:
        completion["data"]["success"] = "yes"
    session = interpret_source(source)
    assert session is not None

    action = session["file_actions"][0]
    assert action["activity"] is not None
    assert action["execution_outcome"] == "unknown"
    if boundary in ("missing", "duplicate", "cross-agent"):
        assert action["change"] is None
        for unmatched in session["file_actions"][1:]:
            assert unmatched["activity"] is None


def test_duplicate_call_ids_cannot_borrow_modification_evidence():
    source, _ = _tool_source("edit")
    owner = source.state_records[0]
    duplicate = deepcopy(owner)
    duplicate["id"] = "duplicate-owner"
    source.state_records.insert(1, duplicate)
    session = interpret_source(source)
    assert session is not None

    attempts = []
    for action in session["file_actions"]:
        if action["activity"] is not None:
            attempts.append(action)
    assert len(attempts) == 2
    for action in attempts:
        assert action["execution_outcome"] == "unknown"
        assert action["change"] is None


@pytest.mark.parametrize(
    "boundary", ["missing", "empty", "null", "duplicate-child", "broken-parent"]
)
def test_task_fields_and_identity_retain_recorded_uncertainty(boundary):
    source, request = _tool_source("task")
    payload = request["arguments"]
    if boundary == "missing":
        payload.pop("prompt")
        payload.pop("description")
    elif boundary == "empty":
        payload["prompt"] = ""
        payload["description"] = ""
    elif boundary == "null":
        payload["prompt"] = None
        payload["description"] = None
    else:
        for record in list(source.state_records):
            if record["type"] != "subagent.started":
                continue
            if boundary == "duplicate-child":
                duplicate = deepcopy(record)
                duplicate["id"] = "another-start"
                source.state_records.append(duplicate)
            else:
                record["parentId"] = "missing-parent"
    session = interpret_source(source)
    assert session is not None

    task = session["subagent_tasks"][0]
    if boundary in ("missing", "null"):
        assert "prompt" not in task
        assert "description" not in task
    elif boundary == "empty":
        assert task["prompt"] == ""
        assert task["description"] == ""
    else:
        assert "agent_id" not in task
        for orphan in session["subagent_tasks"][1:]:
            assert orphan["activity"] is None
            assert orphan["agent_id"]
    assert recorded_calls(session)[request["toolCallId"]]["input"] == payload


def test_enrichment_preserves_all_existing_json_and_model_content(monkeypatch):
    source = native_source()
    enriched = interpret_source(source)
    assert enriched is not None
    monkeypatch.setattr(extractor, "interpret_actions", lambda *args: None)
    baseline = interpret_source(source)
    assert baseline is not None
    fields = ("file_actions", "subagent_tasks", "invocations")
    for field in fields:
        enriched.pop(field)
    for message in enriched["events"]:
        for field in fields:
            message.pop(field, None)
        for call in message.get("tool_calls", []):
            for field in fields:
                call.pop(field, None)

    assert enriched == baseline
    assert export_session(enriched, 17) == export_session(baseline, 17)


def test_database_only_facts_survive_without_conversation():
    source = native_source()
    source.state_records = []
    source.turns = []
    session = interpret_source(source)
    assert session is not None
    assert session["events"] == []
    assert session["file_actions"]


def test_malformed_call_fields_preserve_unknown_path_and_full_input():
    source, request = _tool_source("view")
    # A malformed request without lifecycle IDs still has native containment.
    request["arguments"] = "opaque"
    request.pop("toolCallId")
    source.state_records = [source.state_records[0]]
    session = interpret_source(source)
    assert session is not None

    (action,) = session["file_actions"]
    assert action["path"] is None
    assert "native_id" not in action
    assert action["activity"]["message_i"] == session["events"][0]["i"]
    assert session["events"][0]["tool_calls"][0]["input"] == "opaque"
    assert action["execution_outcome"] == "unknown"
    assert action["change"] is None


def test_native_request_without_call_id_keeps_contained_action():
    source, request = _tool_source("edit")
    # Removing the ID prevents lifecycle joins, but not request containment.
    request.pop("toolCallId")
    source.state_records = [source.state_records[0]]
    session = interpret_source(source)
    assert session is not None

    (action,) = session["file_actions"]
    assert action["activity"] is not None
    assert action["change"] is None
    assert "native_id" not in action
    assert action["path"] == request["arguments"]["path"]


def _slash_source():
    source = native_source()
    selected = []
    for record in source.state_records:
        if record["type"] == "user.message" and record["data"]["content"].startswith(
            "/"
        ):
            selected.append(record)
    assert len(selected) == 1, "Capture must supply the operator's slash submission"
    source.state_records = selected
    source.files = []
    source.turns = []
    return source, selected[0]


@pytest.mark.parametrize(
    "arguments", ["", '  café 雪 "two words"', "[$review](skill://review)"]
)
def test_unlinked_slash_commands_keep_exact_arguments_and_recorded_links(arguments):
    source, record = _slash_source()
    text = "/controlled" + (" " + arguments if arguments else "")
    record["data"]["content"] = text
    record["data"].pop("transformedContent")
    session = interpret_source(source)
    assert session is not None

    (invocation,) = session["invocations"]
    assert invocation["kind"] == "command"
    assert invocation["name"] == "controlled"
    assert invocation["slash_text"] == text
    assert invocation["arguments"] == arguments
    assert "definitions" not in invocation
    if "skill://" in arguments:
        assert invocation["skill_links"] == [
            {"syntax": arguments, "target": "skill://review"}
        ]


def test_standalone_link_and_definition_path_do_not_establish_invocation():
    source, record = _slash_source()
    record["data"]["content"] = (
        "See [$review](skill://review) and /skills/review/SKILL.md"
    )
    record["data"].pop("transformedContent")
    session = interpret_source(source)
    assert session is not None

    assert session["invocations"] == []
    assert "invocations" not in session["events"][0]


@pytest.mark.parametrize("content", ["", None, "missing"])
def test_unlinked_native_invocations_preserve_definitions_without_loading(content):
    source = native_source()
    selected = []
    for record in source.state_records:
        if record["type"] == "skill.invoked":
            selected.append(record)
    assert len(selected) == 2
    record = selected[0]
    record.pop("id")
    record["data"].pop("path")
    if content == "missing":
        record["data"].pop("content")
    else:
        record["data"]["content"] = content
    source.state_records = [record]
    source.files = []
    source.turns = []
    session = interpret_source(source)
    assert session is not None

    (invocation,) = session["invocations"]
    assert invocation["activity"] is None
    assert "native_id" not in invocation
    assert invocation["name"] == record["data"]["name"]
    if content == "":
        assert invocation["definitions"] == [{"text": ""}]
    else:
        assert "definitions" not in invocation


def test_native_invocations_cannot_borrow_ownership_from_names_or_broken_links():
    source = native_source()
    definitions = []
    for record in source.state_records:
        if record["type"] == "skill.invoked":
            # Keep names identical, but break both recorded ownership chains.
            record["parentId"] = "not-recorded"
            definitions.append(record)
        elif record["type"] == "user.message" and record["data"]["content"].startswith(
            "/"
        ):
            record["parentId"] = "not-recorded"
    assert len(definitions) == 2
    session = interpret_source(source)
    assert session is not None

    assert len(session["invocations"]) == 5
    unassociated = []
    for invocation in session["invocations"]:
        if invocation["activity"] is None:
            unassociated.append(invocation)
        else:
            assert "definitions" not in invocation
    assert len(unassociated) == 2
    for invocation, record in zip(unassociated, definitions, strict=True):
        assert invocation["definitions"][0]["text"] == record["data"]["content"]


def test_repeated_slash_submissions_have_separate_invocations():
    source, record = _slash_source()
    repeated = deepcopy(record)
    repeated["id"] = "repeated-slash"
    source.state_records.append(repeated)
    session = interpret_source(source)
    assert session is not None

    first, second = session["invocations"]
    assert first["ref"] != second["ref"]
    assert first["slash_text"] == second["slash_text"]
    assert first["activity"]["message_i"] != second["activity"]["message_i"]


def test_database_facts_preserve_recorded_empty_names_and_paths():
    source = native_source()
    source.state_records = []
    source.turns = []
    source.files = [source.files[0]]
    source.files[0]["tool_name"] = ""
    source.files[0]["file_path"] = ""
    session = interpret_source(source)
    assert session is not None

    (action,) = session["file_actions"]
    assert action["action_name"] == ""
    assert action["path"] == ""


@pytest.mark.parametrize("separator", [" ", "\t", "\n"])
def test_slash_arguments_preserve_whitespace_after_the_command_separator(separator):
    source, record = _slash_source()
    arguments = ' café 雪 "two words"\nnext line'
    record["data"]["content"] = "/controlled" + separator + arguments
    record["data"].pop("transformedContent")
    session = interpret_source(source)
    assert session is not None

    (invocation,) = session["invocations"]
    assert invocation["name"] == "controlled"
    assert invocation["arguments"] == arguments
    assert invocation["slash_text"] == record["data"]["content"]


@pytest.mark.parametrize(
    "target", ["skill://review(one)", r"skill://review\)one", "skill://café/雪"]
)
def test_invocation_skill_links_preserve_complete_syntax_and_targets(target):
    source, record = _slash_source()
    syntax = f"[$review]({target})"
    record["data"]["content"] = f"/controlled {syntax} {syntax}"
    record["data"].pop("transformedContent")
    session = interpret_source(source)
    assert session is not None

    (invocation,) = session["invocations"]
    expected = {"syntax": syntax, "target": target}
    assert invocation["skill_links"] == [expected, expected]


@pytest.mark.parametrize("syntax", ["[$review](unfinished(", "[$review](split\nline)"])
def test_incomplete_skill_links_are_not_exported_as_shortened_links(syntax):
    source, record = _slash_source()
    record["data"]["content"] = "/controlled " + syntax
    record["data"].pop("transformedContent")
    session = interpret_source(source)
    assert session is not None

    (invocation,) = session["invocations"]
    assert "skill_links" not in invocation
    assert invocation["arguments"] == syntax


def test_link_syntax_inside_a_target_is_not_a_second_invocation_link():
    source, record = _slash_source()
    target = "skill://[$inner](nested)"
    syntax = f"[$outer]({target})"
    record["data"]["content"] = "/controlled " + syntax
    record["data"].pop("transformedContent")
    session = interpret_source(source)
    assert session is not None

    (invocation,) = session["invocations"]
    assert invocation["skill_links"] == [{"syntax": syntax, "target": target}]
