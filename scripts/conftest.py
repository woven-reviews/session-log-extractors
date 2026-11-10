"""Disposable assessment state backed by the committed native Claude capture."""

import json
import subprocess

import pytest
from capture_fixture import git
from claude_fixture import TRANSCRIPT

from session_log_extractors import capture_runtime


@pytest.fixture
def capture(tmp_path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    repo = tmp_path / "candidate"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Candidate")
    git(repo, "config", "user.email", "candidate@example.invalid")
    (repo / "answer.txt").write_text("before")
    git(repo, "add", "answer.txt")
    git(repo, "commit", "-qm", "Initial answer")
    (repo / "answer.txt").write_text("staged answer")
    git(repo, "add", "answer.txt")
    (repo / "answer.txt").write_text("unstaged answer")
    # Change only native cwd evidence to point at this isolated assessment.
    records = []
    for line in TRANSCRIPT.read_text().splitlines():
        record = json.loads(line)
        if "cwd" in record:
            record["cwd"] = str(repo)
        records.append(json.dumps(record))
    transcript = repo / "native.jsonl"
    transcript.write_text("\n".join(records) + "\n")
    job = capture_runtime.CaptureJob(
        harness="claude",
        session_id=TRANSCRIPT.stem,
        transcript=str(transcript),
        repo=str(repo),
        remote=str(remote),
    )
    return capture_runtime, job, repo, remote
