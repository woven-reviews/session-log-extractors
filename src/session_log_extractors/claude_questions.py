"""Interpret Claude questions from canonical calls and their complete native results.

Text-keyed answers can identify only a unique question within an ID-matched
call. Repeated keys, missing IDs, and duplicate call IDs retain ambiguity.
"""

import re
from collections import Counter

from session_log_extractors.json_types import JsonValue
from session_log_extractors.session_model import (
    QUESTION_ANSWERED,
    QUESTION_REFUSED,
    QUESTION_UNKNOWN,
    REFUSAL_CANDIDATE,
    REFUSAL_SETTINGS,
    REFUSAL_UNKNOWN,
    Message,
    Question,
    QuestionActivityReference,
    QuestionAssociation,
    QuestionChoice,
    QuestionEvidence,
    QuestionRefusal,
    QuestionResponse,
    QuestionSelection,
    ToolCall,
    ToolResult,
)

ASK_USER_QUESTION = "AskUserQuestion"
_NOTES_ONLY = "(notes only)"
_USER_REJECTED = "user-rejected"
_REJECTED_RESULT = "User rejected tool use"
_REJECTED_PREFIX = "The user doesn't want to proceed with this tool use."
_SETTINGS_DENIED = "permission-rule"
_RECOMMENDED = "(Recommended)"
_TEXT_PREFIXES = ("Your questions have been answered: ", "The user answered: ")
_TEXT_SUFFIXES = (
    ". You can now continue with these answers in mind.",
    ". Read the answers carefully — they may request clarification, changes, or that you not proceed — and follow what they actually say.",
)
_TEXT_ANSWER = re.compile(
    r'"(.*?)"=(?:"(.*?)"(?= selected preview:| notes:|, ".*?"=|$)|\(no option selected\))'
    r'(.*?)(?=, ".*?"=|$)',
    re.DOTALL,
)


def _text_annotation(text: str) -> dict[str, JsonValue]:
    preview, notes_marker, notes = text.partition(" notes: ")
    annotation: dict[str, JsonValue] = {}
    if notes_marker:
        annotation["notes"] = notes
    if preview.startswith(" selected preview:\n"):
        annotation["preview"] = preview.removeprefix(" selected preview:\n")
    return annotation


def _offered_options(
    value: JsonValue, question_ref: str, question_index: int
) -> list[QuestionChoice]:
    options: list[QuestionChoice] = []
    if not isinstance(value, list):
        return options
    for index, native in enumerate(value):
        if not isinstance(native, dict):
            continue
        label = native.get("label")
        if not isinstance(label, str):
            continue
        option = QuestionChoice(ref=f"{question_ref}/option/{index}", label=label)
        description, preview = native.get("description"), native.get("preview")
        if isinstance(description, str):
            option["description"] = description
        if isinstance(preview, str):
            option["preview"] = preview
        if _RECOMMENDED in label:
            option["recommendation_markers"] = [
                QuestionEvidence(
                    field=f"tool_use.input.questions[{question_index}].options[{index}].label",
                    value=label,
                )
            ]
        options.append(option)
    return options


def _offered_questions(
    call: ToolCall, activity: QuestionActivityReference
) -> list[Question]:
    tool_input = call.get("input")
    if not isinstance(tool_input, dict):
        return []
    native_questions = tool_input.get("questions")
    if not isinstance(native_questions, list):
        return []
    questions: list[Question] = []
    for index, native in enumerate(native_questions):
        if not isinstance(native, dict):
            continue
        text = native.get("question")
        if not isinstance(text, str):
            continue
        ref = f"claude/question/{activity['message_i']}/{activity['tool_call_index']}/{index}"
        question_activity = QuestionActivityReference(
            message_i=activity["message_i"],
            tool_call_index=activity["tool_call_index"],
            evidence=activity["evidence"]
            + [
                QuestionEvidence(
                    field=f"tool_use.input.questions[{index}].question", value=text
                )
            ],
        )
        question = Question(
            ref=ref,
            text=text,
            options=_offered_options(native.get("options"), ref, index),
            activity=question_activity,
        )
        header, multiple = native.get("header"), native.get("multiSelect")
        if isinstance(header, str):
            question["header"] = header
        if isinstance(multiple, bool):
            question["multiple_selections"] = multiple
        questions.append(question)
    return questions


