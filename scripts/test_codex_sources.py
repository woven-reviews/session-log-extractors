"""Codex source boundaries, using captured native records."""

import base64
import json
from pathlib import Path

import pytest

from session_log_extractors import codex_sources as sources
from session_log_extractors import extract_codex_session_log as codex

FIXTURES = Path(__file__).parent / "fixtures" / "codex"


@pytest.mark.parametrize("name", ["image", "model", "question", "tool_patch_large"])
def test_loaded_source_interprets_without_filesystem(name, monkeypatch):
    path = FIXTURES / f"{name}.jsonl"
    expected = codex.extract_transcript(path)
    source = codex.read_source(path)

    def forbidden(*args, **kwargs):
        pytest.fail("interpretation accessed the filesystem")

    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(Path, "resolve", forbidden)
    monkeypatch.setattr(Path, "expanduser", forbidden)
    actual = codex.build_raw_envelope(source)
    assert actual == expected
    assert actual["events"]


def test_supplied_path_images_are_portable(monkeypatch):
    entries = sources.load_entries(FIXTURES / "image.jsonl")
    # Replace the captured inline image with supplied local/missing assets.
    payload = next(
        entry["payload"]
        for entry in entries
        if entry.get("type") == "response_item"
        and entry["payload"].get("role") == "user"
        and any(
            block.get("type") == "input_image"
            for block in entry["payload"].get("content", [])
        )
    )
    payload["content"] = [
        block for block in payload["content"] if block.get("type") != "input_image"
    ] + [
        {"type": "input_image", "image_url": "/supplied.png"},
        {"type": "input_image", "image_url": "/missing.png"},
    ]
    raw = (FIXTURES / "pixel.png").read_bytes()

    def forbidden(*args, **kwargs):
        pytest.fail("interpretation tried to read an asset")

    monkeypatch.setattr(Path, "open", forbidden)
    session = codex.interpret_source(
        sources.CodexSource(
            Path("supplied.jsonl"),
            entries,
            image_inputs={"/supplied.png": (raw, "png")},
        )
    )
    assert session is not None
    events = session["events"]
    images = next(event["images"] for event in events if event.get("images"))
    assert base64.b64decode(images[0]["data"]) == raw
    assert images[0]["media_type"] == "image/png"
    assert images[1] == {"unavailable": True, "ref": "/missing.png"}


@pytest.mark.parametrize("line", [50, 51, 80, 81])
def test_metadata_uses_physical_line_limits(line, tmp_path):
    path = tmp_path / "rollout.jsonl"
    record = (FIXTURES / "model.jsonl").read_text().splitlines()[0]
    path.write_text("\n" * (line - 1) + record + "\n")
    source = codex.read_source(path)
    assert source.session_id == (sources.session_id_from_file(path))
    assert source.cwd == sources.session_cwd_from_file(path)
    assert (source.session_id is not None) == (line <= 50)
    assert (source.cwd is not None) == (line <= 80)


def test_source_reads_transcript_only_once(tmp_path, monkeypatch):
    path = tmp_path / "capture.jsonl"
    path.write_bytes((FIXTURES / "question.jsonl").read_bytes())
    original = Path.open
    reads = []

    def tracked(self, *args, **kwargs):
        if self == path:
            reads.append(self)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", tracked)
    assert codex.extract_transcript(path)
    assert reads == [path]


def test_reader_snapshots_optional_assets(tmp_path, monkeypatch):
    image = tmp_path / "asset"
    raw = (FIXTURES / "pixel.png").read_bytes()
    image.write_bytes(raw)
    missing = tmp_path / "missing.png"
    refs = [
        {"type": "path", "path": str(image)},
        {"type": "path", "path": str(missing)},
        {"type": "path", "path": str(image)},
        {"type": "path", "path": 123},
        {"type": "base64", "data": "AAAA"},
    ]
    assert sources.load_image_inputs(refs) == {
        str(image): (raw, "img"),
        str(missing): None,
    }

    def unreadable(*args, **kwargs):
        raise PermissionError("asset denied")

    monkeypatch.setattr(Path, "read_bytes", unreadable)
    assert sources.load_image_inputs(refs) == {str(image): None, str(missing): None}


