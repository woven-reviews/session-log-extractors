"""The selected interactive Codex capture preserves recorded native evidence."""

import base64
import json
from copy import deepcopy

import pytest
from codex_capture import (
    ASSETS,
    CHILD_TRANSCRIPT,
    TRANSCRIPT,
    human_message,
    question_interaction,
)

from session_log_extractors import extract_codex_session_log as codex
from session_log_extractors.session_log_envelope import export_session


def test_native_human_text_preserves_literal_harness_markup():
    source = codex.read_source(TRANSCRIPT)
    expected = {}
    for entry in source.entries:
        payload = entry["payload"]
        if payload.get("role") != "user":
            continue
        for block in payload.get("content", []):
            if block.get(
                "type"
            ) == "input_text" and "CAPTURE_LITERAL_FENCED" in block.get("text", ""):
                expected[payload["id"]] = block["text"]
    assert expected, "The capture must contain the operator-controlled fenced XML"

    session = codex.interpret_source(source)
    actual = {}
    for event in session["events"]:
        if event.get("id") in expected:
            actual[event["id"]] = event["text"]
    assert actual == expected


def test_native_repeated_skill_expansions_retain_recorded_identity():
    source = codex.read_source(TRANSCRIPT)
    recorded = []
    for entry in source.entries:
        payload = entry["payload"]
        metadata = payload.get("internal_chat_message_metadata_passthrough", {})
        if metadata.get("content_item_kinds") == ["skills.selected_skill_instructions"]:
            recorded.append(payload)
    assert len(recorded) == 2, "Both real native skill invocations are required"

    session = codex.interpret_source(source)
    invocations = []
    for event in session["events"]:
        if event.get("skill_invocation"):
            invocations.append(event)
    assert len(invocations) == 2
    for event, payload in zip(invocations, recorded, strict=True):
        assert event["id"] == payload["id"]
        assert (
            event["turn_id"]
            == payload["internal_chat_message_metadata_passthrough"]["turn_id"]
        )
        assert event["text"] == payload["content"][0]["text"]
        assert event["skill_invocation"] == {
            "name": "capture-greeting",
            "path": "/tmp/CAPTURE_WORKSPACE/.agents/skills/capture-greeting/SKILL.md",
        }
    assert session["skills_used"] == ["capture-greeting"]


def _calls_by_id(session):
    calls = {}
    for event in session["events"]:
        for call in event.get("tool_calls", []):
            assert call["id"] not in calls, (
                "Native calls must not be replayed as new activity"
            )
            calls[call["id"]] = call
    return calls


def test_native_calls_keep_complete_inputs_results_and_recorded_ownership():
    source = codex.read_source(TRANSCRIPT)
    session = codex.interpret_source(source)
    calls = _calls_by_id(session)
    requests = []
    results = []
    for entry in source.entries:
        if entry["type"] != "response_item":
            continue
        payload = entry["payload"]
        if payload.get("type") in ("function_call", "custom_tool_call"):
            requests.append(payload)
        elif payload.get("type") in ("function_call_output", "custom_tool_call_output"):
            results.append(payload)
    assert len(calls) == len(requests) > 2
    large_inputs = []
    for request in requests:
        call = calls[request["call_id"]]
        expected = (
            request["input"]
            if request["type"] == "custom_tool_call"
            else json.loads(request["arguments"])
        )
        assert call["input"] == expected
        if isinstance(expected, str) and len(expected.encode()) > 4096:
            large_inputs.append(call["id"])
    assert large_inputs, "A literal recorded input over 4 KB is required"
    for result in results:
        call = calls[result["call_id"]]
        assert call["result_record"]["tool_call_id"] == result["call_id"]
        assert call["result_record"]["content"] == result["output"]
        assert call["result_record"]["id"] == result["id"]
        assert "result_truncated" not in call
    assert session["unmatched_tool_results"] == []


@pytest.mark.parametrize("budget", [0, 17, 4096, -1])
def test_native_export_budgets_preserve_inputs_and_do_not_mutate(budget):
    session = codex.interpret_source(codex.read_source(TRANSCRIPT))
    before = deepcopy(session)
    original_calls = list(_calls_by_id(session).values())
    exported = export_session(session, budget)
    exported_calls = []
    for event in exported["events"]:
        exported_calls.extend(event.get("tool_calls", []))
    assert len(exported_calls) == len(original_calls)
    large_results = []
    for actual, original in zip(exported_calls, original_calls, strict=True):
        assert actual["input"] == original["input"]
        encoded = original["result"].encode()
        expected = encoded if budget < 0 else encoded[:budget]
        assert actual["result"] == expected.decode(errors="ignore")
        assert actual["result_bytes"] == len(encoded)
        assert actual["result_truncated"] is (budget >= 0 and len(encoded) > budget)
        if len(encoded) > 12000:
            large_results.append(original)
    assert large_results, "The native multibyte result must exceed 12 KB"
    assert (ASSETS / "large.txt").read_text() in large_results[0]["result"]
    assert session == before


