"""Extraction must use recorded evidence without reading local definitions."""

import extract_claude_session_log as claude
import extract_codex_session_log as codex
import extract_copilot_session_log as copilot
import pytest


@pytest.mark.parametrize("extractor", [claude, codex, copilot])
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


@pytest.mark.parametrize("extractor", [claude, codex])
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
