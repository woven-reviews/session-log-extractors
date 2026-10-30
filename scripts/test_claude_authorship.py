"""Authorship from the interactive Claude capture and explicit boundary mutations."""

import json
from copy import deepcopy
from pathlib import Path

import pytest

from session_log_extractors import extract_claude_session_log as claude
from session_log_extractors.claude_sources import ClaudeSource
from session_log_extractors.session_log_envelope import export_session

ROOT = Path(__file__).parent / "fixtures" / "claude" / "2.1.312"
(TRANSCRIPT,) = (ROOT / "native").glob("*.jsonl")


def _recorded_user(marker):
    matches = []
    for entry in claude.load_entries(TRANSCRIPT):
        content = entry.get("message", {}).get("content")
        if entry.get("type") == "user" and isinstance(content, str):
            if marker in content and not entry.get("isCompactSummary"):
                matches.append(entry)
    assert len(matches) == 1, f"Expected one native user record for {marker}"
    (entry,) = matches
    return entry


def _queued_record():
    matches = []
    for entry in claude.load_entries(TRANSCRIPT):
        if entry.get("attachment", {}).get("type") == "queued_command":
            matches.append(entry)
    assert len(matches) == 1, "Expected a native queued command"
    (entry,) = matches
    return entry


def _interpret(entries):
    session = claude.interpret_source(ClaudeSource(TRANSCRIPT, entries))
    assert session is not None
    return session


def _single_message(entry):
    session = _interpret([entry])
    assert len(session["events"]) == 1
    (message,) = session["events"]
    return message


def _sources(message):
    sources = []
    for span in message["authorship"]:
        sources.append(span["source"])
    return sources


@pytest.mark.parametrize("prompt_source", ["typed", "queued"])
def test_interactive_human_text_is_candidate_without_stripping_literal_markup(
    prompt_source,
):
    matches = []
    for entry in claude.load_entries(TRANSCRIPT):
        if entry.get("promptSource") == prompt_source:
            matches.append(entry)
    assert matches, f"Missing native {prompt_source} input"
    for entry in matches:
        expected_text = entry["message"]["content"]
        if isinstance(expected_text, list):
            text_blocks = []
            for block in expected_text:
                if block.get("type") == "text":
                    text_blocks.append(block["text"])
            expected_text = "\n".join(text_blocks)
        message = _single_message(entry)
        assert message["text"] == expected_text
        assert message["id"] == entry["uuid"]
        assert message["ts"] == entry["timestamp"]
        assert _sources(message) == ["candidate"]
        (span,) = message["authorship"]
        assert span["start"] == 0 and span["end"] == len(expected_text)
        assert span["ts"] == entry["timestamp"]
        assert {"field": "origin.kind", "value": "human"} in span["markers"]
    literal_markup = _recorded_user("CAPTURE_LITERAL_MARKUP")
    literal_message = _single_message(literal_markup)
    assert literal_message["text"] == literal_markup["message"]["content"]
    assert _sources(literal_message) == ["candidate"]


def test_native_injected_records_remain_harness_activity():
    entries = []
    for entry in claude.load_entries(TRANSCRIPT):
        origin = entry.get("origin", {})
        if entry.get("type") == "user" and (
            entry.get("isMeta")
            or entry.get("isCompactSummary")
            or origin.get("kind") == "task-notification"
        ):
            entries.append(entry)
    assert entries, "Missing native injected activity"
    for entry in entries:
        message = _single_message(entry)
        assert _sources(message) == ["harness"]
        assert message["ts"] == entry["timestamp"]
        content = entry["message"]["content"]
        if isinstance(content, list):
            text_blocks = []
            for block in content:
                text_blocks.append(block["text"])
            content = "\n".join(text_blocks)
        assert message["text"] == content
        assert message["authorship"][0]["ts"] == entry["timestamp"]


def test_native_queued_prompt_has_candidate_words_inside_harness_wrapper():
    entry = _queued_record()
    attachment = entry["attachment"]
    original = deepcopy(entry)
    message = _single_message(entry)
    (rendered,) = entry["rendered"]
    expected_text = rendered["content"]
    assert message["role"] == entry["renderedRole"] == "system"
    assert message["text"] == expected_text
    assert message["id"] == entry["uuid"]
    assert message["parent_id"] == entry["parentUuid"]
    assert message["ts"] == entry["timestamp"]
    assert _sources(message) == ["harness", "candidate", "harness"]
    prefix, candidate, suffix = message["authorship"]
    candidate_text = expected_text[candidate["start"] : candidate["end"]]
    assert candidate_text == attachment["prompt"]
    assert prefix["start"] == 0 and prefix["end"] == candidate["start"]
    assert candidate["end"] == suffix["start"] and suffix["end"] == len(expected_text)
    assert candidate["ts"] == attachment["timestamp"]
    assert {
        "field": "attachment.source_uuid",
        "value": attachment["source_uuid"],
    } in candidate["markers"]
    assert entry == original


