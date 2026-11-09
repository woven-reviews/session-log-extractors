"""Installed command dispatch preserves each harness's native JSON contract."""

import importlib
import json
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from claude_fixture import TRANSCRIPT as CLAUDE_TRANSCRIPT
from codex_fixture import TRANSCRIPT as CODEX_TRANSCRIPT
from copilot_fixture import DATABASE, SESSION_ID

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "harness, selection",
    [
        ("claude", ["--transcript", str(CLAUDE_TRANSCRIPT)]),
        ("codex", ["--transcript", str(CODEX_TRANSCRIPT)]),
        ("copilot", [SESSION_ID, "--db", str(DATABASE)]),
    ],
)
def test_console_exports_native_json(harness, selection, capsys):
    cli = importlib.import_module("session_log_extractors.cli")
    status = cli.main([harness, *selection, "--output", "-"])
    assert status == 0
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["schema"] == "raw-session-log/1"
    assert envelope["harness"] == harness
    assert envelope["events"]


def test_release_metadata_and_installed_entrypoint():
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text())
    project = metadata["project"]
    assert project["version"] == "1.0.0"
    assert project["scripts"]["session-log"] == "session_log_extractors.cli:main"


@pytest.mark.parametrize("args, code", [([], 2), (["unknown"], 2), (["--help"], 0)])
def test_console_usage(args, code):
    cli = importlib.import_module("session_log_extractors.cli")
    with pytest.raises(SystemExit) as error:
        cli.main(args)
    assert error.value.code == code


def test_installed_console_help():
    result = subprocess.run(
        [str(Path(sys.executable).with_name("session-log")), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "claude" in result.stdout + result.stderr
