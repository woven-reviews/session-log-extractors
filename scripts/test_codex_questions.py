"""Codex question interpretation through complete native and controlled results."""

import json
from copy import deepcopy
from pathlib import Path

import pytest
from codex_capture import TRANSCRIPT, question_interaction, response_items

from session_log_extractors.codex_sources import CodexSource, load_entries
from session_log_extractors.extract_codex_session_log import interpret_source
from session_log_extractors.session_log_envelope import export_session


def _interpret(entries):
    session = interpret_source(CodexSource(Path("unused.jsonl"), entries))
    assert session is not None
    return session


def _controlled(output, *, questions=None, error=False):
    # Mutate only native question/result fields needed for a boundary case.
    user, call, result = question_interaction()
    if questions is not None:
        call["payload"]["arguments"] = json.dumps({"questions": questions})
    result["payload"]["output"] = output
    result["payload"]["is_error"] = error
    return [user, call, result]


def _question(identifier="q"):
    return {
        "id": identifier,
        "question": "Choose?",
        "options": [
            {"label": "A (Recommended)", "description": "First"},
            {"label": "A", "description": "Second"},
        ],
    }


def test_native_question_text_and_options_match_recorded_fields():
    entries = load_entries(TRANSCRIPT)
    session = _interpret(entries)
    questions = session["interaction_questions"]
    native_calls = []
    for entry in response_items(entries, "function_call"):
        if entry["payload"]["name"] == "request_user_input":
            native_calls.append(entry["payload"])
    assert len(native_calls) >= 3, "Capture lacks question workflows"
    expected_question_count = 0
    for native_call in native_calls:
        expected_question_count += len(
            json.loads(native_call["arguments"])["questions"]
        )
    assert len(questions) == expected_question_count
    for question in questions:
        message = session["events"][question["activity"]["message_i"]]
        call = message["tool_calls"][question["activity"]["tool_call_index"]]
        recorded = call["input"]
        matches = []
        for item in recorded["questions"]:
            if item["id"] == question["native_id"]:
                matches.append(item)
        (native,) = matches
        assert question["text"] == native["question"]
        assert question["header"] == native["header"]
        for choice, option in zip(question["options"], native["options"], strict=True):
            assert choice["label"] == option["label"]
            assert choice["description"] == option["description"]


def test_native_answers_and_cancellation_remain_distinct():
    session = _interpret(load_entries(TRANSCRIPT))
    responses = session["question_responses"]
    by_ref = {}
    for question in session["interaction_questions"]:
        by_ref[question["ref"]] = question
    answered = []
    cancelled = []
    notes = []
    custom = []
    for response in responses:
        if response["outcome"] == "cancelled":
            cancelled.append(response)
            assert response["cancellation"]["by"] == "candidate"
            assert response["cancellation"]["explanation"].startswith("aborted by user")
            assert "refusal" not in response and "candidate_feedback" not in response
            continue
        assert response["outcome"] == "answered"
        answered.append(response)
        question = by_ref[response["question"]["question_ref"]]
        message = session["events"][response["activity"]["message_i"]]
        call = message["tool_calls"][0]
        recorded = json.loads(call["result_record"]["content"])["answers"]
        values = recorded[question["native_id"]]["answers"]
        labels = []
        for selection in response["selections"]:
            labels.append(selection["label"])
            assert selection["label"] in values
        if "free_text" in response:
            notes.append(response)
            assert "user_note: " + response["free_text"] in values
        if not labels:
            custom.append(response)
            assert response["free_text"]
        else:
            assert len(labels) == 1
        assert "candidate_feedback" not in response
    assert len(answered) == 3 and len(cancelled) == 1
    assert len(notes) == 2 and len(custom) == 1


def test_interpretation_attaches_canonical_questions_and_responses_before_export():
    session = _interpret(load_entries(TRANSCRIPT))
    questions = []
    responses = []
    for message in session["events"]:
        for call in message.get("tool_calls", []):
            if call["name"] != "request_user_input":
                assert "interaction_questions" not in call
                assert "question_responses" not in call
                continue
            questions.extend(call["interaction_questions"])
            responses.extend(call["question_responses"])
    assert questions, "Capture lacks questions"
    assert responses, "Capture lacks responses"
    assert questions == session["interaction_questions"]
    assert responses == session["question_responses"]
    for question, indexed in zip(
        questions, session["interaction_questions"], strict=True
    ):
        assert question is indexed
    for response, indexed in zip(responses, session["question_responses"], strict=True):
        assert response is indexed

    expected = export_session(session)
    del session["interaction_questions"]
    del session["question_responses"]
    actual = export_session(session)

    assert actual == expected


