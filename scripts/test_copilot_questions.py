"""Copilot question facts come from structured native records, never prose."""

import json
from copy import deepcopy

import pytest
from copilot_fixture import native_source, recorded_calls

from session_log_extractors import copilot_protocol
from session_log_extractors import extract_copilot_session_log as copilot
from session_log_extractors.session_log_envelope import export_session


def _question_source():
    """Select one captured choice, controlling its property key for boundaries."""
    source = native_source()
    owner = None
    for record in source.state_records:
        if record["type"] != copilot_protocol.ASSISTANT_MESSAGE:
            continue
        for request in record["data"].get("toolRequests", []):
            properties = (
                request.get("arguments", {})
                .get("requestedSchema", {})
                .get("properties", {})
            )
            if request["name"] == copilot_protocol.ASK_USER and len(properties) == 1:
                ((name, field),) = properties.items()
                if field.get("type") != "string" or not field.get("enum"):
                    continue
                owner = record
                call_id = request["toolCallId"]
                break
        else:
            continue
        break
    assert owner is not None, "Required captured single-choice question is missing"
    selected = []
    for record in source.state_records:
        if record is owner or record["data"].get("toolCallId") == call_id:
            selected.append(record)
    source.state_records = selected
    # Preserve the native values and IDs; a stable field key makes controlled
    # malformed-value tests independent of the model's choice of schema names.
    owner["data"]["toolRequests"] = [request]
    request["arguments"]["requestedSchema"]["properties"] = {"choice": field}
    response = _human_response(source)["data"]["response"]
    response["content"] = {"choice": response["content"][name]}
    response["requestedSchema"]["properties"] = {"choice": field}
    source.turns = []
    source.files = []
    return source


def _human_response(source):
    for record in source.state_records:
        if record["type"] == copilot_protocol.HUMAN_RESPONSE_RECORDED:
            return record
    pytest.fail("Required captured answer is missing")


@pytest.mark.parametrize(
    "payload",
    [None, "raw", {}, {"requestedSchema": {}}, {"requestedSchema": {"properties": []}}],
)
def test_malformed_question_containers_have_no_invented_questions(payload):
    source = _question_source()
    source.state_records[0]["data"]["toolRequests"][0]["arguments"] = payload
    session = copilot.interpret_source(source)
    assert session["interaction_questions"] == []
    (response,) = session["question_responses"]
    assert response["question"] is None
    call = session["events"][0]["tool_calls"][0]
    assert call["input"] == payload


@pytest.mark.parametrize("answer", [None, 17, ["Leaf", False]])
def test_invalid_answer_values_remain_recorded_unknowns(answer):
    source = _question_source()
    _human_response(source)["data"]["response"]["content"] = {"choice": answer}
    session = copilot.interpret_source(source)
    (response,) = session["question_responses"]
    assert response["outcome"] == "unknown"
    assert response["recorded_answer"] == answer
    assert "selections" not in response
    assert "free_text" not in response


@pytest.mark.parametrize("content", [None, False, {}, []])
def test_missing_or_invalid_answer_maps_remain_unknown(content):
    source = _question_source()
    _human_response(source)["data"]["response"]["content"] = content
    session = copilot.interpret_source(source)
    (response,) = session["question_responses"]
    assert response["outcome"] == "unknown"
    assert response["question"] is None


def test_schema_unknowns_and_duplicate_options_do_not_invent_option_ownership():
    source = _question_source()
    request = source.state_records[0]["data"]["toolRequests"][0]
    request["arguments"]["requestedSchema"]["properties"] = {
        "choice": {"type": "unknown", "enum": ["Leaf", "Leaf", None]},
        "malformed": False,
    }
    native = _human_response(source)["data"]["response"]
    native["content"] = {"choice": "Leaf"}
    native["feedback"] = "Candidate feedback, separate from the answer"
    session = copilot.interpret_source(source)
    (question,) = session["interaction_questions"]
    (response,) = session["question_responses"]
    assert "header" not in question
    assert "multiple_selections" not in question
    assert len(question["options"]) == 2
    assert response["selections"] == [{"label": "Leaf"}]
    assert response["candidate_feedback"] == native["feedback"]
    assert "refusal" not in response


