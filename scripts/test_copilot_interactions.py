"""Controlled malformed and legacy inputs supplement the interactive capture."""

from copilot_fixture import native_source

from session_log_extractors.copilot_interactions import (
    subagent_summaries,
)


def test_inline_child_summary_uses_only_the_recorded_agent_scope():
    source = native_source()
    starts = []
    for record in source.state_records:
        if record["type"] == "subagent.started":
            starts.append(record)
    assert len(starts) == 1, "Missing bounded subagent evidence"
    (start,) = starts
    agent_id = start["agentId"]
    results = []
    tool_names = []
    for record in source.state_records:
        if record.get("agentId") != agent_id:
            continue
        if record["type"] == "assistant.message" and record["data"].get("content"):
            results.append(record["data"]["content"])
        if record["type"] == "tool.execution_start":
            tool_names.append(record["data"]["toolName"])

    summaries = subagent_summaries(source.state_records)

    assert set(summaries) == {agent_id}
    summary = summaries[agent_id]
    assert summary["task"] == start["data"]["agentDescription"]
    assert summary["tool_count"] == len(tool_names)
    assert set(summary["tool_names"]) == set(tool_names)
    assert isinstance(summary["result"], str) and summary["result"]
    assert summary["result"] == results[-1]