def test_full_native_cli_preserves_order_ownership_and_authorship(capsys):
    entries = claude.load_entries(TRANSCRIPT)
    original = deepcopy(entries)
    session = _interpret(entries)
    native_ids = []
    emitted_ids = []
    for entry in entries:
        native_ids.append(entry.get("uuid"))
    last_position = -1
    for index, message in enumerate(session["events"]):
        assert message["i"] == index
        position = native_ids.index(message["id"])
        assert position > last_position
        last_position = position
        emitted_ids.append(message["id"])
    queued = _queued_record()
    assert emitted_ids.count(queued["uuid"]) == 1
    assert claude.main(["--transcript", str(TRANSCRIPT), "--output", "-"]) == 0
    exported = json.loads(capsys.readouterr().out)
    for message, actual in zip(session["events"], exported["events"], strict=True):
        assert actual["text"] == message["text"]
        assert actual["ts"] == message["ts"]
        assert actual.get("authorship") == message.get("authorship")
        assert actual.get("images") == message.get("images")
        actual_calls = actual.get("tool_calls", [])
        for call, actual_call in zip(
            message.get("tool_calls", []), actual_calls, strict=True
        ):
            assert actual_call["input"] == call["input"]
            if "result_record" in call:
                assert call["result_record"]["tool_call_id"] == call["id"]
    assert entries == original


@pytest.mark.parametrize("source", [None, "sdk", "suggestion_accepted"])
def test_human_origin_without_original_input_evidence_stays_unknown(source):
    entry = _recorded_user("CAPTURE_LITERAL_MARKUP")
    # Desktop/SDK human-origin records can contain injected context. Retain the
    # native human origin but remove/replace the stronger input-source evidence.
    entry.pop("promptSource")
    if source is not None:
        entry["promptSource"] = source
    message = _single_message(entry)
    assert _sources(message) == ["unknown"]
    assert message["text"] == entry["message"]["content"]


@pytest.mark.parametrize("origin", [None, "unrecognized", "human"])
def test_role_and_turn_origin_alone_do_not_establish_candidate(origin):
    entry = _recorded_user("CAPTURE_LITERAL_MARKUP")
    entry.pop("promptSource")
    entry["origin"] = None if origin is None else {"kind": origin}
    message = _single_message(entry)
    assert _sources(message) == ["unknown"]


@pytest.mark.parametrize("timestamp", ["different-recorded-time", None, "missing"])
def test_queued_unicode_offsets_use_the_contents_own_timestamp(timestamp):
    entry = _queued_record()
    attachment = entry["attachment"]
    original_prompt = attachment["prompt"]
    prompt = "  café 雪 🙂e\u0301\n"
    attachment["prompt"] = prompt
    entry["rendered"][0]["content"] = entry["rendered"][0]["content"].replace(
        original_prompt, prompt
    )
    if timestamp == "missing":
        attachment.pop("timestamp")
    else:
        attachment["timestamp"] = timestamp
    message = _single_message(entry)
    candidate = message["authorship"][1]
    assert message["text"][candidate["start"] : candidate["end"]] == prompt
    assert message["ts"] == entry["timestamp"]
    if timestamp == "missing":
        assert "ts" not in candidate
    else:
        assert candidate["ts"] == timestamp


@pytest.mark.parametrize("body", ["missing", "repeated", "empty"])
def test_queued_prompt_needs_an_unambiguous_recorded_text_boundary(body):
    entry = _queued_record()
    attachment = entry["attachment"]
    # Vary only the prompt/rendered agreement; no wrapper-pattern inference.
    if body == "missing":
        attachment["prompt"] = "not present in rendered text"
    elif body == "repeated":
        entry["rendered"][0]["content"] += attachment["prompt"]
    else:
        attachment["prompt"] = ""
    message = _single_message(entry)
    assert _sources(message) == ["unknown"]
    assert message["text"] == entry["rendered"][0]["content"]


