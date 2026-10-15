#!/usr/bin/env python3
"""Checks that fixture redaction keeps the native transcript shape."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import generate_claude_fixtures as generator
from generate_claude_fixtures import clean

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "claude"
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
    return [json.loads(line) for line in (FIXTURES / f"{case}.jsonl").read_text().splitlines()]


def _replace(entries, old, new):
    return json.loads(json.dumps(entries, ensure_ascii=False).replace(old, new))


def _fake_claude(monkeypatch, tmp_path, *, source=None, mutate=lambda entries: entries,
                 returncode=0, transcript=True, sidecar=True, meta=True):
    """Stand in for the claude CLI: write the committed native transcript to PROJECTS.

    The fixtures are already sanitized, so regenerating from them must reproduce them.
    """
    projects = tmp_path / "projects"
    monkeypatch.setattr(generator, "PROJECTS", projects)
    calls = []

    def fake_subprocess(args, **kwargs):
        calls.append((args, kwargs["cwd"]))
        session_id = args[args.index("--session-id" if "--session-id" in args else "--resume") + 1]
        case = source or next(c for key, c in PROMPT_CASES.items() if key in args[2]) \
            if "--session-id" in args else None
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
                native = FIXTURES / "subagent" / "subagents"
                (agents / "agent-fixture.jsonl").write_text(
                    (native / "agent-fixture.jsonl").read_text())
                if meta:
                    (agents / "agent-fixture.meta.json").write_text(
                        (native / "agent-fixture.meta.json").read_text())
        return SimpleNamespace(returncode=returncode, stderr="boom")

    monkeypatch.setattr(generator.subprocess, "run", fake_subprocess)
    return calls


def test_main_regenerates_committed_fixtures_from_native_transcripts(monkeypatch, tmp_path):
    output = tmp_path / "repo" / "claude"
    monkeypatch.setattr(generator, "ROOT", output)
    calls = _fake_claude(monkeypatch, tmp_path)
    generator.main()
    written = sorted(p.relative_to(output).as_posix() for p in output.rglob("*") if p.is_file())
    assert written == [
        "baseline.jsonl", "continued.jsonl", "large_result.jsonl", "model.jsonl",
        "permission_denial.jsonl",
        "project/.claude/commands/fixture-command.md",
        "project/.claude/skills/fixture-skill/SKILL.md",
        "skill_command.jsonl", "subagent.jsonl",
        "subagent/subagents/agent-fixture.jsonl",
        "subagent/subagents/agent-fixture.meta.json",
    ]
    # Compare parsed records: committed baseline.jsonl is compact JSON, same content.
    def records(path):
        text = path.read_text()
        return text if path.suffix == ".md" else [json.loads(line) for line in text.splitlines()]

    for name in written:
        assert records(output / name) == records(FIXTURES / name), name
    # Every capture runs headless and isolated, outside the repo.
    for args, cwd in calls:
        assert ["--permission-mode", "dontAsk"] == args[args.index("--permission-mode"):][:2]
        assert ["--setting-sources", "project"] == args[args.index("--setting-sources"):][:2]
        assert "--strict-mcp-config" in args
        assert tmp_path not in Path(cwd).parents
    assert not list(output.parent.glob("claude-fixture-staging-*"))


@pytest.mark.parametrize(("case", "options", "message"), [
    ("baseline", {"returncode": 1}, "baseline: Claude exited 1: boom"),
    ("baseline", {"transcript": False}, "baseline: expected one transcript, found 0"),
    ("skill_command", {"source": "baseline"}, "skill_command: expected Skill tool call"),
    ("skill_command",
     {"mutate": lambda e: _replace(e, "<command-name>/fixture-command</command-name>", "")},
     "skill_command: direct slash command was not recorded"),
    ("permission_denial",
     {"mutate": lambda e: _replace(_replace(e, "denied", "refused"), "Denied", "Refused")},
     "permission_denial: no denial result"),
    ("model", {"mutate": lambda e: _replace(e, "<command-name>/model</command-name>", "")},
     "model: direct /model command was not recorded"),
    ("model",
     {"mutate": lambda e: [
         {**row, "message": {**row["message"], "model": "claude-haiku"}}
         if row.get("type") == "assistant" else row for row in e]},
     "model: resume did not produce two model IDs"),
    ("baseline", {"mutate": lambda e: [row for row in e if row.get("type") != "assistant"]},
     "baseline: transcript has no assistant message"),
    ("continued", {"mutate": lambda e: _replace(e, "RED-FOX-23", "UNKNOWN")},
     "continued: follow-up did not retain the fallback codename"),
    ("subagent", {"sidecar": False}, "subagent: expected one sidecar, found 0"),
    ("subagent", {"meta": False}, "subagent: missing native metadata sidecar"),
])
def test_run_case_rejects_incomplete_native_capture(monkeypatch, tmp_path, case, options, message):
    _fake_claude(monkeypatch, tmp_path, **{"source": case, **options})
    with pytest.raises(RuntimeError) as exc:
        generator.run_case(case, "prompt")
    assert str(exc.value) == message


def test_clean_preserves_records_blocks_and_ids():
    session_id = "12345678-1234-1234-1234-123456789abc"
    entries = [
        {"type": "system", "message": "init", "sessionId": session_id},
        {"type": "user", "message": {"content": []}, "toolUseResult": {"stdout": "ok"}},
        {"type": "assistant", "message": {"content": [
            {"type": "thinking", "thinking": "private", "signature": "sig"},
            {"type": "text", "text": "/tmp/fixture-project"},
        ]}},
    ]
    rows = [json.loads(line) for line in clean(entries, Path("/tmp/fixture-project")).splitlines()]
    assert [row["type"] for row in rows] == ["system", "user", "assistant"]
    assert rows[0]["sessionId"] == session_id
    assert rows[1]["toolUseResult"] == {"stdout": "ok"}
    assert rows[2]["message"]["content"] == [
        {"type": "thinking", "thinking": "[redacted]", "signature": "sig"},
        {"type": "text", "text": "/workspace"},
    ]


def test_clean_rejects_unknown_thinking_shape():
    entries = [{"message": {"content": [{"type": "thinking", "text": "new format"}]}}]
    try:
        clean(entries, Path("/tmp/fixture-project"))
    except RuntimeError as exc:
        assert str(exc) == "unexpected thinking block format"
    else:
        raise AssertionError("unknown thinking format must be reviewed")


def test_clean_redacts_encoded_project_dir():
    entries = [{"text": "/home/x/.claude/projects/-srv-fixture-project/memory/"}]
    assert "-workspace/memory/" in clean(entries, Path("/srv/fixture-project"))


def test_clean_rejects_leftover_private_temp_path():
    entries = [{"text": "-private-var-folders-ab-T-other"}]
    try:
        clean(entries, Path("/tmp/fixture-project"))
    except RuntimeError as exc:
        assert str(exc) == "private path remains after sanitizing"
    else:
        raise AssertionError("encoded temp path must be rejected")
