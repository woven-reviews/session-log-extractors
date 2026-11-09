"""Explicit selection contract exercised with controlled native capture copies."""

import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from claude_fixture import TRANSCRIPT as CLAUDE_TRANSCRIPT
from codex_fixture import CHILD_TRANSCRIPT as CODEX_CHILD
from codex_fixture import TRANSCRIPT as CODEX_TRANSCRIPT
from copilot_fixture import DATABASE

from session_log_extractors import codex_sources, copilot_sources
from session_log_extractors import extract_claude_session_log as claude

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(params=["claude", "codex", "copilot"])
def harness(request):
    return request.param


def _transcript_copy(harness, path, session_id):
    """Change only recorded identity in the complete native transcript."""
    native = CLAUDE_TRANSCRIPT if harness == "claude" else CODEX_TRANSCRIPT
    records = []
    for line in native.read_text().splitlines():
        entry = json.loads(line)
        if harness == "claude" and "sessionId" in entry:
            entry["sessionId"] = session_id
        elif harness == "codex" and entry.get("type") == "session_meta":
            entry["payload"]["id"] = session_id
        records.append(json.dumps(entry))
    path.write_text("\n".join(records) + "\n")


def _store(tmp_path, harness, session_ids):
    """Copy native inputs, varying only IDs and discovery filenames/order."""
    if harness != "copilot":
        for index, session_id in enumerate(session_ids):
            _transcript_copy(harness, tmp_path / f"unrelated-{index}.jsonl", session_id)
        return tmp_path
    database = tmp_path / "session-store.db"
    shutil.copyfile(DATABASE, database)
    with sqlite3.connect(database) as conn:
        conn.row_factory = sqlite3.Row
        row = dict(conn.execute("SELECT * FROM sessions").fetchone())
        conn.execute("DELETE FROM sessions")
        columns = ", ".join(row)
        placeholders = ", ".join(["?"] * len(row))
        for session_id in session_ids:
            row["id"] = session_id
            conn.execute(
                f"INSERT INTO sessions ({columns}) VALUES ({placeholders})",
                tuple(row.values()),
            )
        # Retain native conversation turns for the first controlled session.
        conn.execute("UPDATE turns SET session_id = ?", (session_ids[0],))
    return database


def _select(harness, store, selector):
    if harness == "claude":
        return claude.select_transcript(selector, None, str(store))
    if harness == "codex":
        return codex_sources.select_transcript(selector, None, str(store))
    with copilot_sources.get_db_connection(store) as conn:
        session = copilot_sources.get_session_by_id(conn, selector)
    return session["id"] if session else None