def test_question_without_call_id_retains_native_containment_but_no_answer_link():
    source = _question_source()
    del source.state_records[0]["data"]["toolRequests"][0]["toolCallId"]
    retained = []
    for record in source.state_records:
        if record["type"] != copilot_protocol.TOOL_EXECUTION_START:
            retained.append(record)
    source.state_records = retained
    session = copilot.interpret_source(source)
    (question,) = session["interaction_questions"]
    (response,) = session["question_responses"]
    assert question["activity"]["message_i"] == 0
    assert question["activity"]["tool_call_index"] == 0
    assert response["question"] is None


def test_responses_before_requests_use_ids_instead_of_encounter_order():
    source = _question_source()
    response_record = _human_response(source)
    source.state_records.remove(response_record)
    source.state_records.insert(0, response_record)
    session = copilot.interpret_source(source)
    (question,) = session["interaction_questions"]
    (response,) = session["question_responses"]
    assert response["question"]["question_ref"] == question["ref"]


def test_whole_request_cancellation_does_not_guess_a_question_in_a_group():
    source = _question_source()
    request = source.state_records[0]["data"]["toolRequests"][0]
    fields = request["arguments"]["requestedSchema"]["properties"]
    fields["second"] = deepcopy(fields["choice"])
    retained = []
    for record in source.state_records:
        if record["type"] == copilot_protocol.HUMAN_RESPONSE_RECORDED:
            continue
        if record["type"] == copilot_protocol.TOOL_EXECUTION_COMPLETE:
            record["data"]["toolTelemetry"]["properties"]["elicitation_action"] = (
                copilot_protocol.ELICITATION_CANCEL
            )
            record["data"]["result"] = None
        retained.append(record)
    source.state_records = retained
    session = copilot.interpret_source(source)
    assert len(session["interaction_questions"]) == 2
    (response,) = session["question_responses"]
    assert response["outcome"] == "cancelled"
    assert response["question"] is None
    assert response["cancellation"] == {"by": "candidate"}


def test_malformed_native_record_data_is_ignored():
    source = _question_source()
    source.state_records.append({"type": "human_response.recorded", "data": False})
    session = copilot.interpret_source(source)
    assert len(session["question_responses"]) == 1


def _assert_native_questions(source):
    session = copilot.interpret_source(source)
    questions_by_call = _questions_by_call(session)
    answers_by_id = {}
    for response in session["question_responses"]:
        if response["outcome"] == "answered":
            answers_by_id.setdefault(response["native_id"], []).append(response)
    seen_answers = 0
    for record in source.state_records:
        if record["type"] != copilot_protocol.HUMAN_RESPONSE_RECORDED:
            continue
        data = record["data"]
        native = data["response"]
        if native["responseKind"] != copilot_protocol.ASK_USER:
            continue
        responses = answers_by_id[record["id"]]
        assert len(responses) == len(native["content"])
        for response in responses:
            question = None
            for candidate in questions_by_call[data["toolCallId"]].values():
                if candidate["ref"] == response["question"]["question_ref"]:
                    question = candidate
            assert question is not None
            _assert_answer_preservation(question, response, native["content"])
            seen_answers += 1
    assert seen_answers >= 6, "Required single/multiple/free-text evidence is missing"
    _assert_required_question_features(session)
    return session


def _assert_required_question_features(session):
    features = set()
    questions = {}
    owners = {}
    for question in session["interaction_questions"]:
        questions[question["ref"]] = question
        activity = question["activity"]
        owner = (activity["message_i"], activity["tool_call_index"])
        owners[owner] = owners.get(owner, 0) + 1
    assert max(owners.values()) >= 2, "Missing native multiple-question request"
    for response in session["question_responses"]:
        association = response["question"]
        assert association is not None
        question = questions[association["question_ref"]]
        selections = response.get("selections", [])
        labels = []
        for selection in selections:
            labels.append(selection["label"])
        assert len(labels) == len(set(labels))
        if len(selections) >= 2:
            features.add("multiple selections")
        if response.get("free_text"):
            features.add("selection plus note" if selections else "custom text")
        for first in question["options"]:
            for second in question["options"]:
                if first != second and first["label"] in second["label"]:
                    if labels == [second["label"]]:
                        features.add("overlapping labels")
    assert features == {
        "multiple selections",
        "selection plus note",
        "custom text",
        "overlapping labels",
    }


