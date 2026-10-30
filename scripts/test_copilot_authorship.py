"""Authorship contracts grounded in the selected interactive Copilot capture."""

from copy import deepcopy

import pytest
from copilot_fixture import native_source

from session_log_extractors import copilot_authorship as authorship
from session_log_extractors import extract_copilot_session_log as copilot
from session_log_extractors.session_log_envelope import export_session


def _first_user_record():
    source = native_source()
    for record in source.state_records:
        if record["type"] == "user.message":
            return record
    pytest.fail("Missing native human input")


def _text_from_source(message, source):
    text = message["text"]
    segments = []
    for span in message.get("authorship", []):
        if span["source"] == source:
            segments.append(text[span["start"] : span["end"]])
    return segments


def test_native_human_child_and_steering_inputs_keep_recorded_provenance():
    source = native_source()
    original = deepcopy(source)
    session = copilot.interpret_source(source)
    messages = {}
    for message in session["events"]:
        messages[message["id"]] = message
    sources_seen = set()
    steering_seen = False
    for record in source.state_records:
        if record["type"] != "user.message":
            continue
        data = record["data"]
        message = messages[record["id"]]
        expected_source = "other_agent" if record.get("agentId") else "candidate"
        sources_seen.add(expected_source)
        assert message["submitted"] == {
            "text": data["content"],
            "authorship": expected_source,
        }
        assert message["ts"] == record["timestamp"]
        assert message.get("agent_id") == record.get("agentId")
        expected_text = data.get("transformedContent", data["content"])
        assert message["text"] == expected_text
        if data["content"] in expected_text:
            actual_segments = _text_from_source(message, expected_source)
            assert actual_segments == [data["content"]]
        if record.get("agentId"):
            marker = {"field": "data.source", "value": data["source"]}
            assert marker in message["source_markers"]
            assert _text_from_source(message, "candidate") == []
        if data.get("delivery") == "steering":
            steering_seen = True
            assert "transformedContent" not in data
            actual_segments = _text_from_source(message, "candidate")
            assert actual_segments == [data["content"]]
    assert sources_seen == {"candidate", "other_agent"}
    assert steering_seen, "Missing native original-only steering evidence"
    assert source == original


def test_native_slash_expansion_retains_candidate_invocation_and_arguments():
    source = native_source()
    expanded = []
    for record in source.state_records:
        if record["type"] != "user.message":
            continue
        data = record["data"]
        transformed = data.get("transformedContent")
        if isinstance(transformed, str) and data["content"] not in transformed:
            expanded.append(record)
    assert len(expanded) == 1, "Missing replaced original input evidence"
    (record,) = expanded
    data = record["data"]
    session = copilot.interpret_source(source)
    message = None
    for event in session["events"]:
        if event.get("id") == record["id"]:
            message = event
    assert message is not None
    assert message["submitted"] == {"text": data["content"], "authorship": "candidate"}
    assert message["text"] == data["transformedContent"]
    assert message["command"] + " " + message["command_args"] == data["content"]
    assert _text_from_source(message, "unknown") == []
    candidate_text = "".join(_text_from_source(message, "candidate"))
    assert message["command"] in candidate_text
    assert message["command_args"] in candidate_text
    harness_text = "".join(_text_from_source(message, "harness"))
    for record in source.state_records:
        if (
            record["type"] == "skill.invoked"
            and record["data"].get("trigger") == "user-invoked"
        ):
            assert record["data"]["content"] in harness_text


