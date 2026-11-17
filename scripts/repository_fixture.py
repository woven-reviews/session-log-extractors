"""Temporary Git checkouts and native capture copies for selection tests."""

import contextlib
import io
import json
import os
import subprocess

import pytest
from claude_fixture import TRANSCRIPT as CLAUDE_TRANSCRIPT
from codex_fixture import TRANSCRIPT as CODEX_TRANSCRIPT
from copilot_fixture import TRANSCRIPT as COPILOT_TRANSCRIPT

from session_log_extractors import codex_sources, copilot_sources
from session_log_extractors import extract_claude_session_log as claude
from session_log_extractors import extract_codex_session_log as codex
from session_log_extractors import extract_copilot_session_log as copilot


def _git(cwd, *args):
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=True,
    )


def init_checkout(path):
    """Give a prepared test directory a real Git checkout boundary."""
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init")


def make_checkouts(tmp_path):
    main = tmp_path / "assessment"
    main.mkdir()
    _git(main, "init")
    _git(
        main,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.org",
        "commit",
        "--allow-empty",
        "-m",
        "Initial",
    )
    linked = tmp_path / "external-feature"
    _git(main, "worktree", "add", "-b", "feature", str(linked))
    (main / "backend").mkdir()
    (linked / "frontend").mkdir()
    return main, linked


def session_store_root(home, harness):
    return {
        "claude": home / ".claude" / "projects",
        "codex": home / ".codex" / "sessions",
        "copilot": home / ".copilot" / "session-state",
    }[harness]


def _set_session_evidence(harness, entry, session_id, cwd):
    if harness == "claude":
        if "sessionId" in entry:
            entry["sessionId"] = session_id
        if "cwd" in entry:
            entry["cwd"] = cwd
    elif harness == "codex" and entry.get("type") in ("session_meta", "turn_context"):
        entry["payload"]["cwd"] = cwd
        if entry["type"] == "session_meta":
            entry["payload"]["id"] = session_id
    elif harness == "copilot":
        if entry.get("type") in ("session.start", "session.resume"):
            entry["data"]["context"] = {"cwd": cwd}
            if entry["type"] == "session.start":
                entry["data"]["sessionId"] = session_id


def copy_native_session(home, harness, session_id, cwd, order=1):
    """Vary only identity, directory evidence and ordering in complete captures.

    Claude stores deliberately use unrelated encoded names: ownership must come
    from transcript evidence. Copilot start/resume evidence is changed together;
    later tests change resume alone to check precedence.
    """
    native = {
        "claude": CLAUDE_TRANSCRIPT,
        "codex": CODEX_TRANSCRIPT,
        "copilot": COPILOT_TRANSCRIPT,
    }[harness]
    store = session_store_root(home, harness)
    if harness == "copilot":
        path = store / session_id / "events.jsonl"
    else:
        path = store / f"store-{session_id}" / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for line in native.read_text().splitlines():
        entry = json.loads(line)
        _set_session_evidence(harness, entry, session_id, cwd)
        if harness == "copilot":
            entry["timestamp"] = f"2026-11-{order:02d}T12:00:00Z"
        lines.append(json.dumps(entry))
    path.write_text("\n".join(lines) + "\n")
    os.utime(path, (order, order))
    return path


def run_cli(harness, home, cwd, *args):
    modules = {"claude": claude, "codex": codex, "copilot": copilot}
    stdout = io.StringIO()
    stderr = io.StringIO()
    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(cwd)
        patch.setenv("HOME", str(home))
        patch.setattr(claude, "PROJECTS_ROOT", session_store_root(home, "claude"))
        patch.setattr(
            codex_sources, "CODEX_SESSIONS_ROOT", session_store_root(home, "codex")
        )
        patch.setattr(
            copilot_sources, "COPILOT_STATE_ROOT", session_store_root(home, "copilot")
        )
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                status = modules[harness].main(list(args), project_root=cwd)
            except SystemExit as exc:
                status = exc.code
    return subprocess.CompletedProcess(
        list(args), status, stdout.getvalue(), stderr.getvalue()
    )
