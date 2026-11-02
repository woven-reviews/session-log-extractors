"""Interpret Copilot elicitation records against canonical, ID-scoped calls."""

import json
from dataclasses import dataclass
from dataclasses import field as dataclass_field

from session_log_extractors.copilot_protocol import (
    ASK_USER,
    ELICITATION_CANCEL,
    HUMAN_RESPONSE_RECORDED,
    TOOL_EXECUTION_COMPLETE,
)
from session_log_extractors.json_types import JsonValue, string_value
from session_log_extractors.session_model import (
    Message,
    Question,
    QuestionActivityReference,
    QuestionAssociation,
    QuestionChoice,
    QuestionEvidence,
    QuestionResponse,
    QuestionSelection,
    ToolCall,
)


@dataclass
class _QuestionGroup:
    questions: dict[str, Question]
    call: ToolCall
    activity: QuestionActivityReference


@dataclass
class CopilotQuestionInterpretation:
    """Question facts and result-only activity indexed by native record order."""

    questions: list[Question] = dataclass_field(default_factory=list)
    responses: list[QuestionResponse] = dataclass_field(default_factory=list)
    unassociated_results: dict[int, ToolCall] = dataclass_field(default_factory=dict)


def _schema_questions(call: ToolCall, prefix: str) -> dict[str, Question]:
    payload = call.get("input")
    if not isinstance(payload, dict):
        return {}
    schema = payload.get("requestedSchema")
    properties = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(properties, dict):
        return {}
    questions: dict[str, Question] = {}
    for name, field in properties.items():
        if not isinstance(field, dict):
            continue
        ref = f"{prefix}:{len(questions)}"
        question = Question(
            ref=ref,
            native_id=name,
            text=string_value(payload.get("message")) or "",
            options=_schema_options(field, ref),
        )
        title = string_value(field.get("title"))
        if title is not None:
            question["header"] = title
        description = string_value(field.get("description"))
        if description is not None:
            question["description"] = description
        if field.get("type") in ("string", "array"):
            question["multiple_selections"] = field["type"] == "array"
        questions[name] = question
    return questions


def _schema_options(field: dict[str, JsonValue], ref: str) -> list[QuestionChoice]:
    items = field.get("items")
    choices = (
        items if field.get("type") == "array" and isinstance(items, dict) else field
    )
    labels = choices.get("enum")
    options = []
    if isinstance(labels, list):
        for index, label in enumerate(labels):
            if isinstance(label, str):
                options.append(QuestionChoice(ref=f"{ref}:o{index}", label=label))
    return options