def test_native_runtime_context_is_retained_in_source_order_and_exported():
    source = native_source()
    session = copilot.interpret_source(source)
    original = deepcopy(session)
    exported = export_session(session, 17)
    positions = {}
    runtime_records = {}
    for index, record in enumerate(source.state_records):
        positions[record["id"]] = index
        if record["type"] in (
            "system.message",
            "system.notification",
            "instruction.update",
        ):
            runtime_records[record["id"]] = record
    assert runtime_records, "Missing runtime activity evidence"
    observed_positions = []
    observed_runtime = set()
    for message, event in zip(session["events"], exported["events"], strict=True):
        observed_positions.append(positions[message["id"]])
        for field in (
            "text",
            "authorship",
            "source_markers",
            "submitted",
            "agent_id",
            "images",
        ):
            assert event.get(field) == message.get(field)
        if message["id"] not in runtime_records:
            continue
        observed_runtime.add(message["id"])
        record = runtime_records[message["id"]]
        assert event["role"] == "system"
        assert event["text"] == record["data"].get("content", "")
        assert event["ts"] == record["timestamp"]
        assert _text_from_source(event, "candidate") == []
        assert {"field": "type", "value": record["type"]} in event["source_markers"]
        if record["type"] == "system.notification":
            marker = {"field": "data.kind", "value": record["data"]["kind"]}
            assert marker in event["source_markers"]
        if record["type"] == "instruction.update":
            marker = {"field": "data.authority", "value": record["data"]["authority"]}
            assert marker in event["source_markers"]
    assert observed_runtime == set(runtime_records)
    assert observed_positions == sorted(observed_positions)
    assert session == original


@pytest.mark.parametrize(
    ("original", "transformed", "expected"),
    [
        (" Hi 🙂 ", " Hi 🙂 ", ["candidate"]),
        ("Hi 🙂", "prefix Hi 🙂 suffix", ["harness", "candidate", "harness"]),
        ("Hi 🙂", "Hi 🙂 suffix", ["candidate", "harness"]),
        ("", "entirely injected", ["harness"]),
        (None, "unknown input", ["unknown"]),
        ("original", "replacement", ["unknown"]),
        ("aa", "aaa", ["unknown"]),
        ("same", "same same", ["unknown"]),
        ("<system-reminder>literal input</system-reminder>", None, ["candidate"]),
        ("", "", []),
        ("original", 42, ["candidate"]),
    ],
)
def test_transformation_boundaries_use_unchanged_unicode_text(
    original, transformed, expected
):
    record = _first_user_record()
    # Controlled text mutations cover boundaries absent from the native capture.
    record["data"]["content"] = original
    record["data"]["transformedContent"] = transformed
    record["data"]["parentAgentTaskId"] = "not-authorship-evidence"
    text, spans = authorship.user_content(record)
    expected_text = transformed if isinstance(transformed, str) else original or ""
    actual_sources = []
    for span in spans:
        actual_sources.append(span["source"])
        assert 0 <= span["start"] < span["end"] <= len(text)
    assert text == expected_text
    assert actual_sources == expected


@pytest.mark.parametrize(
    ("source", "continuation", "expected"),
    [
        ("user", False, "candidate"),
        ("system", False, "harness"),
        ("agent-fixture", False, "other_agent"),
        ("agent-", False, "unknown"),
        ("unrecognized", False, "unknown"),
        ({"unknown": True}, False, "unknown"),
        (None, False, "unknown"),
        ("user", True, "unknown"),
        ("system", True, "harness"),
    ],
)
def test_explicit_source_and_conflicts_override_role(source, continuation, expected):
    record = _first_user_record()
    record["data"]["source"] = source
    record["data"]["isAutopilotContinuation"] = continuation
    original = record["data"]["content"]
    text, spans = authorship.user_content(record)
    matching = []
    for span in spans:
        if text[span["start"] : span["end"]] == original:
            matching.append(span)
    (span,) = matching
    assert span["source"] == expected
    assert {"field": "data.source", "value": source} in span["markers"]
    submitted = authorship.submitted_text(record)
    assert submitted == {"text": original, "authorship": expected}


@pytest.mark.parametrize(
    ("scope", "continuation", "expected"),
    [
        (False, False, "candidate"),
        (True, False, "unknown"),
        (False, True, "harness"),
        (True, True, "harness"),
    ],
)
def test_omitted_source_respects_agent_scope_and_autopilot(
    scope, continuation, expected
):
    record = _first_user_record()
    record["data"].pop("source", None)
    if scope:
        record["agentId"] = "recipient"
    if continuation:
        record["data"]["isAutopilotContinuation"] = True
    submitted = authorship.submitted_text(record)
    assert submitted["authorship"] == expected


def _controlled_selection_record():
    """Selection schema boundary only; the interactive fixture has image attachments."""
    record = _first_user_record()
    record["data"]["content"] = "prompt"
    record["data"]["transformedContent"] = "prefix selected suffix prompt"
    record["data"]["attachments"] = [
        {
            "type": "selection",
            "filePath": "/workspace/file.py",
            "displayName": "file.py",
            "text": "selected",
            "selection": {
                "start": {"line": 0, "character": 0},
                "end": {"line": 0, "character": 8},
            },
        }
    ]
    return record