@pytest.mark.parametrize("limit", [0, 17, -1])
def test_native_structured_question_facts_survive_export(limit):
    session = _assert_native_questions(native_source())
    original = deepcopy(session)
    exported = export_session(session, limit)
    assert set(exported) == {
        "schema",
        "harness",
        "session_id",
        "events",
        "cwd",
        "started_at",
        "models",
        "truncation",
    }
    exported_questions = []
    exported_responses = []
    for message, exported_message in zip(
        session["events"], exported["events"], strict=True
    ):
        for call, exported_call in zip(
            message.get("tool_calls", []),
            exported_message.get("tool_calls", []),
            strict=True,
        ):
            if call["name"] != copilot_protocol.ASK_USER:
                continue
            assert (
                exported_call["interaction_questions"] == call["interaction_questions"]
            )
            assert exported_call["question_responses"] == call["question_responses"]
            exported_questions.extend(exported_call["interaction_questions"])
            exported_responses.extend(exported_call["question_responses"])
    assert exported_questions == session["interaction_questions"]
    assert exported_responses == session["question_responses"]
    cancellations = []
    for response in session["question_responses"]:
        if response["outcome"] == "cancelled":
            cancellations.append(response)
    assert len(cancellations) == 1, "Required question cancellation is missing"
    (cancellation,) = cancellations
    assert cancellation["cancellation"]["by"] == "candidate"
    assert cancellation["question"]["evidence"]
    assert "candidate_feedback" not in cancellation
    assert "refusal" not in cancellation
    exported_questions[0]["options"].clear()
    exported_responses[0]["selections"].clear()
    assert session == original


def test_export_uses_canonical_calls_without_session_question_indexes():
    session = _assert_native_questions(native_source())
    expected = export_session(session, 0)
    del session["interaction_questions"]
    del session["question_responses"]

    actual = export_session(session, 0)

    assert actual == expected


@pytest.mark.parametrize("missing", ["toolCallId", "field", "agent", "duplicate"])
def test_unmatched_answers_are_retained_without_guessing(missing):
    source = _question_source()
    record = _human_response(source)
    if missing == "toolCallId":
        del record["data"]["toolCallId"]
    elif missing == "field":
        record["data"]["response"]["content"] = {"unknown": "unmatched text"}
    elif missing == "agent":
        record["agentId"] = "different-agent"
    else:
        owner = deepcopy(source.state_records[0])
        owner["id"] = "repeated-owner"
        source.state_records.append(owner)
    session = copilot.interpret_source(source)
    (response,) = session["question_responses"]
    assert response["question"] is None
    assert response["outcome"] == "answered"
    assert response["free_text"]
    assert ("activity" in response) is (missing == "field")
    exported = export_session(session, 0)
    exported_responses = []
    for message in exported["events"]:
        for call in message.get("tool_calls", []):
            exported_responses.extend(call["question_responses"])
    assert exported_responses == [response]


def test_repeated_identical_questions_keep_distinct_occurrences():
    source = _question_source()
    repeated = deepcopy(source.state_records)
    for record in repeated:
        record["id"] += "-repeated"
        if record["type"] == copilot_protocol.ASSISTANT_MESSAGE:
            for request in record["data"]["toolRequests"]:
                request["toolCallId"] += "-repeated"
        elif "toolCallId" in record["data"]:
            record["data"]["toolCallId"] += "-repeated"
    source.state_records.extend(repeated)
    session = copilot.interpret_source(source)
    first, second = session["interaction_questions"]
    assert first["text"] == second["text"]
    assert first["native_id"] == second["native_id"]
    assert first["ref"] != second["ref"]
    first_answer, second_answer = session["question_responses"]
    assert first_answer["question"]["question_ref"] == first["ref"]
    assert second_answer["question"]["question_ref"] == second["ref"]


def test_tool_errors_and_display_text_never_create_question_refusals():
    source = _question_source()
    selected = []
    for record in source.state_records:
        if record["type"] == copilot_protocol.HUMAN_RESPONSE_RECORDED:
            continue
        if record["type"] == copilot_protocol.TOOL_EXECUTION_COMPLETE:
            record["data"]["success"] = False
            record["data"]["result"] = {"content": "User cancelled the request."}
            record["data"].pop("toolTelemetry")
        selected.append(record)
    source.state_records = selected
    session = copilot.interpret_source(source)
    assert session["interaction_questions"]
    assert session["question_responses"] == []
    for call in recorded_calls(session).values():
        assert "permission_denial" not in call


