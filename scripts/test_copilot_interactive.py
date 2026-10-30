"""Contracts independently grounded in the current interactive native capture."""

import base64
import json
from copy import deepcopy

import pytest
from copilot_fixture import (
    ASSETS,
    DATABASE,
    SESSION_ID,
    STATE_ROOT,
    native_source,
    recorded_calls,
)

from session_log_extractors import extract_copilot_session_log as copilot
from session_log_extractors.session_log_envelope import export_session


def test_native_human_markup_survives_interpretation():
    source = native_source()
    native = None
    for record in source.state_records:
        if record["type"] == "user.message":
            native = record
            break

    assert native is not None, "Required human markup evidence is missing"
    session = copilot.interpret_source(source)
    actual = session["events"][0]["text"]
    expected = native["data"]["content"]

    assert "CAPTURE_LITERAL_MARKUP" in expected
    assert actual == expected


def test_native_question_answers_use_recorded_maps_and_call_ids():
    _assert_recorded_question_answers(native_source())


def _assert_recorded_question_answers(source):
    responses = []
    for record in source.state_records:
        if record["type"] == "human_response.recorded":
            if record["data"]["response"]["responseKind"] == "ask_user":
                responses.append(record)
    assert len(responses) >= 5, "Required question evidence is missing"

    session = copilot.interpret_source(source)
    calls = recorded_calls(session)

    for response in responses:
        data = response["data"]
        call = calls[data["toolCallId"]]
        answers = data["response"]["content"]
        questions = call["questions"]
        assert len(questions) == len(answers)
        for question in questions:
            expected = answers[question["field_name"]]
            assert question["recorded_answer"] == expected
            assert isinstance(question["answer_text"], str)
            assert question["answer_text"]
            offered = []
            for option in question["options"]:
                offered.append(option["label"])
            values = [expected] if isinstance(expected, str) else expected
            chosen = []
            for value in values:
                if value in offered:
                    chosen.append(value)
            assert question["chosen_labels"] == chosen
            assert len(chosen) == len(set(chosen))


def test_native_plan_decisions_follow_boolean_approval_and_feedback():
    source = native_source()
    session = copilot.interpret_source(source)
    calls = recorded_calls(session)
    decisions = []
    for record in source.state_records:
        if record["type"] != "human_response.recorded":
            continue
        data = record["data"]
        response = data["response"]
        if response["responseKind"] != "exit_plan_mode":
            continue
        decision = calls[data["toolCallId"]]["plan_decision"]
        expected = "approved" if response["approved"] else "rejected"
        assert decision["decision"] == expected
        assert decision["plan"] == response["planContent"]
        assert decision["message"] == response.get("feedback", "")
        decisions.append(expected)
    assert set(decisions) == {"approved", "rejected"}


def test_native_child_messages_keep_their_recorded_agent_identity():
    source = native_source()
    child_messages = {}
    for record in source.state_records:
        if record["type"] == "assistant.message" and record.get("agentId"):
            child_messages[record["id"]] = record["agentId"]
    assert child_messages, "Required child activity is missing"

    session = copilot.interpret_source(source)
    observed = {}
    for message in session["events"]:
        if message.get("id") in child_messages:
            observed[message["id"]] = message["agent_id"]
            if message.get("text"):
                assert message["authorship"]
                for span in message["authorship"]:
                    assert span["source"] == "other_agent"
                    assert {"field": "agentId", "value": message["agent_id"]} in span[
                        "markers"
                    ]
    assert observed == child_messages


def test_native_user_shell_retains_command_and_complete_result():
    source = native_source()
    requests = []
    for record in source.state_records:
        if record["type"] == "tool.user_requested":
            requests.append(record)
    assert len(requests) == 1, "Required user shell evidence is missing"
    (request,) = requests
    data = request["data"]

    session = copilot.interpret_source(source)
    shell_messages = []
    for message in session["events"]:
        if message.get("shell_command") == data["arguments"]["command"]:
            shell_messages.append(message)
    assert len(shell_messages) == 1
    (message,) = shell_messages
    assert message["shell_result"]["tool_call_id"] == data["toolCallId"]
    assert "CAPTURE_USER_STDOUT_1" in message["shell_result"]["content"]["content"]
    assert "CAPTURE_USER_STDERR_1" in message["shell_result"]["content"]["content"]
    assert message["authorship"]
    for span in message["authorship"]:
        assert span["source"] == "candidate"
    original = deepcopy(session)
    assert export_session(session, -1)["events"]
    assert session == original


