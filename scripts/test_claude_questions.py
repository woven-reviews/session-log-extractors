"""Claude decisions use native associations and survive export limits."""

from copy import deepcopy
from pathlib import Path

import pytest

from session_log_extractors import claude_protocol
from session_log_extractors import extract_claude_session_log as claude
from session_log_extractors.claude_sources import ClaudeSource
from session_log_extractors.session_log_envelope import export_session

ROOT = Path(__file__).parent / "fixtures" / "claude" / "2.1.312" / "native"
(TRANSCRIPT,) = ROOT.glob("*.jsonl")


def _interaction():
    """Select the captured multi-question call by its native multiSelect flag."""
    entries = claude.load_entries(TRANSCRIPT)
    matches = []
    for entry in entries:
        if entry.get("type") != claude_protocol.ASSISTANT_RECORD:
            continue
        for block in claude._content_blocks(entry):
            if (
                not isinstance(block, dict)
                or block.get("name") != claude_protocol.ASK_USER_QUESTION
            ):
                continue
            for question in block["input"]["questions"]:
                if question.get("multiSelect") is True:
                    matches.append((entry, block))
                    break
    assert len(matches) == 1, "Capture must contain one multi-question interaction"
    ((assistant, call),) = matches
    return assistant, _recorded_result(entries, call["id"]), call


def _recorded_result(entries, call_id):
    matches = []
    for entry in entries:
        if entry.get("type") != claude_protocol.USER_RECORD:
            continue
        for block in claude._content_blocks(entry):
            if isinstance(block, dict) and block.get("tool_use_id") == call_id:
                matches.append(entry)
    assert len(matches) == 1, "Expected one result with the recorded call ID"
    return matches[0]


def _interpret(entries):
    session = claude.interpret_source(ClaudeSource(TRANSCRIPT, entries))
    assert session is not None
    return session


def _conversation_questions(session):
    questions = []
    for message in session["events"]:
        for call in message.get("tool_calls", []):
            questions.extend(call.get("interaction_questions", []))
    return questions


def _conversation_responses(session):
    responses = []
    for message in session["events"]:
        for call in message.get("tool_calls", []):
            responses.extend(call.get("question_responses", []))
        for record in message.get("tool_results", []):
            responses.extend(record.get("question_responses", []))
    return responses


def _orphan_results(session):
    records = []
    for message in session["events"]:
        records.extend(message.get("tool_results", []))
    return records


def test_native_question_selections_survive_export():
    session = _interpret(claude.load_entries(TRANSCRIPT))
    questions = _conversation_questions(session)
    responses = _conversation_responses(session)
    assert questions and responses
    questions_by_ref = {}
    for question in questions:
        questions_by_ref[question["ref"]] = question
        assert question["text"]
        assert question["activity"]["evidence"]
    multiselect_seen = False
    for response in responses:
        assert response["question"] is not None
        question = questions_by_ref[response["question"]["question_ref"]]
        assert response["question"]["evidence"]
        if not response.get("selections"):
            continue
        options_by_ref = {}
        for option in question["options"]:
            options_by_ref[option["ref"]] = option
        selected_labels = []
        for selection in response["selections"]:
            assert (
                selection["label"] == options_by_ref[selection["option_ref"]]["label"]
            )
            selected_labels.append(selection["label"])
        assert len(selected_labels) == len(set(selected_labels))
        if question.get("multiple_selections"):
            assert len(selected_labels) > 1
            multiselect_seen = True
    assert multiselect_seen, "Native capture must exercise multiple selections"
    original = deepcopy(session)
    exported = export_session(session, 1)
    exported_questions = _conversation_questions(exported)
    exported_responses = _conversation_responses(exported)
    assert exported_questions == questions
    assert exported_responses == responses
    assert session == original
    first_activity = questions[0]["activity"]
    exported_call = exported["events"][first_activity["message_i"]]["tool_calls"][
        first_activity["tool_call_index"]
    ]
    exported_call["question_responses"].clear()
    assert session == original


