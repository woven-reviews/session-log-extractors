"""Codex empty, invalid and partial input outcomes through production CLI paths."""

import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest
from codex_fixture import TRANSCRIPT

from session_log_extractors import codex_protocol, codex_sources
from session_log_extractors import extract_codex_session_log as codex
from session_log_extractors.session_log_envelope import export_session


def _bookkeeping_records():
    records = []
    for line in TRANSCRIPT.read_text().splitlines():
        record = json.loads(line)
        if record.get("type") in (
            codex_protocol.SESSION_META,
            codex_protocol.TURN_CONTEXT,
        ):
            records.append(record)
    assert records, "Native capture must contain bookkeeping metadata"
    return records


def _write_records(path, records):
    lines = []
    for record in records:
        lines.append(json.dumps(record))
    path.write_text("\n".join(lines) + "\n")


def _empty_transcript(path, cwd, session_id):
    records = _bookkeeping_records()
    # Change only discovery/identity metadata to isolate each selected transcript.
    records[0]["payload"].update(cwd=str(cwd), id=session_id)
    _write_records(path, records)


@pytest.mark.parametrize("content", ["", " \n\t\n", "{}\n", '{"unrelated": true}\n'])
@pytest.mark.parametrize("destination", ["stdout", "explicit", "default"])
def test_empty_input_exports_normal_envelope(content, destination, tmp_path, capsys):
    transcript = tmp_path / "empty.jsonl"
    transcript.write_text(content)
    output = tmp_path / "explicit.json"
    args = ["--transcript", str(transcript)]
    if destination == "stdout":
        args += ["--output", "-"]
    elif destination == "explicit":
        args += ["--output", str(output)]
    else:
        output = tmp_path / "codex_session_log_raw_empty.json"

    status = codex.main(args, project_root=tmp_path)

    captured = capsys.readouterr()
    envelope = json.loads(
        captured.out if destination == "stdout" else output.read_text()
    )
    assert status == 0
    assert envelope["events"] == []
    assert envelope["harness"] == "codex"
    assert envelope["session_id"] == "empty"
    assert envelope["cwd"] is None
    assert envelope["started_at"] is None
    assert envelope["models"] == []
    assert "warning:" not in captured.err
    assert "error:" not in captured.err
    if destination != "stdout":
        assert captured.out == ""


def test_bookkeeping_interpretation_is_source_free_and_preserves_metadata(monkeypatch):
    source = codex.read_source(TRANSCRIPT)
    source.entries = _bookkeeping_records()
    expected_timestamp = source.entries[0]["timestamp"]
    expected_models = []
    for entry in source.entries:
        model = entry.get("payload", {}).get("model")
        if isinstance(model, str) and model and model not in expected_models:
            expected_models.append(model)

    def forbidden(*args, **kwargs):
        pytest.fail("Empty interpretation accessed the filesystem")

    monkeypatch.setattr(Path, "open", forbidden)
    session = codex.EXTRACTOR.interpret_source(source)
    original = deepcopy(session)
    envelope = export_session(session)
    compatibility = codex.build_raw_envelope(source)

    assert session["events"] == []
    assert envelope == compatibility
    assert envelope["session_id"] == source.session_id
    assert envelope["cwd"] == source.cwd
    assert envelope["started_at"] == expected_timestamp
    assert envelope["models"] == expected_models
    assert expected_models
    assert session == original


@pytest.mark.parametrize(
    "content", ["bad\n", 'null\n[]\n42\ntrue\n"text"\n', "bad\n[]\n"]
)
@pytest.mark.parametrize("stdout", [False, True])
def test_wholly_invalid_input_fails_without_export(content, stdout, tmp_path, capsys):
    transcript = tmp_path / "invalid.jsonl"
    transcript.write_text(content)
    output = tmp_path / "out.json"

    status = codex.main(
        ["--transcript", str(transcript), "--output", "-" if stdout else str(output)]
    )

    captured = capsys.readouterr()
    assert status == 1
    assert captured.out == ""
    assert not output.exists()
    assert str(transcript) in captured.err
    assert "expected at least one JSON object" in captured.err