def test_controlled_selection_schema_preserves_editor_coordinates_and_ranges():
    record = _controlled_selection_record()
    text, spans = authorship.user_content(record)
    message = {"text": text, "authorship": spans}
    actual_selected = _text_from_source(message, "ide")
    assert actual_selected == ["selected"]
    (attachment,) = authorship.context_attachments(record["data"])
    native = record["data"]["attachments"][0]
    assert attachment["selection"] == native["selection"]
    assert attachment["file_path"] == native["filePath"]
    assert attachment["text"] == native["text"]


@pytest.mark.parametrize("selected", [None, "", "missing", "prefix", "prompt"])
def test_ambiguous_or_original_selection_does_not_invent_ide_ranges(selected):
    record = _controlled_selection_record()
    record["data"]["transformedContent"] = "prefix prefix prompt"
    record["data"]["attachments"][0]["text"] = selected
    text, spans = authorship.user_content(record)
    actual_selected = _text_from_source({"text": text, "authorship": spans}, "ide")
    assert actual_selected == []


def test_system_original_is_not_reclassified_as_editor_context():
    record = _controlled_selection_record()
    record["data"]["source"] = "system"
    record["data"]["attachments"][0]["text"] = "prompt"
    text, spans = authorship.user_content(record)
    actual_selected = _text_from_source({"text": text, "authorship": spans}, "ide")
    assert actual_selected == []


def test_unknown_context_fields_and_malformed_coordinates_are_preserved():
    record = _controlled_selection_record()
    native = record["data"]["attachments"][0]
    native.update(
        {
            "future": [True],
            "mentionIndex": 0,
            "byteLength": 3,
            "taggedFilesEntry": {"recorded": True},
            "assetId": "asset",
            "mimeType": "text/plain",
            "mediaType": "text/plain",
            "path": "/workspace/file.py",
            "data": "YWJj",
            "dataUrl": "data:text/plain;base64,YWJj",
        }
    )
    native["selection"]["start"]["future"] = "keep"
    native["selection"]["end"] = {"character": False, "line": "malformed"}
    native["selection"]["future"] = [False]
    (context,) = authorship.context_attachments(record["data"])
    assert context["unrecognized_fields"] == {"future": [True]}
    assert context["selection"]["unrecognized_fields"] == {"future": [False]}
    assert context["selection"]["start"]["unrecognized_fields"] == {"future": "keep"}
    assert context["selection"]["end"]["unrecognized_fields"] == {"line": "malformed"}
    assert context["data"] == native["data"]
    assert context["data_url"] == native["dataUrl"]
    assert context["media_type"] == context["mime_type"] == "text/plain"
    assert context["byte_length"] == 3
    assert context["asset_id"] == "asset"
    assert context["path"] == native["path"]
    assert context["mention_index"] == 0
    assert context["tagged_files_entry"] == native["taggedFilesEntry"]


def test_context_container_boundaries_keep_non_image_context():
    assert authorship.context_attachments({"attachments": None}) == []
    contexts = authorship.context_attachments(
        {
            "attachments": [
                False,
                {"type": "directory", "path": "/workspace"},
                {"type": "file", "mimeType": "image/png", "path": "/workspace/red.png"},
                {
                    "type": "file",
                    "mimeType": "text/plain",
                    "path": "/workspace/notes.png",
                },
                {
                    "type": "selection",
                    "selection": {"start": {"line": "invalid"}, "end": False},
                },
            ]
        }
    )
    assert len(contexts) == 3
    assert contexts[0] == {"kind": "directory", "path": "/workspace"}
    assert contexts[1]["mime_type"] == "text/plain"
    assert contexts[2]["selection"] == {
        "start": {"unrecognized_fields": {"line": "invalid"}},
        "unrecognized_fields": {"end": False},
    }


def test_missing_record_data_has_no_invented_text_or_submission():
    record = {"type": "user.message", "data": None}
    assert authorship.user_content(record) == ("", [])
    assert authorship.submitted_text(record) is None
    assert authorship.source_markers(record) == [
        {"field": "type", "value": "user.message"}
    ]
    assert authorship.submitted_text({"data": {"content": None}}) is None


