#!/usr/bin/env python3
"""Minimal asserts for local SKILL.md metadata extraction."""

from __future__ import annotations

import tempfile
from pathlib import Path

from skill_metadata import (
    load_command_details,
    load_skill_details,
    render_skill_lines,
)


def test_scalar_frontmatter_and_heading_are_rendered():
    with tempfile.TemporaryDirectory() as tmp_name:
        path = Path(tmp_name) / "sample" / "SKILL.md"
        path.parent.mkdir()
        path.write_text(
            "---\nname: sample\ndescription: A useful sample skill.\n---\n\n"
            "# Sample Workflow\n",
            encoding="utf-8",
        )
        details = load_skill_details("sample", str(path), Path(tmp_name))
        assert details["name"] == "sample"
        assert details["description"] == "A useful sample skill."
        assert details["title"] == "Sample Workflow"
        assert details["path"] == str(path.resolve())

        rendered = "\n".join(render_skill_lines("sample", details))
        assert "**sample** — A useful sample skill." in rendered
        assert f"`{path.resolve()}`" in rendered
        assert "_Title:_ Sample Workflow" in rendered


def test_folded_description_is_supported():
    with tempfile.TemporaryDirectory() as tmp_name:
        path = Path(tmp_name) / "folded" / "SKILL.md"
        path.parent.mkdir()
        path.write_text(
            "---\nname: folded\ndescription: >-\n  First sentence.\n"
            "  Second sentence.\n---\n",
            encoding="utf-8",
        )
        details = load_skill_details("folded", str(path), Path(tmp_name))
        assert details["description"] == "First sentence. Second sentence."


def test_section_headings_render_as_annotated_steps():
    with tempfile.TemporaryDirectory() as tmp_name:
        path = Path(tmp_name) / "stepped" / "SKILL.md"
        path.parent.mkdir()
        path.write_text(
            "---\nname: stepped\ndescription: Does stepped work.\n---\n\n"
            "# Stepped\n\n## Provisioning\n- Spin up the box.\n\n"
            "## Debugging\nRead the logs.\n",
            encoding="utf-8",
        )
        details = load_skill_details("stepped", str(path), Path(tmp_name))
        assert details["sections"] == [
            {"heading": "Provisioning", "summary": "Spin up the box."},
            {"heading": "Debugging", "summary": "Read the logs."},
        ]
        rendered = "\n".join(render_skill_lines("stepped", details))
        assert "_Steps:_" in rendered
        assert "1. Provisioning — Spin up the box." in rendered
        assert "2. Debugging — Read the logs." in rendered


def test_command_frontmatter_fields_render():
    with tempfile.TemporaryDirectory() as tmp_name:
        root = Path(tmp_name)
        path = root / ".claude" / "commands" / "runit.md"
        path.parent.mkdir(parents=True)
        path.write_text(
            "---\ndescription: Run it.\nallowed-tools: Bash(python3:*), Read\n"
            "argument-hint: <session-id>\n---\n\nDo the run.\n",
            encoding="utf-8",
        )
        details = load_command_details("/runit", root)
        assert details["meta"]["allowed-tools"] == "Bash(python3:*), Read"
        rendered = "\n".join(render_skill_lines("/runit", details, kind="Command"))
        assert "_Allowed tools:_ Bash(python3:*), Read" in rendered
        assert "_Arguments:_ <session-id>" in rendered


def test_command_details_load_and_render_with_kind():
    with tempfile.TemporaryDirectory() as tmp_name:
        root = Path(tmp_name)
        path = root / ".claude" / "commands" / "do-thing.md"
        path.parent.mkdir(parents=True)
        path.write_text(
            "---\ndescription: Do the thing.\n---\n\nRun the thing, then report.\n",
            encoding="utf-8",
        )
        details = load_command_details("/do-thing", root)
        assert details["description"] == "Do the thing."
        assert details["overview"] == "Run the thing, then report."
        rendered = "\n".join(render_skill_lines("/do-thing", details, kind="Command"))
        assert "_Command used:_ **/do-thing** — Do the thing." in rendered
        assert "_Overview:_ Run the thing, then report." in rendered


def test_prose_command_falls_back_to_paragraph_steps():
    with tempfile.TemporaryDirectory() as tmp_name:
        root = Path(tmp_name)
        path = root / ".claude" / "commands" / "prose.md"
        path.parent.mkdir(parents=True)
        path.write_text(
            "---\ndescription: Prose command.\n---\n\n"
            "Run the thing.\n\n!`do-it`\n\n**Then verify.** Check the output.\n",
            encoding="utf-8",
        )
        details = load_command_details("/prose", root)
        # `!`-exec line skipped; two prose paragraphs become two steps.
        assert [s["heading"] for s in details["sections"]] == [
            "Run the thing.",
            "**Then verify.** Check the output.",
        ]
        rendered = "\n".join(render_skill_lines("/prose", details, kind="Command"))
        assert "1. Run the thing." in rendered
        assert "2. **Then verify.** Check the output." in rendered


def test_single_paragraph_body_stays_overview_not_steps():
    with tempfile.TemporaryDirectory() as tmp_name:
        root = Path(tmp_name)
        path = root / ".claude" / "commands" / "solo.md"
        path.parent.mkdir(parents=True)
        path.write_text(
            "---\ndescription: Solo.\n---\n\nJust one line of prose.\n",
            encoding="utf-8",
        )
        details = load_command_details("/solo", root)
        assert details["sections"] == []
        rendered = "\n".join(render_skill_lines("/solo", details, kind="Command"))
        assert "_Overview:_ Just one line of prose." in rendered
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