@pytest.mark.parametrize("limit", [0, 8, -1])
def test_native_structured_facts_survive_export_caps(limit):
    session = _interpret(load_entries(TRANSCRIPT))
    expected_questions = {}
    expected_responses = {}
    for question in session["interaction_questions"]:
        expected_questions[question["ref"]] = question
    for response in session["question_responses"]:
        expected_responses[response["ref"]] = response
    original = deepcopy(session)
    exported = export_session(session, limit)
    assert "interaction_questions" not in exported
    assert "question_responses" not in exported
    actual_questions = {}
    actual_responses = {}
    for event in exported["events"]:
        for call in event.get("tool_calls", []):
            for question in call.get("interaction_questions", []):
                assert question["activity"]["message_i"] == event["i"]
                actual_questions[question["ref"]] = question
            for response in call.get("question_responses", []):
                assert response["activity"]["message_i"] == event["i"]
                actual_responses[response["ref"]] = response
    assert actual_questions == expected_questions
    assert actual_responses == expected_responses
    assert session == original
    for question in actual_questions.values():
        question["options"].clear()
    for response in actual_responses.values():
        response.clear()
    assert session == original


def test_native_answers_use_whole_labels_and_keep_free_text():
    output = {"answers": {"q": {"answers": ["A", "user_note: café, A"]}}}
    session = _interpret(_controlled(json.dumps(output), questions=[_question()]))
    (question,) = session["interaction_questions"]
    (response,) = session["question_responses"]
    assert question["options"][0]["recommendation_markers"] == [
        {"field": "label", "value": "A (Recommended)"}
    ]
    assert response["selections"] == [
        {"label": "A", "option_ref": question["options"][1]["ref"]}
    ]
    assert response["free_text"] == "café, A"
    assert response["question"]["question_ref"] == question["ref"]


def test_repeated_calls_link_out_of_order_results_by_id():
    user, first, first_result = question_interaction()
    second = deepcopy(first)
    second["payload"]["call_id"] = "second-call"
    second_result = deepcopy(first_result)
    second_result["payload"]["call_id"] = "second-call"
    second_result["payload"]["id"] = "second-result"
    entries = [user, first, second, second_result, first_result]
    session = _interpret(entries)
    questions = session["interaction_questions"]
    assert len(questions) == 4
    refs = set()
    for question in questions:
        refs.add(question["ref"])
    assert len(refs) == 4
    responses = session["question_responses"]
    assert len(responses) == 4
    expected_message_indices = {first_result["payload"]["id"]: 1, "second-result": 2}
    associated_refs = set()
    for response in responses:
        assert (
            response["activity"]["message_i"]
            == expected_message_indices[response["native_id"]]
        )
        associated_refs.add(response["question"]["question_ref"])
        associated = None
        for question in questions:
            if question["ref"] == response["question"]["question_ref"]:
                associated = question
        assert associated is not None
        assert response["activity"]["message_i"] == associated["activity"]["message_i"]
    assert associated_refs == refs


@pytest.mark.parametrize(
    "questions", [[_question(), _question()], [_question("other")]]
)
def test_ambiguous_or_unmatched_answer_ids_remain_unassociated(questions):
    output = {"answers": {"q": {"answers": ["A"]}}}
    session = _interpret(_controlled(json.dumps(output), questions=questions))
    (response,) = session["question_responses"]
    assert response["question"] is None
    assert response["outcome"] == "answered"
    assert "free_text" not in response
    assert response["selections"] == [{"label": "A"}]


def test_missing_call_retains_unassociated_native_answers():
    user, _, result = question_interaction()
    session = _interpret([user, result])
    assert session["interaction_questions"] == []
    assert len(session["question_responses"]) == 2
    for response in session["question_responses"]:
        assert response["question"] is None
        assert "activity" not in response
    assert (
        session["unmatched_tool_results"][0]["content"] == result["payload"]["output"]
    )
    exported = export_session(session)
    assert len(exported["events"]) == len(session["events"]) == 1
    assert "unmatched_tool_results" not in exported
    assert "question_responses" not in exported
    assert "tool_calls" not in exported["events"][0]
    assert "tool_results" not in exported["events"][0]


@pytest.mark.parametrize(
    "output,error,count",
    [
        ("tool failed", True, 0),
        ("request_user_input is unavailable in Default mode", True, 0),
        ("unrecognized response", False, 1),
        (None, False, 1),
        ({}, False, 1),
        ('{"answers":{"q":{"answers":[]}}}', False, 1),
    ],
)
def test_errors_and_unknown_answers_do_not_invent_refusals(output, error, count):
    session = _interpret(_controlled(output, questions=[_question()], error=error))
    responses = session["question_responses"]
    assert len(responses) == count
    for response in responses:
        assert response["outcome"] == "unknown"
        assert "refusal" not in response and "cancellation" not in response


def test_unanswered_question_has_no_response():
    user, call, _ = question_interaction()
    session = _interpret([user, call])
    assert len(session["interaction_questions"]) == 2
    assert session["question_responses"] == []