def _association(
    questions: list[Question], key: str, evidence: list[QuestionEvidence]
) -> QuestionAssociation | None:
    matches = [question for question in questions if question["text"] == key]
    if len(matches) != 1:
        return None
    return QuestionAssociation(
        question_ref=matches[0]["ref"],
        evidence=evidence + matches[0]["activity"]["evidence"],
    )


def _selections(value: str, question: Question) -> list[QuestionSelection]:
    """Only an exact, unambiguous whole-label partition establishes selections."""
    options = question["options"]
    labels = [option["label"] for option in options]
    chosen = [value] if value in labels else value.split(", ")
    if len(chosen) > 1 and question.get("multiple_selections") is not True:
        return []
    if len(chosen) != len(set(chosen)) or any(
        labels.count(label) != 1 for label in chosen
    ):
        return []
    selections: list[QuestionSelection] = []
    for label in chosen:
        option = options[labels.index(label)]
        selections.append(QuestionSelection(label=label, option_ref=option["ref"]))
    return selections


def _result_text(content: JsonValue) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        text = block.get("text")
        if isinstance(text, str):
            parts.append(text)
    return " ".join(parts)


def _denial_evidence(record: ToolResult) -> QuestionEvidence | None:
    kind = record.get("native_denial_kind")
    if kind:
        return QuestionEvidence(field="toolDenialKind", value=kind)
    if record.get("native_result") == _REJECTED_RESULT:
        return QuestionEvidence(field="toolUseResult", value=_REJECTED_RESULT)
    content = _result_text(record.get("content"))
    if content.startswith(_REJECTED_PREFIX):
        return QuestionEvidence(
            field="tool_result.content", value=record.get("content")
        )
    return None


class _ClaudeQuestionInterpreter:
    """Assemble question occurrences and evidenced responses for one session."""

    def __init__(self) -> None:
        self.questions: list[Question] = []
        self.responses: list[QuestionResponse] = []

    def answers(
        self,
        payload: dict[str, JsonValue],
        questions: list[Question],
        activity: QuestionActivityReference | None,
    ) -> None:
        answers = payload.get("answers")
        annotations = payload.get("annotations")
        if not isinstance(answers, dict):
            answers = {}
        keys = list(answers)
        if isinstance(annotations, dict):
            keys.extend(key for key in annotations if key not in answers)
        for key in keys:
            value = answers.get(key)
            field = (
                "toolUseResult.answers key"
                if key in answers
                else "toolUseResult.annotations key"
            )
            evidence = [QuestionEvidence(field=field, value=key)]
            if activity is not None:
                evidence = activity["evidence"] + evidence
            association = _association(questions, key, evidence)
            annotation = annotations.get(key) if isinstance(annotations, dict) else None
            self.answer(value, association, annotation, activity)

    def answer(
        self,
        value: JsonValue,
        association: QuestionAssociation | None,
        annotation: JsonValue,
        activity: QuestionActivityReference | None,
    ) -> None:
        response = QuestionResponse(
            ref=f"claude/response/{len(self.responses)}",
            question=association,
            outcome=QUESTION_UNKNOWN,
        )
        if activity is not None:
            response["activity"] = activity
        if isinstance(value, str):
            self.answer_content(response, value)
        if isinstance(annotation, dict):
            notes, preview = annotation.get("notes"), annotation.get("preview")
            if isinstance(notes, str):
                response["candidate_feedback"] = notes
                if notes:
                    response["outcome"] = QUESTION_ANSWERED
            if isinstance(preview, str):
                response["selected_preview"] = preview
        self.responses.append(response)

    def answer_content(self, response: QuestionResponse, value: str) -> None:
        if value == _NOTES_ONLY:
            return
        if value:
            response["outcome"] = QUESTION_ANSWERED
        association = response["question"]
        matching = [
            question
            for question in self.questions
            if association is not None
            and question["ref"] == association["question_ref"]
        ]
        selections = _selections(value, matching[0]) if matching else []
        if selections:
            response["selections"] = selections
        else:
            response["free_text"] = value

    def result(
        self,
        record: ToolResult,
        questions: list[Question],
        activity: QuestionActivityReference | None,
    ) -> None:
        payload = record.get("native_result")
        if isinstance(payload, dict) and (
            "answers" in payload or "annotations" in payload
        ):
            self.answers(payload, questions, activity)
            return
        denial = _denial_evidence(record)
        if denial is not None:
            self.refusal(record, questions, activity, denial)
            return
        if record["is_error"]:
            return
        self.text_answers(_result_text(record.get("content")), questions, activity)

    def refusal(
        self,
        record: ToolResult,
        questions: list[Question],
        activity: QuestionActivityReference | None,
        denial: QuestionEvidence,
    ) -> None:
        refusal = QuestionRefusal(by=REFUSAL_UNKNOWN)
        if denial["field"] != "toolDenialKind" or denial["value"] == _USER_REJECTED:
            refusal["by"] = REFUSAL_CANDIDATE
        elif record.get("native_denial_kind") == _SETTINGS_DENIED:
            refusal["by"] = REFUSAL_SETTINGS
        evidence = [denial]
        if activity is not None:
            evidence = activity["evidence"] + evidence
        association = None
        if len(questions) == 1:
            association = QuestionAssociation(
                question_ref=questions[0]["ref"], evidence=evidence
            )
        response = QuestionResponse(
            ref=f"claude/response/{len(self.responses)}",
            question=association,
            outcome=QUESTION_REFUSED,
            refusal=refusal,
        )
        content = _result_text(record.get("content"))
        if content:
            response["refusal"]["explanation"] = content
        if activity is not None:
            response["activity"] = activity
        self.responses.append(response)

    def text_answers(
        self,
        text: str,
        questions: list[Question],
        activity: QuestionActivityReference | None,
    ) -> None:
        """Recognize explicit native answer pairs, never a first-value fallback."""
        if not text.startswith(_TEXT_PREFIXES):
            return
        for prefix in _TEXT_PREFIXES:
            text = text.removeprefix(prefix)
        for suffix in _TEXT_SUFFIXES:
            if text.endswith(suffix):
                text = text.removesuffix(suffix)
                break
        else:
            text = text.removesuffix(".")
        for match in _TEXT_ANSWER.finditer(text):
            key, value, annotation_text = match.groups()
            evidence = [
                QuestionEvidence(
                    field="tool_result.content answer pair", value=match.group()
                )
            ]
            if activity is not None:
                evidence = activity["evidence"] + evidence
            association = _association(questions, key, evidence)
            self.answer(
                _NOTES_ONLY if value is None else value,
                association,
                _text_annotation(annotation_text),
                activity,
            )


