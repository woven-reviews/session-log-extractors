"""Copilot source readers and supplied-input interpretation."""

import base64
import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import pytest
from copilot_fixture import (
    ASSETS,
    TRANSCRIPT,
    native_records,
    single_image_source,
    write_events_log,
)
from repository_fixture import init_checkout

from session_log_extractors import copilot_sources as sources
from session_log_extractors import extract_copilot_session_log as copilot

PNG = (ASSETS / "red.png").read_bytes()


def test_reader_snapshots_optional_assets_once(tmp_path, monkeypatch):
    image = tmp_path / "pixel.png"
    image.write_bytes((ASSETS / "red.png").read_bytes())
    missing = tmp_path / "missing.png"
    image_references = []
    for path in (image, missing, image):
        image_references.append({"path": str(path)})
    image_references += [{"path": 123}, {"path": " "}, {}]
    original = Path.read_bytes
    reads = []

    def read(path):
        reads.append(path)
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", read)
    actual_images = sources.load_image_inputs(image_references)
    assert actual_images == {
        str(image): original(image),
        str(missing): None,
    }
    assert reads == [image, missing]

    def unreadable(*args, **kwargs):
        raise PermissionError("asset denied")

    monkeypatch.setattr(Path, "read_bytes", unreadable)
    actual_images = sources.load_image_inputs(image_references)
    assert actual_images == {str(image): None, str(missing): None}


def test_copilot_reader_missing_image_returns_none():
    actual_read_image = sources.read_image("/does/not/exist.png")
    assert actual_read_image is None


def test_copilot_preloaded_image_extension_is_per_reference():
    image_references = []
    for mime in ("image/png", "image/jpeg"):
        image_references.append({"path": "/noext", "type": mime})
    images = copilot.envelope_images(image_references, {"/noext": PNG})
    actual_media_types = []
    for image in images:
        actual_media_types.append(image["media_type"])
    assert actual_media_types == ["image/png", "image/jpeg"]


def test_native_reader_and_interpretation_are_source_free(monkeypatch):
    source = copilot.read_source(TRANSCRIPT)
    expected = copilot.build_raw_envelope(source, -1)
    original = deepcopy(source)

    def forbidden(*args, **kwargs):
        pytest.fail("Interpretation accessed a source")

    for method in ("open", "exists", "is_file", "resolve", "expanduser"):
        monkeypatch.setattr(Path, method, forbidden)
    session = copilot.EXTRACTOR.interpret_source(source)
    assert session["events"]
    assert session["cwd"] == source.session.cwd
    assert session["started_at"] == source.session.started_at
    assert copilot.build_raw_envelope(source, -1) == expected
    assert source == original


def _lifecycle_source(tmp_path, records):
    """Load explicitly controlled incomplete/malformed lifecycle evidence."""
    lines = []
    for record in records:
        lines.append(json.dumps(record))
    path = write_events_log(tmp_path, "unrelated-directory", lines)
    return sources.read_source(path)


@pytest.mark.parametrize("start_cwd", [None, "", "/original"])
def test_start_cwd_precedes_resume_cwd(tmp_path, start_cwd):
    records = native_records()
    # Change only the captured lifecycle context/times, preserving conversation.
    for record in records:
        if record["type"] == "session.start":
            record["data"]["sessionId"] = "recorded"
            record["data"]["startTime"] = "2025-01-01"
            record["data"]["context"]["cwd"] = start_cwd
        elif record["type"] == "session.resume":
            record["data"]["context"]["cwd"] = "/resumed"
            record["timestamp"] = "2027-01-01"
    source = _lifecycle_source(tmp_path, records)
    assert source.session.session_id == "recorded"
    assert source.session.cwd == (start_cwd or "/resumed")
    assert source.session.started_at == "2025-01-01"
    assert source.session.last_activity_at == datetime(2027, 1, 1, tzinfo=timezone.utc)


def test_missing_metadata_stays_unknown_and_does_not_use_filesystem_times(tmp_path):
    source = _lifecycle_source(
        tmp_path,
        [
            {"type": "session.start", "data": {"sessionId": "recorded"}},
            {"type": "session.resume", "timestamp": "invalid", "data": None},
            {"type": "user.message", "timestamp": 42, "data": {"content": "hello"}},
        ],
    )
    assert source.session.cwd is None
    assert source.session.started_at is None
    assert source.session.last_activity_at is None
    assert copilot.output_identifier(source.session) == "recorded"
    envelope = copilot.build_raw_envelope(source)
    assert envelope["started_at"] is None
    assert envelope["cwd"] is None


