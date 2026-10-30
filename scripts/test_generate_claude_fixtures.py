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
(TRANSCRIPT,) = (ROOT / "native").glob("*.jsonl")
(SUBAGENT,) = (TRANSCRIPT.with_suffix("") / "subagents").glob("*.jsonl")


def _recorded_session_entries():
    return read_source(TRANSCRIPT).entries


def _require_single_match(matches, description):
    assert len(matches) == 1, f"Expected one {description}, found {len(matches)}"
    (match,) = matches
    return match


def _message_blocks(entry):
    content = entry.get("message", {}).get("content", [])
    return content if isinstance(content, list) else [{"type": "text", "text": content}]


def _recorded_user_message(marker):
    return _require_single_match(
        [
            entry
            for entry in _recorded_session_entries()
            if entry.get("type") == "user"
            and not entry.get("isMeta")
            and not entry.get("isCompactSummary")
            and any(
                isinstance(block, dict)
                and block.get("type") == "text"
                and marker in block["text"]
                for block in _message_blocks(entry)
            )
        ],
        f"user message containing {marker!r}",
    )


def _recorded_tool_interaction_entries(name, input_contains):
    entries = _recorded_session_entries()
    requests = [
        (entry, block)
        for entry in entries
        if entry.get("type") == "assistant"
        for block in _message_blocks(entry)
        if isinstance(block, dict)
        and block.get("type") == "tool_use"
        and block.get("name") == name
        and input_contains in json.dumps(block.get("input"), ensure_ascii=False)
    ]
    assert requests, f"Missing {name} activity matching {input_contains!r}"
    messages = [entry for entry, _ in requests]
    call_ids = {block["id"] for _, block in requests}
    # Associate results using IDs from the already selected native calls.
    results = [
        entry
        for entry in entries
        if entry.get("type") == "user"
        and any(
            isinstance(block, dict)
            and block.get("type") == "tool_result"
            and block.get("tool_use_id") in call_ids
            for block in _message_blocks(entry)
        )
    ]
    assert len(results) == len(requests), "Missing or ambiguous tool results"
    return [entry for entry in entries if entry in messages or entry in results]


def _recorded_conversation_entries():
    user = _recorded_user_message("Remember **CAPTURE_CONTEXT_CEDAR_73**")
    # Any native assistant prose can exercise collection and sanitization.
    replies = [
        entry
        for entry in _recorded_session_entries()
        if entry.get("type") == "assistant"
        and any(
            isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
            and block["text"].strip()
            for block in _message_blocks(entry)
        )
    ]
    assert replies, "Missing native assistant text"
    return [user, *replies]


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


def _recorded_skill_and_command_entries():
    entries = _recorded_tool_interaction_entries("Skill", "capture-leaf")
    entries.append(_recorded_user_message("<command-name>/capture-echo</command-name>"))
    # Adapt the native command to the legacy generator's fixed prompt marker.
    return _replace(entries, "/capture-echo", "/fixture-command")


def _recorded_conversation_with_legacy_codename():
    entries = _recorded_session_entries()
    # The legacy generator checks this codename in its follow-up response.
    for entry in entries:
        if entry.get("type") == "assistant":
            for block in _message_blocks(entry):
                if isinstance(block, dict) and block.get("type") == "text":
                    block["text"] = "RED-FOX-23 " + block["text"]
    return entries


def _recorded_large_read_entries():
    return _recorded_tool_interaction_entries("Read", "multibyte.txt")


def _recorded_settings_denial_entries():
    return _recorded_tool_interaction_entries("Bash", "CAPTURE_SETTINGS_REFUSAL")


def _recorded_subagent_invocation_entries():
    return _recorded_tool_interaction_entries("Agent", "")


def _recorded_model_switch_entries():
    return _recorded_session_entries()


# Each factory names the native scenario supplied to a legacy generator prompt.
GENERATOR_SCENARIOS = {
    "baseline": _recorded_conversation_entries,
    "skill_command": _recorded_skill_and_command_entries,
    "continued": _recorded_conversation_with_legacy_codename,
    "large_result": _recorded_large_read_entries,
    "permission_denial": _recorded_settings_denial_entries,
    "subagent": _recorded_subagent_invocation_entries,
    "model": _recorded_model_switch_entries,
}


def _native_entries_for_generator_case(case):
    return GENERATOR_SCENARIOS[case]()


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
            rows = mutate(_native_entries_for_generator_case(case))
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


@pytest.fixture
def generated_capture(monkeypatch, tmp_path):
    output = tmp_path / "repo" / "claude"
    monkeypatch.setattr(generator, "ROOT", output)
    calls = _fake_claude(monkeypatch, tmp_path)
    generator.main()
    return output, calls


