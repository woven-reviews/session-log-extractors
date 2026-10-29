"""Extraction must use recorded evidence without reading local definitions."""

import json
import shutil
from pathlib import Path

import pytest

from session_log_extractors import copilot_sources
from session_log_extractors import extract_claude_session_log as claude
from session_log_extractors import extract_codex_session_log as codex
from session_log_extractors import extract_copilot_session_log as copilot


@pytest.mark.parametrize("command_args", ["fixture", None])
def test_codex_invocations_do_not_load_definitions(command_args, monkeypatch):
    fixture_root = Path(__file__).parent / "fixtures" / "codex"
    source = codex.read_source(fixture_root / "slash.jsonl")
    if command_args is None:
        # Remove only the captured command arguments to exercise their absence.
        next(
            e["payload"] for e in source.entries if e["payload"].get("role") == "user"
        )["content"][0]["text"] = "/review"
    # The fixture has no explicit skill call. Add a narrow structured call,
    # repeated to retain the existing de-duplication assertion.
    call = {
        "type": "response_item",
        "payload": {
            "type": "function_call",
            "name": "skills.read",
            "arguments": '{"package": "recorded-skill"}',
        },
    }
    empty_call = {
        **call,
        "payload": {**call["payload"], "arguments": '{"package": ""}'},
    }
    source.entries.extend([call, call, empty_call])

    def forbidden(*args, **kwargs):
        pytest.fail("Extraction must not read external definitions")

    monkeypatch.setattr(Path, "open", forbidden)
    session = codex.interpret_source(source)
    assert session["skills_used"] == ["recorded-skill"]
    user = session["events"][0]
    assert (user["command"], user["command_args"]) == ("/review", command_args)


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
    assert session["skills_used"] == ["fixture-skill"]
    assert any(
        event.get("command") == "/fixture-command" for event in session["events"]
    )
    envelope = claude.extract_transcript(transcript)
    assert envelope is not None
    output = json.dumps(envelope)
    for marker in definitions.values():
        assert marker not in output
    # These markers belong to recorded assistant replies and must remain.
    assert "SKILL_FIXTURE_MARKER" in output
    assert "COMMAND_FIXTURE_MARKER" in output


def test_copilot_skill_invocations_do_not_load_definitions(monkeypatch):
    fixtures = FIXTURES.parent / "copilot"
    records = copilot_sources.parse_state_records(
        (fixtures / "skill" / "events.jsonl").read_text().splitlines()
    )
    # A repeated invocation and an empty name must preserve deduplication.
    invoked = next(record for record in records if record["type"] == "skill.invoked")
    records.extend([invoked, {"type": "skill.invoked", "data": {"name": ""}}])
    source = copilot_sources.CopilotSource(
        {"id": "recorded-skill"}, [], state_records=records
    )

    def forbidden(*args, **kwargs):
        pytest.fail("Extraction must not load external skill definitions")

    with monkeypatch.context() as guard:
        guard.setattr(Path, "open", forbidden)
        session = copilot.interpret_source(source)
    assert session["skills_used"] == ["fixture-skill"]
