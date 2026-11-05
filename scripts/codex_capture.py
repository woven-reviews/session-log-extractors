"""Explicit selection and small native-record helpers for the Codex test capture."""

from pathlib import Path

from session_log_extractors import codex_protocol
from session_log_extractors.codex_sources import load_entries

ROOT = Path(__file__).parent / "fixtures" / "codex" / "0.166.0"
TRANSCRIPT = (
    ROOT
    / "native"
    / "rollout-2026-10-30T15-49-18-01a18e5c-0900-7111-91f6-195916aa5e3c.jsonl"
)
CHILD_TRANSCRIPT = (
    ROOT
    / "native"
    / "rollout-2026-10-30T15-53-14-01a18e5f-a3ec-7360-a6f5-c31e4e8ebb77.jsonl"
)
ASSETS = ROOT / "assets"


def response_items(entries, kind):
    matches = []
    for entry in entries:
        if (
            entry.get("type") == codex_protocol.RESPONSE_ITEM
            and entry["payload"].get("type") == kind
        ):
            matches.append(entry)
    assert matches, f"Capture lacks required response items: {kind}"
    return matches


def human_message(entries):
    for entry in response_items(entries, "message"):
        payload = entry["payload"]
        metadata = payload.get("internal_chat_message_metadata_passthrough", {})
        if payload.get("role") == "user" and metadata.get("content_item_kinds") == [
            codex_protocol.USER_TEXT
        ]:
            return entry
    raise AssertionError("Capture lacks a native human text message")


def image_message(entries):
    for entry in response_items(entries, "message"):
        payload = entry["payload"]
        if payload.get("role") != "user":
            continue
        images = []
        for block in payload.get("content", []):
            if block.get("type") == codex_protocol.INPUT_IMAGE:
                images.append(block)
        if len(images) == 2:
            return entry
    raise AssertionError("Capture lacks the two-image native user message")


def exec_interactions():
    """Two recorded same-name calls and their ID-matched results for boundaries."""
    entries = load_entries(TRANSCRIPT)
    calls = []
    for entry in response_items(entries, codex_protocol.CUSTOM_TOOL_CALL):
        if entry["payload"]["name"] == "exec":
            calls.append(entry)
            if len(calls) == 2:
                break
    assert len(calls) == 2
    results = []
    for call in calls:
        for entry in response_items(entries, codex_protocol.CUSTOM_TOOL_CALL_OUTPUT):
            if entry["payload"]["call_id"] == call["payload"]["call_id"]:
                results.append(entry)
                break
    assert len(results) == 2
    return [human_message(entries), *calls, *results]


def question_interaction():
    """One native request/answer pair, without unrelated calls in the full session."""
    entries = load_entries(TRANSCRIPT)
    for call in response_items(entries, codex_protocol.FUNCTION_CALL):
        if call["payload"]["name"] != codex_protocol.REQUEST_USER_INPUT:
            continue
        for result in response_items(entries, codex_protocol.FUNCTION_CALL_OUTPUT):
            if result["payload"]["call_id"] == call["payload"]["call_id"]:
                return [human_message(entries), call, result]
    raise AssertionError("Capture lacks an ID-matched structured question result")
