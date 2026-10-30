"""Interpret Copilot's structured human responses without parsing rendered prose."""

from session_log_extractors.json_types import JsonValue, string_value
from session_log_extractors.question_types import QuestionOption
from session_log_extractors.session_model import (
    PlanDecision,
    QuestionAnswer,
    SubagentSummary,
    ToolCall,
)
from session_log_extractors.tool_inputs import ToolInput


def questions_from_schema(payload: ToolInput) -> list[QuestionAnswer]:
    if not isinstance(payload, dict):
        return []
    schema = payload.get("requestedSchema")
    if not isinstance(schema, dict):
        return []
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return []
    questions = []
    for name, field in properties.items():
        if isinstance(field, dict):
            questions.append(
                _question(name, field, string_value(payload.get("message")) or "")
            )
    return questions


def _question(name: str, field: dict[str, JsonValue], message: str) -> QuestionAnswer:
    items = field.get("items")
    choices = (
        items if field.get("type") == "array" and isinstance(items, dict) else field
    )
    labels = choices.get("enum")
    options = []
    if isinstance(labels, list):
        for label in labels:
            if isinstance(label, str):
                options.append(QuestionOption(label=label))
    return QuestionAnswer(
        field_name=name,
        header=string_value(field.get("title")) or name,
        question=message,
        options=options,
    )


def apply_human_response(call: ToolCall, data: dict[str, JsonValue]) -> None:
    response = data.get("response")
    if not isinstance(response, dict):
        return
    if call["name"] == "ask_user" and response.get("responseKind") == "ask_user":
        _answer_questions(call, response)
    if (
        call["name"] == "exit_plan_mode"
        and response.get("responseKind") == "exit_plan_mode"
    ):
        call["plan_decision"] = _plan_decision(response)


def apply_permission_refusal(call: ToolCall, data: dict[str, JsonValue]) -> None:
    """A human refusal requires the native decision and its recorded call ID."""
    result = data.get("result")
    if (
        not isinstance(result, dict)
        or result.get("kind") != "denied-interactively-by-user"
    ):
        return
    call["permission_denial"] = {
        "tool": call["name"],
        "message": string_value(result.get("feedback")) or "",
        "by": "user",
    }


def apply_configured_refusal(call: ToolCall) -> None:
    """Use Copilot's tool-result refusal prefix, never assistant explanation."""
    error = call.get("error")
    result = error.get("message") if isinstance(error, dict) else call.get("result")
    if isinstance(result, str) and result.startswith(
        "Permission to run this tool was denied due to the following rules:"
    ):
        call["permission_denial"] = {
            "tool": call["name"],
            "message": result,
            "by": "settings",
        }


def _answer_questions(call: ToolCall, response: dict[str, JsonValue]) -> None:
    answers = response.get("content")
    if not isinstance(answers, dict):
        return
    for question in call.get("questions", []):
        value = answers.get(question.get("field_name", ""))
        answer = _recorded_answer(value)
        if answer is None:
            continue
        question["recorded_answer"] = answer
        question["answer_text"] = (
            answer if isinstance(answer, str) else ", ".join(answer)
        )
        values = [answer] if isinstance(answer, str) else answer
        labels = []
        for option in question["options"]:
            label = option.get("label")
            if label is not None and label in values:
                labels.append(label)
        question["chosen_labels"] = labels


def _recorded_answer(value: JsonValue) -> str | list[str] | None:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        answers = []
        for item in value:
            if not isinstance(item, str):
                return None
            answers.append(item)
        return answers
    return None


def _plan_decision(response: dict[str, JsonValue]) -> PlanDecision:
    approved = response.get("approved")
    decision = "unknown"
    if approved is True:
        decision = "approved"
    elif approved is False:
        decision = "rejected"
    return PlanDecision(
        plan=string_value(response.get("planContent")) or "",
        decision=decision,
        edited_plan="",
        message=string_value(response.get("feedback")) or "",
    )


def subagent_summaries(
    records: list[dict[str, JsonValue]],
) -> dict[str, SubagentSummary]:
    """Summarize inline child records separately, keyed by recorded agent ID."""
    summaries: dict[str, SubagentSummary] = {}
    for record in records:
        data = record.get("data")
        agent_id = string_value(record.get("agentId"))
        if (
            record.get("type") == "subagent.started"
            and agent_id
            and isinstance(data, dict)
        ):
            summaries[agent_id] = SubagentSummary(
                agent_type=string_value(data.get("agentName")) or "",
                start_ts=string_value(record.get("timestamp")),
                task=string_value(data.get("agentDescription")) or "",
                tool_count=0,
                tool_names=[],
                result="",
            )
    for record in records:
        agent_id = string_value(record.get("agentId"))
        data = record.get("data")
        if agent_id is None or agent_id not in summaries or not isinstance(data, dict):
            continue
        summary = summaries[agent_id]
        if record.get("type") == "tool.execution_start":
            summary["tool_count"] += 1
            name = string_value(data.get("toolName"))
            if name and name not in summary["tool_names"]:
                summary["tool_names"].append(name)
        if record.get("type") == "assistant.message":
            text = string_value(data.get("content"))
            if text:
                summary["result"] = text
    return summaries
