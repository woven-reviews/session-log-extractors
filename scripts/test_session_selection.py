"""Boundary cases for streaming recorded-ID discovery mechanics."""

import os
import tempfile
from pathlib import Path

import pytest

from session_log_extractors.session_selection import (
    cwd_matches,
    newest,
    paths_overlap,
    read_recorded_id,
)


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


@pytest.mark.parametrize(
    "recorded, strict, expected",
    [
        (None, False, False),
        ("", False, False),
        ("/work", True, True),
        ("/work/app", True, False),
        ("/work/app", False, True),
        ("/work", False, True),
        ("/elsewhere", False, False),
    ],
)
def test_cwd_matching_preserves_resolved_overlap(recorded, strict, expected):
    actual = cwd_matches(recorded, Path("/work"), strict=strict)
    assert actual is expected


def test_paths_overlap_in_both_directions(tmp_path):
    child = tmp_path / "a" / "b"
    child.mkdir(parents=True)
    other = tmp_path / "c"
    other.mkdir()
    ancestor_match = paths_overlap(tmp_path, child)
    descendant_match = paths_overlap(child, tmp_path)
    unrelated_match = paths_overlap(child, other)
    assert ancestor_match is True
    assert descendant_match is True
    assert unrelated_match is False


def test_newest_selects_mtime_and_handles_empty_input(tmp_path):
    old = tmp_path / "old.jsonl"
    new = tmp_path / "new.jsonl"
    old.write_text("{}")
    new.write_text("{}")
    os.utime(old, (1, 1))
    os.utime(new, (1000, 1000))

    empty = newest([])
    selected = newest(iter([old, new]))

    assert empty is None
    assert selected == new


def test_newest_returns_none_for_empty_and_picks_max_mtime():
    actual = newest([])
    assert actual is None
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        old = tmp / "old.jsonl"
        new = tmp / "new.jsonl"
        old.write_text("{}")
        new.write_text("{}")
        os.utime(old, (1, 1))
        os.utime(new, (1000, 1000))
        actual = newest([old, new])
        assert actual == new


def test_paths_overlap_reverse_direction_and_no_overlap():
    actual = paths_overlap(Path("/work"), Path("/work/app"))
    assert actual is True
    actual = paths_overlap(Path("/work/one"), Path("/work/two"))
    assert actual is False


def test_cwd_matches_strict_and_non_strict():
    actual = cwd_matches(None, Path("/work"))
    assert actual is False
    actual = cwd_matches("/work", Path("/work"), strict=True)
    assert actual is True
    actual = cwd_matches("/work/app", Path("/work"), strict=True)
    assert actual is False
    actual = cwd_matches("/work/app", Path("/work"))
    assert actual is True


def test_copilot_paths_overlap_both_directions_and_neither(tmp_path):
    child = tmp_path / "a" / "b"
    child.mkdir(parents=True)
    other = tmp_path / "c"
    other.mkdir()
    actual = paths_overlap(tmp_path, child)
    assert actual is True
    actual = paths_overlap(child, tmp_path)
    assert actual is True
    actual = paths_overlap(child, other)
    assert actual is False


def test_copilot_cwd_matches_blank_and_strict(tmp_path):
    actual = cwd_matches(None, Path.cwd())
    assert actual is False
    actual = cwd_matches("", Path.cwd())
    assert actual is False
    actual = cwd_matches(str(Path.cwd()), Path.cwd(), strict=True)
    assert actual is True
    actual = cwd_matches(str(tmp_path / "sub"), tmp_path, strict=True)
    assert actual is False


def test_newest_empty_and_picks_latest_mtime():
    actual = newest([])
    assert actual is None
    tmp = Path(tempfile.mkdtemp())
    a, b = tmp / "a.jsonl", tmp / "b.jsonl"
    a.write_text("{}")
    b.write_text("{}")
    os.utime(a, (1, 1))
    os.utime(b, (1000, 1000))
    actual = newest([a, b])
    assert actual == b
