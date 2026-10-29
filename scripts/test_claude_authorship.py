"""Claude provenance uses recorded fields, never role or wrapper spelling."""

import json
from copy import deepcopy
from pathlib import Path

import pytest

from session_log_extractors import extract_claude_session_log as claude
from session_log_extractors.claude_sources import ClaudeSource
from session_log_extractors.session_log_envelope import export_session

FIXTURES = Path(__file__).parent / "fixtures" / "claude"


def _interpret(entries):
    return claude.interpret_source(ClaudeSource(Path("session.jsonl"), entries))


def test_native_injected_expansions_reach_production_json(capsys):
    path = FIXTURES / "skill_command.jsonl"
    entries = claude.load_entries(path)
    session = _interpret(entries)
    original = deepcopy(session)
    users = [
        e
        for e in entries
        if e.get("type") == "user" and not claude._is_tool_result_entry(e)
    ]
    messages = [e for e in session["events"] if e["role"] == "user"]
    visible = [
        e
        for e in entries
        if e in users
        or (
            e.get("type") == "assistant"
            and any(
                isinstance(b, dict) and b.get("type") in ("text", "tool_use")
                for b in claude._content_blocks(e)
            )
        )
    ]
    assert [m["id"] for m in session["events"]] == [e["uuid"] for e in visible]
    assert [m["ts"] for m in session["events"]] == [e["timestamp"] for e in visible]
    assert len(messages) == len(users)
    for native, message in zip(users, messages, strict=True):
        assert message["text"] == claude._human_text(native)
        assert message["id"] == native["uuid"]
        assert message["ts"] == native["timestamp"]
        span = message["authorship"][0]
        assert span["start"] == 0 and span["end"] == len(message["text"])
        assert span["ts"] == native["timestamp"]
        assert span["source"] == (
            "harness" if native.get("isMeta") is True else "unknown"
        )
        if native.get("isMeta") is True:
            assert {"field": "isMeta", "value": True} in span["markers"]
    injected = [m for m in messages if m["authorship"][0]["source"] == "harness"]
    assert len(injected) == 2
    assert "SKILL_FIXTURE_MARKER" in injected[0]["text"]
    assert "COMMAND_FIXTURE_MARKER" in injected[1]["text"]
    call = next(
        c
        for m in session["events"]
        for c in m.get("tool_calls", [])
        if c["name"] == "Skill"
    )
    assert {"field": "sourceToolUseID", "value": call["id"]} in injected[0][
        "authorship"
    ][0]["markers"]
    assert call["result_record"]["tool_call_id"] == call["id"]
    assert claude.main(["--transcript", str(path), "--output", "-"]) == 0
    decoded = json.loads(capsys.readouterr().out)
    assert decoded["events"] == export_session(session)["events"]
    assert [e["authorship"] for e in decoded["events"] if e["role"] == "user"] == [
        m["authorship"] for m in messages
    ]
    assert session == original


@pytest.mark.parametrize("is_meta", [True, False, None, "true"])
@pytest.mark.parametrize("timestamp", ["recorded-time", None, "missing"])
def test_unicode_wrappers_keep_offsets_and_only_exact_meta_is_evidence(
    is_meta, timestamp
):
    # Replace only a native expansion's text and provenance values to exercise
    # Unicode/whitespace and absent/null timestamps without inventing a schema.
    native = deepcopy(
        next(
            e
            for e in claude.load_entries(FIXTURES / "skill_command.jsonl")
            if e.get("isMeta") is True
        )
    )
    text = "  Hi 🙂e\u0301\n<system-reminder>context</system-reminder>\n\n\n"
    native["message"]["content"][0]["text"] = text
    native["isMeta"] = is_meta
    if timestamp == "missing":
        native.pop("timestamp")
    else:
        native["timestamp"] = timestamp
    message = _interpret([native])["events"][0]
    assert message["text"] == text
    span = message["authorship"][0]
    assert span["start"] == 0 and span["end"] == len(text)
    assert span["source"] == ("harness" if is_meta is True else "unknown")
    assert {"field": "isMeta", "value": is_meta} in span["markers"]
    if timestamp == "missing":
        assert "ts" not in span
    else:
        assert span["ts"] == timestamp
    assert export_session(_interpret([native]))["events"][0]["authorship"] == [span]


@pytest.mark.parametrize(
    "text",
    [
        "<task-notification>context</task-notification>",
        "<bash-stdout>output</bash-stdout>",
        "<command-name>/fixture-command</command-name>",
        "  plain 🙂  ",
    ],
)
def test_unmarked_external_text_is_preserved_and_unknown(text):
    native = deepcopy(
        next(
            e
            for e in claude.load_entries(FIXTURES / "skill_command.jsonl")
            if e.get("type") == "user"
        )
    )
    native["message"]["content"] = text
    message = _interpret([native])["events"][0]
    assert message["text"] == text
    assert message["authorship"][0]["source"] == "unknown"
    assert {"field": "userType", "value": "external"} in message["authorship"][0][
        "markers"
    ]


def test_image_only_meta_keeps_ownership_without_empty_ranges():
    images = claude.load_entries(FIXTURES / "images.jsonl")[0]["message"]["content"]
    native = next(
        e
        for e in claude.load_entries(FIXTURES / "skill_command.jsonl")
        if e.get("isMeta") is True
    )
    # Add the existing image fixture's blocks to a native injected message, then
    # remove only its text to exercise image-only activity with a recorded ID.
    native["message"]["content"].extend(b for b in images if b.get("type") == "image")
    entries = [native]
    original = _interpret(entries)
    native["message"]["content"] = [
        b for b in native["message"]["content"] if b.get("type") == "image"
    ]
    session = _interpret(entries)
    message = session["events"][0]
    assert message["text"] == ""
    assert not message.get("authorship")
    assert message["images"] == original["events"][0]["images"]
    assert message["id"] == original["events"][0]["id"]
    assert message["ts"] == original["events"][0]["ts"]
    assert export_session(session)["events"][0]["images"] == message["images"]