def test_explicit_refusal_keeps_harness_explanation_and_candidate_feedback():
    # Older explicitly attributed rejection wording; not a generic tool error.
    output = (
        "The user doesn't want to proceed with this tool use. "
        "The tool use was rejected. The user said:\nAsk later"
    )
    session = _interpret(_controlled(output, questions=[_question()]))
    (response,) = session["question_responses"]
    assert response["outcome"] == "refused"
    assert response["refusal"] == {
        "by": "candidate",
        "explanation": "The user doesn't want to proceed with this tool use. The tool use was rejected.",
    }
    assert response["candidate_feedback"] == "Ask later"
    assert "free_text" not in response


def test_cancellation_with_multiple_questions_does_not_guess_which_one():
    session = _interpret(_controlled("aborted by user after 1s"))
    (response,) = session["question_responses"]
    assert response["outcome"] == "cancelled"
    assert response["question"] is None
    assert response["activity"]["evidence"]


@pytest.mark.parametrize("error", [False, True])
@pytest.mark.parametrize(
    "output,outcome",
    [
        ("aborted by user after 1.2s", "cancelled"),
        ("The user doesn't want to proceed with this tool use.", "refused"),
    ],
)
def test_explicit_decisions_survive_the_error_flag(output, outcome, error):
    session = _interpret(_controlled(output, questions=[_question()], error=error))
    (response,) = session["question_responses"]
    assert response["outcome"] == outcome
    assert response["question"] is not None
    assert "candidate_feedback" not in response


def test_literal_sentinel_option_remains_selected():
    question = _question()
    question["options"].append({"label": "None of the above"})
    output = {"answers": {"q": {"answers": ["None of the above", "user_note: "]}}}
    session = _interpret(_controlled(output, questions=[question]))
    (response,) = session["question_responses"]
    assert response["free_text"] == ""
    assert response["selections"][0]["label"] == "None of the above"
    assert response["selections"][0]["option_ref"]


def test_duplicate_option_labels_keep_selection_without_guessing_occurrence():
    question = _question()
    question["options"].append({"label": "A"})
    output = {"answers": {"q": {"answers": ["A", "A"]}}}
    session = _interpret(_controlled(output, questions=[question]))
    (response,) = session["question_responses"]
    assert response["selections"] == [{"label": "A"}, {"label": "A"}]


@pytest.mark.parametrize("value", [None, 5, {}, {"answers": [3, None]}])
def test_unrecognized_answer_values_remain_unknown_and_raw(value):
    output = {"answers": {"q": value}}
    entries = _controlled(output, questions=[_question()])
    session = _interpret(entries)
    (response,) = session["question_responses"]
    assert response["outcome"] == "unknown"
    call = session["events"][-1]["tool_calls"][0]
    assert call["result_record"]["content"] == output


@pytest.mark.parametrize("options", [None, [None, {}, {"label": 2}, {"label": "A"}]])
def test_malformed_options_do_not_erase_valid_questions(options):
    question = {"question": "Choose?", "options": options}
    session = _interpret(
        _controlled({"answers": {"Choose?": "A"}}, questions=[question, None, {}])
    )
    (retained,) = session["interaction_questions"]
    assert retained["text"] == "Choose?"
    assert "native_id" not in retained and "header" not in retained
    (response,) = session["question_responses"]
    assert response["outcome"] == "answered"
    assert response["question"]["question_ref"] == retained["ref"]


def test_repeated_text_cannot_link_text_keyed_answer():
    question = {"question": "Choose?", "options": []}
    output = {"answers": {"Choose?": {"answers": ["user_note: custom"]}}}
    session = _interpret(_controlled(output, questions=[question, question]))
    (response,) = session["question_responses"]
    assert response["question"] is None
    assert response["free_text"] == "custom"


def test_unmatched_error_is_not_a_candidate_answer():
    user, _, result = question_interaction()
    result["payload"]["is_error"] = True
    session = _interpret([user, result])
    assert session["question_responses"] == []


def test_question_call_without_ids_retains_questions_without_guessing_results():
    user, call, result = question_interaction()
    del call["payload"]["call_id"]
    del result["payload"]["call_id"]
    session = _interpret([user, call, result])
    assert len(session["interaction_questions"]) == 2
    for response in session["question_responses"]:
        assert response["question"] is None and "activity" not in response


def test_missing_question_array_is_not_an_interaction():
    user, call, result = question_interaction()
    call["payload"]["arguments"] = "{}"
    session = _interpret([user, call, result])
    assert session["interaction_questions"] == []
    assert session["question_responses"] == []


def test_unavailable_tool_result_without_error_flag_is_not_an_answer():
    output = "request_user_input is unavailable in Default mode"
    session = _interpret(_controlled(output, questions=[_question()]))
    assert session["question_responses"] == []


def test_duplicate_literal_sentinel_options_do_not_erase_the_selection():
    question = _question()
    question["options"] = [
        {"label": "None of the above"},
        {"label": "None of the above"},
    ]
    output = {"answers": {"q": {"answers": ["None of the above", "user_note: note"]}}}
    session = _interpret(_controlled(output, questions=[question]))
    (response,) = session["question_responses"]
    assert response["selections"] == [{"label": "None of the above"}]
    assert response["free_text"] == "note"