def test_peer_origin_overrides_meta_and_locates_the_other_agents_words():
    # Boundary case: origin.kind=peer with a separate body was observed in local
    # interactive main logs, not the committed Agent/task-notification capture.
    entry = _recorded_user("CAPTURE_LITERAL_MARKUP")
    body = "peer reply 🙂"
    entry["origin"] = {
        "kind": "peer",
        "from": "worker",
        "senderTaskId": "worker-task",
        "body": body,
    }
    entry["promptSource"] = "system"
    entry["turnOrigin"] = "peer"
    entry["isMeta"] = True
    entry["message"]["content"] = "<agent-message>\n" + body + "\n</agent-message>"
    message = _single_message(entry)
    assert _sources(message) == ["harness", "other_agent", "harness"]
    peer = message["authorship"][1]
    assert message["text"][peer["start"] : peer["end"]] == body
    assert {"field": "origin.kind", "value": "peer"} in peer["markers"]
    assert {"field": "origin.senderTaskId", "value": "worker-task"} in peer["markers"]


def test_peer_with_missing_body_remains_unknown_even_when_meta():
    entry = _recorded_user("CAPTURE_LITERAL_MARKUP")
    entry["origin"] = {"kind": "peer"}
    entry["isMeta"] = True
    message = _single_message(entry)
    assert _sources(message) == ["unknown"]


def test_image_only_injection_retains_images_without_empty_ranges():
    matches = []
    for entry in claude.load_entries(TRANSCRIPT):
        content = entry.get("message", {}).get("content")
        if entry.get("type") != "user" or not isinstance(content, list):
            continue
        for block in content:
            if block.get("type") == "image":
                matches.append(entry)
                break
    assert matches, "Missing native pasted images"
    entry = matches[0]
    original_message = _single_message(entry)
    images = []
    for block in entry["message"]["content"]:
        if block.get("type") == "image":
            images.append(block)
    entry["message"]["content"] = images
    entry["isMeta"] = True
    message = _single_message(entry)
    assert message["text"] == ""
    assert not message.get("authorship")
    assert message["images"] == original_message["images"]
    assert message["id"] == original_message["id"]
    exported = export_session(_interpret([entry]))
    assert exported["events"][0]["images"] == original_message["images"]


@pytest.mark.parametrize("rendering", [None, [], [{"content": None}], "unsupported"])
def test_queued_original_prompt_survives_unavailable_rendering(rendering):
    entry = _queued_record()
    entry["rendered"] = rendering
    message = _single_message(entry)
    assert message["text"] == entry["attachment"]["prompt"]
    assert _sources(message) == ["candidate"]


@pytest.mark.parametrize("is_meta", [False, None, "true"])
@pytest.mark.parametrize("timestamp", [None, "missing"])
def test_native_markers_and_missing_timestamps_do_not_invent_authorship(
    is_meta, timestamp
):
    entry = _recorded_user("CAPTURE_LITERAL_MARKUP")
    # Strip strong provenance from a recorded human prompt, then vary only the
    # marker and timestamp boundaries. A string 'true' is not recorded true.
    entry.pop("promptSource")
    entry.pop("origin")
    entry.pop("turnOrigin")
    entry["isMeta"] = is_meta
    if timestamp == "missing":
        entry.pop("timestamp")
    else:
        entry["timestamp"] = timestamp
    message = _single_message(entry)
    assert _sources(message) == ["unknown"]
    (span,) = message["authorship"]
    assert {"field": "isMeta", "value": is_meta} in span["markers"]
    if timestamp == "missing":
        assert "ts" not in span
    else:
        assert span["ts"] is None


@pytest.mark.parametrize("kind", ["peer", "queued"])
def test_original_body_without_wrapper_has_one_range(kind):
    if kind == "peer":
        entry = _recorded_user("CAPTURE_LITERAL_MARKUP")
        body = entry["message"]["content"]
        entry["origin"] = {"kind": "peer", "body": body}
        expected_source = "other_agent"
    else:
        entry = _queued_record()
        body = entry["attachment"]["prompt"]
        entry["rendered"][0]["content"] = body
        expected_source = "candidate"
    message = _single_message(entry)
    assert message["text"] == body
    assert _sources(message) == [expected_source]
    (span,) = message["authorship"]
    assert span["start"] == 0 and span["end"] == len(body)


def test_overlapping_original_body_matches_are_ambiguous():
    entry = _queued_record()
    entry["attachment"]["prompt"] = "aa"
    entry["rendered"][0]["content"] = "aaa"
    message = _single_message(entry)
    assert _sources(message) == ["unknown"]


