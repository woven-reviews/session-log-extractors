"""Extraction must use recorded evidence without reading local definitions."""

import json
from pathlib import Path

import pytest
from codex_capture import TRANSCRIPT as CODEX_TRANSCRIPT
from copilot_fixture import native_source

from session_log_extractors import extract_claude_session_log as claude
from session_log_extractors import extract_codex_session_log as codex
from session_log_extractors import extract_copilot_session_log as copilot

ROOT = Path(__file__).parent / "fixtures" / "claude" / "2.1.312"
(TRANSCRIPT,) = (ROOT / "native").glob("*.jsonl")


def test_codex_invocations_do_not_load_definitions(monkeypatch):
    source = codex.read_source(CODEX_TRANSCRIPT)

    def forbidden(*args, **kwargs):
        pytest.fail("Extraction must not read external definitions")

    monkeypatch.setattr(Path, "open", forbidden)
    session = codex.interpret_source(source)
    assert session["skills_used"] == ["capture-greeting"]
    invocations = []
    for event in session["events"]:
        if event.get("skill_invocation"):
            invocations.append(event)
    assert len(invocations) == 2
    for invocation in invocations:
        assert invocation["skill_invocation"]["name"] == "capture-greeting"


@pytest.mark.parametrize("arguments", ["fixture", None])
def test_legacy_codex_commands_do_not_load_definitions(arguments, monkeypatch):
    # The installed interactive CLI rejects custom slash commands. Keep the old
    # command shape as a focused compatibility case, independent of fixtures.
    text = "/review" + (" " + arguments if arguments else "")
    source = codex.CodexSource(
        Path("unused.jsonl"),
        [
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": text}],
                },
            }
        ],
    )

    def forbidden(*args, **kwargs):
        pytest.fail("Extraction must not read external definitions")

    monkeypatch.setattr(Path, "open", forbidden)
    session = codex.interpret_source(source)
    user = session["events"][0]
    assert (user["command"], user["command_args"]) == ("/review", arguments)


def test_local_definitions_are_not_read_or_added_to_the_envelope(tmp_path, monkeypatch):
    project = (tmp_path / "project").resolve()
    project.mkdir()
    definitions = {
        project / ".claude/skills/capture-leaf/SKILL.md": "LOCAL_SKILL_ONLY_MARKER",
        project / ".claude/commands/capture-echo.md": "LOCAL_COMMAND_ONLY_MARKER",
    }
    for path, marker in definitions.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(marker, encoding="utf-8")

    monkeypatch.chdir(project)
    original_open = Path.open

    def guarded_open(path, *args, **kwargs):
        if path.resolve() in definitions:
            pytest.fail(f"Extraction must not read local definitions: {path}")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    transcript = TRANSCRIPT
    session = claude.interpret_source(claude.read_source(transcript))
    assert session["skills_used"] == ["capture-leaf"]
    commands = []
    for event in session["events"]:
        if event.get("command"):
            commands.append(event["command"])
    assert "/capture-echo" in commands
    envelope = claude.extract_transcript(transcript)
    assert envelope is not None
    output = json.dumps(envelope)
    for marker in definitions.values():
        assert marker not in output
    # These markers belong to recorded assistant replies and must remain.
    assert "CAPTURE_SKILL_LEAF" in output
    assert "CAPTURE_COMMAND_ECHO" in output


def test_copilot_skill_invocations_do_not_load_definitions(monkeypatch):
    source = native_source()
    # Repeated invocations are native; add only the malformed empty-name boundary.
    source.state_records.append({"type": "skill.invoked", "data": {"name": ""}})

    def forbidden(*args, **kwargs):
        pytest.fail("Extraction must not load external skill definitions")

    with monkeypatch.context() as guard:
        guard.setattr(Path, "open", forbidden)
        session = copilot.interpret_source(source)
    assert session["skills_used"] == ["capture-greeting", "capture-command"]
