import json
import stat
from datetime import UTC, datetime

import pytest
from gitutil import sh

from session_log_extractors.capture import gitpush
from session_log_extractors.capture.locate import Snapshot

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


def snap(session="s1", text='{"n":1}\n', harness="claude"):
    files = {f"session_logs/{harness}/{session}.jsonl": text}
    return Snapshot(harness, session, files, {"harness_version": "2.1"})


def user_state(clone):
    return {
        "head": sh(clone, "git", "rev-parse", "HEAD"),
        "branch": sh(clone, "git", "branch", "--show-current"),
        "status": sh(clone, "git", "status", "--porcelain=v1"),
        "index": sh(clone, "git", "ls-files", "-s"),
        "diff": sh(clone, "git", "diff"),
        "branches": sh(clone, "git", "for-each-ref", "refs/heads"),
    }


def remote_file(remote, path, ref="session-logs"):
    return sh(remote, "git", "show", f"{ref}:{path}")


def test_first_push_creates_the_branch_without_touching_user_state(clone, remote):
    before = user_state(clone)

    outcome = gitpush.commit_and_push(clone, [snap()], NOW)

    assert outcome.status == gitpush.PUSHED
    assert user_state(clone) == before
    assert (clone / "untracked.txt").read_text() == "u\n"
    assert remote_file(remote, "session_logs/claude/s1.jsonl") == '{"n":1}\n'
    manifest = json.loads(remote_file(remote, "session_logs/manifest.json"))
    assert manifest["sessions"]["claude/s1"]["harness_version"] == "2.1"
    assert manifest["sessions"]["claude/s1"]["updated_at"] == NOW.isoformat()


def test_later_pushes_replace_the_session_and_keep_history_linear(clone, remote):
    gitpush.commit_and_push(clone, [snap(text="one\n"), snap("keep", "kept\n")], NOW)
    before = user_state(clone)

    outcome = gitpush.commit_and_push(clone, [snap(text="two\n")], NOW)

    assert outcome.status == gitpush.PUSHED
    assert user_state(clone) == before
    assert remote_file(remote, "session_logs/claude/s1.jsonl") == "two\n"
    assert remote_file(remote, "session_logs/claude/keep.jsonl") == "kept\n"
    manifest = json.loads(remote_file(remote, "session_logs/manifest.json"))
    assert set(manifest["sessions"]) == {"claude/s1", "claude/keep"}
    parents = sh(remote, "git", "rev-list", "--parents", "session-logs").splitlines()
    assert len(parents) == 2
    assert [len(line.split()) - 1 for line in parents] == [1, 0]


def test_replacing_a_session_removes_its_stale_files_but_not_lookalikes(clone, remote):
    first = snap("abc")
    first.files["session_logs/claude/abc/subagents/agent-1.jsonl"] = "old\n"
    gitpush.commit_and_push(clone, [first, snap("abcd", "other\n")], NOW)

    gitpush.commit_and_push(clone, [snap("abc", "new\n")], NOW)

    tree = sh(remote, "git", "ls-tree", "-r", "--name-only", "session-logs").split()
    assert "session_logs/claude/abc/subagents/agent-1.jsonl" not in tree
    assert "session_logs/claude/abcd.jsonl" in tree


def test_conflict_with_a_concurrent_push_is_retried_on_the_new_parent(
    clone, remote, tmp_path, monkeypatch
):
    gitpush.commit_and_push(clone, [snap("a", "a\n")], NOW)
    calls = []
    real = gitpush.fetch_parent

    def stale_then_real(repo):
        calls.append(1)
        if len(calls) == 1:
            return None
        return real(repo)

    monkeypatch.setattr(gitpush, "fetch_parent", stale_then_real)

    outcome = gitpush.commit_and_push(clone, [snap("b", "b\n")], NOW)

    assert outcome.status == gitpush.PUSHED
    assert len(calls) == 2
    assert remote_file(remote, "session_logs/claude/a.jsonl") == "a\n"
    assert remote_file(remote, "session_logs/claude/b.jsonl") == "b\n"


def test_persistent_conflict_gives_up_quietly(clone, monkeypatch):
    monkeypatch.setattr(gitpush, "fetch_parent", lambda repo: None)
    gitpush.commit_and_push(clone, [snap("a", "a\n")], NOW)

    outcome = gitpush.commit_and_push(clone, [snap("b", "b\n")], NOW)

    assert outcome.status == gitpush.TRANSIENT


HOOK = """#!/bin/sh
while read old new ref; do
  if git grep -q BADSECRET "$new" -- session_logs; then
    echo "error: GH013: Repository rule violations found for $ref." >&2
    echo "  - Push cannot contain secrets" >&2
    exit 1
  fi
done
"""


def install_hook(remote, body=HOOK):
    hook = remote / "hooks" / "pre-receive"
    hook.write_text(body)
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR)


def test_secret_scanning_rejection_is_final_and_pushes_nothing(clone, remote):
    install_hook(remote)
    text = '{"n":1}\n{"leak":"BADSECRET"}\n'

    outcome = gitpush.commit_and_push(clone, [snap(text=text)], NOW)

    # The filter plug-in owns what may be saved, so a block is reported, never worked around.
    assert outcome.status == gitpush.BLOCKED
    branches = sh(remote, "git", "branch", "--list", "session-logs")
    assert branches.strip() == ""


def test_lost_access_is_reported_as_disabled(clone, tmp_path):
    sh(clone, "git", "remote", "set-url", "origin", str(tmp_path / "gone.git"))

    outcome = gitpush.commit_and_push(clone, [snap()], NOW)

    assert outcome.status == gitpush.DISABLED


def test_unreachable_remote_is_transient(clone):
    sh(clone, "git", "remote", "set-url", "origin", "http://127.0.0.1:1/none.git")

    outcome = gitpush.commit_and_push(clone, [snap()], NOW)

    assert outcome.status == gitpush.TRANSIENT


@pytest.mark.parametrize(
    ("stderr", "kind"),
    [
        ("remote: error: GH013: Repository rule violations", "secret"),
        ("! [rejected] main (non-fast-forward)", "conflict"),
        ("remote: Permission to org/repo.git denied to user.", "access"),
        ("fatal: Authentication failed for 'https://github.com/x'", "access"),
        ("fatal: unable to access: Could not resolve host", "transient"),
    ],
)
def test_classify(stderr, kind):
    assert gitpush.classify(stderr) == kind


def test_git_error_carries_stderr(clone):
    with pytest.raises(gitpush.GitError) as info:
        gitpush.git(clone, "cat-file", "-p", "deadbeef")

    assert info.value.stderr


def test_read_manifest_returns_empty_for_missing_or_unknown_parents(clone, remote):
    gitpush.commit_and_push(clone, [snap()], NOW)
    parent = gitpush.fetch_parent(clone)
    assert gitpush._read_manifest(clone, parent)["version"] == 1
    assert gitpush._read_manifest(clone, "0" * 40) == {}
    assert gitpush._read_manifest(clone, None) == {}