def test_reader_uses_first_id_and_first_valid_cwd(tmp_path):
    path = tmp_path / "capture.jsonl"
    # An invalid ID in the first session_meta prevents later IDs from winning;
    # cwd instead falls through to the first nonempty string, including context.
    entries = [
        {"type": "session_meta", "payload": {"id": 123, "cwd": ""}},
        {"type": "turn_context", "payload": {"cwd": 123}},
        {"type": "turn_context", "payload": {"cwd": "/first"}},
        {"type": "session_meta", "payload": {"id": "later", "cwd": "/later"}},
    ]
    path.write_text("\n".join(json.dumps(entry) for entry in entries))
    source = sources.read_source(path)
    assert source.session_id is None
    assert source.identifier == "capture"
    assert source.cwd == "/first"
    assert source.entries == entries


def test_reader_preserves_diagnostics_and_source_order(tmp_path, capsys):
    path = tmp_path / "broken.jsonl"
    lines = (FIXTURES / "model.jsonl").read_text().splitlines()
    # Insert malformed/non-record lines around native records, without reordering.
    path.write_text("\n".join(["bad", "", "null", lines[0], "bad"] + lines[1:]))
    source = sources.read_source(path)
    assert source.entries == sources.load_entries(FIXTURES / "model.jsonl")
    assert capsys.readouterr().err == (
        "warning: skipping malformed JSON on line 1\n"
        "warning: skipping malformed JSON on line 5\n"
    )


def test_reader_propagates_transcript_failures(tmp_path, monkeypatch):
    path = tmp_path / "missing.jsonl"
    with pytest.raises(FileNotFoundError):
        sources.read_source(path)

    def unreadable(*args, **kwargs):
        raise PermissionError("transcript denied")

    monkeypatch.setattr(Path, "open", unreadable)
    with pytest.raises(PermissionError, match="transcript denied"):
        sources.read_source(path)


@pytest.mark.parametrize("record_type", ["event_msg", "response_item"])
def test_reader_loads_native_path_images(record_type, tmp_path, monkeypatch):
    entries = sources.load_entries(FIXTURES / "image.jsonl")
    image = tmp_path / "snapshot.png"
    image.write_bytes((FIXTURES / "pixel.png").read_bytes())
    missing = tmp_path / "missing.png"
    user = next(
        entry["payload"]
        for entry in entries
        if entry.get("type") == "response_item"
        and entry["payload"].get("role") == "user"
    )
    # The capture has inline content only; replace its image block with paths.
    if record_type == "response_item":
        user["content"] = [
            block for block in user["content"] if block.get("type") != "input_image"
        ] + [
            {"type": "input_image", "image_url": str(path)} for path in (image, missing)
        ]
    else:
        entries.append(
            {
                "type": "event_msg",
                "payload": {
                    "type": "user_message",
                    "message": "path image boundary",
                    "local_images": [str(image), str(missing)],
                },
            }
        )
    transcript = tmp_path / "capture.jsonl"
    transcript.write_text("\n".join(json.dumps(entry) for entry in entries))
    source = codex.read_source(transcript)
    assert source.image_inputs == {
        str(image): (image.read_bytes(), "png"),
        str(missing): None,
    }
    expected = codex.build_raw_envelope(source)

    def forbidden(*args, **kwargs):
        pytest.fail("interpretation tried to reopen a loaded asset")

    monkeypatch.setattr(Path, "open", forbidden)
    assert codex.build_raw_envelope(source) == expected


@pytest.mark.parametrize("batch", [False, True])
def test_cli_reuses_loaded_metadata_for_output_names(batch, tmp_path, monkeypatch):
    path = tmp_path / "capture.jsonl"
    path.write_bytes((FIXTURES / "question.jsonl").read_bytes())
    original = Path.open
    reads = []

    def tracked(self, *args, **kwargs):
        if self == path:
            reads.append(self)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", tracked)
    if batch:
        monkeypatch.setattr(sources, "matching_transcripts", lambda *a, **k: [path])
    args = (
        ["--all", "--sessions-root", str(tmp_path)]
        if batch
        else ["--transcript", str(path)]
    )
    assert codex.main(args, project_root=tmp_path) == 0
    assert reads == [path]
    assert len(list(tmp_path.glob("codex_session_log_raw_*.json"))) == 1