@pytest.mark.parametrize("human_turn", [False, None])
def test_queued_human_origin_needs_recorded_human_turn_evidence(human_turn):
    entry = _queued_record()
    entry["attachment"]["humanTurn"] = human_turn
    message = _single_message(entry)
    assert _sources(message) == ["unknown"]


def test_queued_empty_activity_and_malformed_attachment_do_not_create_messages():
    empty = _queued_record()
    empty["attachment"]["prompt"] = ""
    empty["rendered"] = []
    malformed = deepcopy(empty)
    malformed["attachment"] = None
    source = ClaudeSource(TRANSCRIPT, [empty, malformed])
    session = claude.interpret_source(source)
    assert session is None


def _recorded_interruptions():
    interruptions = []
    for entry in claude.load_entries(TRANSCRIPT):
        content = entry.get("message", {}).get("content")
        if content == [
            {"type": "text", "text": "[Request interrupted by user for tool use]"}
        ]:
            interruptions.append(entry)
    assert interruptions, "Missing captured user-interruption notices"
    return interruptions


def test_native_user_interruptions_remain_candidate_visible_through_export():
    entries = _recorded_interruptions()
    original = deepcopy(entries)
    session = _interpret(entries)
    exported = export_session(session)
    for native, message, actual in zip(
        entries, session["events"], exported["events"], strict=True
    ):
        (block,) = native["message"]["content"]
        expected_text = block["text"]
        actual_sources = _sources(message)
        assert actual_sources == ["candidate"]
        assert message["text"] == actual["text"] == expected_text
        assert message["ts"] == actual["ts"] == native["timestamp"]
        assert message["id"] == native["uuid"]
        assert actual["authorship"] == message["authorship"]
        (span,) = message["authorship"]
        assert span["start"] == 0 and span["end"] == len(expected_text)
        assert {"field": "message.content[0].text", "value": expected_text} in span[
            "markers"
        ]
    assert entries == original


@pytest.mark.parametrize("shape", ["quoted", "extra_block", "string", "assistant_role"])
def test_interruption_recognition_does_not_label_other_text_as_user_action(shape):
    entries = _recorded_interruptions()
    entry = entries[0]
    # Mutate only the captured control-message shape. An incidental mention or
    # different container must not be mistaken for the recorded user action.
    if shape == "quoted":
        entry["message"]["content"][0]["text"] += " is an example notice."
    elif shape == "extra_block":
        entry["message"]["content"].append(
            {"type": "text", "text": "unattributed words"}
        )
    elif shape == "string":
        entry["message"]["content"] = entry["message"]["content"][0]["text"]
    else:
        entry["message"]["role"] = "assistant"
    message = _single_message(entry)
    actual_sources = _sources(message)
    assert actual_sources == ["unknown"]


def test_explicit_injection_evidence_overrides_control_notice_spelling():
    entries = _recorded_interruptions()
    entry = entries[0]
    entry["isMeta"] = True
    message = _single_message(entry)
    actual_sources = _sources(message)
    assert actual_sources == ["harness"]


def _recorded_shell_input():
    matches = []
    for entry in claude.load_entries(TRANSCRIPT):
        content = entry.get("message", {}).get("content")
        if isinstance(content, str) and content.startswith("<bash-input>"):
            matches.append(entry)
    assert len(matches) == 1, "Expected one native user-run shell command"
    (entry,) = matches
    return entry


def test_native_user_shell_command_remains_candidate_visible_through_export():
    entry = _recorded_shell_input()
    original = deepcopy(entry)
    session = _interpret([entry])
    (message,) = session["events"]
    exported = export_session(session)
    (actual,) = exported["events"]
    expected_text = entry["message"]["content"]
    expected_command = expected_text.removeprefix("<bash-input>").removesuffix(
        "</bash-input>"
    )
    actual_sources = _sources(message)
    assert actual_sources == ["candidate"]
    assert message["shell_command"] == expected_command
    assert message["text"] == actual["text"] == expected_text
    assert message["ts"] == actual["ts"] == entry["timestamp"]
    assert message["id"] == entry["uuid"]
    assert actual["authorship"] == message["authorship"]
    (span,) = message["authorship"]
    assert span["start"] == 0 and span["end"] == len(expected_text)
    assert {"field": "message.content", "value": expected_text} in span["markers"]
    assert entry == original


