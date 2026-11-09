"""Container sources are exact recorded files, never directory-wide mappings."""

import base64
import json
import shutil
import sqlite3
from dataclasses import replace

import pytest
from codex_fixture import ASSETS as CODEX_ASSETS
from codex_fixture import TRANSCRIPT as CODEX_TRANSCRIPT
from copilot_fixture import DATABASE, SESSION_ID, STATE_ROOT

from session_log_extractors import capture_extraction, capture_jobs


def test_only_explicit_files_can_be_resolved(capture, tmp_path):
    _module, job, _repo, _remote = capture
    recorded = r"C:\Users\Candidate\assessment\assets\red.png"
    destination = tmp_path / "red.png"
    mount = capture_jobs.SourceMount(recorded, str(destination))
    scoped = replace(job, repo=r"C:\Users\Candidate\assessment", mounts=(mount,))
    assert scoped.resolve(recorded) == destination
    assert scoped.resolve(recorded.lower()) == destination
    for unmounted in (
        r"C:\Users\Candidate",
        r"C:\Users\Candidate\assessment\assets\blue.png",
    ):
        with pytest.raises(ValueError, match="mounted"):
            scoped.resolve(unmounted)
    assert scoped.owns(r"C:\Users\Candidate\assessment\nested")
    assert not scoped.owns(r"C:\Users\Candidate\assessment-other")
    assert not scoped.owns(r"C:\Users\Candidate\assessment\..\different")
    assert not scoped.owns(None)


def test_mount_records_roundtrip_without_home_fields(capture, tmp_path):
    _module, job, _repo, _remote = capture
    mount = capture_jobs.SourceMount(job.transcript, "/sources/native.jsonl")
    scoped = replace(job, mounts=(mount,))
    path = tmp_path / "request.json"
    path.write_text(scoped.serialize())
    assert capture_jobs.load_job(path) == scoped
    serialized = json.loads(path.read_text())
    assert "home" not in serialized
    assert "mounted_home" not in serialized


@pytest.mark.parametrize(
    "mounts", ["bad", [1], [{"recorded": 1, "mounted": "/source"}]]
)
def test_malformed_mounts_are_rejected(capture, tmp_path, mounts):
    _module, job, _repo, _remote = capture
    value = json.loads(job.serialize())
    value["mounts"] = mounts
    path = tmp_path / "request.json"
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="mount"):
        capture_jobs.load_job(path)


def _windows_image_transcript(path):
    """Keep the native image owner while replacing one inline asset with a path."""
    recorded_asset = r"C:\Users\Candidate\assessment\red.png"
    recorded_transcript = r"C:\Users\Candidate\.codex\session.jsonl"
    records = []
    session_id = None
    replaced = False
    for line in CODEX_TRANSCRIPT.read_text().splitlines():
        record = json.loads(line)
        payload = record.get("payload", {})
        if record.get("type") == "session_meta":
            session_id = payload["id"]
        if "cwd" in payload:
            payload["cwd"] = r"C:\Users\Candidate\assessment"
        if payload.get("role") == "user":
            for block in payload.get("content", []):
                if block.get("type") == "input_image" and not replaced:
                    # Replace one recorded inline image with the same prepared
                    # asset at a Windows file path; retain its native owner.
                    block["image_url"] = recorded_asset
                    replaced = True
        records.append(json.dumps(record))
    assert replaced and session_id
    path.write_text("\n".join(records))
    return session_id, recorded_asset, recorded_transcript


@pytest.mark.parametrize("available", [True, False])
def test_windows_native_image_uses_only_its_explicit_file(capture, tmp_path, available):
    _module, job, _repo, _remote = capture
    transcript = tmp_path / "session.jsonl"
    session_id, recorded_asset, recorded_transcript = _windows_image_transcript(
        transcript
    )
    mounts = [capture_jobs.SourceMount(recorded_transcript, str(transcript))]
    if available:
        mounts.append(
            capture_jobs.SourceMount(recorded_asset, str(CODEX_ASSETS / "red.png"))
        )
    scoped = replace(
        job,
        harness="codex",
        session_id=session_id,
        repo=r"C:\Users\Candidate\assessment",
        transcript=recorded_transcript,
        mounts=tuple(mounts),
    )
    assert capture_extraction.referenced_assets(scoped) == [recorded_asset]
    envelope = capture_extraction.extract(scoped)
    images = []
    for event in envelope["events"]:
        images.extend(event.get("images", []))
    if available:
        expected = base64.b64encode((CODEX_ASSETS / "red.png").read_bytes()).decode()
        actual = []
        for image in images:
            if "data" in image:
                actual.append(image["data"])
        assert expected in actual
    else:
        missing = []
        for image in images:
            if image.get("unavailable"):
                missing.append(image["ref"])
        assert recorded_asset in missing


def test_source_inspection_cli_reads_only_the_selected_request(
    capture, tmp_path, capsys
):
    _module, job, _repo, _remote = capture
    request = tmp_path / "request.json"
    request.write_text(job.serialize())
    status = capture_extraction.main([str(request)])
    assert status == 0
    assert json.loads(capsys.readouterr().out) == []


def test_copilot_asset_discovery_uses_native_attachment_records(capture, tmp_path):
    _module, job, _repo, _remote = capture
    database = tmp_path / "session-store.db"
    shutil.copyfile(DATABASE, database)
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE sessions SET cwd = ?", (job.repo,))
    selected = replace(
        job,
        harness="copilot",
        session_id=SESSION_ID,
        transcript=str(database),
        state_log=str(STATE_ROOT / SESSION_ID / "events.jsonl"),
    )
    paths = capture_extraction.referenced_assets(selected)
    assert isinstance(paths, list)
    assert paths, "Native capture must contain image attachment path evidence"


def test_inspection_rejects_unowned_session_before_disclosing_assets(capture):
    _module, job, _repo, _remote = capture
    with pytest.raises(ValueError, match="assessment"):
        capture_extraction.referenced_assets(replace(job, repo="/unrelated"))


def test_recorded_image_path_cannot_bypass_the_admitted_mapping(capture, tmp_path):
    _module, job, _repo, _remote = capture
    transcript = tmp_path / "session.jsonl"
    session_id, old_asset, recorded_transcript = _windows_image_transcript(transcript)
    recorded_asset = str(CODEX_ASSETS / "red.png")
    # Give a recorded image an existing path in the reader's own namespace,
    # then explicitly map that path to a different prepared asset.
    text = transcript.read_text().replace(
        json.dumps(old_asset), json.dumps(recorded_asset)
    )
    transcript.write_text(text)
    mounts = (
        capture_jobs.SourceMount(recorded_transcript, str(transcript)),
        capture_jobs.SourceMount(recorded_asset, str(CODEX_ASSETS / "blue.png")),
    )
    scoped = replace(
        job,
        harness="codex",
        session_id=session_id,
        repo=r"C:\Users\Candidate\assessment",
        transcript=recorded_transcript,
        mounts=mounts,
    )
    envelope = capture_extraction.extract(scoped)
    images = []
    for event in envelope["events"]:
        images.extend(event.get("images", []))
    assert images, "Native capture must retain the selected image owner"
    expected = base64.b64encode((CODEX_ASSETS / "blue.png").read_bytes()).decode()
    assert images[0]["data"] == expected