def test_native_notes_previews_free_text_and_refusals_preserve_recorded_values():
    session = _interpret(claude.load_entries(TRANSCRIPT))
    recorded_responses = _conversation_responses(session)
    recorded_questions = _conversation_questions(session)
    notes_seen = False
    previews_seen = False
    free_text_seen = False
    refusals_seen = False
    for response in recorded_responses:
        activity = response["activity"]
        call = session["events"][activity["message_i"]]["tool_calls"][
            activity["tool_call_index"]
        ]
        payload = call["result_record"]["native_result"]
        if response["outcome"] == "refused":
            assert response["refusal"]["by"] == "candidate"
            assert "candidate_feedback" not in response
            refusals_seen = True
            continue
        association = response["question"]
        questions = {}
        for question in recorded_questions:
            questions[question["ref"]] = question
        text = questions[association["question_ref"]]["text"]
        annotation = payload["annotations"].get(text, {})
        if "notes" in annotation:
            assert response["candidate_feedback"] == annotation["notes"]
            notes_seen = True
        if "preview" in annotation:
            assert response["selected_preview"] == annotation["preview"]
            previews_seen = True
        if "free_text" in response:
            assert response["free_text"] == payload["answers"][text]
            free_text_seen = True
    assert notes_seen and previews_seen and free_text_seen and refusals_seen


def test_structured_answers_override_display_and_preserve_complete_payload():
    assistant, result, call = _interaction()
    payload = result["toolUseResult"]
    payload["future_field"] = {"nested": [None, True]}
    result["message"]["content"][0]["content"] = '"unrelated"="wrong"'
    session = _interpret([assistant, result])
    recorded_responses = _conversation_responses(session)
    assert len(recorded_responses) == len(payload["answers"])
    for response in recorded_responses:
        assert response["outcome"] == "answered"
        assert response["question"] is not None
    exported = export_session(session, 0)
    exported_call = exported["events"][0]["tool_calls"][0]
    assert exported_call["input"] == call["input"]
    assert exported_call["result_record"]["native_result"] == payload
    assert exported_call["result"] == ""


@pytest.mark.parametrize(
    "mode", ["unmatched_key", "repeated_text", "missing_id", "duplicate_id"]
)
def test_ambiguous_answers_remain_unassociated(mode):
    assistant, result, call = _interaction()
    # Mutate captured evidence to remove a uniquely provable association.
    text = call["input"]["questions"][0]["question"]
    payload = result["toolUseResult"]
    payload["answers"] = {text: "A custom answer"}
    entries = [assistant, result]
    if mode == "unmatched_key":
        payload["answers"] = {"a different question": "A custom answer"}
    elif mode == "repeated_text":
        call["input"]["questions"].append(deepcopy(call["input"]["questions"][0]))
    elif mode == "missing_id":
        del result["message"]["content"][0]["tool_use_id"]
    else:
        entries = [assistant, deepcopy(assistant), result]
    session = _interpret(entries)
    recorded_responses = _conversation_responses(session)
    (response,) = recorded_responses
    assert response["question"] is None
    assert response["free_text"] == "A custom answer"
    if mode in {"missing_id", "duplicate_id"}:
        assert "activity" not in response


def test_repeated_questions_in_separate_calls_keep_distinct_occurrences():
    assistant, result, call = _interaction()
    second_assistant, second_result = deepcopy((assistant, result))
    second_call = second_assistant["message"]["content"][0]
    second_call["id"] = "another-recorded-call"
    second_result["message"]["content"][0]["tool_use_id"] = second_call["id"]
    session = _interpret([assistant, second_assistant, second_result, result])
    recorded_questions = _conversation_questions(session)
    recorded_responses = _conversation_responses(session)
    question_refs = set()
    associated_refs = set()
    for question in recorded_questions:
        question_refs.add(question["ref"])
    for response in recorded_responses:
        associated_refs.add(response["question"]["question_ref"])
    assert len(question_refs) == 2 * len(call["input"]["questions"])
    assert associated_refs == question_refs


@pytest.mark.parametrize("error", [False, True])
def test_unanswered_questions_and_generic_tool_errors_are_not_refusals(error):
    assistant, result, _ = _interaction()
    entries = [assistant]
    if error:
        result.pop("toolUseResult")
        result["message"]["content"][0].update(content="Tool failed", is_error=True)
        entries.append(result)
    session = _interpret(entries)
    recorded_questions = _conversation_questions(session)
    recorded_responses = _conversation_responses(session)
    assert recorded_questions
    assert recorded_responses == []


