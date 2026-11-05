"""Batch filenames retain their existing collision suffix sequence."""

from session_log_extractors.session_output import unique_identifier


def test_unique_identifier_suffixes_duplicates():
    used: set[str] = set()
    identifier = unique_identifier("sess", used)
    assert identifier == "sess"
    identifier = unique_identifier("sess", used)
    assert identifier == "sess-2"
    identifier = unique_identifier("sess", used)
    assert identifier == "sess-3"


def test_copilot_unique_identifier_increments_on_collision():
    used = set()
    actual_identifier = unique_identifier("day_id", used)
    assert actual_identifier == "day_id"
    actual_identifier = unique_identifier("day_id", used)
    assert actual_identifier == "day_id-2"
    actual_identifier = unique_identifier("day_id", used)
    assert actual_identifier == "day_id-3"

    assert used == {"day_id", "day_id-2", "day_id-3"}
