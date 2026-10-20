"""Commit snapshots to the ``session-logs`` branch with git plumbing, never touching the user's work.

A commit is built in a private index file (``GIT_INDEX_FILE``), so the candidate's index,
HEAD, branches, and working tree are never read or written. The new commit is parented on
the last pushed ``session-logs`` commit, kept in a private ref (``refs/woven/session-logs``),
so history stays linear and a later snapshot replaces an earlier one.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import HOOK_VERSION
from .locate import Snapshot

BRANCH = "session-logs"
PRIVATE_REF = f"refs/woven/{BRANCH}"
MANIFEST_PATH = "session_logs/manifest.json"
IDENTITY = {
    "GIT_AUTHOR_NAME": "Woven Capture",
    "GIT_AUTHOR_EMAIL": "capture@woven.invalid",
    "GIT_COMMITTER_NAME": "Woven Capture",
    "GIT_COMMITTER_EMAIL": "capture@woven.invalid",
}

PUSHED = "pushed"
DISABLED = "disabled"
TRANSIENT = "transient"
BLOCKED = "blocked"

_SECRET_REJECTION = ("GH013", "push protection", "cannot contain secrets")
_CONFLICT = ("non-fast-forward", "fetch first", "[rejected]", "stale info")
_ACCESS_LOST = (
    "permission to",
    "permission denied",
    "authentication failed",
    "could not read username",
    "repository not found",
    "invalid username or token",
    "returned error: 403",
    "access denied",
    "does not appear to be a git repository",
)


class GitError(Exception):
    """A git command failed; ``stderr`` holds what it said."""

    def __init__(self, stderr: str) -> None:
        super().__init__(stderr)
        self.stderr = stderr


@dataclass
class Outcome:
    status: str
    detail: str = ""
    commit: str | None = None


def git(
    repo: Path, *args: str, env: dict[str, str] | None = None, data: bytes | None = None
) -> bytes:
    """Run git in ``repo``; raise ``GitError`` on a non-zero exit."""
    merged = {**os.environ, "GIT_TERMINAL_PROMPT": "0", **(env or {})}
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        input=data,
        capture_output=True,
        env=merged,
        check=False,
    )
    if proc.returncode != 0:
        raise GitError((proc.stderr or proc.stdout).decode("utf-8", errors="replace"))
    return proc.stdout


def git_dir(repo: Path) -> Path:
    out = git(repo, "rev-parse", "--absolute-git-dir").decode().strip()
    return Path(out)


def fetch_parent(repo: Path) -> str | None:
    """Fetch the remote ``session-logs`` branch into a private ref; None if it doesn't exist yet."""
    listed = git(repo, "ls-remote", "origin", f"refs/heads/{BRANCH}").decode().strip()
    if not listed:
        return None
    git(repo, "fetch", "-q", "origin", f"+refs/heads/{BRANCH}:{PRIVATE_REF}")
    return git(repo, "rev-parse", PRIVATE_REF).decode().strip()


def _belongs(path: str, snap: Snapshot) -> bool:
    base = f"session_logs/{snap.harness}/{snap.session_id}"
    return path == f"{base}.jsonl" or path.startswith(f"{base}/")


