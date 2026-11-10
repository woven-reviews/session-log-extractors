"""Native host checks run without Python in the candidate's hook path."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _hook(repo, action):
    if os.name == "nt":
        command = [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(ROOT / "hooks/capture.ps1"),
        ]
    else:
        command = ["sh", str(ROOT / "hooks/capture.sh")]
    return subprocess.run(
        [*command, action, "claude"],
        cwd=repo,
        input="{}",
        capture_output=True,
        text=True,
        timeout=15,
        env={**os.environ, "DOCKER_HOST": "tcp://127.0.0.1:1"},
        check=False,
    )


@pytest.fixture
def checkout(tmp_path):
    repo = tmp_path / "assessment with spaces"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    return repo


def test_unrelated_checkout_is_inactive(checkout):
    result = _hook(checkout, "pre")
    assert result.returncode == 0
    assert result.stdout == ""


def test_invalid_pin_blocks_tools_but_leaves_chat_available(checkout):
    (checkout / ".session-log-capture").write_text("image:latest")
    denied = _hook(checkout, "pre")
    assert denied.returncode == 2
    assert "outside the agent" in denied.stderr
    stopped = _hook(checkout, "stop")
    assert stopped.returncode == 0


def test_docker_failure_is_reported_and_signal_is_retained(checkout):
    (checkout / ".session-log-capture").write_text("sha256:" + "0" * 64)
    denied = _hook(checkout, "pre")
    assert denied.returncode == 2
    assert "Docker" in denied.stderr
    stopped = _hook(checkout, "stop")
    assert stopped.returncode == 0
    signals = list((checkout / ".git/session-log-capture/incoming").glob("*.json"))
    assert len(signals) == 1
    assert json.loads(signals[0].read_text()) == {}


def test_notice_is_chat_only_even_without_docker(checkout):
    (checkout / ".session-log-capture").write_text("image:latest")
    result = _hook(checkout, "notice")
    assert result.returncode == 0
    assert json.loads(result.stdout)["systemMessage"]


@pytest.mark.skipif(
    os.name == "nt",
    reason="Windows refuses all reparse paths; symlink creation needs privileges",
)
@pytest.mark.parametrize("scope", ["home", "parent", "root"])
def test_home_and_ancestor_aliases_are_rejected_before_docker(checkout, scope):
    (checkout / ".session-log-capture").write_text("sha256:" + "0" * 64)
    state = checkout / ".git/session-log-capture"
    protected = {
        "home": Path.home(),
        "parent": Path.home().parent,
        "root": Path(Path.home().anchor),
    }
    state.symlink_to(protected[scope], target_is_directory=True)
    denied = _hook(checkout, "pre")
    assert denied.returncode == 2
    assert "home directory" in denied.stderr


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX archive packaging; Windows command definitions checked below",
)
def test_text_bundles_contain_each_declared_hook(tmp_path):
    subprocess.run(
        ["sh", str(ROOT / "scripts/package-plugins.sh"), str(tmp_path)], check=True
    )
    for platform in ("posix", "windows"):
        destination = tmp_path / platform
        destination.mkdir()
        shutil.unpack_archive(
            str(tmp_path / f"session-log-plugins-{platform}.tar.gz"), destination
        )
        for harness in ("claude", "codex", "copilot"):
            assert (destination / harness / "hooks/capture.sh").is_file()
            assert (destination / harness / "hooks/capture.ps1").is_file()
        definitions = json.loads((destination / "claude/hooks/hooks.json").read_text())
        handler = definitions["hooks"]["PreToolUse"][0]["hooks"][0]
        assert handler["command"] == (
            "powershell.exe" if platform == "windows" else "sh"
        )


def test_native_windows_command_definitions_select_powershell():
    codex = json.loads((ROOT / "plugins/codex/hooks/hooks.json").read_text())
    for entries in codex["hooks"].values():
        assert "capture.ps1" in entries[0]["hooks"][0]["commandWindows"]
    copilot = json.loads((ROOT / "plugins/copilot/hooks/hooks.json").read_text())
    for entries in copilot["hooks"].values():
        assert "capture.ps1" in entries[0]["powershell"]