def test_native_export_splits_a_recorded_multibyte_character_safely():
    session = codex.interpret_source(codex.read_source(TRANSCRIPT))
    for call in _calls_by_id(session).values():
        result = call["result"]
        if len(result.encode()) > 12000:
            selected = call
            break
    else:
        pytest.fail("Capture lacks a large recorded result")
    prefix = ""
    for character in selected["result"]:
        if len(character.encode()) > 1:
            budget = len(prefix.encode()) + 1
            break
        prefix += character
    else:
        pytest.fail("Capture lacks a multibyte boundary")
    exported_calls = []
    for event in export_session(session, budget)["events"]:
        exported_calls.extend(event.get("tool_calls", []))
    original_calls = list(_calls_by_id(session).values())
    selected_index = original_calls.index(selected)
    assert exported_calls[selected_index]["result"] == prefix
    assert exported_calls[selected_index]["result_truncated"] is True


def test_native_patch_details_keep_added_content_diffs_and_long_paths():
    source = codex.read_source(TRANSCRIPT)
    expected = []
    for entry in source.entries:
        item = entry["payload"].get("item", {})
        if item.get("type") == "FileChange" and item.get("status") == "completed":
            expected.append(item)
    assert len(expected) >= 3
    session = codex.interpret_source(source)
    assert len(session["patch_results"]) == len(expected)
    for actual, native in zip(session["patch_results"], expected, strict=True):
        assert actual["id"] == native["id"]
        assert actual["stdout"] == native["stdout"]
        assert actual["stderr"] == native["stderr"]
        for change in actual["file_changes"]:
            recorded = native["changes"][change["path"]]
            if change["path"].endswith("/literal-large.txt"):
                assert change["content"] == (ASSETS / "literal-large.txt").read_text()
            assert change["change_type"] == recorded["type"]
            for field in ("content", "unified_diff", "move_path"):
                assert (field in change) == (field in recorded)
                if field in recorded:
                    assert change[field] == recorded[field]
    long_paths = []
    for path in session["files_changed"]:
        if len(path.removeprefix("/tmp/CAPTURE_WORKSPACE/")) > 100:
            long_paths.append(path)
    assert long_paths, "The prepared long Unicode path must survive"


def test_native_child_interpretation_excludes_inherited_parent_history():
    source = codex.read_source(CHILD_TRANSCRIPT)
    metadata = source.entries[0]["payload"]
    boundary = metadata["subagent_history_start_ordinal"]
    expected_call_ids = []
    inherited_call_ids = []
    for entry in source.entries:
        payload = entry["payload"]
        if entry["type"] == "response_item" and payload.get("type") in (
            "function_call",
            "custom_tool_call",
        ):
            destination = (
                expected_call_ids
                if entry["ordinal"] >= boundary
                else inherited_call_ids
            )
            destination.append(payload["call_id"])
    assert expected_call_ids and inherited_call_ids
    session = codex.interpret_source(source)
    assert session["session_id"] == metadata["id"]
    assert session["parent_session_id"] == metadata["parent_thread_id"]
    assert list(_calls_by_id(session)) == expected_call_ids


def test_native_user_shell_preserves_command_and_both_stream_markers():
    source = codex.read_source(TRANSCRIPT)
    expected = []
    for entry in source.entries:
        payload = entry["payload"]
        metadata = payload.get("internal_chat_message_metadata_passthrough", {})
        if metadata.get("content_item_kinds") == ["shell.user_command"]:
            expected.append(payload)
    assert len(expected) == 1
    session = codex.interpret_source(source)
    actual = []
    for event in session["events"]:
        if event.get("id") == expected[0]["id"]:
            actual.append(event)
    assert len(actual) == 1
    assert actual[0]["text"] == expected[0]["content"][0]["text"]
    assert "CAPTURE_USER_STDOUT" in actual[0]["text"]
    assert "CAPTURE_USER_STDERR" in actual[0]["text"]
    assert actual[0]["shell_command"]


def test_native_image_results_keep_structured_payload_separate_from_attachments():
    session = codex.interpret_source(codex.read_source(TRANSCRIPT))
    returned_images = []
    for call in _calls_by_id(session).values():
        content = call["result_record"]["content"]
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "input_image":
                    returned_images.append(block)
    assert len(returned_images) == 1
    raw = base64.b64decode(returned_images[0]["image_url"].split(",", 1)[1])
    assert raw == (ASSETS / "red.png").read_bytes()


def test_native_cli_stdout_and_file_agree(tmp_path, capsys):
    destination = tmp_path / "capture.json"
    file_status = codex.main(
        [
            "--transcript",
            str(TRANSCRIPT),
            "--output",
            str(destination),
            "--tool-result-bytes",
            "17",
        ]
    )
    file_diagnostics = capsys.readouterr()
    stdout_status = codex.main(
        ["--transcript", str(TRANSCRIPT), "--output", "-", "--tool-result-bytes", "17"]
    )
    stdout = capsys.readouterr()
    assert file_status == stdout_status == 0
    assert file_diagnostics.out == ""
    assert "unsupported tool input schemas" in file_diagnostics.err
    assert "unsupported tool input schemas" in stdout.err
    assert json.loads(stdout.out) == json.loads(destination.read_text())


