import os
import subprocess
import sys
from types import SimpleNamespace

import pytest
from gitutil import sh

from session_log_extractors.capture import pusher
from session_log_extractors.capture.locate import Snapshot


def snap(text="one\n", session="s1"):
    return Snapshot(
        "claude",
        session,
        {f"session_logs/claude/{session}.jsonl": text},
        {"harness_version": "2"},
    )


class FakeClock:
    """Time that only moves when the worker sleeps, so debounce is tested without waiting."""

    def __init__(self, start):
        self.t = start
        self.sleeps = 0

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds
        self.sleeps += 1


def fake_clock_for(clone):
    state = pusher.state_dir(clone)
    newest = max(
        (p.stat().st_mtime for p in state.rglob("*") if p.is_file()), default=0
    )
    fake = FakeClock(newest)
    return fake, pusher.Clock(now=fake.now, sleep=fake.sleep)


def remote_text(remote, path):
    return sh(remote, "git", "show", f"session-logs:{path}")


def test_stage_writes_pending_files_and_load_pending_round_trips(clone):
    pusher.stage(clone, snap("hello\n"))

    loaded = pusher.load_pending(clone)

    assert len(loaded) == 1
    staged, signature = loaded[0]
    assert staged.files == {"session_logs/claude/s1.jsonl": "hello\n"}
    assert staged.meta == {"harness_version": "2"}
    assert list(signature) == ["session_logs/claude/s1.jsonl"]


def test_staging_the_same_session_again_replaces_the_files(clone):
    pusher.stage(clone, snap("old\n"))
    pusher.stage(clone, snap("new\n"))

    assert (
        pusher.load_pending(clone)[0][0].files["session_logs/claude/s1.jsonl"]
        == "new\n"
    )


def test_load_pending_skips_corrupt_entries(clone):
    pusher.stage(clone, snap())
    (pusher.state_dir(clone) / pusher.META / "bad__x.json").write_text("{not json")
    (pusher.state_dir(clone) / pusher.META / "gone__y.json").write_text(
        '{"harness":"h","session_id":"y","files":["nope"]}'
    )

    assert len(pusher.load_pending(clone)) == 1


def test_worker_waits_for_quiet_then_pushes_the_newest_snapshot(clone, remote):
    pusher.stage(clone, snap("turn 1\n"))
    pusher.stage(clone, snap("turn 2\n"))
    fake, clock = fake_clock_for(clone)

    pusher.run_worker(clone, clock)

    assert remote_text(remote, "session_logs/claude/s1.jsonl") == "turn 2\n"
    assert fake.sleeps >= int(pusher.QUIET_SECONDS)
    assert pusher.load_pending(clone) == []
    assert not pusher.worker_running(clone)


def test_worker_pushes_at_the_maximum_interval_even_if_never_quiet(
    clone, remote, monkeypatch
):
    pusher.stage(clone, snap("busy\n"))
    fake, _ = fake_clock_for(clone)
    started = fake.t
    pushed_at = []
    real_commit = pusher.gitpush.commit_and_push

    def record(repo, snaps):
        pushed_at.append(fake.t - started)
        return real_commit(repo, snaps)

    def keep_touching(seconds):
        fake.sleep(seconds)
        path = pusher.state_dir(clone) / pusher.PENDING / "session_logs/claude/s1.jsonl"
        if path.exists():
            os.utime(path, (fake.t, fake.t))

    monkeypatch.setattr(pusher.gitpush, "commit_and_push", record)

    pusher.run_worker(clone, pusher.Clock(now=fake.now, sleep=keep_touching))

    assert remote_text(remote, "session_logs/claude/s1.jsonl") == "busy\n"
    assert pushed_at[0] >= pusher.MAX_INTERVAL_SECONDS


def test_snapshot_staged_during_a_push_is_kept_for_the_next_round(
    clone, remote, monkeypatch
):
    pusher.stage(clone, snap("first\n"))
    real_commit = pusher.gitpush.commit_and_push

    staged = []

    def stage_newer_then_push(repo, snaps):
        if not staged:
            staged.append(True)
            pusher.stage(repo, snap("second\n"))
        return real_commit(repo, snaps)

    monkeypatch.setattr(pusher.gitpush, "commit_and_push", stage_newer_then_push)
    fake, clock = fake_clock_for(clone)

    pusher.run_worker(clone, clock)

    assert remote_text(remote, "session_logs/claude/s1.jsonl") == "second\n"
    assert pusher.load_pending(clone) == []


