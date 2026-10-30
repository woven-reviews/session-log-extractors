"""Production extraction uses the public read/interpret stage contract."""

import json
import sqlite3
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest
from copilot_fixture import DATABASE, SESSION_ID, native_source, native_tool_source

from session_log_extractors import copilot_sources
from session_log_extractors import extract_claude_session_log as claude
from session_log_extractors import extract_codex_session_log as codex
from session_log_extractors import extract_copilot_session_log as copilot
from session_log_extractors.claude_sources import ClaudeSource
from session_log_extractors.session_log_envelope import export_session

ROOT = Path(__file__).parent / "fixtures" / "claude" / "2.1.312"
(TRANSCRIPT,) = (ROOT / "native").glob("*.jsonl")


FIXTURES = Path(__file__).parent / "fixtures" / "claude"


def _recorded_claude_read_interaction():
    entries = claude.read_source(TRANSCRIPT).entries
    read_interactions = []
    for entry in entries:
        if entry.get("type") == "assistant":
            for block in claude._content_blocks(entry):
                if (
                    isinstance(block, dict)
                    and block.get("type") == "tool_use"
                    and (block.get("name") == "Read")
                    and (
                        Path(block["input"].get("file_path", "")).name
                        == "multibyte.txt"
                    )
                ):
                    read_interactions.append((entry, block))
    matches = read_interactions
    assert len(matches) == 1, "Expected one multibyte.txt Read"
    ((assistant, call),) = matches
    result_messages = []
    for entry in entries:
        if entry.get("type") != "user":
            continue
        for block in claude._content_blocks(entry):
            if isinstance(block, dict) and block.get("type") == "tool_result":
                if block.get("tool_use_id") == call["id"]:
                    result_messages.append(entry)
                    break
    results = result_messages
    assert len(results) == 1, "Expected one result for the selected Read"
    (result,) = results
    return ClaudeSource(TRANSCRIPT, [assistant, result]), call


def test_cli_uses_the_public_extractor_stages(monkeypatch, capsys):
    stages = []

    class RecordingExtractor:
        def read_source(self, request):
            stages.append("read")
            return claude.read_source(request)

        def interpret_source(self, source):
            stages.append("interpret")
            return claude.interpret_source(source)

    monkeypatch.setattr(claude, "EXTRACTOR", RecordingExtractor(), raising=False)
    status = claude.main(["--transcript", str(TRANSCRIPT), "--output", "-"])
    assert status == 0
    assert json.loads(capsys.readouterr().out)["events"]
    assert stages == ["read", "interpret"]


def _read_contract_source(extractor, selection, harness, full_result, monkeypatch):
    """Load each harness's native fixtures before enabling the source-access guard."""
    if harness == "copilot":
        source = native_source()
    else:
        source = extractor.read_source(selection)
        if harness == "codex" and full_result:
            # Extend a captured completion past the default export cap.
            completion_payloads = []
            for event in source.entries:
                if event["payload"].get("type") == "custom_tool_call_output":
                    completion_payloads.append(event["payload"])
            next(iter(completion_payloads))["output"] = "x" * 20000

    return source


# Extend this native-fixture matrix as other harnesses adopt Extractor.
CONTRACT_CASES = [
    (claude.EXTRACTOR, TRANSCRIPT, "claude", True),
    (copilot.EXTRACTOR, None, "copilot", True),
]
for codex_name in [
    "slash.jsonl",
    "image.jsonl",
    "tool_patch_large.jsonl",
    "question.jsonl",
    "denial.jsonl",
    "model.jsonl",
    "plan.jsonl",
]:
    CONTRACT_CASES.append(
        (
            codex.EXTRACTOR,
            FIXTURES.parent / "codex" / codex_name,
            "codex",
            codex_name == "tool_patch_large.jsonl",
        )
    )


@pytest.mark.parametrize(
    ("extractor", "selection", "harness", "full_result"), CONTRACT_CASES
)
def test_public_interpretation_is_source_free_and_export_is_non_mutating(
    extractor, selection, harness, full_result, monkeypatch
):
    source = _read_contract_source(
        extractor, selection, harness, full_result, monkeypatch
    )

    def forbidden(*args, **kwargs):
        pytest.fail("interpret_source accessed its source store")

    with monkeypatch.context() as guard:
        guard.setattr(sqlite3, "connect", forbidden)
        for method in [
            "open",
            "is_file",
            "is_dir",
            "stat",
            "iterdir",
            "read_text",
            "read_bytes",
        ]:
            guard.setattr(Path, method, forbidden)
        session = extractor.interpret_source(source)
    assert session is not None
    assert session["harness"] == harness
    original = deepcopy(session)
    for limit in [0, 17, -1]:
        assert export_session(session, limit)["events"]
        assert session == original
    if full_result:
        interpreted_calls = []
        for event in session["events"]:
            for call in event.get("tool_calls", []):
                interpreted_calls.append(call)
        calls = interpreted_calls
        has_large_result = []
        for call in calls:
            has_large_result.append(
                isinstance(call.get("result"), str)
                and len(call["result"].encode()) > 10000
            )
        assert any(has_large_result)
        has_no_export_flags = []
        for call in calls:
            has_no_export_flags.append("result_truncated" not in call)
        assert all(has_no_export_flags)