@pytest.mark.parametrize(
    "records, diagnostic",
    [
        ([], "no recorded"),
        (
            [{"type": "session.resume", "data": {"context": {"cwd": "/workspace"}}}],
            "no recorded",
        ),
        ([{"type": "session.start", "data": {"sessionId": 17}}], "no recorded"),
        (
            [
                {
                    "type": "session.start",
                    "agentId": "child",
                    "data": {"sessionId": "child"},
                }
            ],
            "no recorded",
        ),
        (
            [
                {"type": "session.start", "data": {"sessionId": "a"}},
                {"type": "session.start", "data": {"sessionId": "b"}},
            ],
            "conflicting recorded",
        ),
    ],
)
def test_unestablished_or_conflicting_identity_fails(tmp_path, records, diagnostic):
    with pytest.raises(ValueError, match=diagnostic):
        _lifecycle_source(tmp_path, records)


def test_models_preserve_producer_order_without_configured_or_internal_models(tmp_path):
    source = _lifecycle_source(
        tmp_path,
        [
            {
                "type": "session.start",
                "data": {"sessionId": "recorded", "selectedModel": "configured"},
            },
            {"type": "session.model_change", "data": {"newModel": "unused"}},
            {"type": "model.model_call_started", "data": {"model": "internal"}},
            {"type": "assistant.message", "data": {"model": "first"}},
            {
                "type": "assistant.message",
                "agentId": "child",
                "data": {"model": "second"},
            },
            {"type": "assistant.message", "data": {"model": "first"}},
            {"type": "assistant.message", "data": {"model": ""}},
            {"type": "assistant.message", "data": None},
        ],
    )
    assert source.models == ["first", "second"]


@pytest.mark.parametrize(
    "recorded_cwd, strict, matches",
    [
        ("/workspace", True, True),
        ("/workspace/child", True, False),
        ("/workspace/child", False, True),
        ("/", False, True),
        ("/unrelated", False, False),
        (None, False, False),
    ],
)
def test_directory_selection(recorded_cwd, strict, matches):
    session = sources.CopilotSource(
        sources.CopilotSession(Path("unused"), "recorded", recorded_cwd)
    )
    actual = sources.matching_sessions([session], Path("/workspace"), strict)
    assert actual == ([session] if matches else [])


def test_newest_repository_selection_and_deterministic_ties(tmp_path):
    init_checkout(tmp_path)
    first = sources.CopilotSource(sources.CopilotSession(Path("a"), "a", str(tmp_path)))
    second = sources.CopilotSource(
        sources.CopilotSession(Path("b"), "b", str(tmp_path / "child"))
    )
    unrelated = sources.CopilotSource(
        sources.CopilotSession(Path("c"), "c", str(tmp_path.parent))
    )

    newest = sources.newest_session([second, first, unrelated], tmp_path)
    strict = sources.newest_session([second, unrelated], tmp_path, True)
    no_match = sources.newest_session([unrelated], tmp_path)
    empty = sources.newest_session([], tmp_path)

    assert newest == second
    assert strict is None
    assert no_match is None
    assert empty is None


def test_duplicate_recorded_ids_are_ambiguous():
    sessions = [
        sources.CopilotSource(sources.CopilotSession(Path("a"), "same")),
        sources.CopilotSource(sources.CopilotSession(Path("b"), "same")),
    ]
    with pytest.raises(FileNotFoundError, match="ambiguous"):
        sources.get_session_by_id(sessions, "same")


def test_loading_images_keeps_path_bytes_after_file_removal(tmp_path, monkeypatch):
    source = single_image_source()
    image = tmp_path / "red.png"
    image.write_bytes(PNG)
    # Replace one native binary-asset reference with a controlled path-only input.
    for record in source.state_records:
        if record["type"] == "user.message":
            attachment = record["data"]["attachments"][0]
            attachment.pop("assetId", None)
            attachment["path"] = str(image)
    lines = []
    # Retain lifecycle evidence even when narrowing the native image exchange.
    for record in native_records():
        if record["type"] == "session.start":
            lines.append(json.dumps(record))
    for record in source.state_records:
        lines.append(json.dumps(record))
    path = write_events_log(tmp_path / "state", "image", lines)
    loaded = copilot.EXTRACTOR.read_source(copilot.CopilotRequest(path))
    image.unlink()

    def forbidden(*args, **kwargs):
        pytest.fail("Interpretation read an image")

    monkeypatch.setattr(Path, "open", forbidden)
    envelope = copilot.build_raw_envelope(loaded)
    images = []
    for event in envelope["events"]:
        images.extend(event.get("images", []))
    assert len(images) == 1
    assert base64.b64decode(images[0]["data"]) == PNG