def test_text_fallback_requires_exact_question_key_without_first_answer_guess():
    assistant, result, call = _interaction()
    # Older records lack structured results; one explicitly named answer only.
    result.pop("toolUseResult")
    text = call["input"]["questions"][0]["question"]
    label = call["input"]["questions"][0]["options"][0]["label"]
    result["message"]["content"][0]["content"] = (
        f'Your questions have been answered: "{text}"="{label}".'
    )
    session = _interpret([assistant, result])
    recorded_responses = _conversation_responses(session)
    recorded_questions = _conversation_questions(session)
    (response,) = recorded_responses
    assert response["question"]["question_ref"] == recorded_questions[0]["ref"]
    assert response["selections"][0]["label"] == label


def test_recommendations_previews_and_free_text_are_separate():
    assistant, result, call = _interaction()
    # Controlled labels establish the recommendation marker and comma boundary.
    question = call["input"]["questions"][0]
    question["options"] = [
        {
            "label": "First (Recommended)",
            "description": "Suggested",
            "preview": "first preview",
        },
        {"label": "Second", "description": "Alternative", "preview": "second preview"},
    ]
    result["toolUseResult"]["answers"] = {question["question"]: "Second"}
    result["toolUseResult"]["annotations"] = {
        question["question"]: {"notes": "Candidate note", "preview": "second preview"}
    }
    session = _interpret([assistant, result])
    recorded_responses = _conversation_responses(session)
    recorded_questions = _conversation_questions(session)
    offered = recorded_questions[0]["options"]
    (response,) = recorded_responses
    assert offered[0]["recommendation_markers"]
    assert "recommendation_markers" not in offered[1]
    assert response["selections"] == [
        {"label": "Second", "option_ref": offered[1]["ref"]}
    ]
    assert response["candidate_feedback"] == "Candidate note"
    assert response["selected_preview"] == "second preview"
    assert "free_text" not in response


@pytest.mark.parametrize(
    "kind,expected_source",
    [
        (claude_protocol.USER_REJECTED, "candidate"),
        (claude_protocol.SETTINGS_RULE, "settings"),
        ("future-denial", "unknown"),
    ],
)
def test_refusal_preserves_source_and_explanation_without_inventing_feedback(
    kind, expected_source
):
    assistant, result, _ = _interaction()
    result["toolUseResult"] = "Native rejection"
    result["toolDenialKind"] = kind
    result["message"]["content"][0].update(content="Harness explanation", is_error=True)
    session = _interpret([assistant, result])
    recorded_responses = _conversation_responses(session)
    (response,) = recorded_responses
    assert response["outcome"] == "refused"
    assert response["question"] is None, (
        "A call-level refusal cannot identify one of several questions"
    )
    assert response["refusal"] == {
        "by": expected_source,
        "explanation": "Harness explanation",
    }
    assert "candidate_feedback" not in response
    exported = export_session(session, 1)
    record = exported["events"][0]["tool_calls"][0]["result_record"]
    assert record["content"] == "H"
    assert record["native_result"] == "N"


@pytest.mark.parametrize("with_blocks", [False, True])
def test_unmatched_native_payload_remains_available_in_json(with_blocks):
    assistant, result, _ = _interaction()
    expected_payload = deepcopy(result["toolUseResult"])
    if with_blocks:
        result["message"]["content"][0]["tool_use_id"] = "missing-call"
    else:
        result["message"]["content"] = []
    session = _interpret([assistant, result])
    exported = export_session(session, 0)
    exported_orphan_results = _orphan_results(exported)
    exported_responses = _conversation_responses(exported)
    (unmatched,) = exported_orphan_results
    if with_blocks:
        assert unmatched["native_result"] == expected_payload
        assert unmatched["content"] == ""
    else:
        assert unmatched["content"] == expected_payload
    assert len(exported_responses) == len(expected_payload["answers"])
    for response in exported_responses:
        assert response["question"] is None
        assert "activity" not in response


@pytest.mark.parametrize("answer", [None, 7, {"future": [True]}, ""])
def test_unsupported_answer_shapes_retain_unknown_outcome_and_raw_value(answer):
    assistant, result, call = _interaction()
    key = call["input"]["questions"][0]["question"]
    result["toolUseResult"]["answers"] = {key: answer}
    session = _interpret([assistant, result])
    recorded_responses = _conversation_responses(session)
    (response,) = recorded_responses
    assert response["outcome"] == "unknown"
    assert "refusal" not in response
    raw = export_session(session, 0)["events"][0]["tool_calls"][0]["result_record"]
    assert raw["native_result"]["answers"][key] == answer