def test_main_writes_all_capture_artifacts(generated_capture):
    output, _ = generated_capture
    written = sorted(
        path.relative_to(output).as_posix()
        for path in output.rglob("*")
        if path.is_file()
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


@pytest.mark.parametrize("case", GENERATOR_SCENARIOS)
def test_main_preserves_native_records_after_sanitization(generated_capture, case):
    output, _ = generated_capture
    native_entries = _native_entries_for_generator_case(case)
    # clean() has its own focused tests; this checks staging of its complete output.
    expected = [
        json.loads(line)
        for line in clean(native_entries, Path("/unused-workspace")).splitlines()
    ]
    actual = [
        json.loads(line) for line in (output / f"{case}.jsonl").read_text().splitlines()
    ]
    assert actual == expected


def test_main_preserves_subagent_records_and_metadata(generated_capture):
    output, _ = generated_capture
    sidecar = output / "subagent/subagents/agent-fixture.jsonl"
    native_entries = [json.loads(line) for line in SUBAGENT.read_text().splitlines()]
    expected = [
        json.loads(line)
        for line in clean(native_entries, Path("/unused-workspace")).splitlines()
    ]
    assert [json.loads(line) for line in sidecar.read_text().splitlines()] == expected
    assert json.loads(sidecar.with_suffix(".meta.json").read_text()) == json.loads(
        SUBAGENT.with_suffix(".meta.json").read_text()
    )


@pytest.mark.parametrize(
    "definition",
    [
        ".claude/commands/fixture-command.md",
        ".claude/skills/fixture-skill/SKILL.md",
    ],
)
def test_main_stages_project_definitions(generated_capture, definition):
    output, _ = generated_capture
    assert "FIXTURE_MARKER" in (output / "project" / definition).read_text()


def test_main_runs_cli_headless_and_isolated(generated_capture, tmp_path):
    _, calls = generated_capture
    assert calls
    for args, cwd in calls:
        permission_index = args.index("--permission-mode")
        settings_index = args.index("--setting-sources")
        assert args[permission_index + 1] == "dontAsk"
        assert args[settings_index + 1] == "project"
        assert "--strict-mcp-config" in args
        assert tmp_path not in Path(cwd).parents


def test_main_removes_staging_directory(generated_capture):
    output, _ = generated_capture
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
    entries = _recorded_session_entries()
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


@pytest.mark.parametrize(
    "workspace", [Path("/tmp/fixture-project"), Path("fixture-project")]
)
def test_clean_redacts_workspace_in_captured_text(workspace):
    entries = _recorded_conversation_entries()
    blocks = [
        block
        for entry in entries
        if entry.get("type") == "assistant"
        for block in _message_blocks(entry)
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    assert blocks
    # Insert raw and resolved workspace paths into native text to exercise
    # redaction even though the committed capture is already sanitized.
    for block in blocks:
        block["text"] = "Read /workspace/hello.py and /workspace/hello.py"
    expected = deepcopy(entries)
    for block in blocks:
        block["text"] = f"Read {workspace}/hello.py and {workspace.resolve()}/hello.py"
    rows = [json.loads(line) for line in clean(entries, workspace).splitlines()]
    assert rows == expected


def test_clean_rejects_unknown_thinking_shape():
    entries = _recorded_session_entries()
    blocks = [
        block
        for entry in entries
        for block in _message_blocks(entry)
        if isinstance(block, dict) and block.get("type") == "thinking"
    ]
    assert blocks
    # Introduce the unsupported shape on every thinking block, without choosing
    # an arbitrary occurrence from the capture.
    for block in blocks:
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


@pytest.mark.parametrize(
    "case",
    [
        "baseline",
        "skill_command",
        "large_result",
        "permission_denial",
        "subagent",
        "model",
        "continued",
    ],
)
def test_case_selection_survives_reordered_records_regenerated_ids_and_prose(
    case, monkeypatch
):
    expected = _native_entries_for_generator_case(case)
    native = _recorded_session_entries()
    for entry in native:
        if "uuid" in entry:
            entry["uuid"] = "regenerated-" + entry["uuid"]
        for block in _message_blocks(entry):
            if (
                entry.get("type") == "assistant"
                and isinstance(block, dict)
                and block.get("type") == "text"
            ):
                block["text"] = "Acknowledged; this response uses different wording."
            if isinstance(block, dict) and block.get("type") == "tool_use":
                block["id"] = "regenerated-" + block["id"]
            elif isinstance(block, dict) and block.get("type") == "tool_result":
                block["tool_use_id"] = "regenerated-" + block["tool_use_id"]
        content = entry.get("message", {}).get("content")
        if isinstance(content, list):
            content.reverse()
    native.reverse()
    monkeypatch.setattr(__name__ + "._recorded_session_entries", lambda: native)
    selected = _native_entries_for_generator_case(case)
    assert sorted(entry["type"] for entry in selected) == sorted(
        entry["type"] for entry in expected
    )
    assert all(
        entry["uuid"].startswith("regenerated-")
        for entry in selected
        if "uuid" in entry
    )
    expected_inputs = sorted(
        (block["name"], json.dumps(block["input"], sort_keys=True))
        for entry in expected
        for block in _message_blocks(entry)
        if isinstance(block, dict) and block.get("type") == "tool_use"
    )
    actual_inputs = sorted(
        (block["name"], json.dumps(block["input"], sort_keys=True))
        for entry in selected
        for block in _message_blocks(entry)
        if isinstance(block, dict) and block.get("type") == "tool_use"
    )
    assert actual_inputs == expected_inputs
