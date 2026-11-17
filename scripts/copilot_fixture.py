"""The explicitly selected current Copilot capture, shared by offline tests."""

from pathlib import Path

from session_log_extractors import copilot_protocol
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
    return sources.read_source(TRANSCRIPT)


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
        if record["type"] != copilot_protocol.ASSISTANT_MESSAGE:
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
        if (
            record["type"] == copilot_protocol.USER_MESSAGE
            and data.get("messageId") == message_id
        ):
            selected.append(record)
        elif record == owner or data.get("toolCallId") == request["toolCallId"]:
            selected.append(record)
        elif record["type"] == copilot_protocol.ASSISTANT_MESSAGE:
            if data.get("originatingMessageId") == message_id and not data.get(
                "toolRequests"
            ):
                selected.append(record)
    source.state_records = selected
    return source


def single_image_source():
    """Narrow native two-image input to its red attachment for legacy boundaries."""
    source = native_source()
    assets = []
    users = []
    for record in source.state_records:
        if record["type"] == copilot_protocol.BINARY_ASSET:
            assets.append(record)
        if (
            record["type"] == copilot_protocol.USER_MESSAGE
            and len(record["data"].get("attachments", [])) == 2
        ):
            users.append(record)
    assert len(users) == 1, "Expected one native two-image message"
    (user,) = users
    user["data"]["attachments"] = user["data"]["attachments"][:1]
    source.state_records = [user, *assets]
    return source


def native_prose_source():
    """Select a tool-free recall exchange for focused fallback boundary tests."""
    source = native_source()
    users = []
    for record in source.state_records:
        if record["type"] == copilot_protocol.USER_MESSAGE and record["data"][
            "content"
        ].startswith("Without tools, recall"):
            users.append(record)
    assert len(users) == 1, "Expected one operator recall request"
    (user,) = users
    selected = [user]
    for record in source.state_records:
        if record["type"] == copilot_protocol.ASSISTANT_MESSAGE:
            if record["data"].get("originatingMessageId") == user["data"]["messageId"]:
                selected.append(record)
    source.state_records = selected
    return source


def write_events_log(state_root: Path, session_id: str, lines: list[str]) -> Path:
    """Write supplied lines under an explicit isolated state directory."""
    session_dir = state_root / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    path = session_dir / "events.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