@pytest.mark.parametrize(
    "invalid",
    [
        None,
        "invalid",
        {"questions": "invalid"},
        {"questions": [None, {}, {"question": 42}]},
    ],
)
def test_malformed_question_containers_do_not_invent_questions(invalid):
    assistant, result, call = _interaction()
    call["input"] = invalid
    session = _interpret([assistant, result])
    recorded_responses = _conversation_responses(session)
    recorded_questions = _conversation_questions(session)
    assert recorded_questions == []
    assert recorded_responses
    for response in recorded_responses:
        assert response["question"] is None


@pytest.mark.parametrize("options", [None, "invalid", [None, {}, {"label": 42}]])
def test_invalid_options_do_not_discard_question_or_answer(options):
    assistant, result, call = _interaction()
    question = call["input"]["questions"][0]
    question["options"] = options
    session = _interpret([assistant, result])
    recorded_questions = _conversation_questions(session)
    recorded_responses = _conversation_responses(session)
    assert recorded_questions[0]["text"] == question["question"]
    assert recorded_questions[0]["options"] == []
    assert recorded_responses[0]["outcome"] == "answered"


def test_annotations_without_an_answer_keep_candidate_notes():
    assistant, result, call = _interaction()
    question = call["input"]["questions"][0]
    result["toolUseResult"]["answers"] = {}
    result["toolUseResult"]["annotations"] = {
        question["question"]: {"notes": "Recorded note"}
    }
    session = _interpret([assistant, result])
    recorded_responses = _conversation_responses(session)
    (response,) = recorded_responses
    assert response["candidate_feedback"] == "Recorded note"
    assert response["outcome"] == "answered"
    assert "free_text" not in response


@pytest.mark.parametrize(
    "value", ["First", "First, Second", "Second", "A custom, comma answer"]
)
def test_controlled_whole_labels_and_commas(value):
    assistant, result, call = _interaction()
    question = call["input"]["questions"][0]
    question["options"] = [
        {"label": "First"},
        {"label": "First, Second"},
        {"label": "Second"},
    ]
    result["toolUseResult"]["answers"] = {question["question"]: value}
    session = _interpret([assistant, result])
    recorded_responses = _conversation_responses(session)
    (response,) = recorded_responses
    if value == "A custom, comma answer":
        assert response["free_text"] == value
        assert "selections" not in response
    else:
        (selection,) = response["selections"]
        assert selection["label"] == value
        assert "free_text" not in response


def test_single_choice_cannot_become_multiple_selections_from_comma_text():
    assistant, result, call = _interaction()
    question = call["input"]["questions"][0]
    question["multiSelect"] = False
    question["options"] = [{"label": "One"}, {"label": "Two"}]
    result["toolUseResult"]["answers"] = {question["question"]: "One, Two"}
    session = _interpret([assistant, result])
    recorded_responses = _conversation_responses(session)
    (response,) = recorded_responses
    assert "selections" not in response
    assert response["free_text"] == "One, Two"


def _text_only_question_source():
    """Remove alternate payloads to exercise older text-only result records."""
    entries = claude.load_entries(TRANSCRIPT)
    payloads = []
    for entry in entries:
        payload = entry.get("toolUseResult")
        if isinstance(payload, dict) and "answers" in payload:
            payloads.append(payload)
            del entry["toolUseResult"]
    assert payloads, "Capture must contain structured question results"
    return entries, payloads


@pytest.mark.parametrize(
    "native_field,response_field",
    [("notes", "candidate_feedback"), ("preview", "selected_preview")],
)
def test_text_only_native_annotations_preserve_exact_values(
    native_field, response_field
):
    entries, payloads = _text_only_question_source()
    expected = []
    for payload in payloads:
        for annotation in payload["annotations"].values():
            if native_field in annotation:
                expected.append(annotation[native_field])
    session = _interpret(entries)
    recorded_responses = _conversation_responses(session)
    actual = []
    for response in recorded_responses:
        if response_field in response:
            actual.append(response[response_field])
    assert expected, "Capture must exercise this annotation"
    assert actual == expected


