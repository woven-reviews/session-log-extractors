"""The explicitly selected current Copilot capture, shared by offline tests."""

import json
from pathlib import Path

from session_log_extractors import copilot_sources as sources

ROOT = Path(__file__).parent / "fixtures" / "copilot" / "1.0.104-3"
STATE_ROOT = ROOT / "native" / "session-state"
(TRANSCRIPT,) = STATE_ROOT.glob("*/events.jsonl")
SESSION_ID = TRANSCRIPT.parent.name
DATABASE = ROOT / "native" / "session-store.db"
ASSETS = ROOT / "assets"


def native_records():
    return sources.parse_state_records(TRANSCRIPT.read_text().splitlines())


def native_source():
    """Load the whole selected session without consulting the operator's store."""
    rows = json.loads((ROOT / "native" / "session-store.json").read_text())
    (session,) = rows["sessions"]
    models = []
    for usage in rows["assistant_usage_events"]:
        model = usage["model"]
        if model and model not in models:
            models.append(model)
    files = sorted(rows["session_files"], key=lambda row: row["first_seen_at"])
    return sources.CopilotSource(
        session,
        rows["turns"],
        files=files,
        state_records=native_records(),
        models=models,
    )


def recorded_calls(session):
    calls = {}
    for message in session["events"]:
        for call in message.get("tool_calls", []):
            if "id" in call:
                calls[call["id"]] = call
    return calls


def native_tool_source():
    """Select the operator-requested stdout/stderr call and its native owners."""
    source = native_source()
    requests = []
    for record in source.state_records:
        if record["type"] != "assistant.message":
            continue
        for request in record["data"].get("toolRequests", []):
            arguments = request.get("arguments", {})
            if request["name"] == "bash" and "CAPTURE_STDOUT_OK" in arguments.get(
                "command", ""
            ):
                requests.append((record, request))
    assert len(requests) == 1, "Expected one stdout/stderr call"
    ((owner, request),) = requests
    message_id = owner["data"]["originatingMessageId"]
    selected = []
    for record in source.state_records:
        data = record["data"]
        if record["type"] == "user.message" and data.get("messageId") == message_id:
            selected.append(record)
        elif record == owner or data.get("toolCallId") == request["toolCallId"]:
            selected.append(record)
        elif record["type"] == "assistant.message":
            if data.get("originatingMessageId") == message_id and not data.get(
                "toolRequests"
            ):
                selected.append(record)
    source.state_records = selected
    source.files = []
    return source


def single_image_source():
    """Narrow native two-image input to its red attachment for legacy boundaries."""
    source = native_source()
    assets = []
    users = []
    for record in source.state_records:
        if record["type"] == "session.binary_asset":
            assets.append(record)
        if (
            record["type"] == "user.message"
            and len(record["data"].get("attachments", [])) == 2
        ):
            users.append(record)
    assert len(users) == 1, "Expected one native two-image message"
    (user,) = users
    user["data"]["attachments"] = user["data"]["attachments"][:1]
    source.state_records = [user, *assets]
    source.files = []
    return source


def native_prose_source():
    """Select a tool-free recall exchange for focused fallback boundary tests."""
    source = native_source()
    users = []
    for record in source.state_records:
        if record["type"] == "user.message" and record["data"]["content"].startswith(
            "Without tools, recall"
        ):
            users.append(record)
    assert len(users) == 1, "Expected one operator recall request"
    (user,) = users
    selected = [user]
    for record in source.state_records:
        if record["type"] == "assistant.message":
            if record["data"].get("originatingMessageId") == user["data"]["messageId"]:
                selected.append(record)
    source.state_records = selected
    source.files = []
    return source
