#!/usr/bin/env python3
"""Checks that fixture redaction keeps the native transcript shape."""

import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import generate_claude_fixtures as generator
import pytest
from generate_claude_fixtures import clean

from session_log_extractors.claude_sources import read_source

ROOT = Path(__file__).parent / "fixtures" / "claude" / "2.1.312"
TRANSCRIPT = ROOT / "native/65036844-09ed-4e91-8ed7-8636dfd163db.jsonl"
SUBAGENT = TRANSCRIPT.with_suffix("") / "subagents/agent-a2923ceb80fc3e3ac.jsonl"


def _entries(*lines):
    # One-based native line references are documented in 2.1.312/coverage.json.
    entries = read_source(TRANSCRIPT).entries
    return [entries[line - 1] for line in lines] if lines else entries


# The first prompt of each case in main(), so the fake CLI can tell which one runs.
PROMPT_CASES = {
    "BASELINE_FIXTURE_MARKER": "baseline",
    "/fixture-skill": "skill_command",
    "large.txt": "large_result",
    "denied.txt": "permission_denial",
    "subagent": "subagent",
    "FIRST_MODEL_FIXTURE_MARKER": "model",
    "context.txt": "continued",
}


def _fixture_entries(case):
    # The legacy print-mode generator validates its own prompt-specific markers.
    # Adapt the current interactive evidence only at those marker boundaries.
    lines_by_case = {
        "baseline": (6,),
        "skill_command": (236, 237, 538, 539),
        "large_result": (124, 125),
        "permission_denial": (492, 493),
        "subagent": (544, 545),
    }
    entries = _entries(*lines_by_case[case]) if case in lines_by_case else _entries()
    if case == "baseline":
        # Include the captured assistant reply after the opening human prompt.
        entries = [e for e in _entries() if e.get("type") in ("user", "assistant")][:2]
    if case == "skill_command":
        entries += _entries(245)
        entries = _replace(entries, "/capture-echo", "/fixture-command")
    if case == "continued":
        # These two response markers belong to the generator's hard-coded prompt.
        answers = [
            block
            for entry in entries
            if entry.get("type") == "assistant"
            for block in entry["message"]["content"]
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        for block in answers[-2:]:
            block["text"] = "RED-FOX-23 " + block["text"]
    return entries


def _replace(entries, old, new):
    return json.loads(json.dumps(entries, ensure_ascii=False).replace(old, new))


# Existing capture test setup; remove in QUAL-2547.
def _fake_claude(  # noqa: PLR0913
    monkeypatch,
    tmp_path,
    *,
    source=None,
    mutate=lambda entries: entries,
    returncode=0,
    transcript=True,
    sidecar=True,
    meta=True,
):
    """Stand in for the claude CLI: write the committed native transcript to PROJECTS.

    Use current native evidence, adapting markers to the print-mode prompts.
    """
    projects = tmp_path / "projects"
    monkeypatch.setattr(generator, "PROJECTS", projects)
    calls = []

    def fake_subprocess(args, **kwargs):
        calls.append((args, kwargs["cwd"]))
        session_id = args[
            args.index("--session-id" if "--session-id" in args else "--resume") + 1
        ]
        case = (
            source or next(c for key, c in PROMPT_CASES.items() if key in args[2])
            if "--session-id" in args
            else None
        )
        if case and transcript:
            project = projects / "-workspace"
            project.mkdir(parents=True, exist_ok=True)
            rows = mutate(_fixture_entries(case))
            (project / f"{session_id}.jsonl").write_text(
                "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n"
            )
            agents = project / session_id / "subagents"
            if case == "subagent" and sidecar:
                agents.mkdir(parents=True)
                (agents / "agent-fixture.jsonl").write_text(SUBAGENT.read_text())
                if meta:
                    (agents / "agent-fixture.meta.json").write_text(
                        SUBAGENT.with_suffix(".meta.json").read_text()
                    )
        return SimpleNamespace(returncode=returncode, stderr="boom")

    monkeypatch.setattr(generator.subprocess, "run", fake_subprocess)
    return calls


def test_main_stages_current_native_evidence_from_fake_cli(monkeypatch, tmp_path):
    output = tmp_path / "repo" / "claude"
    monkeypatch.setattr(generator, "ROOT", output)
    calls = _fake_claude(monkeypatch, tmp_path)
    generator.main()
    written = sorted(
        p.relative_to(output).as_posix() for p in output.rglob("*") if p.is_file()
    )
    assert written == [
        "baseline.jsonl",
        "continued.jsonl",
        "large_result.jsonl",
        "model.jsonl",
        "permission_denial.jsonl",
        "project/.claude/commands/fixture-command.md",
        "project/.claude/skills/fixture-skill/SKILL.md",
        "skill_command.jsonl",
        "subagent.jsonl",
        "subagent/subagents/agent-fixture.jsonl",
        "subagent/subagents/agent-fixture.meta.json",
    ]

    # Compare complete parsed native records after the generator sanitizes them.
    def records(path):
        text = path.read_text()
        return (
            text
            if path.suffix == ".md"
            else [json.loads(line) for line in text.splitlines()]
        )

    for name in written:
        if name.endswith(".md"):
            assert "FIXTURE_MARKER" in records(output / name)
        elif name.endswith(".meta.json"):
            assert records(output / name) == [
                json.loads(SUBAGENT.with_suffix(".meta.json").read_text())
            ]
        else:
            expected = (
                [json.loads(line) for line in SUBAGENT.read_text().splitlines()]
                if "/subagents/" in name
                else _fixture_entries(Path(name).stem)
            )
            # clean() redacts thinking in place; compare complete native structure.
            expected = [
                json.loads(line)
                for line in clean(expected, tmp_path / "unused-workspace").splitlines()
            ]
            assert records(output / name) == expected, name
    # Every capture runs headless and isolated, outside the repo.
    for args, cwd in calls:
        assert ["--permission-mode", "dontAsk"] == args[
            args.index("--permission-mode") :
        ][:2]
        assert ["--setting-sources", "project"] == args[
            args.index("--setting-sources") :
        ][:2]
        assert "--strict-mcp-config" in args
        assert tmp_path not in Path(cwd).parents
    assert not list(output.parent.glob("claude-fixture-staging-*"))


@pytest.mark.parametrize(
    ("case", "options", "message"),
    [
        ("baseline", {"returncode": 1}, "baseline: Claude exited 1: boom"),
        (
            "baseline",
            {"transcript": False},
            "baseline: expected one transcript, found 0",
        ),
        (
            "skill_command",
            {"source": "baseline"},
            "skill_command: expected Skill tool call",
        ),
        (
            "skill_command",
            {
                "mutate": lambda e: _replace(
                    e, "<command-name>/fixture-command</command-name>", ""
                )
            },
            "skill_command: direct slash command was not recorded",
        ),
        (
            "permission_denial",
            {
                "mutate": lambda e: _replace(
                    _replace(e, "denied", "refused"), "Denied", "Refused"
                )
            },
            "permission_denial: no denial result",
        ),
        (
            "model",
            {
                "mutate": lambda e: _replace(
                    e, "<command-name>/model</command-name>", ""
                )
            },
            "model: direct /model command was not recorded",
        ),
        (
            "model",
            {
                "mutate": lambda e: [
                    {**row, "message": {**row["message"], "model": "claude-haiku"}}
                    if row.get("type") == "assistant"
                    else row
                    for row in e
                ]
            },
            "model: resume did not produce two model IDs",
        ),
        (
            "baseline",
            {"mutate": lambda e: [row for row in e if row.get("type") != "assistant"]},
            "baseline: transcript has no assistant message",
        ),
        (
            "continued",
            {"mutate": lambda e: _replace(e, "RED-FOX-23", "UNKNOWN")},
            "continued: follow-up did not retain the fallback codename",
        ),
        ("subagent", {"sidecar": False}, "subagent: expected one sidecar, found 0"),
        ("subagent", {"meta": False}, "subagent: missing native metadata sidecar"),
    ],
)
def test_run_case_rejects_incomplete_native_capture(
    monkeypatch, tmp_path, case, options, message
):
    _fake_claude(monkeypatch, tmp_path, **{"source": case, **options})
    with pytest.raises(RuntimeError) as exc:
        generator.run_case(case, "prompt")
    assert str(exc.value) == message


def test_clean_preserves_records_blocks_and_ids():
    entries = _entries()
    expected = deepcopy(entries)
    thinking_blocks = [
        block
        for entry in expected
        for block in entry.get("message", {}).get("content", [])
        if isinstance(block, dict) and block.get("type") == "thinking"
    ]
    assert thinking_blocks
    assert any(entry.get("type") == "system" for entry in entries)
    assert any("toolUseResult" in entry for entry in entries)
    for block in thinking_blocks:
        block["thinking"] = "[redacted]"
    # Use an unrelated workspace so only thinking redaction applies here.
    rows = [
        json.loads(line)
        for line in clean(entries, Path("/tmp/fixture-project")).splitlines()
    ]
    assert rows == expected
    assert [row.get("uuid") for row in rows] == [row.get("uuid") for row in entries]


def test_clean_rejects_unknown_thinking_shape():
    entries = _entries()
    block = next(
        block
        for entry in entries
        for block in entry.get("message", {}).get("content", [])
        if isinstance(block, dict) and block.get("type") == "thinking"
    )
    # Replace only the known thinking field with an unsupported upstream shape.
    block["text"] = block.pop("thinking")
    with pytest.raises(
        RuntimeError, match="unexpected thinking block format"
    ) as exc_info:
        clean(entries, Path("/tmp/fixture-project"))
    assert str(exc_info.value) == "unexpected thinking block format"


def test_clean_redacts_encoded_project_dir():
    entries = [{"text": "/home/x/.claude/projects/-srv-fixture-project/memory/"}]
    assert "-workspace/memory/" in clean(entries, Path("/srv/fixture-project"))


def test_clean_rejects_leftover_private_temp_path():
    entries = [{"text": "-private-var-folders-ab-T-other"}]
    with pytest.raises(
        RuntimeError, match="private path remains after sanitizing"
    ) as exc_info:
        clean(entries, Path("/tmp/fixture-project"))

    assert str(exc_info.value) == "private path remains after sanitizing"
