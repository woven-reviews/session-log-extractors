"""Interpret Copilot's structured human responses without parsing rendered prose."""

from session_log_extractors.json_types import JsonValue, string_value
from session_log_extractors.session_model import (
    PlanDecision,
    SubagentSummary,
    ToolCall,
)


def apply_human_response(call: ToolCall, data: dict[str, JsonValue]) -> None:
    response = data.get("response")
    if not isinstance(response, dict):
        return
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
