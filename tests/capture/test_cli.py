import json
import os
import runpy
import subprocess
import sys
import time
from pathlib import Path

import pytest
from gitutil import sh

from session_log_extractors.capture import cli, pusher, sync

SOURCE_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = SOURCE_ROOT / "scripts" / "capture_session.py"


def claude_transcript(tmp_path, text="hello"):
    path = tmp_path / "abc.jsonl"
    entry = {
        "type": "user",
        "version": "2.1",
        "timestamp": "t",
        "message": {"role": "user", "content": text},
    }
    path.write_text(json.dumps(entry) + "\n")
    return path


def hook_payload(tmp_path, clone, text="hello"):
    return json.dumps(
        {
            "session_id": "abc",
            "transcript_path": str(claude_transcript(tmp_path, text)),
            "cwd": str(clone),
        }
    )


@pytest.fixture
def no_spawn(monkeypatch):
    spawned = []
    monkeypatch.setattr(
        cli.pusher, "spawn_worker", lambda repo, script: spawned.append((repo, script))
    )
    return spawned


def test_hook_stages_the_filtered_session_and_starts_a_worker(
    clone, tmp_path, no_spawn
):
    code = cli.main(
        ["--harness", "claude", "--repo", str(clone)],
        hook_payload(tmp_path, clone),
        SCRIPT,
        tmp_path,
    )

    assert code == 0
    staged = pusher.load_pending(clone)[0][0]
    assert list(staged.files) == ["session_logs/claude/abc.jsonl"]
    assert no_spawn == [(clone, SCRIPT)]


def test_harness_is_detected_from_the_payload_when_not_given(clone, tmp_path, no_spawn):
    cli.main(["--repo", str(clone)], hook_payload(tmp_path, clone), SCRIPT, tmp_path)

    assert pusher.load_pending(clone)[0][0].harness == "claude"


def test_unusable_payloads_are_ignored_quietly(clone, tmp_path, no_spawn, capsys):
    for text in ("", "not json", "[1]", json.dumps({"cwd": str(clone)})):
        assert cli.main(["--repo", str(clone)], text, SCRIPT, tmp_path) == 0

    assert pusher.load_pending(clone) == []
    assert no_spawn == []
    assert capsys.readouterr().err == ""


def test_hook_does_nothing_once_capture_is_disabled(clone, tmp_path, no_spawn):
    (pusher.state_dir(clone) / pusher.DISABLED_FLAG).write_text("access lost")

    cli.main(
        ["--harness", "claude", "--repo", str(clone)],
        hook_payload(tmp_path, clone),
        SCRIPT,
        tmp_path,
    )

    assert pusher.load_pending(clone) == []
    assert no_spawn == []


def test_hook_shows_the_workers_last_warning_once(clone, tmp_path, no_spawn, capsys):
    (pusher.state_dir(clone) / pusher.LAST_ERROR).write_text(
        "woven-capture: could not save"
    )

    cli.main(["--repo", str(clone)], "", SCRIPT, tmp_path)
    cli.main(["--repo", str(clone)], "", SCRIPT, tmp_path)

    assert capsys.readouterr().err.count("woven-capture: could not save") == 1


def test_errors_become_one_warning_and_exit_zero(tmp_path, capsys):
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()

    code = cli.main(["--repo", str(not_a_repo)], "{}", SCRIPT, tmp_path)

    assert code == 0
    assert capsys.readouterr().err.startswith("woven-capture: skipped (")


def test_notice_prints_a_user_only_system_message(tmp_path, capsys):
    assert cli.main(["--notice"], "", SCRIPT, tmp_path) == 0

    assert json.loads(capsys.readouterr().out) == {"systemMessage": cli.NOTICE}


def test_worker_mode_runs_the_worker(clone, tmp_path, monkeypatch):
    ran = []
    monkeypatch.setattr(cli.pusher, "run_worker", lambda repo: ran.append(repo))

    cli.main(["--worker", "--repo", str(clone)], "", SCRIPT, tmp_path)

    assert ran == [clone]


def test_repo_root_defaults_to_the_checkout_holding_the_script(clone):
    scripts = clone / "scripts"
    scripts.mkdir()

    assert (
        cli.repo_root(scripts / "capture_session.py", None).resolve() == clone.resolve()
    )


def test_entry_script_runs_the_cli_with_stdin(clone, tmp_path, monkeypatch, capsys):
    class Stdin:
        def isatty(self):
            return False

        def read(self):
            return ""

    monkeypatch.setattr(sys, "argv", ["capture_session.py", "--notice"])
    monkeypatch.setattr(sys, "stdin", Stdin())

    with pytest.raises(SystemExit) as info:
        runpy.run_path(str(SCRIPT), run_name="__main__")

    assert info.value.code == 0
    assert "systemMessage" in capsys.readouterr().out


def test_entry_script_does_not_read_a_terminal():
    module = runpy.run_path(str(SCRIPT), run_name="not_main")

    class Tty:
        def isatty(self):
            return True

        def read(self):
            raise AssertionError("must not read a terminal")

    assert module["run"](["--notice"], Tty()) == 0


def wait_for_remote_file(remote, path, seconds=20):
    """The hook starts a real detached worker, so poll until its push lands."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        shown = subprocess.run(
            ["git", "show", f"session-logs:{path}"],
            cwd=remote,
            capture_output=True,
            text=True,
        )
        if shown.returncode == 0:
            return shown.stdout
        time.sleep(0.2)
    raise AssertionError(f"{path} never reached the remote")


def test_end_to_end_with_a_synced_copy_and_a_real_remote(clone, remote, tmp_path):
    """Sync into the clone, fire the hook as a subprocess, and let the detached worker push."""
    sync.sync(clone, SOURCE_ROOT)
    payload = hook_payload(tmp_path, clone, "end to end")
    env = {
        **os.environ,
        "WOVEN_CAPTURE_QUIET_SECONDS": "0",
        "WOVEN_CAPTURE_IDLE_SECONDS": "1",
    }
    before = sh(clone, "git", "status", "--porcelain=v1")

    hook = subprocess.run(
        [
            sys.executable,
            "-I",
            str(clone / "scripts" / "capture_session.py"),
            "--harness",
            "claude",
        ],
        input=payload,
        text=True,
        capture_output=True,
        env=env,
        cwd=clone,
        check=False,
    )
    assert hook.returncode == 0, hook.stderr
    pushed = wait_for_remote_file(remote, "session_logs/claude/abc.jsonl")

    assert "end to end" in pushed
    manifest = json.loads(
        sh(remote, "git", "show", "session-logs:session_logs/manifest.json")
    )
    assert manifest["sessions"]["claude/abc"]["harness_version"] == "2.1"
    after = sh(clone, "git", "status", "--porcelain=v1")
    assert [
        line
        for line in after.splitlines()
        if "scripts/" not in line and ".claude" not in line
    ] == [
        line
        for line in before.splitlines()
        if "scripts/" not in line and ".claude" not in line
    ]


def test_entry_script_falls_back_to_the_synced_package(clone, monkeypatch):
    """In a starter repo the library is not installed, so the script imports its sibling copy."""
    sync.sync(clone, SOURCE_ROOT)
    monkeypatch.setitem(sys.modules, "session_log_extractors.capture", None)
    monkeypatch.syspath_prepend(str(clone / "scripts"))
    module = runpy.run_path(str(SCRIPT), run_name="not_main")

    try:
        cli_module = module["load_cli"]()
    finally:
        sys.modules.pop("session_log_capture", None)
        sys.modules.pop("session_log_capture.cli", None)

    assert cli_module.__name__ == "session_log_capture.cli"
