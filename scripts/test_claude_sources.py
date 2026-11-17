"""Claude session-directory facts and repository-store discovery."""

from claude_fixture import TRANSCRIPT

from session_log_extractors.claude_sources import (
    load_entries,
    repository_transcripts,
    session_cwd,
)
from session_log_extractors.session_selection import RepositoryBoundary


def test_session_cwd_comes_from_the_native_capture():
    entries = load_entries(TRANSCRIPT)
    actual = session_cwd(entries)
    assert actual == "/tmp/CAPTURE_ROOT/workspace"


def test_session_cwd_uses_first_nonempty_string():
    entries = load_entries(TRANSCRIPT)
    # Vary only the first records' directory fields to exercise missing,
    # malformed and changed directory evidence without inferring from tools.
    for entry in entries:
        entry.pop("cwd", None)
    entries[0]["cwd"] = None
    entries[1]["cwd"] = ""
    entries[2]["cwd"] = 12
    missing = session_cwd(entries)
    entries[3]["cwd"] = "/original"
    entries[4]["cwd"] = "/later"
    recorded = session_cwd(entries)
    assert missing is None
    assert recorded == "/original"


def test_empty_session_has_no_directory():
    actual = session_cwd([])
    assert actual is None


def test_repository_store_skips_invalid_inputs_and_nonfiles(tmp_path, capsys):
    store = tmp_path / "store"
    store.mkdir()
    # Invalid JSON, an unreadable file and a directory named like a transcript
    # are controlled discovery failures that a native capture cannot supply.
    invalid = store / "invalid.jsonl"
    invalid.write_text("not JSON\n")
    unreadable = store / "unreadable.jsonl"
    unreadable.write_text("{}\n")
    unreadable.chmod(0)
    (store / "directory.jsonl").mkdir()
    boundary = RepositoryBoundary((tmp_path.resolve(),))

    selected = repository_transcripts(tmp_path, boundary, project_dir=store)

    stderr = capsys.readouterr().err
    assert selected == []
    assert f"skipping {invalid}: no usable recorded directory evidence" in stderr
    assert f"skipping Claude source {unreadable}" in stderr


def test_unrelated_transcript_tail_is_not_parsed_during_discovery(tmp_path, capsys):
    store = tmp_path / "store"
    store.mkdir()
    unrelated = store / "unrelated.jsonl"
    # A session still being written can end in a partial record. Discovery
    # needs only the first recorded directory, so the tail stays unread.
    unrelated.write_text(TRANSCRIPT.read_text() + '{"partial": \n')
    repository = tmp_path / "repository"
    repository.mkdir()
    boundary = RepositoryBoundary((repository.resolve(),))

    selected = repository_transcripts(tmp_path, boundary, project_dir=store)

    stderr = capsys.readouterr().err
    assert selected == []
    assert stderr == ""
