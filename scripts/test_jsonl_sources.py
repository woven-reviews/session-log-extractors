"""JSONL reading preserves object records and diagnoses invalid physical lines."""

import json
from pathlib import Path

import pytest
from codex_capture import TRANSCRIPT as CODEX_TRANSCRIPT

from session_log_extractors.jsonl_sources import load_jsonl_objects as load_entries
from session_log_extractors.jsonl_sources import load_records

CLAUDE_ROOT = Path(__file__).parent / "fixtures" / "claude" / "2.1.312" / "native"
(CLAUDE_TRANSCRIPT,) = CLAUDE_ROOT.glob("*.jsonl")


@pytest.mark.parametrize("transcript", [CLAUDE_TRANSCRIPT, CODEX_TRANSCRIPT])
def test_load_records_preserves_native_objects_around_malformed_lines(
    transcript, tmp_path, capsys
):
    path = tmp_path / "truncated.jsonl"
    native_lines = transcript.read_text().splitlines()
    first_record = json.loads(native_lines[0])
    second_record = json.loads(native_lines[1])
    # Insert malformed/non-object records without changing the native objects.
    path.write_text(
        "\n"
        + native_lines[0]
        + "\nnot json\n[1, 2]\n"
        + native_lines[1]
        + '\n{"partial'
    )

    records = load_records(path)

    assert records == [(2, first_record), (5, second_record)]
    captured = capsys.readouterr()
    assert captured.err == (
        f"warning: {path}: skipping malformed JSON on line 3\n"
        f"warning: {path}: skipping non-object JSON on line 4\n"
        f"warning: {path}: skipping malformed JSON on line 6\n"
    )


def test_load_records_counts_non_objects_and_ignores_blank_lines(tmp_path, capsys):
    path = tmp_path / "records.jsonl"
    path.write_text('{"type": "user"}\n\n42\n[]\n' + "bad\n" * 4)

    records = load_records(path)

    assert records == [(1, {"type": "user"})]
    diagnostic_lines = capsys.readouterr().err.splitlines()
    assert len(diagnostic_lines) == 6
    assert (
        diagnostic_lines[-1]
        == f"warning: {path}: skipped 6 malformed/non-object records total"
    )


def test_load_records_warns_at_most_five_times_plus_total(tmp_path, capsys):
    path = tmp_path / "invalid.jsonl"
    path.write_text("bad\n" * 7)

    with pytest.raises(ValueError, match="no parseable JSON entries"):
        load_records(path)

    diagnostic_lines = capsys.readouterr().err.splitlines()
    assert len(diagnostic_lines) == 6
    assert (
        diagnostic_lines[-1]
        == f"warning: {path}: skipped 7 malformed/non-object records total"
    )


@pytest.mark.parametrize("content", ["", " \n\t\n"])
def test_blank_input_is_valid_and_silent(content, tmp_path, capsys):
    path = tmp_path / "blank.jsonl"
    path.write_text(content)

    records = load_records(path)

    assert records == []
    captured = capsys.readouterr()
    assert captured.err == ""


@pytest.mark.parametrize("content", ["null", "[]", "42", "true", '"text"', "bad"])
def test_nonblank_input_without_objects_is_rejected(content, tmp_path, capsys):
    path = tmp_path / "invalid.jsonl"
    path.write_text(content)

    with pytest.raises(ValueError, match="expected at least one JSON object"):
        load_records(path)

    diagnostic = capsys.readouterr().err
    assert str(path) in diagnostic
    assert "on line 1" in diagnostic


def test_reader_propagates_missing_and_unreadable_files(tmp_path, monkeypatch):
    path = tmp_path / "missing.jsonl"
    with pytest.raises(FileNotFoundError):
        load_records(path)

    def unreadable(*args, **kwargs):
        raise PermissionError("input denied")

    monkeypatch.setattr(Path, "open", unreadable)
    with pytest.raises(PermissionError, match="input denied"):
        load_records(path)


def test_load_entries_counts_non_dict_and_skips_blank_lines(tmp_path, capsys):
    tmp = tmp_path / "t.jsonl"
    tmp.write_text('{"type": "user"}\n\n42\n[]\n' + "bad\n" * 4)
    entries = load_entries(tmp)
    entry_types = []
    for entry in entries:
        entry_types.append(entry["type"])
    assert entry_types == ["user"]
    captured = capsys.readouterr()
    assert "skipped 6 malformed/non-object records total" in captured.err


def test_load_entries_warns_once_for_many_bad_lines(tmp_path, capsys):
    tmp = tmp_path / "t.jsonl"
    tmp.write_text("bad\n" * 7)
    with pytest.raises(ValueError, match="no parseable JSON entries"):
        load_entries(tmp)
    diagnostics = capsys.readouterr().err
    diagnostic_lines = diagnostics.splitlines()
    assert len(diagnostic_lines) == 6
    assert "skipped 7 malformed/non-object records total" in diagnostics


@pytest.mark.parametrize("contents", ["", " \n\t", "{}\n"])
def test_blank_or_unrelated_objects_are_valid(tmp_path, contents):
    path = tmp_path / "events.jsonl"
    path.write_text(contents)
    entries = load_entries(path)
    assert entries == ([{}] if contents.strip() else [])


def test_transcript_preserves_objects_and_line_numbers(tmp_path, capsys):
    path = tmp_path / "events.jsonl"
    path.write_text('{"first": [1, null]}\n\n42\n{"second": true}\nbad\n')
    entries = load_entries(path)
    diagnostics = capsys.readouterr().err
    assert entries == [{"first": [1, None]}, {"second": True}]
    assert f"{path}: skipping non-object JSON on line 3" in diagnostics
    assert f"{path}: skipping malformed JSON on line 5" in diagnostics


def test_load_entries_skips_malformed_lines_with_warning(tmp_path, capsys):
    native_root = Path(__file__).parent / "fixtures" / "claude" / "2.1.312" / "native"
    [transcript] = native_root.glob("*.jsonl")
    # Keep two captured records; add corruption which a completed capture cannot supply.
    recorded_lines = transcript.read_text().splitlines()[:2]
    expected_records = []
    for line in recorded_lines:
        expected_records.append(json.loads(line))
    path = tmp_path / "truncated.jsonl"
    path.write_text("\n".join(recorded_lines) + '\n{"partial')

    actual_records = load_entries(path)

    assert actual_records == expected_records
    captured = capsys.readouterr()
    assert "skipping malformed JSON on line 3" in captured.err
