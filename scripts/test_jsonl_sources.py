"""Shared transcript validation, diagnostics, and ordered record retention."""

import json
from pathlib import Path

import pytest

from session_log_extractors.jsonl_sources import load_jsonl_objects as load_entries


def test_load_entries_counts_non_dict_and_skips_blank_lines(tmp_path, capsys):
    tmp = tmp_path / "t.jsonl"
    tmp.write_text('{"type": "user"}\n\n42\n[]\n' + "bad\n" * 4)
    entries = load_entries(tmp)
    entry_types = []
    for entry in entries:
        entry_types.append(entry["type"])
    assert entry_types == ["user"]
    assert "skipped 6 malformed/non-object records total" in capsys.readouterr().err


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
    assert "skipping malformed JSON on line 3" in capsys.readouterr().err