def test_text_only_native_quoted_free_text_preserves_exact_answers():
    entries, payloads = _text_only_question_source()
    expected = []
    for payload in payloads:
        for answer in payload["answers"].values():
            if "CAPTURE_CUSTOM" in answer:
                expected.append(answer)
    session = _interpret(entries)
    recorded_responses = _conversation_responses(session)
    actual = []
    for response in recorded_responses:
        if "free_text" in response:
            actual.append(response["free_text"])
    assert expected, "Capture must contain the operator's custom answers"
    assert actual == expected


def test_changed_generated_wording_preserves_structured_contract():
    assistant, result, call = _interaction()
    questions = call["input"]["questions"]
    questions[0].update(question="Reworded multiple choice?", header="Changed header")
    questions[0]["options"] = [
        {"label": "Choice A", "description": "Changed description"},
        {"label": "Choice B", "description": "Another description"},
    ]
    questions[1].update(question="Reworded single choice?", header="Another header")
    questions[1]["options"] = [{"label": "Choice C"}, {"label": "Choice D"}]
    result["toolUseResult"]["questions"] = deepcopy(questions)
    result["toolUseResult"]["answers"] = {
        "Reworded multiple choice?": "Choice A, Choice B",
        "Reworded single choice?": "Choice D",
    }
    result["message"]["content"][0]["content"] = "Different harness display prose"
    assistant["message"]["content"].append(
        {"type": "text", "text": "Different assistant prose"}
    )
    session = _interpret([assistant, result])
    recorded_questions = _conversation_questions(session)
    recorded_responses = _conversation_responses(session)
    actual_texts = []
    actual_selections = []
    for question in recorded_questions:
        actual_texts.append(question["text"])
    for response in recorded_responses:
        labels = []
        for selection in response["selections"]:
            labels.append(selection["label"])
        actual_selections.append(labels)
    assert actual_texts == ["Reworded multiple choice?", "Reworded single choice?"]
    assert actual_selections == [["Choice A", "Choice B"], ["Choice D"]]
    assert recorded_questions[0]["options"][0]["description"] == "Changed description"


@pytest.mark.parametrize("use_native_payload", [False, True])
def test_explicit_legacy_rejection_is_a_candidate_refusal(use_native_payload):
    assistant, result, _ = _interaction()
    result.pop("toolDenialKind", None)
    result["message"]["content"][0].update(
        content="The user doesn't want to proceed with this tool use. Harness instructions.",
        is_error=True,
    )
    if use_native_payload:
        result["toolUseResult"] = claude_protocol.REJECTED_RESULT
    else:
        result.pop("toolUseResult")
    session = _interpret([assistant, result])
    recorded_responses = _conversation_responses(session)
    (response,) = recorded_responses
    assert response["outcome"] == "refused"
    assert response["refusal"]["by"] == "candidate"
    assert "candidate_feedback" not in response


def test_unmatched_text_answer_is_preserved_without_a_question_or_activity_link():
    assistant, result, _ = _interaction()
    result.pop("toolUseResult")
    result["message"]["content"][0].update(
        tool_use_id="missing-call",
        content='The user answered: "Unknown question"="Recorded answer".',
    )
    session = _interpret([assistant, result])
    recorded_responses = _conversation_responses(session)
    (response,) = recorded_responses
    assert response["question"] is None
    assert response["free_text"] == "Recorded answer"
    assert "activity" not in response


def test_plain_tool_output_does_not_create_a_question_response():
    assistant, result, _ = _interaction()
    result.pop("toolUseResult")
    result["message"]["content"][0]["content"] = (
        'Ordinary output with "incidental"="quoted text"'
    )
    session = _interpret([assistant, result])
    recorded_responses = _conversation_responses(session)
    assert recorded_responses == []


def test_duplicate_option_labels_and_repeated_selections_preserve_ambiguity():
    assistant, result, call = _interaction()
    question = call["input"]["questions"][0]
    question["options"] = [{"label": "Repeated"}, {"label": "Repeated"}]
    result["toolUseResult"]["answers"] = {question["question"]: "Repeated"}
    session = _interpret([assistant, result])
    recorded_responses = _conversation_responses(session)
    (response,) = recorded_responses
    assert "selections" not in response
    assert response["free_text"] == "Repeated"
    question["options"] = [{"label": "One"}, {"label": "Two"}]
    result["toolUseResult"]["answers"] = {question["question"]: "One, One"}
    repeated_session = _interpret([assistant, result])
    repeated_responses = _conversation_responses(repeated_session)
    (repeated_response,) = repeated_responses
    assert "selections" not in repeated_response
    assert repeated_response["free_text"] == "One, One"


