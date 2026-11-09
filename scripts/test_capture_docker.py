"""Opt-in host-hook to Docker proof using only disposable fixture files.

Set SESSION_LOG_TEST_IMAGE to an explicitly built immutable image ID. No model
process, real GitHub upload, or host home-directory mount runs in these tests.
"""

import json
import os
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import pytest
from capture_fixture import git

from session_log_extractors.capture_jobs import CaptureJob

ROOT = Path(__file__).resolve().parents[1]
ASSESSMENT_REMOTE = "https://github.com/example/qual-2323-probe.git"
pytestmark = pytest.mark.skipif(
    not os.environ.get("SESSION_LOG_TEST_IMAGE"),
    reason="requires explicitly built capture image",
)


@dataclass
class _DockerCapture:
    job: CaptureJob
    repository: Path
    state: Path
    remote: Path
    executable: Path


@pytest.fixture
def docker_capture(capture, tmp_path):
    _module, job, repo, remote = capture
    executable = tmp_path / "plugin with spaces" / "bin" / "session-log-hook.exe"
    executable.parent.mkdir(parents=True)
    subprocess.run(
        ["go", "build", "-o", str(executable), "."],
        cwd=ROOT / "launcher",
        check=True,
    )
    git(repo, "remote", "add", "origin", ASSESSMENT_REMOTE)
    (repo / ".session-log.json").write_text(
        json.dumps(
            {
                "version": 1,
                "image": os.environ["SESSION_LOG_TEST_IMAGE"],
            }
        )
    )
    state = repo / ".git" / "session-log-capture"
    state.mkdir()
    local_remote = state / "remote.git"
    shutil.copytree(remote, local_remote)
    private = state / "upload.git"
    private.mkdir()
    git(private, "init", "--bare", "-q")
    # Rewrite only the disposable upload repo's target; send nothing to GitHub.
    git(private, "config", "url.file:///state/remote.git.insteadOf", ASSESSMENT_REMOTE)
    context = _DockerCapture(job, repo, state, local_remote, executable)
    try:
        yield context
    finally:
        containers = subprocess.run(
            [
                "docker",
                "ps",
                "-aq",
                "--filter",
                f"label=org.woven.capture.state={state}",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
        for identifier in containers:
            subprocess.run(
                ["docker", "rm", "-f", identifier], capture_output=True, check=False
            )


def _invoke_hook(context, action, extra_env=None):
    payload = json.dumps(
        {
            "cwd": str(context.repository),
            "session_id": context.job.session_id,
            "transcript_path": context.job.transcript,
        }
    )
    events = {"pre": "PreToolUse", "stop": "Stop", "notice": "SessionStart"}
    definitions = json.loads(
        (ROOT / "plugins" / "claude" / "hooks" / "hooks.json").read_text()
    )
    handler = definitions["hooks"][events[action]][0]["hooks"][0]
    command = handler["command"].replace(
        "${CLAUDE_PLUGIN_ROOT}", str(context.executable.parent.parent)
    )
    return subprocess.run(
        [command, *handler["args"]],
        input=payload,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "SESSION_LOG_TOKEN": "synthetic-probe-token",
            **(extra_env or {}),
        },
        timeout=15,
        check=False,
    )


def _wait_for(context, path, *, present):
    deadline = time.monotonic() + 95
    while path.exists() is not present:
        if time.monotonic() >= deadline:
            errors = []
            for name in ("launcher.log", "last_error"):
                if (context.state / name).exists():
                    errors.append((context.state / name).read_text())
            pytest.fail("Capture did not finish: " + "\n".join(errors))
        time.sleep(0.2)


def test_host_hook_survives_slow_upload_and_newer_turn(docker_capture):
    context = docker_capture
    hook = context.remote / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\ntouch /state/upload-started\nsleep 35\n")
    hook.chmod(0o755)
    before = (
        git(context.repository, "diff"),
        git(context.repository, "diff", "--cached"),
        git(context.repository, "rev-parse", "HEAD"),
    )
    readiness = _invoke_hook(context, "pre")
    assert readiness.returncode == 0, readiness.stderr
    started = time.monotonic()
    first = _invoke_hook(context, "stop")
    assert first.returncode == 0, first.stderr
    handoff_seconds = time.monotonic() - started
    assert handoff_seconds < 15
    _wait_for(context, context.state / "upload-started", present=True)
    _assert_scoped_mounts(context)
    second = _invoke_hook(context, "stop")
    assert second.returncode == 0, second.stderr
    _wait_for_empty_queue(context)
    after = (
        git(context.repository, "diff"),
        git(context.repository, "diff", "--cached"),
        git(context.repository, "rev-parse", "HEAD"),
    )
    assert after == before
    assert git(context.remote, "rev-list", "--count", "session-logs") == "2"
    envelope = json.loads(
        git(context.remote, "show", f"session-logs:{context.job.output_path}")
    )
    assert envelope["events"]
    assert envelope["session_id"] == context.job.session_id
    print(f"handoff={handoff_seconds:.3f}s total={time.monotonic() - started:.3f}s")


def _assert_scoped_mounts(context):
    identifiers = subprocess.run(
        [
            "docker",
            "ps",
            "-q",
            "--filter",
            f"label=org.woven.capture.state={context.state}",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert identifiers, "Expected a running worker during the delayed upload"
    expected = {str(context.state), context.job.transcript}
    for identifier in identifiers:
        result = subprocess.run(
            ["docker", "inspect", identifier],
            capture_output=True,
            text=True,
            check=True,
        )
        (container,) = json.loads(result.stdout)
        actual = set()
        for mount in container["Mounts"]:
            actual.add(mount["Source"])
            if mount["Destination"] != "/state":
                assert mount["RW"] is False
        assert actual == expected


def _wait_for_empty_queue(context):
    deadline = time.monotonic() + 110
    while True:
        paths = []
        for directory in ("requests", "pending"):
            paths.extend((context.state / directory).glob("*.json"))
        if not paths:
            return
        if time.monotonic() >= deadline:
            error_path = context.state / "last_error"
            error = (
                error_path.read_text()
                if error_path.exists()
                else "no worker diagnostic"
            )
            pytest.fail("Capture queue did not drain: " + error)
        time.sleep(0.2)


def test_readiness_recovers_after_docker_connection_is_repaired(docker_capture):
    context = docker_capture
    unavailable = _invoke_hook(
        context, "pre", {"DOCKER_HOST": "unix:///nonexistent/qual-2323.sock"}
    )
    assert unavailable.returncode == 2
    assert "outside the agent" in unavailable.stderr
    recovered = _invoke_hook(context, "pre")
    assert recovered.returncode == 0, recovered.stderr


@pytest.mark.skipif(
    os.name == "nt", reason="POSIX delay probe; Windows launcher has native CI"
)
def test_detached_handoff_survives_docker_start_delayed_beyond_hook_timeout(
    docker_capture,
):
    context = docker_capture
    real_docker = shutil.which("docker")
    assert real_docker
    directory = context.state / "delayed-cli"
    directory.mkdir()
    marker = context.state / "delay-finished"
    wrapper = directory / "docker"
    # Delay the first actual container acquisition, then invoke real Docker.
    # This is a lifecycle probe, not a native model or fixture generator fake.
    wrapper.write_text(
        '#!/bin/sh\nif [ "$1" = run ] && [ ! -f '
        + shlex.quote(str(marker))
        + " ]; then sleep 35; touch "
        + shlex.quote(str(marker))
        + "; fi\nexec "
        + shlex.quote(real_docker)
        + ' "$@"\n'
    )
    wrapper.chmod(0o755)
    started = time.monotonic()
    result = _invoke_hook(
        context, "stop", {"PATH": str(directory) + os.pathsep + os.environ["PATH"]}
    )
    assert result.returncode == 0, result.stderr
    assert time.monotonic() - started < 15
    _wait_for_empty_queue(context)
    assert marker.exists()
    assert time.monotonic() - started >= 35
    envelope = json.loads(
        git(context.remote, "show", f"session-logs:{context.job.output_path}")
    )
    assert envelope["events"]
