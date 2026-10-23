"""Extraction must use recorded evidence without reading local definitions."""

import json
import shutil
from pathlib import Path

import extract_claude_session_log as claude
import pytest

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

    monkeypatch.setattr(claude, "PROJECT_ROOT", project)
    monkeypatch.chdir(project)
    original_open = Path.open

    def guarded_open(path, *args, **kwargs):
        if path.resolve() in definitions:
            pytest.fail(f"Extraction must not read local definitions: {path}")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    transcript = FIXTURES / "skill_command.jsonl"
    turns, _ = claude.build_turns(claude.load_entries(transcript))
    assert turns[0].skills_used == ["fixture-skill"]
    assert turns[1].command == "/fixture-command"
    envelope = claude.extract_transcript(transcript)
    assert envelope is not None
    output = json.dumps(envelope)
    for marker in definitions.values():
        assert marker not in output
    # These markers belong to recorded assistant replies and must remain.
    assert "SKILL_FIXTURE_MARKER" in output
    assert "COMMAND_FIXTURE_MARKER" in output
