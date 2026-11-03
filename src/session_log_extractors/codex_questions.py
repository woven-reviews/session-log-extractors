"""Codex question occurrences and responses, interpreted before export policy."""

import json
import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Final

from session_log_extractors.json_types import JsonValue, json_value, parse_json
from session_log_extractors.session_model import (
    AuthorshipSource,
    Question,
    QuestionActivityReference,
    QuestionAssociation,
    QuestionCancellation,
    QuestionChoice,
    QuestionEvidence,
    QuestionRefusal,
    QuestionResponse,
    QuestionResponseOutcome,
    QuestionSelection,
    Session,
    ToolCall,
    ToolResult,
)

REQUEST_USER_INPUT: Final = "request_user_input"
_USER_NOTE: Final = "user_note: "
_CUSTOM_CHOICE: Final = "None of the above"
_RECOMMENDED: Final = "(Recommended)"
_ABORTED = re.compile(r"aborted by user(?: after \d+(?:\.\d+)?s)?\Z")
USER_REFUSAL_PREFIX: Final = "The user doesn't want to proceed with this tool use."
_FEEDBACK_MARKER: Final = "The user said:\n"
_UNAVAILABLE: Final = "request_user_input is unavailable in Default mode"


def _decode(value: JsonValue) -> JsonValue:
    if isinstance(value, str):
        try:
            return parse_json(value)
        except json.JSONDecodeError:
            pass
    return value


def _choices(value: JsonValue, question_ref: str) -> list[QuestionChoice]:
    choices: list[QuestionChoice] = []
    if not isinstance(value, list):
        return choices
    for index, option in enumerate(value):
        if not isinstance(option, dict) or not isinstance(option.get("label"), str):
            continue
        label = str(option["label"])
        choice = QuestionChoice(ref=f"{question_ref}:o{index}", label=label)
        description = option.get("description")
        if isinstance(description, str):
            choice["description"] = description
        if _RECOMMENDED in label:
            choice["recommendation_markers"] = [
                QuestionEvidence(field="label", value=label)
            ]
        choices.append(choice)
    return choices


def _question(native: dict[str, JsonValue], ref: str) -> Question | None:
    text = native.get("question")
    if not isinstance(text, str):
        return None
    question = Question(
        ref=ref, text=text, options=_choices(native.get("options"), ref)
    )
    native_id, header = native.get("id"), native.get("header")
    if isinstance(native_id, str):
        question["native_id"] = native_id
    if isinstance(header, str):
        question["header"] = header
    return question


def _association(
    questions: list[Question], key: str, evidence: list[QuestionEvidence]
) -> tuple[QuestionAssociation | None, Question | None]:
    matches = [question for question in questions if question.get("native_id") == key]
    # A native answer map can also explicitly key by question text in older logs.
    field = "question.id"
    if not matches:
        matches = [question for question in questions if question["text"] == key]
        field = "question.question"
    if len(matches) != 1:
        return None, None
    question = matches[0]
    return QuestionAssociation(
        question_ref=question["ref"],
        evidence=[*evidence, QuestionEvidence(field=field, value=key)],
    ), question


def _selection(label: str, question: Question | None) -> QuestionSelection:
    selection = QuestionSelection(label=label)
    if question is not None:
        matches = [option for option in question["options"] if option["label"] == label]
        if len(matches) == 1:
            selection["option_ref"] = matches[0]["ref"]
    return selection


def _answer(
    response: QuestionResponse, value: JsonValue, question: Question | None
) -> None:
    """Native arrays contain selected labels plus explicitly prefixed user notes."""
    values = value.get("answers") if isinstance(value, dict) else value
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        return
    selections: list[QuestionSelection] = []
    notes: list[str] = []
    for item in values:
        if not isinstance(item, str):
            continue
        if item.startswith(_USER_NOTE):
            notes.append(item.removeprefix(_USER_NOTE))
        else:
            selections.append(_selection(item, question))
    if notes:
        # The TUI inserts this sentinel for custom text; an offered option with
        # that literal label remains a real selection.
        sentinel_offered = question is not None and any(
            option["label"] == _CUSTOM_CHOICE for option in question["options"]
        )
        selections = [
            selection
            for selection in selections
            if selection["label"] != _CUSTOM_CHOICE or sentinel_offered
        ]
        response["free_text"] = "\n".join(notes)
    response["selections"] = selections
    if selections or notes:
        response["outcome"] = QuestionResponseOutcome.ANSWERED


def _decision_outcome(output: JsonValue) -> QuestionResponseOutcome:
    if isinstance(output, str):
        if _ABORTED.fullmatch(output):
            return QuestionResponseOutcome.CANCELLED
        if output.startswith(USER_REFUSAL_PREFIX):
            return QuestionResponseOutcome.REFUSED
    return QuestionResponseOutcome.UNKNOWN


