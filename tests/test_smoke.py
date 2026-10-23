import importlib
import json
import os
import runpy
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

import session_log_extractors
from session_log_extractors import extract_claude_session_log as claude
from session_log_extractors import extract_copilot_session_log as copilot
from session_log_extractors.extract_claude_session_log import encode_project_path
from session_log_extractors.session_log_envelope import raw_filename


def test_package_imports():
    assert session_log_extractors is not None
    assert raw_filename("claude", "smoke") == "claude_session_log_raw_smoke.json"


@pytest.mark.parametrize("harness", ["claude", "codex", "copilot"])
def test_help_uses_call_time_output_root(harness, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("COLUMNS", "1000")
    module = importlib.import_module(
        f"session_log_extractors.extract_{harness}_session_log"
    )
    for name in ["first", "second"]:
        cwd = tmp_path / name
        cwd.mkdir()
        monkeypatch.chdir(cwd)
        with pytest.raises(SystemExit) as exc:
            module.main(["--help"])
        assert exc.value.code == 0
        assert str(cwd) in capsys.readouterr().out
    with pytest.raises(SystemExit) as exc:
        module.main(["--help"], project_root=tmp_path / "explicit")
    assert exc.value.code == 0
    assert str(tmp_path / "explicit") in capsys.readouterr().out


@pytest.mark.parametrize(
    ("harness", "arguments"),
    [
        ("claude", ["--transcript", "scripts/fixtures/claude/baseline.jsonl"]),
        ("codex", ["--transcript", "scripts/fixtures/codex/tool_patch_large.jsonl"]),
        (
            "copilot",
            [
                "7e20450a-af9e-45a5-9aac-be9dcf027221",
                "--db",
                "scripts/fixtures/copilot/session-store.db",
            ],
        ),
    ],
)
def test_script_exports_fixture_json(harness, arguments, tmp_path):
    root = Path(__file__).resolve().parents[1]
    output = tmp_path / "session.json"
    subprocess.run(
        [
            sys.executable,
            str(root / "scripts" / f"extract_{harness}_session_log.py"),
            *arguments,
            "--output",
            str(output),
        ],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    envelope = json.loads(output.read_text())
    assert envelope["schema"] == "raw-session-log/1"
    assert envelope["harness"] == harness
    assert envelope["events"]


@pytest.mark.parametrize("subdirectory", [False, True])
@pytest.mark.parametrize("selection", ["transcript", "all", "discovery"])
def test_claude_checkout_defaults(subdirectory, selection, tmp_path):
    root = Path(__file__).resolve().parents[1]
    checkout = tmp_path / "checkout"
    scripts = checkout / "scripts"
    scripts.mkdir(parents=True)
    wrapper = scripts / "extract_claude_session_log.py"
    shutil.copyfile(root / "scripts" / wrapper.name, wrapper)
    cwd = checkout / "nested" if subdirectory else checkout
    cwd.mkdir(exist_ok=True)
    home = tmp_path / "home"
    project = home / ".claude" / "projects" / encode_project_path(checkout)
    project.mkdir(parents=True)
    transcript = project / "session.jsonl"
    shutil.copyfile(root / "scripts/fixtures/claude/baseline.jsonl", transcript)
    arguments = {
        "transcript": ["--transcript", str(transcript)],
        "all": ["--all", "--project-dir", str(project)],
        "discovery": [],
    }[selection]
    result = subprocess.run(
        [sys.executable, str(wrapper), *arguments],
        cwd=cwd,
        env={**os.environ, "HOME": str(home)},
        check=True,
        capture_output=True,
        text=True,
    )
    output = checkout / "claude_session_log_raw_session.json"
    envelope = json.loads(output.read_text())
    assert envelope["harness"] == "claude"
    assert envelope["events"]
    assert result.stdout == ""
    assert f"wrote {output}" in result.stderr
    assert not (scripts / output.name).exists()
    if subdirectory:
        assert not (cwd / output.name).exists()


def test_claude_wrapper_delegates_with_checkout_root(monkeypatch):
    root = Path(__file__).resolve().parents[1]

    def main(*, project_root):
        assert project_root == root
        return 7

    monkeypatch.setattr(claude, "main", main)
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(
            str(root / "scripts/extract_claude_session_log.py"), run_name="__main__"
        )
    assert exc.value.code == 7


@pytest.mark.parametrize("subdirectory", [False, True])
@pytest.mark.parametrize("selection", ["selected", "discovery", "overrides"])
def test_copilot_checkout_defaults(subdirectory, selection, tmp_path):
    root = Path(__file__).resolve().parents[1]
    checkout = tmp_path / "checkout"
    scripts = checkout / "scripts"
    scripts.mkdir(parents=True)
    wrapper = scripts / "extract_copilot_session_log.py"
    shutil.copyfile(root / "scripts" / wrapper.name, wrapper)
    cwd = checkout / "nested" if subdirectory else checkout
    cwd.mkdir(exist_ok=True)
    home = tmp_path / "home"
    copilot_home = home / ".copilot"
    copilot_home.mkdir(parents=True)
    db = copilot_home / "session-store.db"
    shutil.copyfile(root / "scripts/fixtures/copilot/session-store.db", db)
    session_id = "7e20450a-af9e-45a5-9aac-be9dcf027221"
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("DELETE FROM sessions WHERE id != ?", (session_id,))
        conn.execute("UPDATE sessions SET cwd = ?", (str(checkout),))
        conn.commit()
    state = copilot_home / "session-state" / session_id
    state.mkdir(parents=True)
    (state / "events.jsonl").write_text(
        json.dumps(
            {
                "type": "tool.execution_start",
                "data": {
                    "toolCallId": "test-call",
                    "toolName": "checkout-state-tool",
                    "arguments": {"command": "echo state"},
                },
            }
        )
        + "\n"
    )
    output = checkout / "copilot_session_log_raw_2026-10-14_7e20450a.json"
    arguments = [session_id] if selection == "selected" else []
    if selection == "overrides":
        moved_db = tmp_path / "override.db"
        db.rename(moved_db)
        output = tmp_path / "exports" / output.name
        arguments = ["--db", str(moved_db), "--output", str(output.parent)]
    result = subprocess.run(
        [sys.executable, str(wrapper), *arguments],
        cwd=cwd,
        env={**os.environ, "HOME": str(home)},
        check=True,
        capture_output=True,
        text=True,
    )
    assert list(output.parent.glob("copilot_session_log_raw_*.json")) == [output]
    envelope = json.loads(output.read_text())
    assert envelope["harness"] == "copilot"
    assert "checkout-state-tool" in json.dumps(envelope)
    assert result.stdout == ""
    assert f"wrote {output}" in result.stderr
    assert not list(scripts.glob("copilot_session_log_raw_*.json"))
    if subdirectory:
        assert not list(cwd.glob("copilot_session_log_raw_*.json"))


def test_copilot_wrapper_delegates_with_checkout_root(monkeypatch):
    root = Path(__file__).resolve().parents[1]

    def main(*, project_root):
        assert project_root == root
        return 7

    monkeypatch.setattr(copilot, "main", main)
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(
            str(root / "scripts/extract_copilot_session_log.py"), run_name="__main__"
        )
    assert exc.value.code == 7


@pytest.mark.parametrize("harness", ["codex"])
def test_wrapper_delegates_with_checkout_root(harness, monkeypatch):
    extractor = importlib.import_module(
        f"session_log_extractors.extract_{harness}_session_log"
    )

    root = Path(__file__).resolve().parents[1]

    def main(*, project_root):
        assert project_root == root
        return 7

    monkeypatch.setattr(extractor, "main", main)
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(
            str(root / "scripts" / f"extract_{harness}_session_log.py"),
            run_name="__main__",
        )
    assert exc.value.code == 7


@pytest.mark.parametrize("subdirectory", [False, True])
@pytest.mark.parametrize("selection", ["transcript", "prefix", "all", "discovery"])
@pytest.mark.parametrize("destination", ["default", "explicit", "stdout"])
def test_codex_checkout_paths(subdirectory, selection, destination, tmp_path):
    root = Path(__file__).resolve().parents[1]
    checkout = tmp_path / "checkout"
    scripts = checkout / "scripts"
    scripts.mkdir(parents=True)
    wrapper = scripts / "extract_codex_session_log.py"
    shutil.copyfile(root / "scripts" / wrapper.name, wrapper)
    cwd = checkout / "nested" if subdirectory else checkout
    cwd.mkdir(exist_ok=True)
    home = tmp_path / "home"
    sessions = home / ".codex" / "sessions"
    sessions.mkdir(parents=True)
    rows = [
        {"type": "session_meta", "payload": {"id": "session", "cwd": str(checkout)}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": "hello"}},
    ]
    transcript = sessions / "rollout-session.jsonl"
    transcript.write_text("\n".join(json.dumps(row) for row in rows))
    arguments = {
        "transcript": ["--transcript", str(transcript)],
        "prefix": ["sess", "--sessions-root", str(sessions)],
        "all": ["--all", "--sessions-root", str(sessions)],
        "discovery": [],
    }[selection]
    output = checkout / "codex_session_log_raw_session.json"
    if destination == "explicit":
        output = tmp_path / "exports" / output.name
        output.parent.mkdir()
        target = output.parent if selection in ("all", "discovery") else output
        arguments += ["--output", str(target)]
    elif destination == "stdout":
        arguments += ["--output", "-"]
    result = subprocess.run(
        [sys.executable, str(wrapper), *arguments],
        cwd=cwd,
        env={**os.environ, "HOME": str(home)},
        check=True,
        capture_output=True,
        text=True,
    )
    # Explicit --all retains file output even when --output is '-'.
    stdout = destination == "stdout" and selection != "all"
    envelope = json.loads(result.stdout if stdout else output.read_text())
    assert envelope["harness"] == "codex"
    assert envelope["session_id"] == "session"
    assert envelope["events"][0]["text"] == "hello"
    assert output.exists() is not stdout
    assert not (scripts / output.name).exists()
    if subdirectory:
        assert not (cwd / output.name).exists()