@pytest.mark.parametrize("has_uuid", [False, True])
def test_entry_level_answers_with_multiple_result_blocks_remain_unassociated(has_uuid):
    assistant, result, _ = _interaction()
    if not has_uuid:
        result.pop("uuid")
    # The alternate result is recorded once for the whole native entry. Adding
    # another result block removes proof that it belongs to a particular call.
    extra_block = deepcopy(result["message"]["content"][0])
    extra_block["tool_use_id"] = "unrecorded-call"
    result["message"]["content"].append(extra_block)
    payload = deepcopy(result["toolUseResult"])
    session = _interpret([assistant, result])
    (call,) = session["events"][0]["tool_calls"]
    assert call["result"] == result["message"]["content"][0]["content"]
    assert call["result_record"]["tool_call_id"] == call["id"]
    assert call["question_responses"] == []
    responses = _conversation_responses(session)
    assert len(responses) == len(payload["answers"])
    for response in responses:
        assert response["question"] is None
        assert "activity" not in response
    exported = export_session(session, 0)
    exported_orphan_results = _orphan_results(exported)
    retained_payloads = []
    for record in exported_orphan_results:
        if isinstance(record["content"], dict):
            retained_payloads.append(record["content"])
    assert retained_payloads == [payload]


@pytest.mark.parametrize("answers", [None, ["unsupported"]])
def test_annotations_survive_missing_or_unsupported_answer_maps(answers):
    assistant, result, call = _interaction()
    key = call["input"]["questions"][0]["question"]
    result["toolUseResult"] = {"annotations": {key: {"notes": "Preserved note"}}}
    if answers is not None:
        result["toolUseResult"]["answers"] = answers
    session = _interpret([assistant, result])
    recorded_responses = _conversation_responses(session)
    (response,) = recorded_responses
    assert response["candidate_feedback"] == "Preserved note"
    assert response["question"] is not None
    fields = []
    for evidence in response["question"]["evidence"]:
        fields.append(evidence["field"])
    assert "toolUseResult.annotations key" in fields


def test_text_only_notes_keep_candidate_punctuation():
    assistant, result, call = _interaction()
    result.pop("toolUseResult")
    key = call["input"]["questions"][0]["question"]
    result["message"]["content"][0]["content"] = (
        f'The user answered: "{key}"=(no option selected) notes: Keep this period.. Read the answers carefully — they may request clarification, changes, or that you not proceed — and follow what they actually say.'
    )
    session = _interpret([assistant, result])
    recorded_responses = _conversation_responses(session)
    (response,) = recorded_responses
    assert response["candidate_feedback"] == "Keep this period."
    assert response["outcome"] == "answered"
    assert response["selections"] == []


def test_native_question_fields_preserve_exact_offered_values():
    session = _interpret(claude.load_entries(TRANSCRIPT))
    recorded_questions = _conversation_questions(session)
    observed_questions = 0
    for message in session["events"]:
        for call in message.get("tool_calls", []):
            if call["name"] != claude_protocol.ASK_USER_QUESTION:
                continue
            questions = call["interaction_questions"]
            native_questions = call["input"]["questions"]
            assert len(questions) == len(native_questions)
            for question, native in zip(questions, native_questions, strict=True):
                assert question["text"] == native["question"]
                assert question.get("header") == native.get("header")
                assert question.get("multiple_selections") == native.get("multiSelect")
                assert len(question["options"]) == len(native["options"])
                for option, offered in zip(
                    question["options"], native["options"], strict=True
                ):
                    assert option["label"] == offered["label"]
                    assert option.get("description") == offered.get("description")
                    assert option.get("preview") == offered.get("preview")
                observed_questions += 1
    assert observed_questions == len(recorded_questions)
    assert observed_questions > 0, "Capture must contain offered questions"