def test_complete_native_inputs_results_and_ownership():
    source = native_source()
    session = copilot.interpret_source(source)
    calls = recorded_calls(session)
    results = {}
    for call in calls.values():
        if "result_record" in call:
            result = call["result_record"]
            results[result["id"]] = result
    for message in session["events"]:
        if "shell_result" in message:
            result = message["shell_result"]
            results[result["id"]] = result
    for result in session["unmatched_tool_results"]:
        results[result["id"]] = result
    completions = 0
    starts = 0
    for record in source.state_records:
        data = record["data"]
        if record["type"] == "tool.execution_start":
            starts += 1
            call = calls[data["toolCallId"]]
            assert call["input"] == data["arguments"]
            assert call["execution"]["input"] == data["arguments"]
        if record["type"] == "tool.execution_complete":
            completions += 1
            result = results[record["id"]]
            assert result["tool_call_id"] == data["toolCallId"]
            assert result["content"] == data.get("result")
            if "error" in data:
                assert calls[data["toolCallId"]]["error"] == data["error"]
    assert starts > 20
    assert completions == len(results)


def test_large_native_write_and_long_paths_are_complete():
    session = copilot.interpret_source(native_source())
    writes = []
    long_paths = []
    for call in recorded_calls(session).values():
        payload = call["input"]
        if call["name"] == "create" and payload.get("path", "").endswith(
            "large-write.txt"
        ):
            writes.append(payload["file_text"])
        if call["name"] == "edit" and payload.get("path", "").endswith("greeting.py"):
            long_paths.append(payload["path"])
    assert writes and long_paths
    write_sizes = []
    for text in writes:
        write_sizes.append(len(text.encode()))
    assert max(write_sizes) > 4096
    for path in long_paths:
        assert len(path.removeprefix("/workspace/")) > 100
        assert " " in path and "雪" in path


@pytest.mark.parametrize("budget", [0, 17, 4096, -1])
def test_native_export_budgets_keep_inputs_and_do_not_mutate(budget):
    session = copilot.interpret_source(native_source())
    original = deepcopy(session)
    exported = export_session(session, budget)
    assert len(exported["events"]) == len(session["events"])
    saw_large_result = False
    for message, exported_message in zip(
        session["events"], exported["events"], strict=True
    ):
        for call, exported_call in zip(
            message.get("tool_calls", []),
            exported_message.get("tool_calls", []),
            strict=True,
        ):
            assert exported_call["input"] == call["input"]
            result = call["result"]
            if not isinstance(result, str):
                continue
            result_bytes = len(result.encode())
            saw_large_result |= result_bytes > 10000
            expected = (
                result
                if budget < 0
                else result.encode()[:budget].decode(errors="ignore")
            )
            assert exported_call["result"] == expected
            assert exported_call["result_bytes"] == result_bytes
            assert exported_call["result_truncated"] is (
                budget >= 0 and result_bytes > budget
            )
    assert saw_large_result, "Capture lacks a persisted large result"
    assert session == original


def test_native_budget_splits_an_actual_multibyte_character():
    session = copilot.interpret_source(native_source())
    candidates = []
    for message in session["events"]:
        for call in message.get("tool_calls", []):
            if call["name"] == "view" and call["input"].get("path", "").endswith(
                "large.txt"
            ):
                candidates.append(call)
    assert len(candidates) == 1
    (call,) = candidates
    result = call["result"]
    prefix = 0
    for character in result:
        if len(character.encode()) > 1:
            break
        prefix += len(character.encode())
    budget = prefix + 1
    assert (
        result.encode()[:budget].decode(errors="ignore").encode()
        != result.encode()[:budget]
    )
    exported = export_session(session, budget)
    outputs = []
    for message in exported["events"]:
        for candidate in message.get("tool_calls", []):
            if (
                candidate["name"] == call["name"]
                and candidate["input"] == call["input"]
            ):
                outputs.append(candidate)
    assert len(outputs) == 1
    assert outputs[0]["result"] == result.encode()[:budget].decode(errors="ignore")
    assert outputs[0]["result_truncated"] is True


def test_native_attachment_bytes_types_order_and_message_ownership():
    source = native_source()
    session = copilot.interpret_source(source)
    messages = {}
    for message in session["events"]:
        if "id" in message:
            messages[message["id"]] = message
    assets = {}
    for record in source.state_records:
        if record["type"] == "session.binary_asset":
            assets[record["data"]["assetId"]] = record["data"]
    seen = []
    for record in source.state_records:
        attachments = record["data"].get("attachments", [])
        if record["type"] != "user.message" or not attachments:
            continue
        images = messages[record["id"]]["images"]
        assert len(images) == len(attachments)
        for attachment, image in zip(attachments, images, strict=True):
            asset = assets[attachment["assetId"]]
            assert image["data"] == asset["data"]
            assert image["media_type"] == asset["mimeType"]
            expected_bytes = (
                ASSETS / attachment["displayName"].removeprefix("@")
            ).read_bytes()
            assert base64.b64decode(image["data"]) == expected_bytes
            seen.append(image["media_type"])
    assert seen == ["image/png", "image/png", "image/jpeg", "image/gif", "image/webp"]