def test_public_stage_propagates_reader_errors_and_handles_no_events(tmp_path):
    extractor = claude.EXTRACTOR
    with pytest.raises(FileNotFoundError):
        extractor.read_source(tmp_path / "missing.jsonl")
    assert extractor.interpret_source(ClaudeSource(tmp_path / "s.jsonl", [])) is None
    system_entries = []
    for entry in claude.read_source(TRANSCRIPT).entries:
        if entry.get("type") == "system":
            system_entries.append(entry)
    assert (
        extractor.interpret_source(
            ClaudeSource(
                tmp_path / "s.jsonl",
                system_entries,
            )
        )
        is None
    )


@pytest.mark.parametrize(
    ("request_type", "result_type", "value", "valid"),
    [
        ("Path", "Session | None", "None", True),
        ("str", "Session | None", "None", False),
        ("Path", "str", '"wrong result"', False),
    ],
)
def test_mypy_enforces_extractor_signatures(
    request_type, result_type, value, valid, tmp_path
):
    sample = tmp_path / "contract_example.py"
    sample.write_text(f"""
from pathlib import Path
from session_log_extractors.claude_sources import ClaudeSource
from session_log_extractors.extractor_contract import Extractor
from session_log_extractors.session_model import Session

class Implementation:
    def read_source(self, request: {request_type}) -> ClaudeSource:
        return ClaudeSource(Path("example.jsonl"), [])

    def interpret_source(self, source: ClaudeSource) -> {result_type}:
        return {value}

extractor: Extractor[Path, ClaudeSource] = Implementation()
""")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--no-incremental",
            "--cache-dir",
            str(tmp_path / "cache"),
            str(sample),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if valid:
        assert result.returncode == 0, result.stdout + result.stderr
    else:
        assert result.returncode == 1, result.stdout + result.stderr
        assert "Incompatible types in assignment" in result.stdout
        assert "Extractor" in result.stdout
    assert "import-untyped" not in result.stdout
    assert "import-not-found" not in result.stdout


def test_copilot_cli_uses_public_stages_and_shared_export(monkeypatch, capsys):
    stages = []
    original = copilot.EXTRACTOR
    export = copilot.export_session

    class RecordingExtractor:
        def read_source(self, request):
            stages.append("read")
            return original.read_source(request)

        def interpret_source(self, source):
            stages.append("interpret")
            return original.interpret_source(source)

    def recording_export(session, limit):
        stages.append(("export", limit))
        return export(session, limit)

    monkeypatch.setattr(copilot, "EXTRACTOR", RecordingExtractor())
    monkeypatch.setattr(copilot, "export_session", recording_export)
    monkeypatch.setattr(copilot_sources, "load_state_records", lambda session_id: [])
    status = copilot.main(
        [
            SESSION_ID,
            "--db",
            str(DATABASE),
            "--output",
            "-",
            "--tool-result-bytes",
            "7",
        ]
    )
    assert status == 0
    assert json.loads(capsys.readouterr().out)["events"]
    assert stages == ["read", "interpret", ("export", 7)]


def test_copilot_public_reader_errors_and_empty_source():
    conn = sqlite3.connect(":memory:")
    conn.close()
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        copilot.EXTRACTOR.read_source(copilot.CopilotRequest(conn, {"id": "missing"}))
    assert (
        copilot.EXTRACTOR.interpret_source(
            copilot_sources.CopilotSource({"id": "empty"}, [])
        )
        is None
    )


def _calls_named(session, name):
    calls = []
    for message in session["events"]:
        for call in message.get("tool_calls", []):
            if call["name"] == name:
                calls.append(call)
    assert len(calls) == 1, "Expected one selected call"
    return calls[0]


@pytest.mark.parametrize("harness", ["claude", "copilot"])
@pytest.mark.parametrize("unsupported", [False, True])
def test_native_tool_payloads_keep_unknown_fields_through_export(harness, unsupported):
    """Extend a captured call with a future field and optionally a future tool name."""
    if harness == "claude":
        source, request = _recorded_claude_read_interaction()
        input_key = "input"
        extractor = claude.EXTRACTOR
    else:
        source = native_tool_source()
        requests = []
        for record in source.state_records:
            if record["type"] == "assistant.message":
                requests.extend(record["data"].get("toolRequests", []))
        assert len(requests) == 1
        (request,) = requests
        input_key = "arguments"
        extractor = copilot.EXTRACTOR
    # Keep recorded call IDs/results; only extend the payload and vary recognition.
    request[input_key]["future_field"] = {
        "nested": [None, False, 17, {"value": "kept"}]
    }
    if unsupported:
        request["name"] = "future_tool"
    expected_input = deepcopy(request[input_key])
    session = extractor.interpret_source(source)
    call = _calls_named(session, request["name"])
    assert call["input"] == expected_input
    assert (call["input_schema"] == "unsupported") is unsupported
    if unsupported:
        assert "future_tool" in session["unsupported_tools"]
    original = deepcopy(session)
    exported = export_session(session, 17)
    exported_call = _calls_named(exported, request["name"])
    assert exported_call["input"] == expected_input
    assert session == original
