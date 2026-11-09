"""Capture contracts use native evidence and disposable local Git remotes."""

import base64
import json
import os
import shutil
import sqlite3
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from capture_fixture import git
from codex_fixture import TRANSCRIPT as CODEX_TRANSCRIPT
from copilot_fixture import DATABASE, SESSION_ID, STATE_ROOT

from session_log_extractors import capture_upload, copilot_sources
from session_log_extractors.capture_jobs import load_job
from session_log_extractors.capture_upload import UploadError


def test_capture_exports_complete_native_session_with_explicit_cap(capture):
    module, job, _repo, _remote = capture
    envelope = module.extract(job)
    assert envelope["events"]
    assert envelope["session_id"] == job.session_id
    assert envelope["truncation"]["tool_result_max_bytes"] == 4096
    images = []
    for event in envelope["events"]:
        images.extend(event.get("images", []))
    assert images, "Native capture must supply inline image evidence"


def test_capture_rejects_unowned_session(capture):
    module, job, _repo, _remote = capture
    unrelated = module.CaptureJob(
        harness=job.harness,
        session_id=job.session_id,
        transcript=job.transcript,
        repo="/unrelated",
        remote=job.remote,
    )
    with pytest.raises(ValueError, match="assessment"):
        module.extract(unrelated)


def test_upload_preserves_candidate_state_and_merges_sessions(capture, tmp_path):
    module, job, repo, remote = capture
    before = (
        git(repo, "rev-parse", "HEAD"),
        git(repo, "diff", "--cached"),
        git(repo, "diff"),
        git(repo, "symbolic-ref", "HEAD"),
    )
    state = tmp_path / "state"
    state.mkdir()
    first = module.stage(state, job)
    module.drain(state, quiet_seconds=0, retry_seconds=0)
    assert not first.exists()
    recorded = json.loads(git(remote, "show", f"session-logs:{job.output_path}"))
    assert recorded["events"]
    assert recorded["session_id"] == job.session_id
    after = (
        git(repo, "rev-parse", "HEAD"),
        git(repo, "diff", "--cached"),
        git(repo, "diff"),
        git(repo, "symbolic-ref", "HEAD"),
    )
    assert after == before
    assert (repo / "answer.txt").read_text() == "unstaged answer"


def test_failed_upload_retains_pending_work_for_recovery(capture, tmp_path):
    module, job, _repo, remote = capture
    state = tmp_path / "state"
    state.mkdir()
    pending = module.stage(state, job)
    remote.rename(tmp_path / "offline.git")
    module.drain(state, quiet_seconds=0, retry_seconds=0)
    assert pending.exists()
    assert (state / "last_error").read_text()
    (tmp_path / "offline.git").rename(remote)
    module.drain(state, quiet_seconds=0, retry_seconds=0)
    assert not pending.exists()


@pytest.mark.parametrize("session_id", ["../escape", "", "a/b", "a\\b"])
def test_unsafe_recorded_identifier_is_rejected(capture, session_id):
    module, job, _repo, _remote = capture
    with pytest.raises(ValueError, match="session"):
        module.CaptureJob(
            harness=job.harness,
            session_id=session_id,
            transcript=job.transcript,
            repo=job.repo,
            remote=job.remote,
        )


def test_codex_capture_reads_native_session(capture):
    module, job, repo, _remote = capture
    records = []
    session_id = None
    for line in CODEX_TRANSCRIPT.read_text().splitlines():
        record = json.loads(line)
        payload = record.get("payload", {})
        if "cwd" in payload:
            payload["cwd"] = str(repo)
        if record.get("type") == "session_meta":
            session_id = payload["id"]
        records.append(json.dumps(record))
    assert session_id
    path = repo / "codex.jsonl"
    path.write_text("\n".join(records))
    selected = replace(
        job, harness="codex", session_id=session_id, transcript=str(path)
    )
    envelope = module.extract(selected)
    assert envelope["session_id"] == session_id
    assert envelope["events"]


def test_copilot_capture_reads_native_session(capture, monkeypatch):
    module, job, repo, _remote = capture
    database = repo / "session-store.db"
    shutil.copyfile(DATABASE, database)
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE sessions SET cwd = ?", (str(repo),))
    monkeypatch.setattr(copilot_sources, "COPILOT_STATE_ROOT", STATE_ROOT)
    selected = replace(
        job, harness="copilot", session_id=SESSION_ID, transcript=str(database)
    )
    envelope = module.extract(selected)
    assert envelope["session_id"] == SESSION_ID
    assert envelope["events"]
    with pytest.raises(ValueError, match="exact"):
        module.extract(replace(selected, session_id="missing"))


