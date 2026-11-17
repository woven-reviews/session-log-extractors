"""Boundary cases for streaming recorded-ID discovery mechanics."""

import os
from pathlib import Path

import pytest

from session_log_extractors.session_selection import (
    RepositoryBoundary,
    exact_cwd_match,
    newest,
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
    "recorded, expected",
    [
        (None, False),
        ("", False),
        ("/work", True),
        ("/work/app", False),
        ("/", False),
        ("/elsewhere", False),
    ],
)
def test_exact_cwd_match_excludes_ancestors_and_descendants(recorded, expected):
    actual = exact_cwd_match(recorded, Path("/work"))
    assert actual is expected


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
