"""Boundary cases for streaming recorded-ID discovery mechanics."""

from pathlib import Path

import pytest

from session_log_extractors.session_selection import read_recorded_id


def _identity(record):
    value = record.get("id")
    return value if isinstance(value, str) else None


def test_scan_ignores_malformed_and_unidentified_records(tmp_path):
    path = tmp_path / "mixed.jsonl"
    # Controlled malformed records cannot meaningfully come from native captures.
    path.write_text(
        'bad\nnull\n{}\n{"id": 1}\n{"id": ""}\n{"id": "abc"}\n{"id": "abc"}\n'
    )
    actual = read_recorded_id(path, _identity)
    assert actual == "abc"


def test_scan_missing_or_unreadable_file_is_unidentified(tmp_path, monkeypatch):
    path = tmp_path / "missing.jsonl"
    actual = read_recorded_id(path, _identity)
    assert actual is None

    def denied(*args, **kwargs):
        raise PermissionError("unreadable transcript")

    monkeypatch.setattr(Path, "open", denied)
    actual = read_recorded_id(path, _identity)
    assert actual is None


def test_scan_rejects_conflicting_identity_evidence(tmp_path):
    path = tmp_path / "conflicting.jsonl"
    path.write_text('{"id": "abc"}\n{"id": "different"}\n')
    with pytest.raises(ValueError, match="conflicting recorded session IDs"):
        read_recorded_id(path, _identity)