@pytest.mark.parametrize("content", ["[]", '{"harness": 1}', '{"harness":"claude"}'])
def test_malformed_pending_request_is_rejected(tmp_path, content):
    path = tmp_path / "request.json"
    path.write_text(content)
    with pytest.raises(ValueError, match="request"):
        load_job(path)


def test_capture_requires_complete_configuration(capture):
    _module, job, _repo, _remote = capture
    with pytest.raises(ValueError, match="remote"):
        replace(job, remote="")


def test_newer_turn_arriving_during_upload_is_not_removed(
    capture, tmp_path, monkeypatch
):
    module, job, repo, remote = capture
    state = tmp_path / "state"
    state.mkdir()
    first = module.stage(state, job)
    upload = module.upload
    observed = []

    def upload_with_new_turn(state_dir, destination, files):
        upload(state_dir, destination, files)
        if not observed:
            # Stage another immutable request during the first upload. Its source
            # is the same native session, as when a newer turn has been appended.
            observed.append(module.stage(state_dir, job))

    monkeypatch.setattr(module, "upload", upload_with_new_turn)
    module.drain(state, quiet_seconds=0, retry_seconds=0)
    assert not first.exists()
    assert len(observed) == 1
    assert not observed[0].exists()
    commits = git(remote, "rev-list", "--count", "session-logs")
    assert commits == "2"


def test_upload_retains_other_sessions_and_remote_history(capture, tmp_path):
    module, job, _repo, remote = capture
    state = tmp_path / "state"
    state.mkdir()
    module.upload(state, str(remote), {"session_logs/prior.json": b'{"kept":true}'})
    module.stage(state, job)
    module.drain(state, quiet_seconds=0, retry_seconds=0)
    assert (
        git(remote, "show", "session-logs:session_logs/prior.json") == '{"kept":true}'
    )
    assert git(remote, "rev-list", "--count", "session-logs") == "2"


def test_secret_rejection_quarantines_without_automatic_resend(
    capture, tmp_path, monkeypatch
):
    module, job, _repo, _remote = capture
    state = tmp_path / "state"
    state.mkdir()
    pending = module.stage(state, job)
    attempts = []

    def reject(_state, _remote, _files):
        attempts.append(True)
        raise UploadError(secret_rejection=True)

    monkeypatch.setattr(module, "upload", reject)
    module.drain(state, quiet_seconds=0, retry_seconds=0)
    module.drain(state, quiet_seconds=0, retry_seconds=0)
    assert attempts == [True]
    assert not pending.exists()
    assert (state / "rejected" / pending.name).exists()
    assert "secrets" in (state / "last_error").read_text()


def test_invalid_source_keeps_request_and_reports_error(capture, tmp_path):
    module, job, _repo, _remote = capture
    state = tmp_path / "state"
    state.mkdir()
    pending = module.stage(state, replace(job, transcript="/missing/capture.jsonl"))
    module.drain(state, quiet_seconds=0, retry_seconds=0)
    assert pending.exists()
    assert "source" in (state / "last_error").read_text()


def test_queue_cannot_mix_assessment_remotes(capture, tmp_path):
    module, job, _repo, _remote = capture
    state = tmp_path / "state"
    state.mkdir()
    first = module.stage(state, job)
    second = module.stage(state, replace(job, remote="/unrelated/remote.git"))
    module.drain(state, quiet_seconds=0, retry_seconds=0)
    assert first.exists()
    assert second.exists()


def test_worker_waits_then_rereads_final_source(capture, tmp_path, monkeypatch):
    module, job, _repo, remote = capture
    state = tmp_path / "state"
    state.mkdir()
    pending = module.stage(state, job)
    waits = []
    original_time = module.time.time

    def wait(seconds):
        waits.append(seconds)
        # Complete one native assistant text block after Stop has staged its
        # signal, retaining the block's recorded owner and identity.
        records = []
        for line in Path(job.transcript).read_text().splitlines():
            records.append(json.loads(line))
        final_text = _final_assistant_text(records)
        final_text["text"] = "FINAL_CAPTURE_TEST_MARKER"
        lines = []
        for record in records:
            lines.append(json.dumps(record))
        Path(job.transcript).write_text("\n".join(lines))
        os.utime(pending, (original_time() - 20, original_time() - 20))

    monkeypatch.setattr(module.time, "sleep", wait)
    module.drain(state, quiet_seconds=10, retry_seconds=0)
    assert len(waits) == 1
    assert waits[0] > 0
    assert not pending.exists()
    recorded = json.loads(git(remote, "show", f"session-logs:{job.output_path}"))
    texts = []
    for event in recorded["events"]:
        texts.append(event.get("text"))
    assert "FINAL_CAPTURE_TEST_MARKER" in texts