def test_database_fallback_keeps_literal_text_without_inferred_authorship():
    source = native_source()
    # Retain one captured DB turn while simulating absence of native state records.
    source.state_records = []
    source.turns = [source.turns[0]]
    source.files = []
    original = "  🙂\n<system-reminder>literal DB input</system-reminder>\n\n\n "
    source.turns[0]["user_message"] = original

    session = copilot.interpret_source(source)
    message = session["events"][0]

    assert message["text"] == original
    assert "authorship" not in message
    assert "submitted" not in message


def test_native_assistant_prose_has_recorded_model_provenance():
    source = native_source()
    session = copilot.interpret_source(source)
    messages = {}
    for message in session["events"]:
        messages[message["id"]] = message
    checked = 0
    for record in source.state_records:
        if record["type"] != "assistant.message" or not record["data"].get("content"):
            continue
        data = record["data"]
        assert data["model"] and data["apiCallId"]
        message = messages[record["id"]]
        actual = _text_from_source(message, "other_agent")
        assert actual == [data["content"]]
        assert message["role"] == "assistant"
        assert {"field": "data.model", "value": data["model"]} in message["authorship"][
            0
        ]["markers"]
        assert {"field": "data.apiCallId", "value": data["apiCallId"]} in message[
            "authorship"
        ][0]["markers"]
        checked += 1
    assert checked, "Missing native model-response evidence"


def test_selected_native_capture_has_no_unresolved_nonempty_message_text():
    session = copilot.interpret_source(native_source())
    exported = export_session(session)
    for event in exported["events"]:
        text = event.get("text") or ""
        if not text:
            continue
        cursor = 0
        for span in event.get("authorship", []):
            assert span["source"] != "unknown", event
            assert span["start"] == cursor, event
            cursor = span["end"]
        assert cursor == len(text), event


def test_native_user_shell_output_is_a_candidate_action_outcome():
    source = native_source()
    requests = []
    completions = {}
    for record in source.state_records:
        if record["type"] == "tool.user_requested":
            requests.append(record)
        if record["type"] == "tool.execution_complete":
            completions[record["data"]["toolCallId"]] = record
    assert len(requests) == 1
    (request,) = requests
    result = completions[request["data"]["toolCallId"]]
    assert result["data"]["isUserRequested"] is True
    assert result["parentId"] == request["id"]
    session = copilot.interpret_source(source)
    shells = []
    for message in session["events"]:
        if message.get("id") == request["id"]:
            shells.append(message)
    (message,) = shells
    actual = "".join(_text_from_source(message, "candidate"))
    expected = (
        request["data"]["arguments"]["command"]
        + "\n"
        + result["data"]["result"]["content"]
    )
    assert actual == expected
    assert message["text"] == expected
    assert _text_from_source(message, "harness") == []


def _native_command_records():
    source = native_source()
    invocations = []
    for record in source.state_records:
        if (
            record["type"] == "skill.invoked"
            and record["data"].get("trigger") == "user-invoked"
        ):
            invocations.append(record)
    assert len(invocations) == 1, "Missing deliberate skill invocation"
    (invocation,) = invocations
    submissions = []
    for record in source.state_records:
        if record["type"] == "user.message":
            command = record["data"]["content"].partition(" ")[0]
            if command == "/" + invocation["data"]["name"]:
                submissions.append(record)
    assert len(submissions) == 1
    (submission,) = submissions
    return source, invocation, submission


