"""Checks for the on-demand native Copilot fixture capture."""

import json
import sqlite3
from types import SimpleNamespace

import generate_copilot_fixtures as generator
import pytest
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
        "user.message",
        "assistant.message",
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
        conn.executemany(
            "INSERT INTO sessions VALUES (?, ?)",
            [("fixture-id", str(workspace)), ("private-id", "/private/project")],
        )
        conn.executemany(
            "INSERT INTO turns VALUES (?, ?)",
            [("fixture-id", "hello"), ("private-id", "private conversation")],
        )
    copy_session(source, "fixture-id", destination, workspace)
    with sqlite3.connect(destination) as conn:
        assert conn.execute("SELECT id, cwd FROM sessions").fetchall() == [
            ("fixture-id", "/workspace")
        ]
        assert conn.execute(
            "SELECT session_id, user_message FROM turns"
        ).fetchall() == [("fixture-id", "hello")]
        assert (
            "CREATE TABLE sessions"
            in conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='sessions'"
            ).fetchone()[0]
        )


def test_run_uses_isolated_headless_flags_and_resume(monkeypatch, tmp_path):
    calls = []

    def fake_subprocess(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(generator.subprocess, "run", fake_subprocess)
    generator.run("first prompt", tmp_path, "session-id")
    generator.run(
        "second prompt",
        tmp_path,
        "session-id",
        resume=True,
        attachment=tmp_path / "pixel.png",
    )
    assert "--no-custom-instructions" in calls[0]
    assert "--disable-builtin-mcps" in calls[0]
    assert calls[0][-2:] == ["--session-id", "session-id"]
    assert ["--resume", "session-id"] == calls[1][calls[1].index("--resume") :][:2]
    assert "--attachment" in calls[1]


def _fake_copilot(monkeypatch, tmp_path, mutate=lambda name, resume, rows: rows):
    """Stand in for the copilot CLI: write a minimal native event log and DB rows.

    ``mutate`` may rewrite one invocation's events, or return None to write nothing.
    """
    source = tmp_path / "source.db"
    state = tmp_path / "state"
    output = tmp_path / "output"
    with sqlite3.connect(source) as conn:
        conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, cwd TEXT)")
        conn.execute("CREATE TABLE turns (session_id TEXT, user_message TEXT)")
    monkeypatch.setattr(generator, "STORE", source)
    monkeypatch.setattr(generator, "STATE", state)
    monkeypatch.setattr(generator, "ROOT", output)
    monkeypatch.setattr(
        generator.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(
            stdout="GitHub Copilot CLI 1.0.90-5.\n", returncode=0
        ),
    )
    seen = []

    def fake_run(prompt, workspace, session_id, *, resume=False, attachment=None):
        seen.append((workspace, list(tmp_path.glob("copilot-fixture-staging-*"))))
        name = workspace.parent.name
        events_path = state / session_id / "events.jsonl"
        response = {
            "baseline": "BASELINE_FIXTURE_MARKER",
            "continued": "BLUE-WREN-17" if resume else "READY",
            "tool_success": "TOOL_FIXTURE_MARKER",
            "tool_failure": "exit code 1",
        }.get(name, "fixture")
        new = [
            {
                "type": "user.message",
                "data": {
                    "content": prompt,
                    "attachments": [{"path": str(attachment)}] if attachment else [],
                },
            },
            {"type": "assistant.message", "data": {"content": response}},
        ]
        if name.startswith("tool_"):
            command = (
                "printf TOOL_FIXTURE_MARKER" if name == "tool_success" else "false"
            )
            new.extend(
                [
                    {
                        "type": "tool.execution_start",
                        "data": {
                            "toolCallId": "call-1",
                            "toolName": "bash",
                            "arguments": {"command": command},
                        },
                    },
                    {
                        "type": "tool.execution_complete",
                        "data": {
                            "toolCallId": "call-1",
                            "result": {"content": response},
                        },
                    },
                ]
            )
        if name == "skill":
            new.append({"type": "skill.invoked", "data": {"name": "fixture-skill"}})
        new = mutate(name, resume, new)
        if new is None:
            return
        events_path.parent.mkdir(parents=True, exist_ok=True)
        rows = []
        if events_path.exists():
            rows = [json.loads(line) for line in events_path.read_text().splitlines()]
        rows += new
        events_path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        with sqlite3.connect(source) as conn:
            if not resume:
                conn.execute(
                    "INSERT INTO sessions VALUES (?, ?)", (session_id, str(workspace))
                )
            conn.execute("INSERT INTO turns VALUES (?, ?)", (session_id, prompt))

    monkeypatch.setattr(generator, "run", fake_run)
    return output, seen