def _final_assistant_text(records):
    final_text = None
    for record in records:
        if record.get("type") != "assistant" or record.get("isSidechain"):
            continue
        content = record.get("message", {}).get("content", [])
        if isinstance(content, list):
            for block in content:
                if block.get("type") == "text":
                    final_text = block
    assert final_text is not None, "Native capture must contain assistant text"
    return final_text


def test_request_worker_cannot_consume_another_requests_files(capture, tmp_path):
    module, job, _repo, _remote = capture
    state = tmp_path / "state"
    first = module.stage(state, job)
    second = module.stage(state, job)
    module.drain(state, quiet_seconds=0, retry_seconds=0, request=first.name)
    assert not first.exists()
    assert second.exists()
    module.drain(state, quiet_seconds=0, retry_seconds=0, request=first.name)
    assert second.exists()


def test_worker_rejects_request_path_traversal(capture, tmp_path):
    module, _job, _repo, _remote = capture
    with pytest.raises(ValueError, match="filename"):
        module.drain(tmp_path, request="../outside.json")


def test_worker_cli_empty_queue(capture, tmp_path):
    module, _job, _repo, _remote = capture
    status = module.main([str(tmp_path), "--request", "missing.json"])
    assert status == 0


def test_copilot_capture_uses_selected_event_file(capture, tmp_path, monkeypatch):
    module, job, repo, _remote = capture
    database = repo / "session-store.db"
    shutil.copyfile(DATABASE, database)
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE sessions SET cwd = ?", (str(repo),))
    state_log = STATE_ROOT / SESSION_ID / "events.jsonl"
    selected = replace(
        job,
        harness="copilot",
        session_id=SESSION_ID,
        transcript=str(database),
        state_log=str(state_log),
    )
    monkeypatch.setattr(copilot_sources, "COPILOT_STATE_ROOT", tmp_path / "unrelated")
    envelope = module.extract(selected)
    calls = []
    for event in envelope["events"]:
        calls.extend(event.get("tool_calls", []))
    assert calls, "Selected native event file must retain tool activity"


def test_git_timeout_is_a_retryable_upload_failure(tmp_path, monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("git", 120)

    monkeypatch.setattr(capture_upload.subprocess, "run", timeout)
    with pytest.raises(UploadError) as error:
        capture_upload.upload(tmp_path, "/local/remote.git", {})
    assert error.value.secret_rejection is False


def _recorded_user_image(records):
    for record in records:
        if record.get("type") != "user":
            continue
        content = record.get("message", {}).get("content", [])
        if isinstance(content, list):
            for block in content:
                if block.get("type") == "image":
                    return block["source"]
    raise AssertionError("Native capture must contain user image evidence")


def test_capture_retains_large_recorded_image_payload(capture):
    module, job, _repo, _remote = capture
    records = []
    for line in Path(job.transcript).read_text().splitlines():
        records.append(json.loads(line))
    target = _recorded_user_image(records)
    # Enlarge only the recorded binary payload to exercise retention beyond
    # ordinary upload-warning size, without relying on generated wording.
    data = base64.b64encode(b"x" * 7_000_000).decode()
    target["data"] = data
    lines = []
    for record in records:
        lines.append(json.dumps(record))
    Path(job.transcript).write_text("\n".join(lines))
    envelope = module.extract(job)
    images = []
    clipped = []
    for event in envelope["events"]:
        images.extend(event.get("images", []))
        for call in event.get("tool_calls", []):
            if call.get("result_truncated"):
                clipped.append(call)
    image_payloads = []
    for image in images:
        if "data" in image:
            image_payloads.append(image["data"])
    assert data in image_payloads
    assert clipped, "Native capture must contain results exceeding capture's cap"
    for call in clipped:
        assert len(call["result"].encode()) <= 4096