def test_native_permission_refusals_keep_human_and_configured_sources():
    session = copilot.interpret_source(native_source())
    refusals = []
    for call in recorded_calls(session).values():
        if "permission_denial" in call:
            refusals.append(call["permission_denial"])
    assert len(refusals) == 2
    assert refusals[0]["by"] == "user"
    assert refusals[0]["message"].startswith("CAPTURE_PERMISSION_DENY_1")
    assert refusals[1]["by"] == "settings"
    assert "shell(touch)" in refusals[1]["message"]


def test_native_cli_stdout_and_file_agree(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(copilot.copilot_sources, "COPILOT_STATE_ROOT", STATE_ROOT)
    output = tmp_path / "session.json"
    common = [
        SESSION_ID,
        "--db",
        str(DATABASE),
        "--tool-result-bytes",
        "17",
        "--output",
    ]
    file_status = copilot.main([*common, str(output)])
    file_diagnostics = capsys.readouterr()
    stdout_status = copilot.main([*common, "-"])
    stdout_diagnostics = capsys.readouterr()
    assert file_status == stdout_status == 0
    assert file_diagnostics.out == ""
    assert json.loads(output.read_text()) == json.loads(stdout_diagnostics.out)
    assert "using session:" in file_diagnostics.err
    assert "using session:" in stdout_diagnostics.err


def test_question_contract_tolerates_reworded_capture():
    source = native_source()
    replacements = _question_label_replacements(source)
    for record in source.state_records:
        data = record["data"]
        if record["type"] == "assistant.message":
            data["content"] = "Reworded assistant response."
            for request in data.get("toolRequests", []):
                if request["name"] == "ask_user":
                    _reword_question_payload(request["arguments"], replacements)
        elif record["type"] == "human_response.recorded":
            response = data["response"]
            if response["responseKind"] == "ask_user":
                _reword_question_payload(response, replacements)
                _reword_recorded_answers(response, replacements)
    _assert_recorded_question_answers(source)


def _reword_question_payload(payload, replacements):
    payload["message"] = "Reworded question?"
    for field in payload["requestedSchema"]["properties"].values():
        field["title"] = "Reworded header"
        choices = field.get("items", field)
        if "enum" in choices:
            labels = []
            for label in choices["enum"]:
                labels.append(replacements.get(label, "Reworded " + label))
            choices["enum"] = labels


def _question_label_replacements(source):
    replacements = {}
    for record in source.state_records:
        if record["type"] == "human_response.recorded":
            response = record["data"]["response"]
            if response["responseKind"] != "ask_user":
                continue
            for field in response["requestedSchema"]["properties"].values():
                choices = field.get("items", field)
                for label in choices.get("enum", []):
                    replacements[label] = "Reworded " + label
    return replacements


def _reword_recorded_answers(response, replacements):
    for key, value in response["content"].items():
        if isinstance(value, list):
            changed = []
            for item in value:
                changed.append(replacements.get(item, "Reworded custom answer"))
            response["content"][key] = changed
        else:
            response["content"][key] = replacements.get(value, "Reworded custom answer")


def test_native_skill_calls_and_slash_arguments_remain_distinct():
    source = native_source()
    session = copilot.interpret_source(source)
    skill_calls = []
    commands = []
    for call in recorded_calls(session).values():
        if call["name"] == "skill":
            skill_calls.append(call)
    for message in session["events"]:
        if message.get("command") == "/capture-command":
            commands.append(message)
    assert len(skill_calls) == 2
    assert skill_calls[0]["id"] != skill_calls[1]["id"]
    assert session["skills_used"].count("capture-greeting") == 1
    assert len(commands) == 1
    (command,) = commands
    assert command["command_args"] == 'CAPTURE_ARGS "two words", café 雪'
    assert command["text"] == '/capture-command CAPTURE_ARGS "two words", café 雪'


def test_native_question_cancellation_is_not_an_answer():
    source = native_source()
    session = copilot.interpret_source(source)
    calls = recorded_calls(session)
    answered_ids = set()
    cancelled = []
    for record in source.state_records:
        if record["type"] == "human_response.recorded":
            answered_ids.add(record["data"]["toolCallId"])
    for call in calls.values():
        if call["name"] == "ask_user" and call["id"] not in answered_ids:
            cancelled.append(call)
    assert len(cancelled) == 1, "Missing cancelled question evidence"
    (call,) = cancelled
    assert call["result_record"]
    for question in call["questions"]:
        assert "recorded_answer" not in question
        assert "answer_text" not in question
        assert "chosen_labels" not in question


def test_native_plan_editor_assets_agree_with_approved_plan():
    before = (ASSETS / "plan-before-editor.md").read_text()
    after = (ASSETS / "plan-after-editor.md").read_text()
    assert "CAPTURE_PLAN_EDIT_1" not in before
    assert "CAPTURE_PLAN_EDIT_1" in after
    session = copilot.interpret_source(native_source())
    edited_approvals = []
    for call in recorded_calls(session).values():
        decision = call.get("plan_decision")
        if (
            decision
            and decision["decision"] == "approved"
            and "CAPTURE_PLAN_EDIT_1" in decision["plan"]
        ):
            edited_approvals.append(decision)
    assert len(edited_approvals) == 1
    assert edited_approvals[0]["plan"] == after
    # Copilot recorded approval of an edited file, without a native edited flag.
    assert edited_approvals[0]["edited_plan"] == ""


def test_native_image_tool_result_resolves_bytes_without_changing_raw_payload():
    source = native_source()
    session = copilot.interpret_source(source)
    image_calls = []
    for call in recorded_calls(session).values():
        if call["name"] == "view" and call["input"].get("path", "").endswith("red.png"):
            image_calls.append(call)
    assert len(image_calls) == 1
    (call,) = image_calls
    (image,) = call["result_images"]
    assert base64.b64decode(image["data"]) == (ASSETS / "red.png").read_bytes()
    assert image["media_type"] == "image/png"
    blocks = call["result_record"]["content"]["binaryResultsForLlm"]
    assert blocks[0]["assetId"]
    assert call["result"]


def test_missing_tool_image_asset_is_explicitly_unavailable():
    source = native_source()
    retained = []
    for record in source.state_records:
        if record["type"] != "session.binary_asset":
            retained.append(record)
    source.state_records = retained
    session = copilot.interpret_source(source)
    images = []
    for call in recorded_calls(session).values():
        images.extend(call.get("result_images", []))
    assert len(images) == 1
    assert images[0]["unavailable"] is True
    assert images[0]["ref"].startswith("sha256:")


def test_native_question_capture_contains_each_required_answer_shape():
    session = copilot.interpret_source(native_source())
    features = set()
    for call in recorded_calls(session).values():
        if call["name"] != "ask_user":
            continue
        questions = call["questions"]
        if len(questions) > 1:
            features.add("multiple questions")
        for question in questions:
            _record_question_features(question, features)
    assert features == {
        "multiple questions",
        "multiple selections",
        "custom text",
        "selection plus note",
        "overlapping labels",
    }


def _record_question_features(question, features):
    labels = []
    for option in question["options"]:
        labels.append(option["label"])
    answer = question.get("recorded_answer")
    chosen = question.get("chosen_labels", [])
    if len(chosen) >= 2:
        features.add("multiple selections")
    if isinstance(answer, str) and answer and answer not in labels:
        features.add("custom text")
    if isinstance(answer, list) and len(answer) > len(chosen) and chosen:
        features.add("selection plus note")
    for short in labels:
        for long in labels:
            if short != long and short in long and chosen == [long]:
                features.add("overlapping labels")


def test_duplicate_user_shell_completion_preserves_first_and_unmatched():
    source = native_source()
    completions = []
    for record in source.state_records:
        if record["type"] == "tool.execution_complete" and record["data"].get(
            "isUserRequested"
        ):
            completions.append(record)
    assert len(completions) == 1
    (completion,) = completions
    duplicate = deepcopy(completion)
    duplicate["id"] = "controlled-duplicate-shell-result"
    duplicate["data"]["result"] = {"content": "different duplicate output"}
    source.state_records.append(duplicate)

    session = copilot.interpret_source(source)

    shells = []
    for message in session["events"]:
        if message.get("shell_result"):
            shells.append(message)
    assert len(shells) == 1
    assert shells[0]["shell_result"]["id"] == completion["id"]
    assert shells[0]["shell_result"]["content"] == completion["data"]["result"]
    (unmatched,) = session["unmatched_tool_results"]
    assert unmatched["id"] == duplicate["id"]
    assert unmatched["content"] == duplicate["data"]["result"]


def test_empty_user_shell_output_has_no_empty_authorship_range():
    source = native_source()
    for record in source.state_records:
        if record["type"] == "tool.execution_complete" and record["data"].get(
            "isUserRequested"
        ):
            # Keep the native IDs, replacing only the successful result's output.
            record["data"]["result"] = {"content": ""}
    session = copilot.interpret_source(source)
    shells = []
    for message in session["events"]:
        if message.get("shell_result"):
            shells.append(message)
    assert len(shells) == 1
    (message,) = shells
    assert message["text"] == message["shell_command"]
    assert message["shell_result"]["content"] == {"content": ""}
    for span in message["authorship"]:
        assert 0 <= span["start"] < span["end"] <= len(message["text"])