@pytest.mark.parametrize("shape", ["quoted", "extra_text", "list", "empty", "injected"])
def test_shell_input_recognition_requires_the_native_standalone_record(shape):
    entry = _recorded_shell_input()
    content = entry["message"]["content"]
    if shape == "quoted":
        entry["message"]["content"] = "Example: " + content
    elif shape == "extra_text":
        entry["message"]["content"] = content + "\nOther unattributed words."
    elif shape == "list":
        entry["message"]["content"] = [{"type": "text", "text": content}]
    elif shape == "empty":
        entry["message"]["content"] = "<bash-input>  </bash-input>"
    else:
        entry["isMeta"] = True
    message = _single_message(entry)
    actual_sources = _sources(message)
    expected_source = "harness" if shape == "injected" else "unknown"
    assert actual_sources == [expected_source]


def _recorded_user_shell_activity():
    shell_input = _recorded_shell_input()
    matches = []
    for entry in claude.load_entries(TRANSCRIPT):
        content = entry.get("message", {}).get("content")
        if entry.get("parentUuid") == shell_input["uuid"]:
            if isinstance(content, str) and content.startswith("<bash-stdout>"):
                matches.append(entry)
    assert len(matches) == 1, "Expected one ID-linked user-shell output"
    (shell_output,) = matches
    return shell_input, shell_output


@pytest.mark.parametrize("order", ["native", "intervening_human", "output_first"])
def test_shell_output_follows_recorded_candidate_input_id(order):
    shell_input, shell_output = _recorded_user_shell_activity()
    if order == "intervening_human":
        later = _recorded_user("CAPTURE_LITERAL_MARKUP")
        entries = [shell_input, later, shell_output]
    elif order == "output_first":
        entries = [shell_output, shell_input]
    else:
        entries = [shell_input, shell_output]
    original = deepcopy(entries)
    session = _interpret(entries)
    exported = export_session(session, 0)
    actual_ids = []
    expected_ids = []
    matched_output = []
    for entry in entries:
        expected_ids.append(entry["uuid"])
    for message, actual in zip(session["events"], exported["events"], strict=True):
        actual_ids.append(message["id"])
        if message["id"] == shell_output["uuid"]:
            matched_output.append(message)
            actual_sources = _sources(message)
            assert actual_sources == ["candidate"]
            assert (
                message["text"] == actual["text"] == shell_output["message"]["content"]
            )
            assert message["ts"] == actual["ts"] == shell_output["timestamp"]
            assert message["parent_id"] == shell_input["uuid"]
            assert actual["authorship"] == message["authorship"]
            (span,) = message["authorship"]
            assert {"field": "parentUuid", "value": shell_input["uuid"]} in span[
                "markers"
            ]
    assert len(matched_output) == 1
    assert actual_ids == expected_ids
    assert entries == original


@pytest.mark.parametrize(
    "parent",
    [
        "missing",
        "missing_id",
        "empty_id",
        "unrelated_human",
        "injected_input",
        "duplicate_id",
    ],
)
def test_shell_output_needs_a_unique_candidate_shell_parent(parent):
    shell_input, shell_output = _recorded_user_shell_activity()
    entries = [shell_input, shell_output]
    if parent == "missing":
        entries = [shell_output]
    elif parent == "missing_id":
        shell_output.pop("parentUuid")
    elif parent == "empty_id":
        shell_input["uuid"] = ""
        shell_output.pop("parentUuid")
    elif parent == "unrelated_human":
        human = _recorded_user("CAPTURE_LITERAL_MARKUP")
        shell_output["parentUuid"] = human["uuid"]
        entries = [shell_input, human, shell_output]
    elif parent == "injected_input":
        shell_input["isMeta"] = True
    else:
        entries = [shell_input, deepcopy(shell_input), shell_output]
    session = _interpret(entries)
    output = session["events"][-1]
    actual_sources = _sources(output)
    assert output["id"] == shell_output["uuid"]
    assert actual_sources == ["unknown"]


@pytest.mark.parametrize("shape", ["quoted", "list", "injected"])
def test_shell_output_link_does_not_override_recorded_content_or_provenance(shape):
    shell_input, shell_output = _recorded_user_shell_activity()
    content = shell_output["message"]["content"]
    if shape == "quoted":
        shell_output["message"]["content"] = "Example: " + content
    elif shape == "list":
        shell_output["message"]["content"] = [{"type": "text", "text": content}]
    else:
        shell_output["isMeta"] = True
    session = _interpret([shell_input, shell_output])
    output = session["events"][-1]
    actual_sources = _sources(output)
    expected_source = "harness" if shape == "injected" else "unknown"
    assert actual_sources == [expected_source]


def _recorded_custom_command():
    return _recorded_user("<command-name>/capture-echo</command-name>")


