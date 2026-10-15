"""Native Codex fixture checks; regeneration never runs in tests."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import generate_codex_fixtures as generator
from extract_codex_session_log import build_events, build_turns, load_entries, session_models


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "codex"


def test_native_fixtures_record_headless_codex_shapes():
    tool = load_entries(FIXTURES / "tool_patch_large.jsonl")
    assert any(row["payload"].get("type") == "custom_tool_call" for row in tool)
    assert any(row["payload"].get("item", {}).get("type") == "FileChange" for row in tool)
    assert build_turns(tool)[1] == []  # Current extractor does not interpret this CLI schema.

    slash = load_entries(FIXTURES / "slash.jsonl")
    assert any("/review fixture" in str(row["payload"]) for row in slash)

    image = load_entries(FIXTURES / "image.jsonl")
    assert any("image content omitted" in str(row["payload"]) for row in image)


def test_fixture_only_shapes_keep_audited_behavior():
    source = FIXTURES / "hand-authored"
    tool = load_entries(source / "tool_patch_large.jsonl")
    turns, changed = build_turns(tool)
    events = build_events(tool, tool_result_max_bytes=100)
    assert changed == ["fixture.txt"]
    assert any(call.get("result_truncated") for event in events
               for call in event.get("tool_calls", []))
    assert any(turn.tool_bullets for turn in turns)
    assert session_models(load_entries(source / "model_switch.jsonl")) == [
        "gpt-6.1-sol", "gpt-6-luna"
    ]
    assert any(event.get("images") for event in build_events(
        load_entries(source / "images.jsonl")
    ))


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



def test_clean_filters_startup_and_unrelated_records(tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    rows = [
        {"type": "session_meta", "payload": {"id": "one", "cwd": str(workspace),
                                             "creator_user_id": "secret"}},
        {"type": "turn_context", "payload": {"cwd": str(workspace), "model": "gpt-x"}},
        {"type": "response_item", "payload": {"type": "message", "role": "developer",
                                              "content": []}},
        {"type": "response_item", "payload": {"type": "message", "role": "user",
                                              "content": [{"text": "# AGENTS.md instructions"}]}},
        {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                                              "content": [{"type": "output_text", "text": "ok"}]}},
        {"type": "response_item", "payload": {"type": "custom_tool_call",
                                              "name": "exec", "call_id": "c1",
                                              "input": "fixture.txt"}},
        {"type": "response_item", "payload": {"type": "custom_tool_call_output",
                                              "call_id": "c1", "output": []}},
        {"type": "event_msg", "payload": {"type": "item_completed",
                                         "item": {"type": "CommandExecution"}}},
        {"type": "event_msg", "payload": {"type": "item_completed",
                                         "item": {"type": "FileChange", "changes": []}}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": "hello"}},
        {"type": "event_msg", "payload": {"type": "token_count"}},
        {"type": "world_state", "payload": {}},
    ]
    cleaned = generator.clean(rows, workspace)
    assert [r["payload"].get("type") for r in cleaned] == [
        None, None, "message", "custom_tool_call", "custom_tool_call_output",
        "item_completed", "user_message"
    ]
    assert cleaned[0]["payload"]["cwd"] == "/workspace"
    assert "secret" not in json.dumps(cleaned)


def test_clean_rejects_unredacted_private_path():
    with pytest.raises(RuntimeError, match="private path"):
        generator.clean([{"type": "event_msg", "payload": {
            "type": "user_message", "message": "/var/folders/private"
        }}], Path("/tmp/work"))


def test_select_and_validate_captured_shapes():
    for name in ("tool_patch_large", "slash", "image"):
        rows = [json.loads(line) for line in (FIXTURES / f"{name}.jsonl").read_text().splitlines()]
        generator.validate(name, rows)
    tool = [json.loads(line) for line in (FIXTURES / "tool_patch_large.jsonl").read_text().splitlines()]
    selected = generator.select("slash", tool)
    assert not any(r["payload"].get("type") == "custom_tool_call" for r in selected)
    for name in ("tool_patch_large", "slash", "image"):
        with pytest.raises(RuntimeError):
            generator.validate(name, [])


def _fake_codex(monkeypatch, tmp_path, *, error=None, no_thread=False,
                no_transcript=False, no_assistant=False):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setattr(generator, "SESSIONS", sessions)
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        if error:
            return SimpleNamespace(returncode=1, stderr="boom", stdout="")
        name = Path(args[args.index("-C") + 1]).name
        sid = f"fixture-{name}"
        rows = [
            {"type": "session_meta", "payload": {"id": sid,
                                                 "cwd": args[args.index("-C") + 1]}},
            {"type": "response_item", "payload": {"type": "message", "role": "user",
                "content": [{"type": "input_text", "text":
                    "/review fixture" if name == "slash" else
                    "/workspace/pixel.png" if name == "image" else "run tool"}]}},
        ]
        if not no_assistant:
            rows.append({"type": "response_item", "payload": {
                "type": "message", "role": "assistant",
                "content": [{"type": "output_text", "text": "done"}]}})
        if name == "tool_patch_large":
            rows += [
                {"type": "response_item", "payload": {
                    "type": "custom_tool_call", "name": "exec",
                    "call_id": "c1", "input": "PATCH_FIXTURE_MARKER"}},
                {"type": "event_msg", "payload": {
                    "type": "item_completed", "item": {"type": "FileChange",
                                                      "changes": ["fixture.txt"]}}},
                {"type": "response_item", "payload": {
                    "type": "custom_tool_call_output", "call_id": "c1",
                    "output": [{"type": "input_text", "text": "PATCH_FIXTURE_MARKER\n" * 30}]}}
            ]
        if not no_transcript:
            (sessions / f"rollout-{sid}.jsonl").write_text(
                "\n".join(json.dumps(row) for row in rows) + "\n"
            )
        return SimpleNamespace(returncode=0, stderr="",
                               stdout="" if no_thread else
                               json.dumps({"type": "thread.started", "thread_id": sid}) + "\n")

    monkeypatch.setattr(generator.subprocess, "run", fake_run)
    return calls


def test_main_regenerates_with_fake_cli_only(monkeypatch, tmp_path):
    output = tmp_path / "repo" / "codex"
    output.parent.mkdir()
    copilot = output.parent / "copilot"
    copilot.mkdir()
    (copilot / "pixel.png").write_bytes((FIXTURES.parent / "copilot/pixel.png").read_bytes())
    monkeypatch.setattr(generator, "ROOT", output)
    calls = _fake_codex(monkeypatch, tmp_path)
    generator.main()
    assert sorted(p.name for p in output.glob("*.jsonl")) == [
        "image.jsonl", "slash.jsonl", "tool_patch_large.jsonl"
    ]
    assert len(calls) == 3
    assert all(call[0][0:2] == ["codex", "exec"] for call in calls)
    assert all(call[1]["stdin"] == generator.subprocess.DEVNULL for call in calls)
    assert not list(output.parent.glob("codex-fixture-stage-*"))


@pytest.mark.parametrize(("options", "message"), [
    ({"error": True}, "Codex exited 1"),
    ({"no_thread": True}, "expected one Codex thread"),
    ({"no_transcript": True}, "expected one native transcript"),
    ({"no_assistant": True}, "missing assistant response"),
])
def test_capture_rejects_incomplete_cli_run(monkeypatch, tmp_path, options, message):
    _fake_codex(monkeypatch, tmp_path, **options)
    workspace = tmp_path / "slash"
    workspace.mkdir()
    with pytest.raises(RuntimeError, match=message):
        generator.capture("slash", "/review fixture", workspace)
