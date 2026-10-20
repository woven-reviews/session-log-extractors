from session_log_extractors.capture import records


def test_entries_yields_only_json_objects():
    text = '{"a": 1}\nnot json\n[1, 2]\n\n{"b": 2}\n'

    assert list(records.entries(text)) == [{"a": 1}, {"b": 2}]


def test_copilot_columns_name_the_tables_the_extractor_reads():
    assert "turns" in records.COPILOT_DB_COLUMNS
    assert "session_id" in records.COPILOT_DB_COLUMNS["turns"]