def test_recommended_looking_labels_are_never_actual_selections():
    source = _question_source()
    owner = source.state_records[0]
    request = owner["data"]["toolRequests"][0]
    field = request["arguments"]["requestedSchema"]["properties"]["choice"]
    field["enum"] = ["A (Recommended)", "B"]
    _human_response(source)["data"]["response"]["content"] = {"choice": "B"}
    session = copilot.interpret_source(source)
    (question,) = session["interaction_questions"]
    (response,) = session["question_responses"]
    assert question["options"][0]["label"] == "A (Recommended)"
    assert response["selections"] == [
        {"label": "B", "option_ref": question["options"][1]["ref"]}
    ]


def test_controlled_overlapping_labels_select_only_the_exact_answer():
    source = _question_source()
    request = source.state_records[0]["data"]["toolRequests"][0]
    field = request["arguments"]["requestedSchema"]["properties"]["choice"]
    field["enum"] = ["Leaf", "Leaflet"]
    _human_response(source)["data"]["response"]["content"] = {"choice": "Leaflet"}
    session = copilot.interpret_source(source)
    (question,) = session["interaction_questions"]
    (response,) = session["question_responses"]
    assert response["selections"] == [
        {"label": "Leaflet", "option_ref": question["options"][1]["ref"]}
    ]
    assert response["recorded_answer"] == "Leaflet"


def test_multiple_custom_values_retain_their_recorded_boundaries():
    source = _question_source()
    request = source.state_records[0]["data"]["toolRequests"][0]
    request["arguments"]["requestedSchema"]["properties"]["choice"] = {
        "type": "array",
        "items": {"type": "string", "enum": ["A"]},
    }
    answer = ["A", "comma, note", "line\nnote"]
    _human_response(source)["data"]["response"]["content"] = {"choice": answer}
    session = copilot.interpret_source(source)
    (question,) = session["interaction_questions"]
    (response,) = session["question_responses"]
    assert response["selections"] == [
        {"label": "A", "option_ref": question["options"][0]["ref"]}
    ]
    assert response["free_text"] == "comma, note\nline\nnote"
    assert response["recorded_answer"] == answer
    exported = export_session(session, 0)
    result = exported["events"][0]["tool_calls"][0]
    (exported_response,) = result["question_responses"]
    exported_response["recorded_answer"].clear()
    assert response["recorded_answer"] == answer


def test_response_only_source_exports_a_result_without_inventing_a_request():
    source = _question_source()
    source.state_records = [_human_response(source)]
    session = copilot.interpret_source(source)
    assert session is not None
    assert len(session["events"]) == 1
    assert session["interaction_questions"] == []
    (response,) = session["question_responses"]
    assert response["question"] is None
    exported = export_session(session, 0)
    (event,) = exported["events"]
    (call,) = event["tool_calls"]
    assert call["name"] == "ask_user"
    assert "input" not in call
    assert "id" not in session["events"][0]["tool_calls"][0]
    assert call["question_responses"] == [response]
    assert call["result"] == ""
    assert "question_responses" not in exported
    uncapped = export_session(session, -1)
    uncapped_result = uncapped["events"][0]["tool_calls"][0]
    assert json.loads(uncapped_result["result"]) == source.state_records[0]["data"]


def test_unassociated_results_keep_native_order_and_valid_activity_addresses():
    source = _question_source()
    owner = source.state_records[0]
    unassociated = deepcopy(_human_response(source))
    unassociated["id"] = "controlled-unassociated-before-owner"
    del unassociated["data"]["toolCallId"]
    source.state_records.insert(0, unassociated)

    session = copilot.interpret_source(source)

    actual_ids = []
    for event in session["events"]:
        actual_ids.append(event["id"])
    assert actual_ids == [unassociated["id"], owner["id"]]
    (question,) = session["interaction_questions"]
    unmatched, matched = session["question_responses"]
    assert unmatched["question"] is None
    assert "activity" not in unmatched
    assert question["activity"]["message_i"] == 1
    assert matched["activity"]["message_i"] == 1
    exported = export_session(session, 0)
    unmatched_result = exported["events"][0]["tool_calls"][0]
    matched_result = exported["events"][1]["tool_calls"][0]
    assert unmatched_result["question_responses"] == [unmatched]
    assert matched_result["question_responses"] == [matched]
    assert matched_result["interaction_questions"] == [question]