def _result_activity(
    call: ToolCall, activity: QuestionActivityReference, counts: Counter[str | None]
) -> QuestionActivityReference | None:
    record = call["result_record"]
    call_id = call.get("id")
    if not call_id or counts[call_id] != 1 or record["tool_call_id"] != call_id:
        return None
    return QuestionActivityReference(
        message_i=activity["message_i"],
        tool_call_index=activity["tool_call_index"],
        evidence=[
            QuestionEvidence(field="tool_use.id", value=call_id),
            QuestionEvidence(
                field="tool_result.tool_use_id", value=record["tool_call_id"]
            ),
        ],
    )


def _interpret_questions(
    events: list[Message], unmatched: list[ToolResult]
) -> tuple[list[Question], list[QuestionResponse]]:
    interpreter = _ClaudeQuestionInterpreter()
    counts = Counter(
        call.get("id") for event in events for call in event.get("tool_calls") or []
    )
    alternate_entry_ids = {
        record["id"]
        for record in unmatched
        if record.get("id") and isinstance(record.get("content"), dict)
    }
    for message in events:
        for index, call in enumerate(message.get("tool_calls") or []):
            if call["name"] != ASK_USER_QUESTION:
                continue
            activity = QuestionActivityReference(
                message_i=message["i"],
                tool_call_index=index,
                evidence=[
                    QuestionEvidence(
                        field="message.tool_calls containment", value=index
                    )
                ],
            )
            questions = _offered_questions(call, activity)
            interpreter.questions.extend(questions)
            record = call.get("result_record")
            if record is None or record.get("id") in alternate_entry_ids:
                continue
            result_activity = _result_activity(call, activity, counts)
            interpreter.result(
                record,
                questions if result_activity is not None else [],
                result_activity,
            )
    for record in unmatched:
        payload = record.get("native_result", record.get("content"))
        if isinstance(payload, dict) and (
            "answers" in payload or "annotations" in payload
        ):
            interpreter.answers(payload, [], None)
        elif record.get("id") not in alternate_entry_ids:
            interpreter.result(record, [], None)
    return interpreter.questions, interpreter.responses
