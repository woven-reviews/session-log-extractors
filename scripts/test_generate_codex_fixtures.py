"""Native Codex fixture checks; regeneration never runs in tests."""

import json
import queue
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
    turns, files_changed = build_turns(tool)
    assert files_changed == ["/workspace/fixture.txt"]
    assert len(turns[0].tool_bullets) == 2
    assert turns[0].result_notes[0] == "exec: TOOL_FIXTURE_MARKER"
    assert any(note.startswith("apply_patch: Success. Updated the following files:")
               and "/workspace/fixture.txt" in note for note in turns[0].result_notes)
    calls = [call for event in build_events(tool, tool_result_max_bytes=64)
             for call in event.get("tool_calls", [])]
    assert len(calls) == 2
    assert calls[0]["result"] == "TOOL_FIXTURE_MARKER"
    assert calls[1]["result_truncated"] is True
    assert calls[1]["result_bytes"] > 64

    slash = load_entries(FIXTURES / "slash.jsonl")
    assert build_turns(slash)[0][0].command == "/review"

    image = load_entries(FIXTURES / "image.jsonl")
    assert build_events(image)[0]["images"][0]["bytes"] == (FIXTURES / "pixel.png").stat().st_size

    model = load_entries(FIXTURES / "model.jsonl")
    assert session_models(model) == ["gpt-6.1-sol", "gpt-6-luna"]


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
        {"type": "turn_context", "payload": {"cwd": str(workspace), "model": "gpt-x",
            "collaboration_mode": {"mode": "plan", "settings": {
                "developer_instructions": "private mode instructions"}}}},
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
    assert cleaned[1]["payload"]["collaboration_mode"] == {"mode": "plan"}
    assert "secret" not in json.dumps(cleaned)
    assert "private mode instructions" not in json.dumps(cleaned)


def test_clean_rejects_unredacted_private_path():
    with pytest.raises(RuntimeError, match="private path"):
        generator.clean([{"type": "event_msg", "payload": {
            "type": "user_message", "message": "/var/folders/private"
        }}], Path("/tmp/work"))


def test_select_and_validate_captured_shapes():
    for name in ("tool_patch_large", "slash", "image", "model", "plan", "question", "denial"):
        rows = [json.loads(line) for line in (FIXTURES / f"{name}.jsonl").read_text().splitlines()]
        generator.validate(name, rows)
        if name in ("plan", "question", "denial"):
            assert "developer_instructions" not in json.dumps(rows)
    tool = [json.loads(line) for line in (FIXTURES / "tool_patch_large.jsonl").read_text().splitlines()]
    selected = generator.select("slash", tool)
    assert not any(r["payload"].get("type") == "custom_tool_call" for r in selected)
    for name in ("tool_patch_large", "slash", "image", "model", "plan", "question", "denial"):
        with pytest.raises(RuntimeError):
            generator.validate(name, [])


def _fake_cli(monkeypatch, tmp_path, *, error=None, no_thread=False,
              no_transcript=False, no_assistant=False, resume_error=False):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setattr(generator, "SESSIONS", sessions)
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        if error or (resume_error and "resume" in args):
            return SimpleNamespace(returncode=1, stderr="boom", stdout="")
        if "resume" in args:
            return SimpleNamespace(returncode=0, stderr="", stdout="")
        name = Path(args[args.index("-C") + 1]).name
        rows = [json.loads(line) for line in (FIXTURES / f"{name}.jsonl").read_text().splitlines()]
        sid = rows[0]["payload"]["id"]
        if no_assistant:
            rows = [row for row in rows if not (
                row["type"] == "response_item"
                and row["payload"].get("role") == "assistant"
            )]
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
    output.mkdir(parents=True)
    (output / "pixel.png").write_bytes((FIXTURES / "pixel.png").read_bytes())
    monkeypatch.setattr(generator, "ROOT", output)
    calls = _fake_cli(monkeypatch, tmp_path)
    generator.main()
    assert sorted(p.name for p in output.glob("*.jsonl")) == [
        "image.jsonl", "model.jsonl", "slash.jsonl", "tool_patch_large.jsonl"
    ]
    for generated in output.glob("*.jsonl"):
        assert [json.loads(line) for line in generated.read_text().splitlines()] == [
            json.loads(line) for line in (FIXTURES / generated.name).read_text().splitlines()
        ]
    assert len(calls) == 5
    assert all(call[0][0] == "codex" for call in calls)
    assert sum("resume" in call[0] for call in calls) == 1
    assert all(call[1]["stdin"] == generator.subprocess.DEVNULL for call in calls)
    assert not list(output.parent.glob("codex-fixture-stage-*"))