def _read_manifest(repo: Path, parent: str | None) -> dict[str, Any]:
    if parent is None:
        return {}
    try:
        loaded = json.loads(git(repo, "show", f"{parent}:{MANIFEST_PATH}"))
    except (GitError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def build_manifest(
    previous: dict[str, Any], snaps: list[Snapshot], now: datetime
) -> dict[str, Any]:
    sessions = dict(previous.get("sessions") or {})
    for snap in snaps:
        sessions[f"{snap.harness}/{snap.session_id}"] = {
            **snap.meta,
            "harness": snap.harness,
            "session_id": snap.session_id,
            "hook_version": HOOK_VERSION,
            "updated_at": now.isoformat(),
        }
    return {"version": 1, "sessions": sessions}


def build_commit(
    repo: Path, parent: str | None, snaps: list[Snapshot], now: datetime
) -> str:
    """Create a commit holding ``snaps`` on top of ``parent`` without touching the user's index."""
    state = git_dir(repo) / "woven-capture"
    state.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=state) as tmp:
        env = {**IDENTITY, "GIT_INDEX_FILE": str(Path(tmp) / "index")}
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = now.isoformat()
        _load_parent_tree(repo, parent, snaps, env)
        manifest = build_manifest(_read_manifest(repo, parent), snaps, now)
        files = {MANIFEST_PATH: json.dumps(manifest, indent=1, sort_keys=True) + "\n"}
        for snap in snaps:
            files.update(snap.files)
        _stage(repo, files, env)
        tree = git(repo, "write-tree", env=env).decode().strip()
        args = ["commit-tree", tree, "-m", f"Capture session logs {now.isoformat()}"]
        if parent:
            args += ["-p", parent]
        return git(repo, *args, env=env).decode().strip()


def _load_parent_tree(
    repo: Path, parent: str | None, snaps: list[Snapshot], env: dict[str, str]
) -> None:
    if parent is None:
        git(repo, "read-tree", "--empty", env=env)
        return
    git(repo, "read-tree", parent, env=env)
    existing = git(repo, "ls-files", "-z", env=env).decode().split("\0")
    stale = [p for p in existing if p and any(_belongs(p, s) for s in snaps)]
    for path in stale:
        git(repo, "update-index", "--force-remove", "--", path, env=env)


def _stage(repo: Path, files: dict[str, str], env: dict[str, str]) -> None:
    lines = []
    for path, text in files.items():
        sha = (
            git(
                repo, "hash-object", "-w", "--stdin", data=text.encode("utf-8"), env=env
            )
            .decode()
            .strip()
        )
        lines.append(f"100644 {sha}\t{path}\n")
    git(
        repo,
        "update-index",
        "--add",
        "--index-info",
        data="".join(lines).encode("utf-8"),
        env=env,
    )


def classify(stderr: str) -> str:
    """Name what a failed push means: secret, conflict, access lost, or just transient."""
    lowered = stderr.lower()
    if any(marker.lower() in lowered for marker in _SECRET_REJECTION):
        return "secret"
    if any(marker in lowered for marker in _CONFLICT):
        return "conflict"
    if any(marker in lowered for marker in _ACCESS_LOST):
        return "access"
    return "transient"


def push(repo: Path, sha: str) -> tuple[str, str]:
    """Push ``sha`` as ``session-logs``. Returns (kind, stderr); kind is 'ok' or a classification."""
    try:
        git(repo, "push", "-q", "origin", f"{sha}:refs/heads/{BRANCH}")
    except GitError as err:
        return classify(err.stderr), err.stderr
    git(repo, "update-ref", PRIVATE_REF, sha)
    return "ok", ""


def commit_and_push(
    repo: Path, snaps: list[Snapshot], now: datetime | None = None
) -> Outcome:
    """Commit ``snaps`` and push, retrying once if another push won the race.

    A secret-scanning rejection is final: the filter plug-in is responsible for what
    reaches this point, so the push is reported as blocked and nothing is altered or resent.
    """
    when = now or datetime.now(UTC)
    retried_conflict = False
    while True:
        try:
            sha = build_commit(repo, fetch_parent(repo), snaps, when)
            kind, stderr = push(repo, sha)
        except GitError as err:
            kind, stderr, sha = classify(err.stderr), err.stderr, ""
        if kind == "ok":
            return Outcome(PUSHED, commit=sha)
        if kind == "conflict" and not retried_conflict:
            retried_conflict = True
            continue
        return Outcome(_final_status(kind), detail=stderr.strip()[-400:])


def _final_status(kind: str) -> str:
    return {"access": DISABLED, "secret": BLOCKED}.get(kind, TRANSIENT)