@pytest.mark.parametrize("populated", [False, True])
def test_partial_input_preserves_content_and_diagnoses_skips(
    populated, tmp_path, capsys
):
    transcript = tmp_path / "partial.jsonl"
    if populated:
        text = TRANSCRIPT.read_text()
    else:
        _write_records(transcript, _bookkeeping_records())
        text = transcript.read_text()
    transcript.write_text(text)
    expected = codex.extract_transcript(transcript)
    # Preserve native records and ordering; insert only boundary records.
    transcript.write_text("bad\n\nnull\n" + text + "[]\n")

    status = codex.main(["--transcript", str(transcript), "--output", "-"])

    captured = capsys.readouterr()
    assert status == 0
    assert json.loads(captured.out) == expected
    assert f"{transcript}: skipping malformed JSON on line 1" in captured.err
    assert f"{transcript}: skipping non-object JSON on line 3" in captured.err
    assert "skipping non-object JSON" in captured.err
    assert bool(expected["events"]) is populated


@pytest.mark.parametrize("failure", [None, "read", "write", "invalid", "malformed"])
def test_batch_attempts_every_session_and_keeps_success(
    failure, tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    for name in ["first", "second", "third"]:
        _empty_transcript(tmp_path / f"{name}.jsonl", tmp_path, name)
    out = tmp_path / "out"
    if failure == "write":
        (out / "codex_session_log_raw_second.json").mkdir(parents=True)
    second = tmp_path / "second.jsonl"
    if failure == "invalid":
        second.write_text("bad\n[]\n")
    elif failure == "malformed":
        with second.open("a") as stream:
            stream.write("bad\nnull\n")
    original = Path.open

    def open_source(path, *args, **kwargs):
        if failure == "read" and path.name == "second.jsonl":
            raise PermissionError(f"cannot read {path}")
        return original(path, *args, **kwargs)

    # Selection is unchanged; simulate a read failure after discovery selects it.
    selected = [
        tmp_path / "first.jsonl",
        tmp_path / "second.jsonl",
        tmp_path / "third.jsonl",
    ]
    if failure in ("read", "invalid"):
        monkeypatch.setattr(
            codex_sources, "matching_transcripts", lambda *args, **kwargs: selected
        )
    monkeypatch.setattr(Path, "open", open_source)

    status = codex.main(["--sessions-root", str(tmp_path), "--output", str(out)])

    captured = capsys.readouterr()
    has_failure = failure in ("read", "write", "invalid")
    assert status == (1 if has_failure else 0)
    assert captured.out == ""
    expected_count = 2 if has_failure else 3
    assert f"{expected_count}/3 session(s) written" in captured.err
    for name in ["first", "second", "third"]:
        if has_failure and name == "second":
            continue
        envelope = json.loads((out / f"codex_session_log_raw_{name}.json").read_text())
        assert envelope["events"] == []
        assert envelope["session_id"] == name


def test_batch_output_directory_failure_returns_error(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _empty_transcript(tmp_path / "session.jsonl", tmp_path, "session")
    output = tmp_path / "blocked"
    output.write_text("existing file")

    status = codex.main(["--sessions-root", str(tmp_path), "--output", str(output)])

    captured = capsys.readouterr()
    assert status == 1
    assert captured.out == ""
    assert "could not create output directory" in captured.err
    assert str(output) in captured.err


def test_unreadable_single_session_reports_error(tmp_path, monkeypatch, capsys):
    transcript = tmp_path / "session.jsonl"
    transcript.write_bytes(TRANSCRIPT.read_bytes())

    def unreadable(*args, **kwargs):
        raise PermissionError(f"cannot read {transcript}")

    monkeypatch.setattr(Path, "open", unreadable)
    status = codex.main(["--transcript", str(transcript), "--output", "-"])

    captured = capsys.readouterr()
    assert status == 1
    assert captured.out == ""
    assert f"error: cannot read {transcript}" in captured.err


@pytest.mark.parametrize(("content", "expected_status"), [(" \n", 0), ("[]\n", 1)])
@pytest.mark.parametrize("selection", ["explicit", "positional", "newest"])
def test_script_exit_status_and_stdout_are_production_outcomes(
    content, expected_status, selection, tmp_path
):
    transcript = tmp_path / "input.jsonl"
    transcript.write_text(content)
    script = Path(__file__).parent / "extract_codex_session_log.py"

    args = [sys.executable, str(script), "--output", "-"]
    if selection == "explicit":
        args += ["--transcript", str(transcript)]
    elif selection == "positional":
        args += [str(transcript)]
    else:
        args += ["--sessions-root", str(tmp_path)]
    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == expected_status
    if expected_status:
        assert result.stdout == ""
        assert "error:" in result.stderr
    else:
        assert json.loads(result.stdout)["events"] == []
        assert "error:" not in result.stderr
