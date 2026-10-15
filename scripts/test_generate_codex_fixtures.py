"""Native Codex fixture checks; regeneration never runs in tests."""

import json
from pathlib import Path

import generate_codex_fixtures as generator
from extract_codex_session_log import build_events, build_turns, load_entries, session_models


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "codex"


def test_native_fixtures_cover_audited_shapes():
    tool = load_entries(FIXTURES / "tool_patch_large.jsonl")
    turns, changed = build_turns(tool)
    events = build_events(tool, tool_result_max_bytes=100)
    assert changed == ["fixture.txt"]
    assert any(call.get("result_truncated") for event in events
               for call in event.get("tool_calls", []))
    assert any(turn.tool_bullets for turn in turns)

    model = load_entries(FIXTURES / "model.jsonl")
    assert len(session_models(model)) == 2

    images = load_entries(FIXTURES / "image.jsonl")
    assert any(event.get("images") for event in build_events(images))


def test_sanitizer_keeps_native_shape_and_hides_private_data():
    entries = [
        {"type": "session_meta", "payload": {
            "id": "session-id", "cwd": "/tmp/private-work", "creator_user_id": "secret",
            "base_instructions": {"text": "private instructions"}}},
        {"type": "response_item", "payload": {"type": "function_call",
            "name": "exec_command", "call_id": "call-1",
            "arguments": json.dumps({"cmd": "cat /tmp/private-work/file"})}},
    ]
    cleaned = generator.clean(entries, Path("/tmp/private-work"))
    assert cleaned[0]["payload"] == {"id": "session-id", "cwd": "/workspace"}
    assert json.loads(cleaned[1]["payload"]["arguments"])["cmd"] == "cat /workspace/file"
    assert "secret" not in json.dumps(cleaned)
