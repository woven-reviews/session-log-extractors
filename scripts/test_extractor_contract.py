"""Production extraction uses the public read/interpret stage contract."""

import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from session_log_extractors import extract_claude_session_log as claude
from session_log_extractors.claude_sources import ClaudeSource
from session_log_extractors.session_log_envelope import export_session

FIXTURES = Path(__file__).parent / "fixtures" / "claude"


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
    assert (
        claude.main(["--transcript", str(FIXTURES / "baseline.jsonl"), "--output", "-"])
        == 0
    )
    assert json.loads(capsys.readouterr().out)["events"]
    assert stages == ["read", "interpret"]


# Extend this native-fixture matrix as other harnesses adopt Extractor.
@pytest.mark.parametrize(
    ("extractor", "selection", "harness", "full_result"),
    [
        (claude.EXTRACTOR, FIXTURES / name, "claude", name == "large_result.jsonl")
        for name in [
            "baseline.jsonl",
            "images.jsonl",
            "large_result.jsonl",
            "questions.jsonl",
        ]
    ],
    ids=["claude-baseline", "claude-images", "claude-large-result", "claude-questions"],
)
def test_public_interpretation_is_source_free_and_export_is_non_mutating(
    extractor, selection, harness, full_result, monkeypatch
):
    source = extractor.read_source(selection)

    def forbidden(*args, **kwargs):
        pytest.fail("interpret_source accessed its source store")

    with monkeypatch.context() as guard:
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
        calls = [
            call for event in session["events"] for call in event.get("tool_calls", [])
        ]
        assert any(
            isinstance(call.get("result"), str)
            and len(call["result"].encode()) > 10_000
            for call in calls
        )
        assert all("result_truncated" not in call for call in calls)


def test_public_stage_propagates_reader_errors_and_handles_no_events(tmp_path):
    extractor = claude.EXTRACTOR
    with pytest.raises(FileNotFoundError):
        extractor.read_source(tmp_path / "missing.jsonl")
    assert extractor.interpret_source(ClaudeSource(tmp_path / "s.jsonl", [])) is None
    assert (
        extractor.interpret_source(
            ClaudeSource(tmp_path / "s.jsonl", [{"type": "system"}])
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
