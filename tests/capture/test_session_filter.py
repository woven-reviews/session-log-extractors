import pytest
from conftest import write_filter

from session_log_extractors.capture import session_filter


def test_a_complete_filter_loads_and_applies(tmp_path):
    flt = session_filter.load(write_filter(tmp_path, "identity"))

    assert flt.apply("filter_claude", "text") == "text"


def test_a_missing_filter_module_is_an_error(tmp_path):
    with pytest.raises(session_filter.FilterError, match="no session_log_filter.py"):
        session_filter.load(tmp_path)


def test_a_filter_missing_a_function_is_an_error(tmp_path):
    write_filter(tmp_path, "incomplete")

    with pytest.raises(session_filter.FilterError, match="filter_codex"):
        session_filter.load(tmp_path)


def test_a_filter_that_cannot_be_imported_is_an_error(tmp_path):
    (tmp_path / "session_log_filter.py").write_text("raise RuntimeError('boom')")

    with pytest.raises(session_filter.FilterError, match="could not be loaded"):
        session_filter.load(tmp_path)


def test_a_filter_that_raises_rejects_the_file(tmp_path):
    flt = session_filter.load(write_filter(tmp_path, "raising"))

    with pytest.raises(session_filter.FilterError, match="rejected the file"):
        flt.apply("filter_claude", "text")


def test_a_filter_that_returns_something_other_than_text_rejects_the_file(tmp_path):
    flt = session_filter.load(write_filter(tmp_path, "not_text"))

    with pytest.raises(session_filter.FilterError, match="did not return text"):
        flt.apply("filter_claude", "text")


def test_a_path_that_is_not_a_loadable_module_is_an_error(tmp_path, monkeypatch):
    write_filter(tmp_path, "identity")
    monkeypatch.setattr(
        session_filter.importlib.util, "spec_from_file_location", lambda *a, **k: None
    )

    with pytest.raises(session_filter.FilterError, match="could not be loaded"):
        session_filter.load(tmp_path)