@pytest.mark.parametrize(
    "breakage",
    [
        "missing parent",
        "cycle",
        "duplicate ID",
        "other agent",
        "different activity",
    ],
)
def test_command_attribution_requires_an_unambiguous_same_agent_parent_chain(breakage):
    source, invocation, submission = _native_command_records()
    hook = None
    for record in source.state_records:
        if record["id"] == submission["parentId"]:
            hook = record
    assert hook is not None
    if breakage == "missing parent":
        submission["parentId"] = "not recorded"
    elif breakage == "cycle":
        hook["parentId"] = hook["id"]
    elif breakage == "duplicate ID":
        source.state_records.append(deepcopy(invocation))
    elif breakage == "other agent":
        invocation["agentId"] = "different recipient"
    else:
        hook["type"] = "assistant.message"
    index = authorship.record_index(source.state_records)

    text, spans = authorship.user_content(submission, index)
    actual = _text_from_source({"text": text, "authorship": spans}, "unknown")

    assert actual == [submission["data"]["transformedContent"]]
    assert authorship.submitted_text(submission)["authorship"] == "candidate"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("trigger", "model-invoked"),
        ("name", "unrelated-skill"),
        ("name", None),
        ("content", None),
    ],
)
def test_command_evidence_requires_the_matching_user_invocation(field, value):
    source, invocation, submission = _native_command_records()
    invocation["data"][field] = value
    index = authorship.record_index(source.state_records)

    text, spans = authorship.user_content(submission, index)
    actual = _text_from_source({"text": text, "authorship": spans}, "unknown")

    assert actual == [submission["data"]["transformedContent"]]


def test_malformed_skill_record_does_not_establish_command_activity():
    source, invocation, submission = _native_command_records()
    invocation["data"] = None
    index = authorship.record_index(source.state_records)

    text, spans = authorship.user_content(submission, index)
    actual = _text_from_source({"text": text, "authorship": spans}, "unknown")

    assert actual == [submission["data"]["transformedContent"]]


@pytest.mark.parametrize(
    "breakage",
    [
        "changed body",
        "duplicate body",
        "duplicate arguments",
        "arguments in body",
        "changed notice",
    ],
)
def test_command_notice_and_arguments_need_unique_supported_rendered_boundaries(
    breakage,
):
    source, invocation, submission = _native_command_records()
    data = submission["data"]
    arguments = data["content"].partition(" ")[2]
    if breakage == "changed body":
        data["transformedContent"] = data["transformedContent"].replace(
            invocation["data"]["content"], "changed"
        )
    elif breakage == "duplicate body":
        data["transformedContent"] += invocation["data"]["content"]
    elif breakage == "duplicate arguments":
        data["transformedContent"] += arguments
    elif breakage == "arguments in body":
        invocation["data"]["content"] = arguments
    else:
        data["transformedContent"] = data["transformedContent"].replace(
            "explicitly invoked", "may have invoked"
        )
    index = authorship.record_index(source.state_records)

    text, spans = authorship.user_content(submission, index)
    actual = _text_from_source({"text": text, "authorship": spans}, "unknown")

    assert actual == [data["transformedContent"]]


def test_command_provenance_uses_parent_ids_instead_of_encounter_order():
    source, invocation, submission = _native_command_records()
    source.state_records.remove(invocation)
    source.state_records.append(invocation)
    index = authorship.record_index(source.state_records)

    text, spans = authorship.user_content(submission, index)
    candidate = "".join(
        _text_from_source({"text": text, "authorship": spans}, "candidate")
    )

    assert invocation["data"]["name"] in candidate
    assert submission["data"]["content"].partition(" ")[2] in candidate


def test_no_argument_command_still_keeps_the_deliberate_invocation_visible():
    source, invocation, submission = _native_command_records()
    original = submission["data"]["content"]
    command, _, arguments = original.partition(" ")
    submission["data"]["content"] = command
    submission["data"]["transformedContent"] = submission["data"][
        "transformedContent"
    ].replace(arguments, "")
    index = authorship.record_index(source.state_records)

    text, spans = authorship.user_content(submission, index)
    candidates = _text_from_source({"text": text, "authorship": spans}, "candidate")

    assert len(candidates) == 1
    assert command in candidates[0]
    assert "explicitly invoked" in candidates[0]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("model", None),
        ("model", "<synthetic>"),
        ("apiCallId", ""),
        ("source", "system"),
        ("role", "user"),
    ],
)
def test_assistant_role_without_consistent_model_evidence_remains_unknown(field, value):
    record = None
    for native in native_source().state_records:
        if native["type"] == "assistant.message" and native["data"].get("content"):
            record = native
            break
    assert record is not None
    record["data"][field] = value

    spans = authorship.assistant_authorship(record)

    assert len(spans) == 1
    assert spans[0]["source"] == "unknown"
    assert spans[0]["ts"] == record["timestamp"]