def _cli(harness, store, selector, output):
    option = {"claude": "--project-dir", "codex": "--sessions-root", "copilot": "--db"}
    script = Path(__file__).parent / f"extract_{harness}_session_log.py"
    return subprocess.run(
        [
            sys.executable,
            str(script),
            selector,
            option[harness],
            str(store),
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("ids", [("abc-later", "abc"), ("abc", "abc-later")])
def test_exact_id_wins_in_either_discovery_order(harness, tmp_path, ids):
    store = _store(tmp_path, harness, ids)
    selected = _select(harness, store, "abc")
    if harness == "copilot":
        assert selected == "abc"
    else:
        expected = store / f"unrelated-{ids.index('abc')}.jsonl"
        assert selected == expected


def test_unique_prefix_selects_renamed_transcript(harness, tmp_path):
    store = _store(tmp_path, harness, ["abc-later"])
    selected = _select(harness, store, "abc")
    expected = "abc-later" if harness == "copilot" else store / "unrelated-0.jsonl"
    assert selected == expected


def test_ambiguous_prefix_reports_candidates(harness, tmp_path):
    store = _store(tmp_path, harness, ["abc-later", "abc"])
    with pytest.raises(FileNotFoundError, match="ambiguous") as error:
        _select(harness, store, "ab")
    assert "abc-later" in str(error.value)
    assert "abc" in str(error.value)


@pytest.mark.parametrize("selector", ["missing", "ABC", "unrelated"])
def test_missing_case_mismatch_and_filename_alias_are_rejected(
    harness, tmp_path, selector
):
    store = _store(tmp_path, harness, ["abc-later"])
    if harness == "copilot":
        selected = _select(harness, store, selector)
        assert selected is None
    else:
        with pytest.raises(FileNotFoundError, match=selector):
            _select(harness, store, selector)


@pytest.mark.parametrize("selector", ["a%", "a_", "a[", "a\\"])
def test_copilot_prefix_syntax_is_literal(tmp_path, selector):
    store = _store(tmp_path, "copilot", ["abc", selector + "literal"])
    selected = _select("copilot", store, selector)
    assert selected == selector + "literal"


@pytest.mark.parametrize("selector", ["a%", "a_", "ABC"])
def test_copilot_pattern_or_case_only_match_is_missing(tmp_path, selector):
    store = _store(tmp_path, "copilot", ["abc"])
    selected = _select("copilot", store, selector)
    assert selected is None


@pytest.mark.parametrize(
    "selector, diagnostic",
    [("missing", "matching"), ("ab", "ambiguous"), ("ABC", "matching")],
)
@pytest.mark.parametrize("destination", ["stdout", "new", "existing"])
def test_cli_failure_never_exports_or_overwrites(
    harness, tmp_path, selector, diagnostic, destination
):
    store = _store(tmp_path, harness, ["abc-later", "abc"])
    output = tmp_path / "export.json"
    if destination == "existing":
        output.write_text("existing destination")
    result = _cli(harness, store, selector, "-" if destination == "stdout" else output)
    assert result.returncode == 1
    assert result.stdout == ""
    assert diagnostic in result.stderr
    assert selector in result.stderr
    if destination == "existing":
        assert output.read_text() == "existing destination"
    else:
        assert not output.exists()


def test_cli_success_preserves_export_content_and_identity(harness, tmp_path):
    store = _store(tmp_path, harness, ["abc", "abc-later"])
    exact = _cli(harness, store, "abc", "-")
    assert exact.returncode == 0, exact.stderr
    envelope = json.loads(exact.stdout)
    assert envelope["events"]
    if harness == "copilot":
        assert envelope["session_id"] == "abc"
    else:
        explicit = _cli(harness, store, str(store / "unrelated-0.jsonl"), "-")
        assert explicit.returncode == 0, explicit.stderr
        assert json.loads(explicit.stdout) == envelope


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_duplicate_exact_ids_remain_ambiguous(tmp_path, harness):
    store = _store(tmp_path, harness, ["abc", "abc", "abc-later"])
    with pytest.raises(FileNotFoundError, match="ambiguous"):
        _select(harness, store, "abc")


def test_claude_parent_selection_excludes_child_sidecars(tmp_path):
    parent = tmp_path / CLAUDE_TRANSCRIPT.name
    shutil.copyfile(CLAUDE_TRANSCRIPT, parent)
    shutil.copytree(CLAUDE_TRANSCRIPT.with_suffix(""), parent.with_suffix(""))
    selected = claude.select_transcript(CLAUDE_TRANSCRIPT.stem, None, str(tmp_path))
    assert selected == parent


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_discovery_scans_past_metadata_windows(tmp_path, harness):
    store = _store(tmp_path, harness, ["abc"])
    transcript = store / "unrelated-0.jsonl"
    # Existing export metadata windows are unrelated to ID discovery. Push all
    # native records past them without changing any native identity evidence.
    transcript.write_text("\n" * 100 + transcript.read_text())
    selected = _select(harness, store, "abc")
    assert selected == transcript


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_conflicting_recorded_ids_fail_without_export(tmp_path, harness):
    store = _store(tmp_path, harness, ["abc"])
    other = tmp_path / "other.jsonl"
    _transcript_copy(harness, other, "different")
    transcript = store / "unrelated-0.jsonl"
    # Concatenate two native conversations to model conflicting identity evidence.
    transcript.write_text(transcript.read_text() + other.read_text())
    other.unlink()
    result = _cli(harness, store, "abc", "-")
    assert result.returncode == 1
    assert result.stdout == ""
    assert "conflicting recorded session IDs" in result.stderr
    assert str(transcript) in result.stderr


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_idless_transcript_requires_explicit_file(tmp_path, harness):
    store = _store(tmp_path, harness, [None])
    transcript = store / "unrelated-0.jsonl"
    with pytest.raises(FileNotFoundError, match="matching"):
        _select(harness, store, "unrelated-0")
    # Explicit positional paths and --transcript retain their precedence, even
    # when the file contains no usable recorded ID.
    if harness == "claude":
        select = claude.select_transcript
    else:
        select = codex_sources.select_transcript
    positional = select(str(transcript), None, str(store))
    override = select("missing", str(transcript), str(store))
    assert positional == transcript
    assert override == transcript
    result = _cli(harness, store, str(transcript), "-")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["events"]


def test_codex_parent_and_child_selection_uses_owned_metadata(tmp_path):
    parent = tmp_path / "parent.jsonl"
    child = tmp_path / "child.jsonl"
    shutil.copyfile(CODEX_TRANSCRIPT, parent)
    shutil.copyfile(CODEX_CHILD, child)
    # Native session_meta headers directly supply each recorded identity.
    parent_header = json.loads(CODEX_TRANSCRIPT.read_text().splitlines()[0])
    child_header = json.loads(CODEX_CHILD.read_text().splitlines()[0])
    parent_id = parent_header["payload"]["id"]
    child_id = child_header["payload"]["id"]
    assert parent_id and child_id and parent_id != child_id
    selected_parent = _select("codex", tmp_path, parent_id)
    selected_child = _select("codex", tmp_path, child_id)
    assert selected_parent == parent
    assert selected_child == child


@pytest.mark.parametrize(
    "target, field, value",
    [
        ("header", "parent_thread_id", None),
        ("header", "subagent_history_start_ordinal", None),
        ("header", "subagent_history_start_ordinal", True),
        ("header", "subagent_history_start_ordinal", "78"),
        ("inherited", "ordinal", None),
        ("inherited", "ordinal", True),
        ("inherited", "ordinal", "1"),
        ("inherited", "ordinal", 0),
        ("inherited", "ordinal", "at-boundary"),
        ("inherited-payload", "id", "unrelated"),
    ],
)
def test_codex_inherited_metadata_requires_complete_ownership_evidence(
    tmp_path, target, field, value
):
    entries = []
    for line in CODEX_CHILD.read_text().splitlines():
        entries.append(json.loads(line))
    header = entries[0]
    assert header["type"] == "session_meta"
    parent_id = header["payload"]["parent_thread_id"]
    inherited_records = []
    for entry in entries[1:]:
        if entry.get("type") == "session_meta" and entry["payload"]["id"] == parent_id:
            inherited_records.append(entry)
    (inherited,) = inherited_records
    # Incomplete or contradictory links must never exempt a conflicting ID.
    targets = {
        "header": header["payload"],
        "inherited": inherited,
        "inherited-payload": inherited["payload"],
    }
    record = targets[target]
    if value == "at-boundary":
        value = header["payload"]["subagent_history_start_ordinal"]
    if value is None:
        record.pop(field)
    else:
        record[field] = value
    path = tmp_path / "child.jsonl"
    lines = []
    for entry in entries:
        lines.append(json.dumps(entry))
    path.write_text("\n".join(lines))
    with pytest.raises(ValueError, match="conflicting recorded session IDs"):
        _select("codex", tmp_path, header["payload"]["id"])