def test_unassociated_fields_share_their_recorded_response_only_result():
    source = _question_source()
    response_record = _human_response(source)
    response_record["data"]["response"]["content"] = {
        "first": "plain answer",
        "second": ["comma, value", "next value"],
    }
    source.state_records = [response_record]

    session = copilot.interpret_source(source)
    exported = export_session(session, 0)

    (event,) = exported["events"]
    (call,) = event["tool_calls"]
    first, second = call["question_responses"]
    assert first["question"] is None and second["question"] is None
    assert first["recorded_answer"] == "plain answer"
    assert second["recorded_answer"] == ["comma, value", "next value"]
    assert first["native_id"] == second["native_id"] == response_record["id"]
    assert first["ref"] != second["ref"]
    assert "input" not in call


def test_cancellation_without_its_request_remains_a_response_only_result():
    source = _question_source()
    completions = []
    for record in source.state_records:
        if record["type"] == copilot_protocol.TOOL_EXECUTION_COMPLETE:
            record["data"]["toolTelemetry"]["properties"]["elicitation_action"] = (
                copilot_protocol.ELICITATION_CANCEL
            )
            completions.append(record)
    source.state_records = completions

    session = copilot.interpret_source(source)
    exported = export_session(session, 0)

    (event,) = exported["events"]
    (call,) = event["tool_calls"]
    (response,) = call["question_responses"]
    assert response["question"] is None
    assert "activity" not in response
    assert response["outcome"] == "cancelled"
    assert response["cancellation"]["by"] == "candidate"
    assert call["interaction_questions"] == []


def _assert_answer_preservation(question, response, answers):
    expected = answers[question["native_id"]]
    assert response["recorded_answer"] == expected
    values = [expected] if isinstance(expected, str) else expected
    offered = {}
    for option in question["options"]:
        offered[option["label"]] = option["ref"]
    represented = []
    for selection in response["selections"]:
        label = selection["label"]
        assert label in values
        assert selection["option_ref"] == offered[label]
        represented.append(label)
    if "free_text" in response:
        # The capture's custom answer is a single exact value, including in
        # selection-plus-note arrays. No splitting of commas or quotes is needed.
        text = response["free_text"]
        assert isinstance(text, str) and text
        assert text in values
        assert text not in offered
        represented.append(text)
    assert len(represented) == len(values)
    assert set(represented) == set(values)
    assert "candidate_feedback" not in response


def test_question_contract_tolerates_reworded_capture():
    source = native_source()
    replacements = _question_label_replacements(source)
    for record in source.state_records:
        data = record["data"]
        if record["type"] == copilot_protocol.ASSISTANT_MESSAGE:
            data["content"] = "Reworded assistant response."
            for request in data.get("toolRequests", []):
                if request["name"] == copilot_protocol.ASK_USER:
                    _reword_question_payload(request["arguments"], replacements)
        elif record["type"] == copilot_protocol.HUMAN_RESPONSE_RECORDED:
            response = data["response"]
            if response["responseKind"] == copilot_protocol.ASK_USER:
                _reword_question_payload(response, replacements)
                _reword_recorded_answers(response, replacements)
    _assert_native_questions(source)


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
        if record["type"] == copilot_protocol.HUMAN_RESPONSE_RECORDED:
            response = record["data"]["response"]
            if response["responseKind"] != copilot_protocol.ASK_USER:
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


def _questions_by_call(session):
    questions_by_call = {}
    for question in session["interaction_questions"]:
        activity = question["activity"]
        message = session["events"][activity["message_i"]]
        call = message["tool_calls"][activity["tool_call_index"]]
        questions_by_call.setdefault(call["id"], {})[question["native_id"]] = question
        payload = call["input"]
        field = payload["requestedSchema"]["properties"][question["native_id"]]
        assert question["text"] == payload["message"]
        assert question["header"] == field["title"]
        assert question["multiple_selections"] is (field["type"] == "array")
        assert question.get("description") == field.get("description")
        choices = field.get("items", field)
        offered = []
        for option in question["options"]:
            offered.append(option["label"])
        assert offered == choices.get("enum", [])
        assert activity["evidence"]
    return questions_by_call