def test_native_question_selection_tolerates_rewording_questions_labels_and_answers():
    source = codex.read_source(TRANSCRIPT)
    user, request, result = question_interaction()
    source.entries = [user, request, result]
    arguments = json.loads(request["payload"]["arguments"])
    answers = json.loads(result["payload"]["output"])
    for question_index, question in enumerate(arguments["questions"]):
        question["question"] = f"Reworded question {question_index}?"
        question["header"] = f"New header {question_index}"
        replacements = {}
        for option_index, option in enumerate(question["options"]):
            # Keep overlapping labels while changing every original word.
            label = f"Choice {question_index}" + " extended" * option_index
            replacements[option["label"]] = label
            option["label"] = label
            option["description"] = "A newly worded description."
        selected = answers["answers"][question["id"]]["answers"]
        for index, answer in enumerate(selected):
            selected[index] = replacements.get(answer, answer)
    request["payload"]["arguments"] = json.dumps(arguments)
    result["payload"]["output"] = json.dumps(answers)
    # Assistant wording is variable fixture data, independent of native answers.
    source.entries.append(
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [
                    {"type": "output_text", "text": "A completely reworded response."}
                ],
            },
        }
    )

    session = codex.interpret_source(source)
    call = _calls_by_id(session)[request["payload"]["call_id"]]

    for actual, recorded in zip(call["questions"], arguments["questions"], strict=True):
        assert actual["question"] == recorded["question"]
        assert actual["header"] == recorded["header"]
        assert len(actual["chosen_labels"]) == 1
        selected = answers["answers"][recorded["id"]]["answers"]
        assert actual["chosen_labels"][0] in selected
        labels = []
        for option in recorded["options"]:
            labels.append(option["label"])
        assert actual["chosen_labels"][0] in labels
    assert isinstance(session["events"][-1]["text"], str)
    assert session["events"][-1]["text"]


def test_native_cancellations_and_settings_refusal_remain_distinct():
    source = codex.read_source(TRANSCRIPT)
    session = codex.interpret_source(source)
    calls = _calls_by_id(session)
    cancellations = []
    settings_refusals = []
    for entry in source.entries:
        payload = entry["payload"]
        if entry["type"] != "response_item" or payload.get("type") not in (
            "function_call_output",
            "custom_tool_call_output",
        ):
            continue
        output = payload["output"]
        if isinstance(output, str) and output.startswith("aborted by user"):
            cancellations.append(payload["call_id"])
        if isinstance(output, list) and "read-only sandbox" in json.dumps(output):
            settings_refusals.append(payload["call_id"])
    assert len(cancellations) == 2, (
        "Both permission and question cancellations are required"
    )
    assert len(settings_refusals) == 1, "A separate real settings refusal is required"
    for call_id in cancellations + settings_refusals:
        assert calls[call_id]["result_record"]["tool_call_id"] == call_id
    for call_id in settings_refusals:
        assert "permission_denial" not in calls[call_id], (
            "Settings refusal must not become a human denial"
        )
    canceled_questions = []
    for call_id in cancellations:
        for question in calls[call_id].get("questions", []):
            canceled_questions.append(question)
            assert question["chosen_labels"] == []
            assert question["answer_text"] == ""
    assert canceled_questions


def test_recorded_human_provenance_preserves_outer_whitespace():
    source = codex.read_source(TRANSCRIPT)
    user = human_message(source.entries)
    # A controlled whitespace boundary on an otherwise native human record.
    text = " \n<bash-stdout>literal café</bash-stdout>\n "
    user["payload"]["content"][0]["text"] = text
    source.entries = [user]
    session = codex.interpret_source(source)
    assert session["events"][0]["text"] == text


@pytest.mark.parametrize(
    "kind", ["skills.selected_skill_instructions", "shell.user_command"]
)
def test_legacy_event_view_preserves_recorded_native_user_semantics(kind):
    source = codex.read_source(TRANSCRIPT)
    for entry in source.entries:
        metadata = entry["payload"].get(
            "internal_chat_message_metadata_passthrough", {}
        )
        if metadata.get("content_item_kinds") == [kind]:
            selected = entry
            break
    else:
        pytest.fail(f"Capture lacks required native user record: {kind}")
    source.entries = [selected]
    expected = codex.interpret_source(source)
    # Add only the legacy event view of this exact native input.
    source.entries.append(
        {
            "type": "event_msg",
            "timestamp": selected["timestamp"],
            "payload": {
                "type": "user_message",
                "message": selected["payload"]["content"][0]["text"],
            },
        }
    )

    actual = codex.interpret_source(source)

    assert actual == expected