def test_main_stages_all_six_native_cases(monkeypatch, tmp_path):
    output, seen = _fake_copilot(monkeypatch, tmp_path)
    generator.main()
    manifest = json.loads((output / "manifest.json").read_text())
    assert set(manifest["cases"]) == {
        "baseline",
        "continued",
        "tool_success",
        "tool_failure",
        "skill",
        "image",
    }
    with sqlite3.connect(output / "session-store.db") as conn:
        assert conn.execute("SELECT count(*) FROM sessions").fetchone()[0] == 6
        assert conn.execute("SELECT count(*) FROM turns").fetchone()[0] == 7
    assert "user.message" in (output / "image" / "events.jsonl").read_text()
    assert str(tmp_path) not in (output / "image" / "events.jsonl").read_text()
    # The CLI runs outside the repo; outputs stage beside ROOT, on its filesystem.
    assert all(tmp_path not in workspace.parents for workspace, _ in seen)
    assert all(len(staging) == 1 for _, staging in seen)
    assert not list(tmp_path.glob("copilot-fixture-staging-*"))


def _drop(event_type):
    return lambda rows: [row for row in rows if row["type"] != event_type]


@pytest.mark.parametrize(
    ("case", "breakage", "message"),
    [
        ("baseline", lambda rows: None, "baseline: missing native event log"),
        (
            "baseline",
            _drop("assistant.message"),
            "baseline: incomplete native event log",
        ),
        (
            "continued",
            _drop("user.message"),
            "continued: resume did not produce a second turn",
        ),
        (
            "tool_success",
            _drop("tool.execution_complete"),
            "tool_success: missing native printf TOOL_FIXTURE_MARKER execution",
        ),
        ("skill", _drop("skill.invoked"), "skill: no native skill invocation"),
        (
            "image",
            lambda rows: [
                {**row, "data": {**row["data"], "attachments": []}}
                if row["type"] == "user.message"
                else row
                for row in rows
            ],
            "image: attachment was not recorded",
        ),
        (
            "baseline",
            lambda rows: [
                {**row, "data": {"content": "something else"}}
                if row["type"] == "assistant.message"
                else row
                for row in rows
            ],
            "baseline: missing expected reply BASELINE_FIXTURE_MARKER",
        ),
        (
            "tool_failure",
            lambda rows: [
                {**row, "data": {**row["data"], "result": {"content": "fine"}}}
                if row["type"] == "tool.execution_complete"
                else row
                for row in rows
            ],
            "tool_failure: missing native result exit code 1",
        ),
    ],
)
def test_main_rejects_incomplete_native_capture(
    monkeypatch, tmp_path, case, breakage, message
):
    # Only the continued case's resumed turn is broken for the second-turn check.
    def mutate(name, resume, rows):
        if name != case or (case == "continued" and not resume):
            return rows
        return breakage(rows)

    _fake_copilot(monkeypatch, tmp_path, mutate)
    with pytest.raises(RuntimeError) as exc:
        generator.main()
    assert str(exc.value) == message


def test_run_reports_cli_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(
        generator.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=2, stderr=" auth \n"),
    )
    with pytest.raises(RuntimeError, match="Copilot exited 2: auth"):
        generator.run("prompt", tmp_path, "session-id")


def test_copy_session_rejects_unknown_session(tmp_path):
    source = tmp_path / "source.db"
    with sqlite3.connect(source) as conn:
        conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, cwd TEXT)")
    with pytest.raises(RuntimeError, match="missing native session nope"):
        copy_session(source, "nope", tmp_path / "out.db", tmp_path)


def test_clean_event_lines_rejects_unknown_event_shape(tmp_path):
    with pytest.raises(RuntimeError, match="unexpected Copilot event format"):
        clean_event_lines(['["not", "an", "event"]'], tmp_path)


def test_clean_rejects_other_private_temp_paths(tmp_path):
    line = json.dumps(
        {"type": "user.message", "data": {"content": "/private/var/other/file"}}
    )
    with pytest.raises(RuntimeError, match="private path") as exc_info:
        clean_event_lines([line], tmp_path)

    assert "private path" in str(exc_info.value)
