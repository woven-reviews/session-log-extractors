"""Upload envelopes through an isolated bare repository and private index."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

BRANCH = "refs/heads/session-logs"
_IDENTITY = {
    "GIT_AUTHOR_NAME": "Woven Capture",
    "GIT_AUTHOR_EMAIL": "capture@woven.invalid",
    "GIT_COMMITTER_NAME": "Woven Capture",
    "GIT_COMMITTER_EMAIL": "capture@woven.invalid",
}
_CREDENTIAL_HELPER = (
    '!f() { printf "username=%s\\npassword=%s\\n" '
    '"$SESSION_LOG_GIT_USERNAME" "$SESSION_LOG_GIT_PASSWORD"; }; f'
)


class UploadError(Exception):
    """An upload failure with an explicit secret-rejection classification."""

    def __init__(self, secret_rejection: bool = False) -> None:
        self.secret_rejection = secret_rejection
        super().__init__("capture upload failed; pending work retained")


def _git(repo: Path, args: list[str], data: bytes | None = None) -> bytes:
    env = {
        **os.environ,
        **_IDENTITY,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_INDEX_FILE": str(repo / "capture-index"),
    }
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "-c",
                "credential.helper=",
                "-c",
                f"credential.helper={_CREDENTIAL_HELPER}",
                *args,
            ],
            input=data,
            capture_output=True,
            env=env,
            timeout=120,
            check=False,
        )
    except subprocess.TimeoutExpired as timeout:
        raise UploadError() from timeout
    if result.returncode:
        error = result.stderr.lower()
        secret = any(
            marker in error
            for marker in (b"gh013", b"push protection", b"cannot contain secrets")
        )
        raise UploadError(secret)
    return result.stdout


def upload(state: Path, remote: str, files: dict[str, bytes]) -> None:
    repo = state / "upload.git"
    repo.mkdir(exist_ok=True)
    _git(repo, ["init", "--bare", "-q"])
    listed = _git(repo, ["ls-remote", remote, BRANCH]).strip()
    parent = ""
    if listed:
        _git(repo, ["fetch", "-q", remote, f"+{BRANCH}:refs/capture/parent"])
        parent = _git(repo, ["rev-parse", "refs/capture/parent"]).decode().strip()
    _git(repo, ["read-tree", parent] if parent else ["read-tree", "--empty"])
    entries = []
    for path, content in files.items():
        blob = _git(repo, ["hash-object", "-w", "--stdin"], content).decode().strip()
        entries.append(f"100644 {blob}\t{path}\n")
    _git(repo, ["update-index", "--add", "--index-info"], "".join(entries).encode())
    tree = _git(repo, ["write-tree"]).decode().strip()
    parents = ["-p", parent] if parent else []
    commit = (
        _git(repo, ["commit-tree", tree, *parents, "-m", "Capture session logs"])
        .decode()
        .strip()
    )
    _git(repo, ["push", "-q", remote, f"{commit}:{BRANCH}"])