def test_text_only_content_blocks_keep_explicit_question_associations():
    assistant, result, _ = _interaction()
    expected_count = len(result["toolUseResult"]["answers"])
    result.pop("toolUseResult")
    block = result["message"]["content"][0]
    block["content"] = [{"type": claude_protocol.TEXT_BLOCK, "text": block["content"]}]
    session = _interpret([assistant, result])
    recorded_responses = _conversation_responses(session)
    assert len(recorded_responses) == expected_count
    for response in recorded_responses:
        assert response["question"] is not None
        assert response["outcome"] == "answered"


def test_export_keeps_questions_and_responses_on_the_existing_tool_call():
    assistant, result, _ = _interaction()
    session = _interpret([assistant, result])
    original = deepcopy(session)
    exported = export_session(session, 0)
    assert set(exported) == {
        "schema",
        "harness",
        "session_id",
        "cwd",
        "started_at",
        "models",
        "truncation",
        "events",
    }
    (message,) = exported["events"]
    (call,) = message["tool_calls"]
    assert call["interaction_questions"]
    assert call["question_responses"]
    assert call["result"] == ""
    assert session == original
    call["interaction_questions"].clear()
    call["question_responses"].clear()
    assert session == original


@pytest.mark.parametrize("missing_id", [False, True])
def test_orphan_responses_stay_at_their_native_position_in_the_conversation(missing_id):
    assistant, result, _ = _interaction()
    if missing_id:
        result["message"]["content"][0].pop("tool_use_id")
    else:
        result["message"]["content"][0]["tool_use_id"] = "missing-call"
    later = deepcopy(assistant)
    later["message"]["content"] = [
        {"type": claude_protocol.TEXT_BLOCK, "text": "Later assistant message"}
    ]
    session = _interpret([assistant, result, later])
    original = deepcopy(session)
    exported = export_session(session, 0)
    first, orphan, last = exported["events"]
    assert first["i"] == 0 and orphan["i"] == 1 and last["i"] == 2
    assert first["tool_calls"][0]["question_responses"] == []
    assert orphan["role"] == "user"
    assert orphan["text"] == ""
    (record,) = orphan["tool_results"]
    assert record["tool_call_id"] == (None if missing_id else "missing-call")
    assert record["native_result"] == result["toolUseResult"]
    assert record["question_responses"]
    for response in record["question_responses"]:
        assert response["question"] is None
        assert "activity" not in response
    assert last["text"] == "Later assistant message"
    assert "interaction_questions" not in exported
    assert "question_responses" not in exported
    assert "unmatched_tool_results" not in exported
    assert session == original
    record["question_responses"].clear()
    assert session == original


def test_unknown_tool_rejection_does_not_become_a_question_refusal():
    assistant, result, _ = _interaction()
    result.pop("toolUseResult")
    result["message"]["content"][0].update(
        tool_use_id="unrecorded-tool",
        content="The user doesn't want to proceed with this tool use. Harness instructions.",
        is_error=True,
    )
    session = _interpret([assistant, result])
    recorded_orphan_results = _orphan_results(session)
    recorded_responses = _conversation_responses(session)
    assert recorded_responses == []
    (record,) = recorded_orphan_results
    assert record["is_error"] is True
    assert "question_responses" not in record


def test_repeated_question_call_ids_do_not_capture_the_orphan_response():
    assistant, result, _ = _interaction()
    session = _interpret([assistant, result, deepcopy(assistant)])
    first, orphan, repeated = session["events"]
    for message in [first, repeated]:
        (call,) = message["tool_calls"]
        assert call["interaction_questions"]
        assert call["question_responses"] == []
        assert call["result"] is None
        assert "result_record" not in call
    (record,) = orphan["tool_results"]
    assert record["question_responses"]
    for response in record["question_responses"]:
        assert response["question"] is None
        assert "activity" not in response


