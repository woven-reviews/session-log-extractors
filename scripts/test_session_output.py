"""Batch filenames retain their existing collision suffix sequence."""

from session_log_extractors.session_output import unique_identifier


def test_unique_identifier_suffixes_duplicates():
    used: set[str] = set()

    first = unique_identifier("sess", used)
    second = unique_identifier("sess", used)
    third = unique_identifier("sess", used)

    assert first == "sess"
    assert second == "sess-2"
    assert third == "sess-3"
    assert used == {"sess", "sess-2", "sess-3"}
