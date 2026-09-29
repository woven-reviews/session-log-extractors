#!/usr/bin/env python3
"""Minimal asserts for local SKILL.md metadata extraction."""

from __future__ import annotations

from pathlib import Path

from skill_metadata import (
    load_command_details,
    load_skill_details,
    render_skill_lines,
)

# This project's own real, already-committed skill/command definitions --
# reproducible in any checkout, no external fixture needed.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
REAL_SKILL = PROJECT_ROOT / ".agents/skills/extract-codex-session-logs/SKILL.md"

# A real, public, MIT-licensed SKILL.md (the "ponytail" Claude Code plugin),
# vendored verbatim into tests/fixtures/skills/ -- see that directory's
# README.md for provenance. Used for the frontmatter/section shapes this
# repo's own skills/commands don't happen to use (a folded YAML description,
# an argument-hint field).
PONYTAIL_SKILL = PROJECT_ROOT / "tests/fixtures/skills/ponytail/SKILL.md"

# The same plugin's OpenCode command variant is a real single-paragraph,
# heading-less command body -- no command in this repo's own
# .claude/commands/ happens to be that short. Its own root, so
# load_command_details resolves .claude/commands/ponytail-review.md beneath it.
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
    # Real capture: ponytail's frontmatter uses YAML's folded-scalar
    # (`description: >`) style across many lines.
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
    # Same real file also carries an argument-hint frontmatter field.
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
    # Real capture: this repo's own extract-claude-session-logs command.
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
    # Real capture: ponytail-review's OpenCode command variant -- a short
    # frontmatter description plus a one-paragraph body that differs from it,
    # so both the "_Command used:_ ... — description" and separate
    # "_Overview:_ ..." lines render.
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


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all passed")
