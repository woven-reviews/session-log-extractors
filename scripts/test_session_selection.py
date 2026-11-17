"""Boundary cases for streaming recorded-ID discovery mechanics."""

import os
from pathlib import Path

import pytest

from session_log_extractors.session_selection import (
    RepositoryBoundary,
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


@pytest.mark.parametrize("recorded", [None, "", "relative", "/bad\0path"])
def test_repository_boundary_rejects_unusable_evidence(tmp_path, recorded, capsys):
    boundary = RepositoryBoundary((tmp_path.resolve(),))
    matched = boundary.matches(recorded, tmp_path / "session.jsonl")
    assert matched is False
    assert "no usable recorded directory evidence" in capsys.readouterr().err


def test_repository_discovery_diagnoses_missing_git(tmp_path, monkeypatch):
    def unavailable(*args, **kwargs):
        raise FileNotFoundError("git unavailable")

    monkeypatch.setattr("subprocess.run", unavailable)
    with pytest.raises(FileNotFoundError, match="Git repository boundary"):
        RepositoryBoundary.discover(tmp_path)


def test_repository_discovery_rejects_empty_registry(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "session_log_extractors.session_selection._git_output", lambda *args: ""
    )
    with pytest.raises(
        FileNotFoundError, match="no available Git repository checkouts"
    ):
        RepositoryBoundary.discover(tmp_path)
