"""Stage snapshots locally and push them from one detached, debounced background worker.

The hook itself only stages files under ``<git-dir>/woven-capture/pending`` and starts
a worker if none is running, so it returns at once. The worker holds a lock file, waits
until the session goes quiet (or a maximum interval passes), pushes the newest staged
snapshot, and exits once there has been nothing to push for a while. Because the worker
always pushes whatever is newest when it wakes, a turn that lands inside the debounce
window is never skipped.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import gitpush
from .locate import Snapshot

QUIET_SECONDS = 10.0
MAX_INTERVAL_SECONDS = 60.0
IDLE_EXIT_SECONDS = 30.0
POLL_SECONDS = 1.0
LOCK_STALE_SECONDS = 300.0
PENDING = "pending"
META = "pending-meta"
LOCK = "lock"
DISABLED_FLAG = "disabled"
LAST_ERROR = "last_error"


@dataclass
class Clock:
    """Injectable time source so the debounce logic can be tested without sleeping."""

    now: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep


def _seconds(name: str, default: float) -> float:
    """A timing, overridable by ``WOVEN_CAPTURE_<NAME>_SECONDS`` so tests need not wait."""
    try:
        return float(os.environ[f"WOVEN_CAPTURE_{name}_SECONDS"])
    except (KeyError, ValueError):
        return default


def state_dir(repo: Path) -> Path:
    path = gitpush.git_dir(repo) / "woven-capture"
    path.mkdir(exist_ok=True)
    return path


def is_disabled(repo: Path) -> bool:
    return (state_dir(repo) / DISABLED_FLAG).exists()


def stage(repo: Path, snap: Snapshot) -> None:
    """Write a snapshot's files into the pending area, replacing that session's earlier files."""
    state = state_dir(repo)
    for path, text in snap.files.items():
        _atomic_write(state / PENDING / path, text)
    meta = {
        "harness": snap.harness,
        "session_id": snap.session_id,
        "files": sorted(snap.files),
        "meta": snap.meta,
    }
    _atomic_write(
        state / META / f"{snap.harness}__{snap.session_id}.json", json.dumps(meta)
    )


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _signature(state: Path, snap: Snapshot) -> dict[str, tuple[int, int]]:
    out = {}
    for rel in snap.files:
        stat = (state / PENDING / rel).stat()
        out[rel] = (stat.st_mtime_ns, stat.st_size)
    return out


def load_pending(repo: Path) -> list[tuple[Snapshot, dict[str, tuple[int, int]]]]:
    """Staged snapshots, each with the file signatures that identify exactly what was read."""
    state = state_dir(repo)
    loaded = []
    for meta_file in sorted((state / META).glob("*.json")):
        try:
            info = json.loads(meta_file.read_text(encoding="utf-8"))
            files = {
                rel: (state / PENDING / rel).read_text(encoding="utf-8")
                for rel in info["files"]
            }
            snap = Snapshot(
                info["harness"], info["session_id"], files, info.get("meta") or {}
            )
            loaded.append((snap, _signature(state, snap)))
        except (OSError, ValueError, KeyError):
            continue
    return loaded


def _newest_mtime(loaded: list[tuple[Snapshot, dict[str, tuple[int, int]]]]) -> float:
    return max((m / 1e9 for _, sig in loaded for m, _ in sig.values()), default=0.0)


def _clear_if_unchanged(
    repo: Path, snap: Snapshot, signature: dict[str, tuple[int, int]]
) -> None:
    """Remove staged files only if the hook has not rewritten them since they were read."""
    state = state_dir(repo)
    try:
        current = _signature(state, snap)
    except OSError:
        return
    if current != signature:
        return
    for rel in snap.files:
        (state / PENDING / rel).unlink(missing_ok=True)
    (state / META / f"{snap.harness}__{snap.session_id}.json").unlink(missing_ok=True)


def _acquire(state: Path, clock: Clock) -> bool:
    lock = state / LOCK
    try:
        if clock.now() - lock.stat().st_mtime > LOCK_STALE_SECONDS:
            lock.unlink(missing_ok=True)
    except OSError:
        pass
    try:
        os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
    except FileExistsError:
        return False
    return True


def worker_running(repo: Path, clock: Clock | None = None) -> bool:
    lock = state_dir(repo) / LOCK
    try:
        return (clock or Clock()).now() - lock.stat().st_mtime <= LOCK_STALE_SECONDS
    except OSError:
        return False


def _record_error(state: Path, message: str) -> None:
    (state / LAST_ERROR).write_text(message, encoding="utf-8")


def take_last_error(repo: Path) -> str | None:
    """Return and clear the worker's last failure message so the hook can show it once."""
    path = state_dir(repo) / LAST_ERROR
    try:
        message = path.read_text(encoding="utf-8")
    except OSError:
        return None
    path.unlink(missing_ok=True)
    return message or None


