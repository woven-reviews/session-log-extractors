"""All production CLIs share text limits and complete-output diagnostics."""

import json
from copy import deepcopy
from pathlib import Path

import pytest
from codex_capture import TRANSCRIPT as CODEX_TRANSCRIPT
from copilot_fixture import DATABASE, SESSION_ID, native_records

from session_log_extractors import copilot_sources
from session_log_extractors import extract_claude_session_log as claude
from session_log_extractors import extract_codex_session_log as codex
from session_log_extractors import extract_copilot_session_log as copilot
from session_log_extractors import session_log_envelope as envelope

CLAUDE_ROOT = Path(__file__).parent / "fixtures" / "claude" / "2.1.312" / "native"
(CLAUDE_TRANSCRIPT,) = CLAUDE_ROOT.glob("*.jsonl")


@pytest.fixture(params=[claude, codex, copilot], ids=["claude", "codex", "copilot"])
def production_cli(request, monkeypatch):
    module = request.param
    if module is copilot:
        records = native_records()
        monkeypatch.setattr(
            copilot_sources, "load_state_records", lambda session_id: records
        )
        args = [SESSION_ID, "--db", str(DATABASE)]
    else:
        transcript = CLAUDE_TRANSCRIPT if module is claude else CODEX_TRANSCRIPT
        args = ["--transcript", str(transcript)]
    return module, args


def _text_calls(exported):
    calls = []
    for event in exported["events"]:
        for call in event.get("tool_calls") or []:
            if isinstance(call.get("result"), str):
                calls.append(call)
    return calls


@pytest.mark.parametrize("limit", [None, 0, 17, -1])
@pytest.mark.parametrize("stdout", [False, True])
def test_production_limits(production_cli, limit, stdout, tmp_path, capsys):
    module, args = production_cli
    status = module.main([*args, "--output", "-", "--tool-result-bytes", "-1"])
    assert status == 0
    original = json.loads(capsys.readouterr().out)
    original_calls = _text_calls(original)
    assert original_calls, "Capture must include textual tool results"
    original_sizes = []
    for call in original_calls:
        original_sizes.append(call["result_bytes"])
    assert max(original_sizes) > 8192
    path = tmp_path / "session.json"
    options = [*args, "--output", "-" if stdout else str(path)]
    if limit is not None:
        options.extend(["--tool-result-bytes", str(limit)])
    status = module.main(options)
    assert status == 0
    captured = capsys.readouterr()
    exported = json.loads(captured.out if stdout else path.read_text())
    actual_limit = 8192 if limit is None else limit
    assert exported["truncation"]["tool_result_max_bytes"] == actual_limit
    actual_calls = _text_calls(exported)
    assert len(actual_calls) == len(original_calls)
    for actual, full in zip(actual_calls, original_calls, strict=True):
        text = full["result"]
        expected = (
            text
            if actual_limit < 0
            else text.encode()[:actual_limit].decode(errors="ignore")
        )
        assert actual["result"] == expected
        assert actual["result_bytes"] == len(text.encode())
        assert actual["result_truncated"] is (0 <= actual_limit < len(text.encode()))
        assert actual["input"] == full["input"]
    if not stdout:
        assert captured.out == ""


@pytest.mark.parametrize("stdout", [False, True])
def test_production_warning_counts_all_fields(
    production_cli, stdout, tmp_path, monkeypatch, capsys
):
    module, args = production_cli
    original_extractor = module.EXTRACTOR
    sessions = []

    class EnrichedExtractor:
        def read_source(self, request):
            return original_extractor.read_source(request)

        def interpret_source(self, source):
            session = original_extractor.interpret_source(source)
            assert session is not None
            # Add controlled large payloads to a native session: neither alone is 8 MiB.
            event = session["events"][0]
            event["images"] = [envelope.image_from_bytes(b"x" * 4500000, "image/png")]
            event["tool_results"] = [
                {
                    "tool_call_id": None,
                    "ts": None,
                    "is_error": False,
                    "native_result": {"nested": "€" * 1000000},
                }
            ]
            sessions.append(deepcopy(session))
            return session

    monkeypatch.setattr(module, "EXTRACTOR", EnrichedExtractor())
    path = tmp_path / "session.json"
    status = module.main(
        [*args, "--output", "-" if stdout else str(path), "--tool-result-bytes", "0"]
    )
    assert status == 0
    captured = capsys.readouterr()
    payload = captured.out if stdout else path.read_text()
    exported = json.loads(payload)
    assert len(payload.encode()) > 8 * 1024 * 1024
    assert "warning:" in captured.err
    assert "--tool-result-bytes" in captured.err
    event = exported["events"][0]
    original_event = sessions[0]["events"][0]
    assert event["images"] == original_event["images"]
    assert event["tool_results"] == original_event["tool_results"]
    if not stdout:
        assert captured.out == ""


def test_cli_help_exposes_shared_default(production_cli, capsys):
    module, _args = production_cli
    with pytest.raises(SystemExit) as raised:
        module.main(["--help"])
    assert raised.value.code == 0
    assert "default: 8192" in capsys.readouterr().out


def test_native_edited_plan_beyond_cap_remains_structured():
    source = claude.read_source(CLAUDE_TRANSCRIPT)
    edited_results = []
    for entry in source.entries:
        native = entry.get("toolUseResult")
        if isinstance(native, dict) and native.get("planWasEdited") is True:
            edited_results.append(entry)
    assert len(edited_results) == 1, "Capture must contain an edited plan approval"
    (entry,) = edited_results
    native = entry["toolUseResult"]
    recorded_plan = native["plan"]
    # Extend only the recorded edited plan in both native representations.
    long_plan = "x" * 10000 + recorded_plan
    native["plan"] = long_plan
    for block in entry["message"]["content"]:
        if block.get("type") == "tool_result" and isinstance(block.get("content"), str):
            block["content"] = block["content"].replace(recorded_plan, long_plan)
    session = claude.interpret_source(source)
    original = deepcopy(session)
    exported = envelope.export_session(session)
    edited_calls = []
    for event in exported["events"]:
        for call in event.get("tool_calls") or []:
            for version in call.get("plan_versions", []):
                if version["kind"] == "edited":
                    edited_calls.append((call, version))
    assert len(edited_calls) == 1
    ((call, version),) = edited_calls
    assert call["result_truncated"] is True
    assert recorded_plan not in call["result"]
    assert version["text"] == long_plan
    assert call["plan_decisions"][0]["outcome"] == "approved"
    assert call["result_record"]["native_result"]["plan"] == long_plan
    assert session == original