class _CopilotQuestionInterpreter:
    """Index question occurrences before interpreting responses, in any order."""

    def __init__(self, events: list[Message]):
        self.interpretation = CopilotQuestionInterpretation()
        self.groups: dict[tuple[str | None, str], list[_QuestionGroup]] = {}
        for event in events:
            for index, call in enumerate(event.get("tool_calls") or []):
                if call["name"] == ASK_USER:
                    self.add_call(event, call, index)

    def add_call(self, event: Message, call: ToolCall, index: int) -> None:
        questions = _schema_questions(
            call, f"copilot:q{len(self.interpretation.questions)}"
        )
        call["interaction_questions"] = list(questions.values())
        call["question_responses"] = []
        evidence = [QuestionEvidence(field="tool.name", value=ASK_USER)]
        call_id = call.get("id")
        if call_id is not None:
            evidence.append(QuestionEvidence(field="toolCallId", value=call_id))
        activity = QuestionActivityReference(
            message_i=event["i"], tool_call_index=index, evidence=evidence
        )
        for question in questions.values():
            question["activity"] = activity
            self.interpretation.questions.append(question)
        if call_id is not None:
            scope = (call.get("agent_id", event.get("agent_id")), call_id)
            self.groups.setdefault(scope, []).append(
                _QuestionGroup(questions, call, activity)
            )

    def group(self, record: dict[str, JsonValue]) -> _QuestionGroup | None:
        data = record.get("data")
        call_id = (
            string_value(data.get("toolCallId")) if isinstance(data, dict) else None
        )
        if call_id is None:
            return None
        matches = self.groups.get((string_value(record.get("agentId")), call_id), [])
        return matches[0] if len(matches) == 1 else None

    def response(
        self,
        record: dict[str, JsonValue],
        question: Question | None,
        group: _QuestionGroup | None,
        position: int,
    ) -> QuestionResponse:
        response = QuestionResponse(
            ref=f"copilot:r{len(self.interpretation.responses)}",
            question=None,
            outcome="unknown",
        )
        native_id = string_value(record.get("id"))
        if native_id is not None:
            response["native_id"] = native_id
        if question is not None:
            evidence = list(question["activity"]["evidence"])
            response["question"] = QuestionAssociation(
                question_ref=question["ref"], evidence=evidence
            )
        self.interpretation.responses.append(response)
        if group is not None:
            response["activity"] = group.activity
            group.call["question_responses"].append(response)
        else:
            self.unassociated_call(record, position)["question_responses"].append(
                response
            )
        return response

    def unassociated_call(
        self, record: dict[str, JsonValue], position: int
    ) -> ToolCall:
        results = self.interpretation.unassociated_results
        if position not in results:
            # This is recorded response-only activity, without a fabricated
            # request ID, input, or question association.
            call = ToolCall(
                name=ASK_USER,
                result=json.dumps(record.get("data"), ensure_ascii=False),
                question_responses=[],
            )
            agent_id = string_value(record.get("agentId"))
            if agent_id is not None:
                call["agent_id"] = agent_id
            results[position] = call
        return results[position]

    def answer(
        self, record: dict[str, JsonValue], native: dict[str, JsonValue], position: int
    ) -> None:
        group = self.group(record)
        content = native.get("content")
        if not isinstance(content, dict) or not content:
            response = self.response(record, None, group, position)
            response["recorded_answer"] = content
            _apply_feedback(response, native)
            return
        for name, value in content.items():
            question = group.questions.get(name) if group is not None else None
            response = self.response(record, question, group, position)
            _apply_answer(response, question, value)
            association = response["question"]
            if association is not None:
                association["evidence"].append(
                    QuestionEvidence(field=f"response.content.{name}", value=value)
                )
            _apply_feedback(response, native)

    def cancellation(
        self, record: dict[str, JsonValue], data: dict[str, JsonValue], position: int
    ) -> None:
        telemetry = data.get("toolTelemetry")
        properties = (
            telemetry.get("properties") if isinstance(telemetry, dict) else None
        )
        if (
            not isinstance(properties, dict)
            or properties.get("elicitation_action") != ELICITATION_CANCEL
        ):
            return
        group = self.group(record)
        question = None
        # Cancellation concerns the whole request. Only a single schema property
        # establishes a particular question; do not distribute it by question count.
        if group is not None and len(group.questions) == 1:
            question = next(iter(group.questions.values()))
        response = self.response(record, question, group, position)
        response["outcome"] = "refused"
        response["refusal"] = {"by": "candidate"}
        result = data.get("result")
        explanation = (
            string_value(result.get("content")) if isinstance(result, dict) else None
        )
        if explanation is not None:
            response["refusal"]["explanation"] = explanation
        association = response["question"]
        if association is not None:
            association["evidence"].append(
                QuestionEvidence(
                    field="toolTelemetry.properties.elicitation_action",
                    value=ELICITATION_CANCEL,
                )
            )


def _apply_feedback(response: QuestionResponse, native: dict[str, JsonValue]) -> None:
    feedback = string_value(native.get("feedback"))
    if feedback is not None:
        response["candidate_feedback"] = feedback


def _answer_values(value: JsonValue) -> list[str] | None:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        values = []
        for item in value:
            if not isinstance(item, str):
                return None
            values.append(item)
        return values
    return None


def _apply_answer(
    response: QuestionResponse, question: Question | None, value: JsonValue
) -> None:
    response["recorded_answer"] = value
    values = _answer_values(value)
    if values is None:
        return
    response["outcome"] = "answered"
    selections: list[QuestionSelection] = []
    free_text = []
    for label in values:
        options = []
        for option in question["options"] if question is not None else []:
            if option["label"] == label:
                options.append(option)
        if options:
            selection = QuestionSelection(label=label)
            if len(options) == 1:
                selection["option_ref"] = options[0]["ref"]
            selections.append(selection)
        else:
            free_text.append(label)
    response["selections"] = selections
    if free_text:
        response["free_text"] = "\n".join(free_text)


def interpret_questions(
    events: list[Message], records: list[dict[str, JsonValue]]
) -> CopilotQuestionInterpretation:
    """Retain each question and response without guessing missing associations."""
    interpreter = _CopilotQuestionInterpreter(events)
    for position, record in enumerate(records):
        data = record.get("data")
        if not isinstance(data, dict):
            continue
        if record.get("type") == HUMAN_RESPONSE_RECORDED:
            response = data.get("response")
            if isinstance(response, dict) and response.get("responseKind") == ASK_USER:
                interpreter.answer(record, response, position)
        elif record.get("type") == TOOL_EXECUTION_COMPLETE:
            interpreter.cancellation(record, data, position)
    return interpreter.interpretation
