#!/usr/bin/env python3
"""Minimal asserts for local SKILL.md metadata extraction."""

from __future__ import annotations

from pathlib import Path

import skill_metadata
from skill_metadata import (
    load_command_details,
    load_skill_details,
    render_skill_lines,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REAL_SKILL = PROJECT_ROOT / ".agents/skills/extract-codex-session-logs/SKILL.md"

PONYTAIL_SKILL = PROJECT_ROOT / "tests/fixtures/skills/ponytail/SKILL.md"

PONYTAIL_COMMAND_ROOT = PROJECT_ROOT / "tests/fixtures/skills/ponytail-command-root"

def test_scalar_frontmatter_and_heading_are_rendered():
    details = load_skill_details(
        "extract-codex-session-logs", str(REAL_SKILL), PROJECT_ROOT
    )
    assert details["name"] == "extract-codex-session-logs"
    assert details["description"].startswith(
        "Manual-only helper for exporting all Codex session logs"
    )
    assert details["title"] == "Extract Codex Session Logs"
    assert details["path"] == str(REAL_SKILL.resolve())

    rendered = "\n".join(render_skill_lines("extract-codex-session-logs", details))
    assert "**extract-codex-session-logs** — Manual-only helper" in rendered
    assert f"`{REAL_SKILL.resolve()}`" in rendered
    assert "_Title:_ Extract Codex Session Logs" in rendered

def test_folded_description_is_supported():
    details = load_skill_details("ponytail", str(PONYTAIL_SKILL), PROJECT_ROOT)
    assert details["description"] == (
        "Forces the laziest solution that actually works, simplest, shortest, "
        "most minimal. Channels a senior dev who has seen everything: question "
        "whether the task needs to exist at all (YAGNI), reach for the "
        "standard library before custom code, native platform features "
        "before dependencies, one line before fifty. Supports intensity "
        "levels: lite, full (default), ultra. Use on ANY coding task: "
        "writing, adding, refactoring, fixing, reviewing, or designing code, "
        'and choosing libraries or dependencies. Also use whenever the user '
        'says "ponytail", "be lazy", "lazy mode", "simplest solution", '
        '"minimal solution", "yagni", "do less", or "shortest path", or '
        "complains about over-engineering, bloat, boilerplate, or "
        "unnecessary dependencies. Do NOT use for non-coding requests "
        "(general knowledge, prose, translation, summaries, recipes)."
    )
    assert details["meta"]["argument-hint"] == "[lite|full|ultra]"
    rendered = "\n".join(render_skill_lines("ponytail", details))
    assert "_Arguments:_ [lite|full|ultra]" in rendered

def test_section_headings_render_as_annotated_steps():
    details = load_skill_details("ponytail", str(PONYTAIL_SKILL), PROJECT_ROOT)
    assert details["sections"] == [
        {
            "heading": "Persistence",
            "summary": (
                "ACTIVE EVERY RESPONSE. No drift back to over-building. "
                "Still active if"
            ),
        },
        {"heading": "The ladder", "summary": "Stop at the first rung that holds:"},
        {
            "heading": "Rules",
            "summary": (
                "No unrequested abstractions: no interface with one "
                "implementation, no factory for one product, no config for "
                "a value..."
            ),
        },
        {
            "heading": "Output",
            "summary": (
                "Code first. Then at most three short lines: what was "
                "skipped, when to add it."
            ),
        },
        {
            "heading": "Intensity",
            "summary": 'Example: "Add a cache for these API responses."',
        },
        {
            "heading": "When NOT to be lazy",
            "summary": (
                "Never simplify away: input validation at trust boundaries, "
                "error handling"
            ),
        },
        {
            "heading": "Boundaries",
            "summary": (
                "Ponytail governs what you build, not how you talk (pair "
                "with Caveman for"
            ),
        },
    ]
    rendered = "\n".join(render_skill_lines("ponytail", details))
    assert "_Steps:_" in rendered
    assert "1. Persistence — ACTIVE EVERY RESPONSE." in rendered
    assert "7. Boundaries — Ponytail governs what you build" in rendered

def test_command_frontmatter_fields_render():
    details = load_command_details("/extract-claude-session-logs", PROJECT_ROOT)
    assert details["meta"]["allowed-tools"] == (
        "Bash(docker build:*), Bash(docker run:*), Bash(git rev-parse:*), "
        "Bash(command -v docker:*), Read, Edit"
    )
    rendered = "\n".join(
        render_skill_lines(
            "/extract-claude-session-logs", details, kind="Command"
        )
    )
    assert (
        "_Allowed tools:_ Bash(docker build:*), Bash(docker run:*), "
        "Bash(git rev-parse:*), Bash(command -v docker:*), Read, Edit"
    ) in rendered

def test_command_details_load_and_render_with_kind():
    details = load_command_details("/ponytail-review", PONYTAIL_COMMAND_ROOT)
    assert details["description"] == (
        "Review changes for over-engineering, what can be deleted"
    )
    assert details["overview"].startswith(
        "Review the current code changes for over-engineering only"
    )
    rendered = "\n".join(
        render_skill_lines("/ponytail-review", details, kind="Command")
    )
    assert (
        "_Command used:_ **/ponytail-review** — Review changes for "
        "over-engineering, what can be deleted"
    ) in rendered
    assert "_Overview:_ Review the current code changes" in rendered

def test_prose_command_falls_back_to_paragraph_steps():
    # Real capture: extract-claude-session-logs.md has no `##` headings, a
    # `!`-exec line that must be skipped, and several prose paragraphs
    # (including a markdown table one command author wrote as a plain
    # paragraph) that become the step fallback.
    details = load_command_details("/extract-claude-session-logs", PROJECT_ROOT)
    headings = [s["heading"] for s in details["sections"]]
    assert len(headings) >= 2
    assert headings[0].startswith("Run the session log extractor and report")
    # The `!`-prefixed shell-exec line is real-world noise this fallback must
    # skip rather than turn into its own (nonsensical) step.
    assert not any(h.startswith("!") for h in headings)
    rendered = "\n".join(
        render_skill_lines("/extract-claude-session-logs", details, kind="Command")
    )
    assert "_Steps:_" in rendered
    assert "1. Run the session log extractor and report" in rendered

def test_single_paragraph_body_stays_overview_not_steps():
    # Same real command as above: one paragraph is below _prose_steps'
    # 2-paragraph threshold for the "steps" fallback, so it stays an overview.
    details = load_command_details("/ponytail-review", PONYTAIL_COMMAND_ROOT)
    assert details["sections"] == []
    rendered = "\n".join(
        render_skill_lines("/ponytail-review", details, kind="Command")
    )
    assert "_Overview:_ Review the current code changes" in rendered
    assert "_Steps:_" not in rendered

def test_missing_definition_keeps_name_only_output():
    details = load_skill_details(
        "definitely-not-an-installed-skill",
        "/missing/skill/SKILL.md",
        Path("/missing/project"),
    )
    assert details == {}
    assert render_skill_lines("unknown", details) == ["_Skill used:_ **unknown**"]

def test_frontmatter_body_and_prose_parsers_cover_supported_shapes():
    assert skill_metadata._frontmatter("missing delimiters") == {}
    assert skill_metadata._frontmatter("---\nname: unfinished") == {}
    metadata = skill_metadata._frontmatter(
        "---\n"
        "name: 'quoted name'\n"
        "description: >-\n  first line\n  second line\n\n"
        "homepage: https://example.test\n"
        "nested:\n  ignored: value\n"
        "---\n"
    )
    assert metadata == {
        "name": "quoted name",
        "description": "first line second line",
        "homepage": "https://example.test",
        "nested": "",
    }
    assert skill_metadata._body_lines("---\nname: sample\n---\nbody") == ["body"]
    assert skill_metadata._heading("# Visible") == "Visible"
    assert skill_metadata._prose_steps(
        "---\nname: sample\n---\nFirst paragraph.\n\n!run this\n\n"
        "```sh\n# ignored\n```\n\nSecond paragraph."
    ) == [
        {"heading": "First paragraph.", "summary": ""},
        {"heading": "Second paragraph.", "summary": ""},
    ]
    assert skill_metadata._overview("```md\nignored\n```\n\nPurpose.\n\nMore.") == (
        "Purpose."
    )

def test_skill_path_candidates_and_cached_read_failures(tmp_path, monkeypatch):
    home = tmp_path / "home"
    project = tmp_path / "project"
    installed = (
        home
        / ".claude/plugins/cache/namespace/1.0/skills/ponytail/SKILL.md"
    )
    installed.parent.mkdir(parents=True)
    installed.write_text("# Plugin skill\n", encoding="utf-8")
    monkeypatch.setattr(skill_metadata.Path, "home", staticmethod(lambda: home))

    details = load_skill_details(
        "namespace:ponytail", project_root=project
    )
    assert details["title"] == "Plugin skill"
    assert list(skill_metadata._candidate_paths("bad/name", None, project)) == []
    assert list(skill_metadata._candidate_paths("..", None, project)) == []
    assert skill_metadata._read_definition(str(tmp_path / "missing")) == {}
    assert skill_metadata._read_definition.cache_info().currsize > 0

def test_command_candidates_and_render_compaction(tmp_path, monkeypatch):
    home = tmp_path / "home"
    commands = home / ".claude/commands"
    commands.mkdir(parents=True)
    (commands / "simple.md").write_text("One line overview.", encoding="utf-8")
    monkeypatch.setattr(skill_metadata.Path, "home", staticmethod(lambda: home))

    details = load_command_details("plugin:simple", project_root=tmp_path / "project")
    assert details["overview"] == "One line overview."
    assert list(
        skill_metadata._command_candidate_paths("nested/command", tmp_path)
    ) == []
    assert skill_metadata._compact(" a\n b ") == "a b"
    rendered = render_skill_lines(
        "simple",
        {
            "description": "d" * 405,
            "title": "Different title",
            "name": "defined name",
            "meta": {"model": "m" * 405},
            "overview": "purpose",
        },
        indent="  ",
        kind="Command",
    )
    assert rendered[0].endswith("...")
    assert "_Title:_ Different title" in rendered[1]
    assert rendered[2].endswith("...")

if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all passed")
