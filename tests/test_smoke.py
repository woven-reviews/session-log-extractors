import json
import subprocess
import sys
from pathlib import Path

import pytest
from session_log_extractors.session_log_envelope import raw_filename

import session_log_extractors


def test_package_imports():
    assert session_log_extractors is not None
    assert raw_filename("claude", "smoke") == "claude_session_log_raw_smoke.json"


@pytest.mark.parametrize(
    ("harness", "arguments"),
    [
        ("claude", ["--transcript", "scripts/fixtures/claude/baseline.jsonl"]),
        ("codex", ["--transcript", "scripts/fixtures/codex/tool_patch_large.jsonl"]),
        (
            "copilot",
            [
                "7e20450a-af9e-45a5-9aac-be9dcf027221",
                "--db",
                "scripts/fixtures/copilot/session-store.db",
            ],
        ),
    ],
)
def test_script_exports_fixture_json(harness, arguments, tmp_path):
    root = Path(__file__).resolve().parents[1]
    output = tmp_path / "session.json"
    subprocess.run(
        [
            sys.executable,
            str(root / "scripts" / f"extract_{harness}_session_log.py"),
            *arguments,
            "--output",
            str(output),
        ],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    envelope = json.loads(output.read_text())
    assert envelope["schema"] == "raw-session-log/1"
    assert envelope["harness"] == harness
    assert envelope["events"]