@pytest.mark.parametrize(
    "labels,value,expected",
    [
        (["Red, green", "Blue"], "Red, green, Blue", ["Red, green", "Blue"]),
        (["Red, green", "Blue"], "Blue, Red, green", ["Blue", "Red, green"]),
        (["Red", "Red, green", "Blue"], "Red, green, Blue", ["Red, green", "Blue"]),
        (["Red", "green, Blue", "Red, green", "Blue"], "Red, green, Blue", []),
        (["Red, green", "Blue"], "Red, green, Red, green", []),
        (["Red, green", "Red, green", "Blue"], "Red, green, Blue", []),
        (["", "Blue"], "Blue, ", []),
        (["Red, green", "Blue"], "Red, green, Unknown", []),
    ],
)
def test_multiselect_requires_unique_complete_label_partition(labels, value, expected):
    assistant, result, call = _interaction()
    # Control only labels and answer to exercise comma boundaries and ambiguity.
    question = call["input"]["questions"][0]
    question["multiSelect"] = True
    question["options"] = []
    for label in labels:
        question["options"].append({"label": label})
    result["toolUseResult"]["answers"] = {question["question"]: value}

    session = _interpret([assistant, result])
    responses = _conversation_responses(export_session(session, 0))

    (response,) = responses
    actual = []
    for selection in response.get("selections", []):
        actual.append(selection["label"])
    assert actual == expected
    if expected:
        assert "free_text" not in response
    else:
        assert response["free_text"] == value


@pytest.mark.parametrize("notes", [None, "", "Recorded feedback"])
def test_notes_only_sentinel_explicitly_answers_without_selections(notes):
    assistant, result, call = _interaction()
    # Keep the captured call/result ownership, controlling the sentinel and notes.
    key = call["input"]["questions"][0]["question"]
    result["toolUseResult"]["answers"] = {key: "(notes only)"}
    result["toolUseResult"]["annotations"] = {}
    if notes is not None:
        result["toolUseResult"]["annotations"][key] = {"notes": notes}

    session = _interpret([assistant, result])
    responses = _conversation_responses(export_session(session, 0))

    (response,) = responses
    assert response["outcome"] == "answered"
    assert response["selections"] == []
    assert "free_text" not in response
    if notes is not None:
        assert response["candidate_feedback"] == notes


def test_native_notes_only_response_has_explicit_empty_selections():
    entries = claude.load_entries(TRANSCRIPT)
    notes_only_results = []
    for entry in entries:
        payload = entry.get("toolUseResult")
        if not isinstance(payload, dict):
            continue
        for key, answer in payload.get("answers", {}).items():
            if answer == "(notes only)":
                notes_only_results.append((entry, key))
    assert notes_only_results, "Capture must exercise notes-only answers"
    for result, key in notes_only_results:
        call_id = result["message"]["content"][0]["tool_use_id"]
        assistant = None
        for entry in entries:
            for block in claude._content_blocks(entry):
                if isinstance(block, dict) and block.get("id") == call_id:
                    assistant = entry
        assert assistant is not None
        session = _interpret([assistant, result])
        responses = _conversation_responses(export_session(session, 0))
        (response,) = responses
        assert response["outcome"] == "answered"
        assert response["selections"] == []
        assert (
            response["candidate_feedback"]
            == result["toolUseResult"]["annotations"][key]["notes"]
        )


@pytest.mark.parametrize("limit", [0, 17, -1])
def test_mismatched_text_answer_never_falls_back_to_the_first_question(limit):
    assistant, result, call = _interaction()
    # Narrow to one captured question to exercise the former single-question
    # fallback; retain call ownership but answer an explicitly different question.
    question = call["input"]["questions"][0]
    call["input"]["questions"] = [question]
    result.pop("toolUseResult")
    text = 'Your questions have been answered: "Unrecorded question"="Custom café, 雪".'
    result["message"]["content"][0]["content"] = text
    session = _interpret([assistant, result])
    original = deepcopy(session)
    (response,) = _conversation_responses(session)
    assert response["question"] is None
    assert response["free_text"] == "Custom café, 雪"
    call = session["events"][0]["tool_calls"][0]
    assert call["result_record"]["content"] == text
    exported = export_session(session, limit)
    assert _conversation_responses(exported) == [response]
    assert session == original


@pytest.mark.parametrize("labels", [("Plan", "Planet"), ("North", "Northwest")])
def test_overlapping_labels_select_only_the_recorded_whole_label(labels):
    assistant, result, call = _interaction()
    shorter, longer = labels
    question = call["input"]["questions"][0]
    question["options"] = [{"label": shorter}, {"label": longer}]
    result["toolUseResult"]["answers"] = {question["question"]: longer}
    session = _interpret([assistant, result])
    (response,) = _conversation_responses(session)
    (selection,) = response["selections"]
    assert selection["label"] == longer
    assert "free_text" not in response
