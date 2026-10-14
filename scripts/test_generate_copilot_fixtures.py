"""Checks for the on-demand native Copilot fixture capture."""

import json
import sqlite3
from types import SimpleNamespace

import generate_copilot_fixtures as generator

from generate_copilot_fixtures import clean_event_lines, copy_session


def test_clean_event_lines_preserves_shape_and_redacts_private_paths(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    rows = [
        {"type": "system.message", "data": {"content": "private prompt"}},
        {"type": "session.usage_checkpoint", "data": {"account": "private"}},
        {"type": "user.message", "data": {"content": str(workspace / "file.py")}},
        {"type": "assistant.message", "data": {"content": "contact user@example.com"}},
    ]
    cleaned = clean_event_lines([json.dumps(row) for row in rows], workspace)
    assert [row["type"] for row in map(json.loads, cleaned.splitlines())] == [
        "user.message", "assistant.message"
    ]
    assert "/workspace/file.py" in cleaned
    assert "user@example.com" not in cleaned


def test_copy_session_keeps_native_schema_and_only_requested_rows(tmp_path):
    source = tmp_path / "source.db"
    destination = tmp_path / "fixture.db"
    workspace = tmp_path / "workspace"
    with sqlite3.connect(source) as conn:
        conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, cwd TEXT)")
        conn.execute("CREATE TABLE turns (session_id TEXT, user_message TEXT)")
        conn.executemany("INSERT INTO sessions VALUES (?, ?)", [
            ("fixture-id", str(workspace)), ("private-id", "/private/project")
        ])
        conn.executemany("INSERT INTO turns VALUES (?, ?)", [
            ("fixture-id", "hello"), ("private-id", "private conversation")
        ])
    copy_session(source, "fixture-id", destination, workspace)
    with sqlite3.connect(destination) as conn:
        assert conn.execute("SELECT id, cwd FROM sessions").fetchall() == [
            ("fixture-id", "/workspace")
        ]
        assert conn.execute("SELECT session_id, user_message FROM turns").fetchall() == [
            ("fixture-id", "hello")
        ]
        assert "CREATE TABLE sessions" in conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='sessions'"
        ).fetchone()[0]


def test_run_uses_isolated_headless_flags_and_resume(monkeypatch, tmp_path):
    calls = []

    def fake_subprocess(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(generator.subprocess, "run", fake_subprocess)
    generator.run("first prompt", tmp_path, "session-id")
    generator.run("second prompt", tmp_path, "session-id", resume=True,
                  attachment=tmp_path / "pixel.png")
    assert "--no-custom-instructions" in calls[0]
    assert "--disable-builtin-mcps" in calls[0]
    assert calls[0][-2:] == ["--session-id", "session-id"]
    assert ["--resume", "session-id"] == calls[1][calls[1].index("--resume"):][:2]
    assert "--attachment" in calls[1]


def test_main_stages_all_six_native_cases(monkeypatch, tmp_path):
    source = tmp_path / "source.db"
    state = tmp_path / "state"
    output = tmp_path / "output"
    with sqlite3.connect(source) as conn:
        conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, cwd TEXT)")
        conn.execute("CREATE TABLE turns (session_id TEXT, user_message TEXT)")
    monkeypatch.setattr(generator, "STORE", source)
    monkeypatch.setattr(generator, "STATE", state)
    monkeypatch.setattr(generator, "ROOT", output)
    monkeypatch.setattr(generator.subprocess, "run", lambda *a, **k: SimpleNamespace(
        stdout="GitHub Copilot CLI 1.0.90-5.\n", returncode=0
    ))

    def fake_run(prompt, workspace, session_id, *, resume=False, attachment=None):
        name = workspace.parent.name
        events_path = state / session_id / "events.jsonl"
        events_path.parent.mkdir(parents=True, exist_ok=True)
        rows = []
        if events_path.exists():
            rows = [json.loads(line) for line in events_path.read_text().splitlines()]
        response = {
            "baseline": "BASELINE_FIXTURE_MARKER",
            "continued": "BLUE-WREN-17" if resume else "READY",
            "tool_success": "TOOL_FIXTURE_MARKER",
            "tool_failure": "exit code 1",
        }.get(name, "fixture")
        rows += [
            {"type": "user.message", "data": {"content": prompt,
                "attachments": [{"path": str(attachment)}] if attachment else []}},
            {"type": "assistant.message", "data": {"content": response}},
        ]
        if name.startswith("tool_"):
            rows.append({"type": "tool.execution_start", "data": {"toolName": "bash"}})
        if name == "skill":
            rows.append({"type": "skill.invoked", "data": {"name": "fixture-skill"}})
        events_path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        with sqlite3.connect(source) as conn:
            if not resume:
                conn.execute("INSERT INTO sessions VALUES (?, ?)", (session_id, str(workspace)))
            conn.execute("INSERT INTO turns VALUES (?, ?)", (session_id, prompt))

    monkeypatch.setattr(generator, "run", fake_run)
    generator.main()
    manifest = json.loads((output / "manifest.json").read_text())
    assert set(manifest["cases"]) == {
        "baseline", "continued", "tool_success", "tool_failure", "skill", "image"
    }
    with sqlite3.connect(output / "session-store.db") as conn:
        assert conn.execute("SELECT count(*) FROM sessions").fetchone()[0] == 6
        assert conn.execute("SELECT count(*) FROM turns").fetchone()[0] == 7
    assert "user.message" in (output / "image" / "events.jsonl").read_text()
    assert str(tmp_path) not in (output / "image" / "events.jsonl").read_text()