def _push_pending(
    repo: Path, loaded: list[tuple[Snapshot, dict[str, tuple[int, int]]]]
) -> str:
    state = state_dir(repo)
    outcome = gitpush.commit_and_push(repo, [snap for snap, _ in loaded])
    if outcome.status == gitpush.PUSHED:
        for snap, signature in loaded:
            _clear_if_unchanged(repo, snap, signature)
    elif outcome.status == gitpush.DISABLED:
        (state / DISABLED_FLAG).write_text(outcome.detail, encoding="utf-8")
        for snap, signature in loaded:
            _clear_if_unchanged(repo, snap, signature)
    elif outcome.status == gitpush.BLOCKED:
        _record_error(
            state,
            "woven-capture: GitHub rejected a session log push; this session was not saved.",
        )
        for snap, signature in loaded:
            _clear_if_unchanged(repo, snap, signature)
    else:
        _record_error(
            state,
            "woven-capture: could not save the session log (will retry on the next turn).",
        )
    return outcome.status


def _drain(repo: Path, clock: Clock) -> bool:
    """Push staged snapshots when quiet or overdue.

    Returns True when it stopped because a push failed transiently: the next hook fire
    starts a fresh worker, so a long outage never leaves one looping forever.
    """
    state = state_dir(repo)
    last_push = last_activity = clock.now()
    while not is_disabled(repo):
        (state / LOCK).touch()
        loaded = load_pending(repo)
        now = clock.now()
        if not loaded:
            if now - last_activity >= _seconds("IDLE", IDLE_EXIT_SECONDS):
                return False
        else:
            last_activity = now
            quiet = now - _newest_mtime(loaded) >= _seconds("QUIET", QUIET_SECONDS)
            if quiet or now - last_push >= _seconds(
                "MAX_INTERVAL", MAX_INTERVAL_SECONDS
            ):
                if _push_pending(repo, loaded) == gitpush.TRANSIENT:
                    return True
                last_push = clock.now()
        clock.sleep(POLL_SECONDS)
    return False


def run_worker(repo: Path, clock: Clock | None = None) -> None:
    """Run as the single worker for this clone until there is nothing left to push."""
    clock = clock or Clock()
    state = state_dir(repo)
    while _acquire(state, clock):
        try:
            gave_up = _drain(repo, clock)
        finally:
            (state / LOCK).unlink(missing_ok=True)
        # A hook may have staged a snapshot just after the idle check; go round again.
        if gave_up or is_disabled(repo) or not load_pending(repo):
            return


def spawn_worker(repo: Path, script: Path) -> None:
    """Start a detached worker for ``repo`` unless one is already running."""
    if worker_running(repo):
        return
    kwargs: dict[str, object] = {"start_new_session": True}
    if sys.platform == "win32":
        kwargs = {
            "creationflags": 0x00000008 | 0x00000200
        }  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(
        [sys.executable, str(script), "--worker", "--repo", str(repo)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **kwargs,
    )
