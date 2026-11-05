"""Summarize recorded Copilot child activity within its agent scope."""

from session_log_extractors.copilot_protocol import (
    ASSISTANT_MESSAGE,
    SUBAGENT_STARTED,
    TOOL_EXECUTION_START,
)
from session_log_extractors.json_types import JsonValue, string_value
from session_log_extractors.session_model import (
    SubagentSummary,
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
            record.get("type") == SUBAGENT_STARTED
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
        if record.get("type") == TOOL_EXECUTION_START:
            summary["tool_count"] += 1
            name = string_value(data.get("toolName"))
            if name and name not in summary["tool_names"]:
                summary["tool_names"].append(name)
        if record.get("type") == ASSISTANT_MESSAGE:
            text = string_value(data.get("content"))
            if text:
                summary["result"] = text
    return summaries