def test_native_human_command_invocation_and_arguments_are_candidate_activity():
    entry = _recorded_custom_command()
    original = deepcopy(entry)
    session = _interpret([entry])
    (message,) = session["events"]
    exported = export_session(session)
    (actual,) = exported["events"]
    expected_text = entry["message"]["content"]
    actual_sources = _sources(message)
    assert actual_sources == ["candidate"]
    assert message["command"] == "/capture-echo"
    assert message["command_args"] == 'CAPTURE_ARGS hello, "café 雪" — two words!'
    assert message["text"] == actual["text"] == expected_text
    assert message["ts"] == actual["ts"] == entry["timestamp"]
    assert message["id"] == entry["uuid"]
    assert actual["authorship"] == message["authorship"]
    assert entry == original


@pytest.mark.parametrize(
    "boundary", ["missing_origin", "missing_turn_origin", "quoted", "list", "injected"]
)
def test_command_invocation_needs_recorded_human_provenance_and_native_shape(boundary):
    entry = _recorded_custom_command()
    content = entry["message"]["content"]
    if boundary == "missing_origin":
        entry.pop("origin")
    elif boundary == "missing_turn_origin":
        entry.pop("turnOrigin")
    elif boundary == "quoted":
        entry["message"]["content"] = "Example: " + content
    elif boundary == "list":
        entry["message"]["content"] = [{"type": "text", "text": content}]
    else:
        entry["isMeta"] = True
    message = _single_message(entry)
    actual_sources = _sources(message)
    expected_source = "harness" if boundary == "injected" else "unknown"
    assert actual_sources == [expected_source]


def test_command_authorship_is_independent_of_fixture_command_name():
    entry = _recorded_custom_command()
    # Keep native provenance and command structure; rename only the disposable
    # fixture command so the rule cannot depend on its capture-specific name.
    entry["message"]["content"] = entry["message"]["content"].replace(
        "capture-echo", "other-command"
    )
    message = _single_message(entry)
    actual_sources = _sources(message)
    assert actual_sources == ["candidate"]
    assert message["command"] == "/other-command"


def _recorded_model_invocations():
    matches = []
    for entry in claude.load_entries(TRANSCRIPT):
        content = entry.get("message", {}).get("content")
        if entry.get("type") == "user" and isinstance(content, str):
            if "<command-name>/model</command-name>" in content:
                matches.append(entry)
    assert matches, "Missing native model-selector invocations"
    return matches


def test_native_model_selector_invocations_remain_candidate_visible():
    entries = _recorded_model_invocations()
    original = deepcopy(entries)
    session = _interpret(entries)
    exported = export_session(session)
    for native, message, actual in zip(
        entries, session["events"], exported["events"], strict=True
    ):
        actual_sources = _sources(message)
        expected_text = native["message"]["content"]
        assert actual_sources == ["candidate"]
        assert message["command"] == "/model"
        assert message["command_args"] is None
        assert message["text"] == actual["text"] == expected_text
        assert message["ts"] == actual["ts"] == native["timestamp"]
        assert message["id"] == native["uuid"]
        assert actual["authorship"] == message["authorship"]
    assert entries == original


@pytest.mark.parametrize(
    "shape", ["quoted", "list", "wrong_command", "extra_text", "injected"]
)
def test_model_selector_recognition_requires_its_native_control_record(shape):
    entries = _recorded_model_invocations()
    entry = entries[0]
    content = entry["message"]["content"]
    if shape == "quoted":
        entry["message"]["content"] = "Example: " + content
    elif shape == "list":
        entry["message"]["content"] = [{"type": "text", "text": content}]
    elif shape == "wrong_command":
        entry["message"]["content"] = content.replace("model", "unsupported-control")
    elif shape == "extra_text":
        entry["message"]["content"] += "\nAdditional unattributed text."
    else:
        entry["isMeta"] = True
    message = _single_message(entry)
    actual_sources = _sources(message)
    expected_source = "harness" if shape == "injected" else "unknown"
    assert actual_sources == [expected_source]


def _recorded_model_selections():
    selections = []
    entries = claude.load_entries(TRANSCRIPT)
    for invocation in _recorded_model_invocations():
        results = []
        for entry in entries:
            content = entry.get("message", {}).get("content")
            if entry.get("parentUuid") == invocation["uuid"]:
                if isinstance(content, str) and content.startswith(
                    "<local-command-stdout>"
                ):
                    results.append(entry)
        assert len(results) == 1, "Expected one ID-linked model-selection result"
        (result,) = results
        selections.append((invocation, result))
    return selections