def _native_shell_records():
    source = native_source()
    requests = []
    for record in source.state_records:
        if record["type"] == "tool.user_requested":
            requests.append(record)
    assert len(requests) == 1
    (request,) = requests
    completions = []
    for record in source.state_records:
        if (
            record["type"] == "tool.execution_complete"
            and record["data"]["toolCallId"] == request["data"]["toolCallId"]
        ):
            completions.append(record)
    assert len(completions) == 1
    (completion,) = completions
    return source, request, completion


@pytest.mark.parametrize(
    "breakage",
    ["duplicate request", "late duplicate request", "other agent", "missing link"],
)
def test_unlinked_or_ambiguous_shell_results_do_not_inherit_candidate_activity(
    breakage,
):
    source, request, completion = _native_shell_records()
    if breakage in ("duplicate request", "late duplicate request"):
        duplicate = deepcopy(request)
        duplicate["id"] = "duplicate-shell-request"
        if breakage == "late duplicate request":
            source.state_records.append(duplicate)
        else:
            source.state_records.insert(source.state_records.index(request), duplicate)
    elif breakage == "other agent":
        completion["agentId"] = "foreign recipient"
    else:
        completion["data"]["toolCallId"] = "unrecorded call"

    session = copilot.interpret_source(source)
    retained = []
    for result in session["unmatched_tool_results"]:
        if result["id"] == completion["id"]:
            retained.append(result)
    shells_with_results = []
    for message in session["events"]:
        if message.get("shell_result"):
            shells_with_results.append(message)

    assert len(retained) == 1
    assert retained[0]["content"] == completion["data"]["result"]
    assert shells_with_results == []


def test_missing_completion_provenance_keeps_shell_output_unresolved():
    source, request, completion = _native_shell_records()
    completion["data"].pop("isUserRequested")
    completion["parentId"] = "unrecorded parent"

    session = copilot.interpret_source(source)
    shells = []
    for message in session["events"]:
        if message.get("id") == request["id"]:
            shells.append(message)
    (message,) = shells
    unknown = _text_from_source(message, "unknown")

    assert unknown == ["\n" + completion["data"]["result"]["content"]]
    assert _text_from_source(message, "candidate") == [
        request["data"]["arguments"]["command"]
    ]


def test_missing_request_record_id_does_not_create_a_null_provenance_marker():
    source, request, completion = _native_shell_records()
    request.pop("id")

    session = copilot.interpret_source(source)
    shells = []
    for message in session["events"]:
        if message.get("shell_result", {}).get("id") == completion["id"]:
            shells.append(message)
    (message,) = shells
    output_span = message["authorship"][-1]
    marker_fields = []
    for marker in output_span["markers"]:
        marker_fields.append(marker["field"])

    assert "request.id" not in marker_fields
    assert {"field": "data.isUserRequested", "value": True} in output_span["markers"]


@pytest.mark.parametrize("eligibility", [False, None])
def test_api_eligibility_flag_does_not_determine_authorship(eligibility):
    record = None
    for native in native_source().state_records:
        if native["type"] == "assistant.message" and native["data"].get("content"):
            record = native
            break
    assert record is not None
    if eligibility is None:
        record["data"].pop("rte", None)
    else:
        record["data"]["rte"] = eligibility

    spans = authorship.assistant_authorship(record)

    assert spans[0]["source"] == "other_agent"


def test_native_parent_link_can_establish_a_shell_outcome_without_the_optional_flag():
    source, request, completion = _native_shell_records()
    completion["data"].pop("isUserRequested")

    session = copilot.interpret_source(source)
    shells = []
    for message in session["events"]:
        if message.get("id") == request["id"]:
            shells.append(message)
    (message,) = shells
    candidate_text = "".join(_text_from_source(message, "candidate"))

    assert candidate_text == message["text"]
    assert {"field": "parentId", "value": request["id"]} in message["authorship"][-1][
        "markers"
    ]


def test_explicit_non_user_completion_does_not_inherit_candidate_activity():
    source, request, completion = _native_shell_records()
    completion["data"]["isUserRequested"] = False

    session = copilot.interpret_source(source)
    shells = []
    for message in session["events"]:
        if message.get("id") == request["id"]:
            shells.append(message)
    (message,) = shells

    assert _text_from_source(message, "unknown") == [
        "\n" + completion["data"]["result"]["content"]
    ]