def test_lost_access_disables_capture_and_exits_quietly(clone, tmp_path):
    sh(clone, "git", "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    pusher.stage(clone, snap())
    _, clock = fake_clock_for(clone)

    pusher.run_worker(clone, clock)

    assert pusher.is_disabled(clone)
    assert pusher.load_pending(clone) == []
    assert pusher.take_last_error(clone) is None


def test_transient_failure_keeps_pending_and_leaves_one_warning(clone):
    sh(clone, "git", "remote", "set-url", "origin", "http://127.0.0.1:1/none.git")
    pusher.stage(clone, snap())
    _, clock = fake_clock_for(clone)

    pusher.run_worker(clone, clock)

    assert len(pusher.load_pending(clone)) == 1
    assert "will retry" in pusher.take_last_error(clone)
    assert pusher.take_last_error(clone) is None


def test_blocked_push_is_dropped_with_a_warning(clone, remote):
    hook = remote / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho 'GH013 push protection' >&2\nexit 1\n")
    hook.chmod(0o755)
    pusher.stage(clone, snap())
    _, clock = fake_clock_for(clone)

    pusher.run_worker(clone, clock)

    assert pusher.load_pending(clone) == []
    assert "not saved" in pusher.take_last_error(clone)


def test_second_worker_does_not_start_while_one_holds_the_lock(clone, remote):
    state = pusher.state_dir(clone)
    (state / pusher.LOCK).write_text("")
    pusher.stage(clone, snap())
    fake, clock = fake_clock_for(clone)

    pusher.run_worker(clone, clock)

    assert fake.sleeps == 0
    assert len(pusher.load_pending(clone)) == 1
    assert pusher.worker_running(clone)


def test_stale_lock_is_taken_over(clone, remote):
    state = pusher.state_dir(clone)
    lock = state / pusher.LOCK
    lock.write_text("")
    old = lock.stat().st_mtime - pusher.LOCK_STALE_SECONDS - 5
    os.utime(lock, (old, old))
    pusher.stage(clone, snap("late\n"))
    _, clock = fake_clock_for(clone)

    pusher.run_worker(clone, clock)

    assert remote_text(remote, "session_logs/claude/s1.jsonl") == "late\n"


def test_worker_exits_after_an_idle_period_with_nothing_staged(clone):
    fake, clock = fake_clock_for(clone)

    pusher.run_worker(clone, clock)

    assert fake.sleeps >= int(pusher.IDLE_EXIT_SECONDS)
    assert not pusher.worker_running(clone)


def test_worker_restarts_if_a_snapshot_lands_as_it_exits(clone, remote):
    pusher.stage(clone, snap("late arrival\n"))
    fake, clock = fake_clock_for(clone)
    calls = {"n": 0}
    real_drain = pusher._drain

    def drain_that_misses_it(repo, clk, refresh=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return
        real_drain(repo, clk, refresh)

    pusher._drain, original = drain_that_misses_it, pusher._drain
    try:
        pusher.run_worker(clone, clock)
    finally:
        pusher._drain = original

    assert calls["n"] == 2
    assert remote_text(remote, "session_logs/claude/s1.jsonl") == "late arrival\n"


def test_spawn_worker_starts_a_detached_process_only_when_none_is_running(
    clone, tmp_path, monkeypatch
):
    launched = []

    def fake_popen(argv, **kwargs):
        launched.append((argv, kwargs))

    monkeypatch.setattr(
        pusher,
        "subprocess",
        SimpleNamespace(Popen=fake_popen, DEVNULL=subprocess.DEVNULL),
    )
    script = tmp_path / "capture_session.py"

    pusher.spawn_worker(clone, script)

    argv, kwargs = launched[0]
    assert argv == [
        sys.executable,
        "-B",
        str(script),
        "--worker",
        "--repo",
        str(clone),
    ]
    assert kwargs["stdout"] == subprocess.DEVNULL
    assert kwargs["start_new_session"] is True
    (pusher.state_dir(clone) / pusher.LOCK).write_text("")
    pusher.spawn_worker(clone, script)
    assert len(launched) == 1


def test_spawn_worker_uses_windows_creation_flags(clone, tmp_path, monkeypatch):
    launched = []
    fake = SimpleNamespace(
        Popen=lambda argv, **kw: launched.append(kw), DEVNULL=subprocess.DEVNULL
    )
    monkeypatch.setattr(pusher, "subprocess", fake)
    monkeypatch.setattr(pusher.sys, "platform", "win32")

    pusher.spawn_worker(clone, tmp_path / "s.py")

    assert launched[0]["creationflags"] == 0x00000208
    assert "start_new_session" not in launched[0]


def test_default_clock_uses_real_time():
    clock = pusher.Clock()

    assert clock.now() > 0
    clock.sleep(0)


@pytest.mark.parametrize("seconds", [0])
def test_worker_running_is_false_without_a_lock(clone, seconds):
    assert not pusher.worker_running(clone, pusher.Clock())


def test_clearing_a_snapshot_whose_files_vanished_is_a_no_op(clone):
    staged = snap()
    pusher.stage(clone, staged)
    signature = pusher.load_pending(clone)[0][1]
    (pusher.state_dir(clone) / pusher.PENDING / "session_logs/claude/s1.jsonl").unlink()

    pusher._clear_if_unchanged(clone, staged, signature)

    assert pusher.load_pending(clone) == []


def staged_from_source(text="user only\n"):
    staged = snap(text)
    staged.source = {"harness": "claude", "payload": {"session_id": "s1"}}
    return staged


def test_the_staged_source_round_trips_through_the_pending_area(clone):
    pusher.stage(clone, staged_from_source())

    assert pusher.load_pending(clone)[0][0].source == {
        "harness": "claude",
        "payload": {"session_id": "s1"},
    }


def test_worker_rereads_the_transcript_at_push_time(clone, remote):
    """The hook fires before the harness flushes the turn, so the staged copy is stale."""
    pusher.stage(clone, staged_from_source("user only\n"))
    transcript = {"text": "user only\n"}
    transcript["text"] += "assistant reply\n"  # written after the hook fired

    def refresh(staged):
        return snap(transcript["text"])

    fake, clock = fake_clock_for(clone)
    pusher.run_worker(clone, clock, refresh)

    assert (
        remote_text(remote, "session_logs/claude/s1.jsonl")
        == "user only\nassistant reply\n"
    )
    assert pusher.load_pending(clone) == []


def test_worker_falls_back_to_the_staged_copy_when_the_source_is_gone(clone, remote):
    pusher.stage(clone, staged_from_source("staged copy\n"))
    fake, clock = fake_clock_for(clone)

    pusher.run_worker(clone, clock, lambda staged: None)

    assert remote_text(remote, "session_logs/claude/s1.jsonl") == "staged copy\n"


def test_worker_does_not_refresh_snapshots_without_a_source(clone, remote):
    pusher.stage(clone, snap("no source\n"))
    fake, clock = fake_clock_for(clone)

    def refresh(staged):
        raise AssertionError("must not be called")

    pusher.run_worker(clone, clock, refresh)

    assert remote_text(remote, "session_logs/claude/s1.jsonl") == "no source\n"


def test_worker_pushes_nothing_when_the_filter_rejects_at_push_time(clone, remote):
    pusher.stage(clone, staged_from_source())

    def refresh(staged):
        raise pusher.FilterError("session_log_filter.py rejected the file")

    fake, clock = fake_clock_for(clone)
    pusher.run_worker(clone, clock, refresh)

    assert sh(remote, "git", "branch", "--list", "session-logs").strip() == ""
    assert pusher.load_pending(clone) == []
    assert "nothing was saved" in pusher.take_last_error(clone)


def deferred_snap():
    staged = staged_from_source()
    staged.files = {"session_logs/claude/s1.json": ""}
    staged.deferred = True
    return staged


def test_a_deferred_placeholder_is_never_pushed_when_the_source_cannot_be_read(
    clone, remote
):
    pusher.stage(clone, deferred_snap())
    fake, clock = fake_clock_for(clone)

    pusher.run_worker(clone, clock, lambda staged: None)

    assert sh(remote, "git", "branch", "--list", "session-logs").strip() == ""
    assert pusher.load_pending(clone) == []
    assert pusher.take_last_error(clone) is None


def test_worker_pushes_nothing_when_the_extractor_fails_at_push_time(clone, remote):
    pusher.stage(clone, deferred_snap())

    def refresh(staged):
        raise pusher.EnvelopeError("extract_claude_session_log.py failed: bad")

    fake, clock = fake_clock_for(clone)
    pusher.run_worker(clone, clock, refresh)

    assert sh(remote, "git", "branch", "--list", "session-logs").strip() == ""
    assert pusher.load_pending(clone) == []
    assert "extract_claude_session_log.py failed" in pusher.take_last_error(clone)


def test_a_rebuilt_envelope_replaces_the_placeholder(clone, remote):
    pusher.stage(clone, deferred_snap())

    def refresh(staged):
        return Snapshot("claude", "s1", {"session_logs/claude/s1.json": '{"ok": 1}\n'})

    fake, clock = fake_clock_for(clone)
    pusher.run_worker(clone, clock, refresh)

    assert remote_text(remote, "session_logs/claude/s1.json") == '{"ok": 1}\n'