@pytest.mark.parametrize("order", ["native", "intervening_human", "result_first"])
def test_model_selection_results_remain_candidate_activity_by_recorded_id(order):
    for invocation, result in _recorded_model_selections():
        if order == "intervening_human":
            human = _recorded_user("CAPTURE_LITERAL_MARKUP")
            entries = [invocation, human, result]
        elif order == "result_first":
            entries = [result, invocation]
        else:
            entries = [invocation, result]
        original = deepcopy(entries)
        session = _interpret(entries)
        exported = export_session(session, 0)
        matched_results = []
        for message, actual in zip(session["events"], exported["events"], strict=True):
            if message["id"] == result["uuid"]:
                matched_results.append(message)
                actual_sources = _sources(message)
                assert actual_sources == ["candidate"]
                assert message["text"] == actual["text"] == result["message"]["content"]
                assert message["ts"] == actual["ts"] == result["timestamp"]
                assert message["parent_id"] == invocation["uuid"]
                assert actual["authorship"] == message["authorship"]
        assert len(matched_results) == 1
        assert entries == original


@pytest.mark.parametrize(
    "parent",
    ["missing", "unrelated_human", "shell_input", "injected_input", "duplicate_id"],
)
def test_model_result_needs_a_unique_candidate_model_invocation(parent):
    selections = _recorded_model_selections()
    invocation, result = selections[0]
    entries = [invocation, result]
    if parent == "missing":
        entries = [result]
    elif parent == "unrelated_human":
        human = _recorded_user("CAPTURE_LITERAL_MARKUP")
        result["parentUuid"] = human["uuid"]
        entries = [invocation, human, result]
    elif parent == "shell_input":
        shell_input = _recorded_shell_input()
        result["parentUuid"] = shell_input["uuid"]
        entries = [invocation, shell_input, result]
    elif parent == "injected_input":
        invocation["isMeta"] = True
    else:
        entries = [invocation, deepcopy(invocation), result]
    session = _interpret(entries)
    output = session["events"][-1]
    actual_sources = _sources(output)
    assert output["id"] == result["uuid"]
    assert actual_sources == ["unknown"]


def test_model_result_classification_does_not_depend_on_notice_wording_or_model_name():
    selections = _recorded_model_selections()
    invocation, result = selections[0]
    # Keep the native output wrapper and recorded association while varying the
    # informational wording, so a renamed model does not change attribution.
    result["message"]["content"] = (
        "<local-command-stdout>Another model is now selected.</local-command-stdout>"
    )
    session = _interpret([invocation, result])
    output = session["events"][-1]
    actual_sources = _sources(output)
    assert actual_sources == ["candidate"]
    assert output["text"] == result["message"]["content"]


@pytest.mark.parametrize("shape", ["quoted", "list", "injected"])
def test_model_result_link_respects_native_shape_and_injection_provenance(shape):
    selections = _recorded_model_selections()
    invocation, result = selections[0]
    content = result["message"]["content"]
    if shape == "quoted":
        result["message"]["content"] = "Example: " + content
    elif shape == "list":
        result["message"]["content"] = [{"type": "text", "text": content}]
    else:
        result["isMeta"] = True
    session = _interpret([invocation, result])
    output = session["events"][-1]
    actual_sources = _sources(output)
    expected_source = "harness" if shape == "injected" else "unknown"
    assert actual_sources == [expected_source]


def _recorded_compact_invocations():
    matches = []
    forms = set()
    for entry in claude.load_entries(TRANSCRIPT):
        content = entry.get("message", {}).get("content")
        if entry.get("type") != "user" or not isinstance(content, str):
            continue
        if content == "/compact":
            forms.add("plain")
            matches.append(entry)
        elif content.startswith("<command-name>/compact</command-name>"):
            forms.add("wrapped")
            matches.append(entry)
    assert forms == {"plain", "wrapped"}, "Missing native compaction-invocation forms"
    return matches


def test_both_native_compaction_invocation_forms_remain_candidate_visible():
    entries = _recorded_compact_invocations()
    original = deepcopy(entries)
    session = _interpret(entries)
    exported = export_session(session)
    for native, message, actual in zip(
        entries, session["events"], exported["events"], strict=True
    ):
        actual_sources = _sources(message)
        expected_text = native["message"]["content"]
        assert actual_sources == ["candidate"]
        assert message["text"] == actual["text"] == expected_text
        assert message["ts"] == actual["ts"] == native["timestamp"]
        assert message["id"] == native["uuid"]
        assert actual["authorship"] == message["authorship"]
        (span,) = message["authorship"]
        assert {"field": "entrypoint", "value": "cli"} in span["markers"]
    assert entries == original