def _decision(response: QuestionResponse, output: JsonValue) -> None:
    response["outcome"] = _decision_outcome(output)
    if not isinstance(output, str):
        return
    if response["outcome"] == QuestionResponseOutcome.CANCELLED:
        response["cancellation"] = QuestionCancellation(
            by=AuthorshipSource.CANDIDATE.value, explanation=output
        )
    elif response["outcome"] == QuestionResponseOutcome.REFUSED:
        explanation, marker, feedback = output.partition(_FEEDBACK_MARKER)
        response["refusal"] = QuestionRefusal(
            by=AuthorshipSource.CANDIDATE.value, explanation=explanation.rstrip()
        )
        if marker:
            response["candidate_feedback"] = feedback


@dataclass
class _QuestionGroup:
    call: ToolCall
    activity: QuestionActivityReference


class _CodexQuestionInterpreter:
    """Track each call's own occurrences; never associate by order or counts."""

    def __init__(self, session: Session) -> None:
        self.questions: list[Question] = []
        self.responses: list[QuestionResponse] = []
        session["interaction_questions"] = self.questions
        session["question_responses"] = self.responses
        self.calls: dict[int, _QuestionGroup] = {}

    def register(self, call: ToolCall, message_i: int) -> None:
        if call["name"].rsplit(".", 1)[-1] != REQUEST_USER_INPUT:
            return
        arguments = call.get("input")
        natives = arguments.get("questions") if isinstance(arguments, dict) else None
        if not isinstance(natives, list):
            return
        activity = QuestionActivityReference(
            message_i=message_i,
            tool_call_index=0,
            evidence=[
                QuestionEvidence(
                    field="function_call.arguments", value=json_value(arguments)
                )
            ],
        )
        if "id" in call:
            activity["evidence"].append(
                QuestionEvidence(field="function_call.call_id", value=call["id"])
            )
        questions = []
        for native in natives:
            if isinstance(native, dict):
                question = _question(native, f"codex:q{len(self.questions)}")
                if question is not None:
                    question["activity"] = deepcopy(activity)
                    self.questions.append(question)
                    questions.append(question)
        call["interaction_questions"] = questions
        call["question_responses"] = []
        self.calls[id(call)] = _QuestionGroup(call, activity)

    def result(self, call: ToolCall | None, result: ToolResult) -> None:
        scope = self.calls.get(id(call)) if call is not None else None
        output = _decode(result.get("content"))
        answers = output.get("answers") if isinstance(output, dict) else None
        if scope is None and (call is not None or not isinstance(answers, dict)):
            return
        if output == _UNAVAILABLE:
            return
        if (
            result["is_error"]
            and _decision_outcome(output) == QuestionResponseOutcome.UNKNOWN
        ):
            return
        questions = scope.call["interaction_questions"] if scope is not None else []
        if isinstance(answers, dict):
            self._record_answers(answers, result, scope)
            return
        response = self._response(result, scope)
        _decision(response, output)
        # A call-wide cancellation/refusal has no per-question identifier.
        # With one contained occurrence only, native containment is sufficient.
        if (
            len(questions) == 1
            and response["outcome"] != QuestionResponseOutcome.UNKNOWN
        ):
            response["question"] = QuestionAssociation(
                question_ref=questions[0]["ref"],
                evidence=deepcopy(response["activity"]["evidence"]),
            )

    def _record_answers(
        self,
        answers: dict[str, JsonValue],
        result: ToolResult,
        scope: _QuestionGroup | None,
    ) -> None:
        questions = scope.call["interaction_questions"] if scope is not None else []
        for key, value in answers.items():
            response = self._response(result, scope)
            evidence = (
                response["activity"]["evidence"] if "activity" in response else []
            )
            response["question"], question = _association(
                questions,
                key,
                [*evidence, QuestionEvidence(field="answers key", value=key)],
            )
            _answer(response, value, question)

    def _response(
        self, result: ToolResult, scope: _QuestionGroup | None
    ) -> QuestionResponse:
        response = QuestionResponse(
            ref=f"codex:r{len(self.responses)}",
            question=None,
            outcome=QuestionResponseOutcome.UNKNOWN,
        )
        if "id" in result:
            response["native_id"] = result["id"]
        if scope is not None:
            scope.call["question_responses"].append(response)
            response["activity"] = deepcopy(scope.activity)
            response["activity"]["evidence"].append(
                QuestionEvidence(
                    field="function_call_output.call_id", value=result["tool_call_id"]
                )
            )
        self.responses.append(response)
        return response
