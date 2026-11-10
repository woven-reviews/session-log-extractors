"""Native hook transport is decoded and staged by the Docker Python runtime."""

import json
from dataclasses import replace

import pytest

from session_log_extractors import capture_hook, capture_jobs
from session_log_extractors.harness import Harness


@pytest.mark.parametrize("harness", list(Harness))
def test_native_hook_fields_produce_explicit_source_mapping(capture, harness):
    _module, job, _repo, _remote = capture
    context = capture_hook.HookContext(
        harness, job.repo, job.remote, r"C:\Users\Candidate\.copilot"
    )
    payload = {"session_id": job.session_id, "transcript_path": job.transcript}
    actual = capture_hook.describe(payload, context)
    assert actual.session_id == job.session_id
    assert actual.checkout_mount == "/source/repo"
    if harness == Harness.COPILOT:
        assert actual.transcript == r"C:\Users\Candidate\.copilot\session-store.db"
        assert actual.state_log.endswith(job.session_id + r"\events.jsonl")
        assert str(actual.resolve(actual.state_log)) == "/source/events.jsonl"
    else:
        assert actual.transcript == job.transcript
        assert str(actual.resolve(job.transcript)) == "/source/transcript"


def test_camel_case_hook_fields_are_supported(capture):
    _module, job, _repo, _remote = capture
    context = capture_hook.HookContext(Harness.CODEX, job.repo, job.remote)
    actual = capture_hook.describe(
        {"sessionId": job.session_id, "transcriptPath": job.transcript}, context
    )
    assert actual.session_id == job.session_id


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"session_id": "../bad", "transcript_path": "/session"},
        {"session_id": "session", "transcript_path": "bad\npath"},
    ],
)
def test_malformed_transport_is_rejected(payload):
    context = capture_hook.HookContext(
        Harness.CLAUDE, "/assessment", "https://github.com/example/repo"
    )
    with pytest.raises(ValueError, match="hook|identifier|line breaks"):
        capture_hook.describe(payload, context)


def test_copilot_requires_explicit_store_location():
    context = capture_hook.HookContext(Harness.COPILOT, "/assessment", "remote")
    with pytest.raises(ValueError, match="store"):
        capture_hook.describe({"sessionId": "session"}, context)


def test_checkout_mapping_retains_only_assessment_files(capture):
    _module, job, repo, _remote = capture
    scoped = replace(job, checkout_mount="/source/repo")
    assert (
        str(scoped.resolve(str(repo / "assets/red.png")))
        == "/source/repo/assets/red.png"
    )
    with pytest.raises(ValueError, match="mounted"):
        scoped.resolve(str(repo / "../outside.png"))


def test_checkout_mapping_rejects_symlinks_to_other_container_files(capture, tmp_path):
    _module, job, repo, _remote = capture
    mounted = tmp_path / "mounted"
    mounted.mkdir()
    (mounted / "escape").symlink_to(tmp_path)
    scoped = replace(job, checkout_mount=str(mounted))
    with pytest.raises(ValueError, match="symlink"):
        scoped.resolve(str(repo / "escape/private"))


def test_worker_recovers_only_matching_sources_and_keeps_other_signals(
    capture, tmp_path, monkeypatch
):
    _module, job, _repo, _remote = capture
    state = tmp_path / "state"
    context = capture_hook.HookContext(Harness.CLAUDE, job.repo, job.remote)
    previous = capture_jobs.stage(state, job)
    unrelated = capture_jobs.stage(state, replace(job, session_id="unrelated"))
    malformed = state / "pending/malformed.json"
    malformed.write_text("[]")
    incoming = state / "incoming"
    incoming.mkdir()
    matching_signal = incoming / "matching.json"
    matching_signal.write_text(
        json.dumps({"session_id": job.session_id, "transcript_path": job.transcript})
    )
    unrelated_signal = incoming / "unrelated.json"
    unrelated_signal.write_text(
        json.dumps({"session_id": "other", "transcript_path": job.transcript})
    )
    bad_signal = incoming / "malformed.json"
    bad_signal.write_text("[]")
    request = state / "request.json"
    request.write_text(job.serialize())
    requests = state / "requests"
    requests.mkdir()
    abandoned = requests / "abandoned.json"
    abandoned.write_text(job.serialize())
    other_request = requests / "other.json"
    other_request.write_text(replace(job, session_id="other").serialize())
    processed = []

    def drain(_state, *, request):
        path = _state / "pending" / request
        processed.append(capture_jobs.load_job(path))
        path.unlink()

    monkeypatch.setattr(capture_hook, "drain", drain)
    capture_hook.consume(state, request, context)
    assert len(processed) == 3
    for actual in processed:
        assert actual == job
    assert not previous.exists()
    assert not request.exists()
    assert not abandoned.exists()
    assert other_request.exists()
    assert not matching_signal.exists()
    assert unrelated.exists() and malformed.exists()
    assert unrelated_signal.exists() and bad_signal.exists()


def test_worker_rejects_a_different_assessment_before_staging(capture, tmp_path):
    _module, job, _repo, _remote = capture
    request = tmp_path / "request.json"
    request.write_text(job.serialize())
    context = capture_hook.HookContext(Harness.CLAUDE, "/unrelated", job.remote)
    with pytest.raises(ValueError, match="assessment"):
        capture_hook.consume(tmp_path, request, context)
    assert request.exists()
    assert not (tmp_path / "pending").exists()


def test_describe_cli_outputs_line_protocol_and_serialized_job(
    capture, tmp_path, capsys
):
    _module, job, _repo, _remote = capture
    path = tmp_path / "hook.json"
    path.write_text(
        json.dumps({"session_id": job.session_id, "transcript_path": job.transcript}),
        encoding="utf-8-sig",
    )
    result = capture_hook.main(
        [
            "describe",
            str(path),
            "--harness",
            "claude",
            "--repo",
            job.repo,
            "--remote",
            job.remote,
        ]
    )
    assert result == 0
    session, transcript, state_log, descriptor = capsys.readouterr().out.splitlines()
    assert (session, transcript, state_log) == (job.session_id, job.transcript, "")
    parsed = json.loads(descriptor)
    assert parsed["checkout_mount"] == "/source/repo"


def test_run_cli_requires_state(capture, tmp_path):
    _module, job, _repo, _remote = capture
    arguments = [
        "run",
        str(tmp_path / "request.json"),
        "--harness",
        "claude",
        "--repo",
        job.repo,
        "--remote",
        job.remote,
    ]
    with pytest.raises(SystemExit) as error:
        capture_hook.main(arguments)
    assert error.value.code == 2


def test_run_cli_uses_python_queue(capture, tmp_path, monkeypatch):
    _module, job, _repo, _remote = capture
    request = tmp_path / "request.json"
    request.write_text(job.serialize())
    calls = []

    def consume(state, selected, context):
        calls.append((state, selected, context.repo))

    monkeypatch.setattr(capture_hook, "consume", consume)
    status = capture_hook.main(
        [
            "run",
            str(request),
            "--state",
            str(tmp_path),
            "--harness",
            "claude",
            "--repo",
            job.repo,
            "--remote",
            job.remote,
        ]
    )
    assert status == 0
    assert calls == [(tmp_path, request, job.repo)]