@pytest.mark.parametrize("shape", ["quoted", "list", "extra_text", "sdk", "injected"])
def test_compact_control_recognition_excludes_other_text_and_unmarked_sdk_prompts(
    shape,
):
    for entry in _recorded_compact_invocations():
        content = entry["message"]["content"]
        if shape == "quoted":
            entry["message"]["content"] = "Example: " + content
        elif shape == "list":
            entry["message"]["content"] = [{"type": "text", "text": content}]
        elif shape == "extra_text":
            entry["message"]["content"] += "\nAdditional unattributed text."
        elif shape == "sdk":
            entry["entrypoint"] = "sdk-cli"
        else:
            entry["isMeta"] = True
        message = _single_message(entry)
        actual_sources = _sources(message)
        expected_source = "harness" if shape == "injected" else "unknown"
        assert actual_sources == [expected_source]


def _recorded_compact_completion():
    matches = []
    entries = claude.load_entries(TRANSCRIPT)
    for invocation in _recorded_compact_invocations():
        content = invocation["message"]["content"]
        if not content.startswith("<command-name>"):
            continue
        for entry in entries:
            result = entry.get("message", {}).get("content")
            if entry.get("parentUuid") == invocation["uuid"]:
                if isinstance(result, str) and result.startswith(
                    "<local-command-stdout>"
                ):
                    matches.append((invocation, entry))
    assert len(matches) == 1, "Expected one ID-linked compaction-completion notice"
    (activity,) = matches
    return activity


@pytest.mark.parametrize("order", ["native", "intervening_human", "result_first"])
def test_compaction_completion_retains_candidate_activity_through_its_parent_id(order):
    invocation, completion = _recorded_compact_completion()
    if order == "intervening_human":
        human = _recorded_user("CAPTURE_LITERAL_MARKUP")
        entries = [invocation, human, completion]
    elif order == "result_first":
        entries = [completion, invocation]
    else:
        entries = [invocation, completion]
    original = deepcopy(entries)
    session = _interpret(entries)
    exported = export_session(session, 0)
    matched_completions = []
    for message, actual in zip(session["events"], exported["events"], strict=True):
        if message["id"] == completion["uuid"]:
            matched_completions.append(message)
            actual_sources = _sources(message)
            assert actual_sources == ["candidate"]
            assert message["text"] == actual["text"] == completion["message"]["content"]
            assert message["ts"] == actual["ts"] == completion["timestamp"]
            assert message["parent_id"] == invocation["uuid"]
            assert actual["authorship"] == message["authorship"]
    assert len(matched_completions) == 1
    assert entries == original


@pytest.mark.parametrize(
    "parent", ["missing", "sdk_input", "injected_input", "duplicate_id"]
)
def test_compaction_completion_needs_a_unique_candidate_cli_invocation(parent):
    invocation, completion = _recorded_compact_completion()
    entries = [invocation, completion]
    if parent == "missing":
        entries = [completion]
    elif parent == "sdk_input":
        invocation["entrypoint"] = "sdk-cli"
    elif parent == "injected_input":
        invocation["isMeta"] = True
    else:
        entries = [invocation, deepcopy(invocation), completion]
    session = _interpret(entries)
    output = session["events"][-1]
    actual_sources = _sources(output)
    assert output["id"] == completion["uuid"]
    assert actual_sources == ["unknown"]


def test_full_capture_resolves_reviewed_activity_and_keeps_generated_summary_harness():
    entries = claude.load_entries(TRANSCRIPT)
    summaries = {}
    for entry in entries:
        if entry.get("isCompactSummary") is True:
            summaries[entry["uuid"]] = entry["message"]["content"]
    assert summaries, "Missing native generated compaction summary"
    session = _interpret(entries)
    unresolved_activity = []
    retained_summaries = []
    for message in session["events"]:
        if message["role"] != "assistant" and message.get("text"):
            assert message.get("authorship"), "Missing activity provenance"
            if "unknown" in _sources(message):
                unresolved_activity.append(message["id"])
        if message["id"] in summaries:
            retained_summaries.append(message["id"])
            actual_sources = _sources(message)
            assert actual_sources == ["harness"]
            assert message["text"] == summaries[message["id"]]
    assert retained_summaries
    assert unresolved_activity == []
