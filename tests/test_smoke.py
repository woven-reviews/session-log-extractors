import json
import os
import runpy
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import session_log_extractors
from session_log_extractors.session_log_envelope import raw_filename


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


@pytest.mark.parametrize("subdirectory", [False, True])
@pytest.mark.parametrize("selection", ["transcript", "all", "discovery"])
def test_claude_checkout_defaults(subdirectory, selection, tmp_path):
    from session_log_extractors.extract_claude_session_log import encode_project_path

    root = Path(__file__).resolve().parents[1]
    checkout = tmp_path / "checkout"
    scripts = checkout / "scripts"
    scripts.mkdir(parents=True)
    wrapper = scripts / "extract_claude_session_log.py"
    shutil.copyfile(root / "scripts" / wrapper.name, wrapper)
    cwd = checkout / "nested" if subdirectory else checkout
    cwd.mkdir(exist_ok=True)
    home = tmp_path / "home"
    project = home / ".claude" / "projects" / encode_project_path(checkout)
    project.mkdir(parents=True)
    transcript = project / "session.jsonl"
    shutil.copyfile(root / "scripts/fixtures/claude/baseline.jsonl", transcript)
    arguments = {
        "transcript": ["--transcript", str(transcript)],
        "all": ["--all", "--project-dir", str(project)],
        "discovery": [],
    }[selection]
    result = subprocess.run(
        [sys.executable, str(wrapper), *arguments],
        cwd=cwd,
        env={**os.environ, "HOME": str(home)},
        check=True,
        capture_output=True,
        text=True,
    )
    output = checkout / "claude_session_log_raw_session.json"
    envelope = json.loads(output.read_text())
    assert envelope["harness"] == "claude"
    assert envelope["events"]
    assert result.stdout == ""
    assert f"wrote {output}" in result.stderr
    assert not (scripts / output.name).exists()
    if subdirectory:
        assert not (cwd / output.name).exists()


def test_claude_wrapper_delegates_with_checkout_root(monkeypatch):
    from session_log_extractors import extract_claude_session_log as claude

    root = Path(__file__).resolve().parents[1]

    def main(*, project_root):
        assert project_root == root
        return 7

    monkeypatch.setattr(claude, "main", main)
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(
            str(root / "scripts/extract_claude_session_log.py"), run_name="__main__"
        )
    assert exc.value.code == 7