def test_interactive_main_drives_requests_and_stages_native_rollouts(monkeypatch, tmp_path):
    output = tmp_path / "codex"
    output.mkdir()
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setattr(generator, "ROOT", output)
    monkeypatch.setattr(generator, "SESSIONS", sessions)
    processes = []

    class FakeProcess:
        def __init__(self, *args, **kwargs):
            assert args[0] == ["codex", "app-server"]
            self.events = queue.Queue()
            self.stdin = self
            self.stdout = self
            self.sent = []
            self.name = None
            processes.append(self)

        def __iter__(self):
            while (event := self.events.get()) is not None:
                yield json.dumps(event) + "\n"

        def write(self, line):
            message = json.loads(line)
            self.sent.append(message)
            method = message.get("method")
            if method == "initialize":
                self.events.put({"id": 1, "result": {}})
            elif method == "thread/start":
                self.name = Path(message["params"]["cwd"]).name
                rows = (FIXTURES / f"{self.name}.jsonl").read_text()
                (sessions / f"rollout-fixture-{self.name}.jsonl").write_text(rows)
                self.events.put({"id": 2, "result": {"thread": {"id": f"fixture-{self.name}"}}})
            elif method == "turn/start":
                self.events.put({"id": 3, "result": {}})
                if self.name == "question":
                    self.events.put({"id": 4, "method": "item/tool/requestUserInput",
                                     "params": {"questions": [{"id": "fixture_owner"}]}})
                elif self.name == "denial":
                    self.events.put({"id": 4, "method": "item/commandExecution/requestApproval"})
                else:
                    self.events.put({"method": "turn/completed",
                                     "params": {"turn": {"status": "completed"}}})
            elif message.get("id") == 4:
                self.events.put({"method": "turn/completed",
                                 "params": {"turn": {"status": "completed"}}})

        def flush(self):
            pass

        def terminate(self):
            self.events.put(None)

        def wait(self, timeout):
            return 0

    monkeypatch.setattr(generator.subprocess, "Popen", FakeProcess)
    generator.interactive_main()
    assert {p.name for p in output.glob("*.jsonl")} == {
        "plan.jsonl", "question.jsonl", "denial.jsonl"
    }
    assert len(processes) == 3
    assert any(m.get("result", {}).get("answers") == {
        "fixture_owner": {"answers": ["QUAL"]}
    } for m in processes[1].sent)
    assert any(m.get("result", {}).get("decision") == "decline"
               for m in processes[2].sent)
    assert not list(output.parent.glob("codex-fixture-stage-*"))


@pytest.mark.parametrize(("options", "message"), [
    ({"error": True}, "Codex exited 1"),
    ({"no_thread": True}, "expected one Codex thread"),
    ({"no_transcript": True}, "expected one native transcript"),
    ({"no_assistant": True}, "missing assistant response"),
])
def test_capture_rejects_incomplete_cli_run(monkeypatch, tmp_path, options, message):
    _fake_cli(monkeypatch, tmp_path, **options)
    workspace = tmp_path / "slash"
    workspace.mkdir()
    with pytest.raises(RuntimeError, match=message):
        generator.capture("slash", "/review fixture", workspace)



def test_capture_rejects_failed_model_resume(monkeypatch, tmp_path):
    _fake_cli(monkeypatch, tmp_path, resume_error=True)
    workspace = tmp_path / "model"
    workspace.mkdir()
    with pytest.raises(RuntimeError, match="resume exited 1"):
        generator.capture("model", "FIRST_MODEL_FIXTURE_MARKER", workspace)
