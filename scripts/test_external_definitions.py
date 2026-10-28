"""Extraction must use recorded evidence without reading local definitions."""

import json
import shutil
from pathlib import Path

import pytest

from session_log_extractors import extract_claude_session_log as claude
from session_log_extractors import extract_codex_session_log as codex
from session_log_extractors import extract_copilot_session_log as copilot


@pytest.mark.parametrize("extractor", [codex, copilot])
def test_skill_invocations_do_not_load_definitions(extractor, monkeypatch):
    def forbidden(*args):
        pytest.fail("Extraction must not load external skill definitions")

    monkeypatch.setattr(extractor, "load_skill_details", forbidden, raising=False)
    turn = (
        extractor.Turn("use skill", "", None, 0)
        if extractor is copilot
        else extractor.Turn("use skill", None)
    )
    turn.add_skill("recorded-skill")
    turn.add_skill("recorded-skill")
    turn.add_skill("")
    assert turn.skills_used == ["recorded-skill"]


@pytest.mark.parametrize("extractor", [codex])
def test_command_invocations_do_not_load_definitions(extractor, monkeypatch):
    def forbidden(*args):
        pytest.fail("Extraction must not load external command definitions")

    monkeypatch.setattr(extractor, "load_command_details", forbidden, raising=False)
    turn = extractor.Turn("", None)
    turn.add_command("/recorded-command", "literal arguments")
    assert (turn.command, turn.command_args) == (
        "/recorded-command",
        "literal arguments",
    )
    turn.add_command("/recorded-command")
    assert turn.command_args is None


FIXTURES = Path(__file__).parent / "fixtures" / "claude"


def test_local_definitions_are_not_read_or_added_to_the_envelope(tmp_path, monkeypatch):
    project = (tmp_path / "project").resolve()
    shutil.copytree(FIXTURES / "project", project)
    definitions = {
        project / ".claude/skills/fixture-skill/SKILL.md": "LOCAL_SKILL_ONLY_MARKER",
        project / ".claude/commands/fixture-command.md": "LOCAL_COMMAND_ONLY_MARKER",
    }
    for path, marker in definitions.items():
        path.write_text(path.read_text(encoding="utf-8") + marker, encoding="utf-8")

    monkeypatch.chdir(project)
    original_open = Path.open

    def guarded_open(path, *args, **kwargs):
        if path.resolve() in definitions:
            pytest.fail(f"Extraction must not read local definitions: {path}")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    transcript = FIXTURES / "skill_command.jsonl"
    session = claude.interpret_source(claude.read_source(transcript))
    interactions = session["interactions"]
    assert interactions[0]["skills_used"] == ["fixture-skill"]
    assert interactions[1]["command"] == "/fixture-command"
    envelope = claude.extract_transcript(transcript)
    assert envelope is not None
    output = json.dumps(envelope)
    for marker in definitions.values():
        assert marker not in output
    # These markers belong to recorded assistant replies and must remain.
    assert "SKILL_FIXTURE_MARKER" in output
    assert "COMMAND_FIXTURE_MARKER" in output
