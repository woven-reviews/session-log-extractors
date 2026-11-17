"""Rollout context regressions using controlled mutations of native captures.

Guardian inputs are constructed, not native guardian captures: only first-line
metadata changes on the complete main capture. Child cases use the native
sidecar; boundary mutations deliberately retain otherwise inherited records.
"""

import json
import os
from copy import deepcopy
from pathlib import Path

import codex_fixture as fixture
import pytest
from repository_fixture import init_checkout

from session_log_extractors import codex_sources as sources
from session_log_extractors import extract_codex_session_log as codex
from session_log_extractors.session_log_envelope import export_session

META = "internal_chat_message_metadata_passthrough"
GUARDIAN_METADATA = {
    "thread_source": "guardian_review",
    "source": {"subagent": {"other": "guardian"}},
}


def _write_rollout(path, capture, changes, omitted_fields=()):
    entries = sources.load_entries(capture)
    entries[0]["payload"].update(changes)
    for key in omitted_fields:
        entries[0]["payload"].pop(key, None)
    lines = []
    for entry in entries:
        lines.append(json.dumps(entry))
    path.write_text("\n".join(lines) + "\n")
    return path


def _sources(message):
    values = []
    for span in message.get("authorship", []):
        values.append(span["source"])
    return values


def _message(session, native):
    for message in session["events"]:
        if message.get("id") == native["payload"]["id"]:
            return message
    raise AssertionError("Recorded message missing from interpreted conversation")


@pytest.fixture
def rollouts(tmp_path, monkeypatch):
    init_checkout(tmp_path)
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "sessions"
    root.mkdir()
    paths = []
    for name, capture, changes in [
        ("main", fixture.TRANSCRIPT, {}),
        ("child", fixture.CHILD_TRANSCRIPT, {}),
        ("guardian", fixture.TRANSCRIPT, GUARDIAN_METADATA),
    ]:
        metadata = {"cwd": str(tmp_path), "id": name + "-recorded-id", **changes}
        path = _write_rollout(root / (name + ".jsonl"), capture, metadata)
        os.utime(path, (100 + len(paths), 100 + len(paths)))
        paths.append(path)
    return root, paths


@pytest.mark.parametrize("bulk_flag", [[], ["--all"]])
def test_bulk_exports_main_and_child_but_skips_newer_guardian(rollouts, bulk_flag):
    root, _ = rollouts
    output = root.parent / "exports"
    status = codex.main(
        [*bulk_flag, "--sessions-root", str(root), "--output", str(output)]
    )
    assert status == 0
    ids = []
    for path in sorted(output.glob("*.json")):
        ids.append(json.loads(path.read_text())["session_id"])
    assert sorted(ids) == ["child-recorded-id", "main-recorded-id"]


@pytest.mark.parametrize("from_child", [False, True])
def test_newest_skips_guardian_and_keeps_child(rollouts, from_child, monkeypatch):
    root, paths = rollouts
    if from_child:
        elsewhere = root.parent / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
    selected = sources.select_transcript(None, None, str(root))
    assert selected == paths[1]


@pytest.mark.parametrize("strict", [False, True])
def test_guardian_only_has_no_automatic_match(rollouts, strict):
    root, paths = rollouts
    paths[0].unlink()
    paths[1].unlink()
    matching = sources.exact_cwd_transcripts(root, root.parent)
    assert matching == []
    with pytest.raises(FileNotFoundError):
        sources.select_transcript(None, None, str(root), strict=strict)
    status = codex.main(["--all", "--sessions-root", str(root)])
    assert status == 1


@pytest.mark.parametrize("selector", ["id", "prefix", "positional", "transcript"])
def test_explicit_guardian_extraction_keeps_conversation(rollouts, selector, capsys):
    root, paths = rollouts
    path = paths[2]
    options = {
        "id": ["guardian-recorded-id"],
        "prefix": ["guardian-recorded"],
        "positional": [str(path)],
        "transcript": ["--transcript", str(path)],
    }
    status = codex.main(
        [*options[selector], "--sessions-root", str(root), "--output", "-"]
    )
    assert status == 0
    exported = json.loads(capsys.readouterr().out)
    baseline = codex.extract_transcript(fixture.TRANSCRIPT)
    assert len(exported["events"]) == len(baseline["events"])
    for actual, original in zip(exported["events"], baseline["events"], strict=True):
        assert actual.get("text") == original.get("text")
        assert "candidate" not in _sources(actual)


@pytest.mark.parametrize(
    "metadata",
    [
        GUARDIAN_METADATA,
        {"thread_source": "guardian_review"},
        {"source": {"subagent": {"other": "guardian"}}},
        {
            "thread_source": "guardian_review",
            "source": {"subagent": {"thread_spawn": {}}},
        },
        {"thread_source": "subagent", "source": {"subagent": {"other": "guardian"}}},
    ],
)
def test_guardian_context_resolves_all_prompts_and_preserves_evidence(
    metadata, tmp_path, monkeypatch
):
    path = _write_rollout(tmp_path / "guardian.jsonl", fixture.TRANSCRIPT, metadata)
    source = codex.read_source(path)
    baseline = codex.interpret_source(codex.read_source(fixture.TRANSCRIPT))
    source_before = deepcopy(source)

    def forbidden(*args, **kwargs):
        pytest.fail("interpretation accessed the filesystem")

    monkeypatch.setattr(Path, "open", forbidden)
    session = codex.interpret_source(source)
    before = deepcopy(session)
    exported = export_session(session)
    assert session == before
    assert source == source_before
    assert len(session["events"]) == len(baseline["events"])
    prompts = fixture.response_items(source.entries, "message")
    checked = 0
    for native in prompts:
        if native["payload"].get(META, {}).get("content_item_kinds") != ["user.text"]:
            continue
        message = _message(session, native)
        assert message["text"] == native["payload"]["content"][0]["text"]
        assert _sources(message) == ["harness"]
        span = message["authorship"][0]
        assert (span["start"], span["end"]) == (0, len(message["text"]))
        for key, value in metadata.items():
            marker = {"field": "session_meta.payload." + key, "value": value}
            assert marker in span["markers"]
        checked += 1
    assert checked > 1
    for actual, original in zip(session["events"], baseline["events"], strict=True):
        assert actual.get("text") == original.get("text")
        assert actual.get("tool_calls") == original.get("tool_calls")
        assert actual.get("images") == original.get("images")
        assert "candidate" not in _sources(actual)
        assert exported["events"][actual["i"]].get("authorship", []) == actual.get(
            "authorship", []
        )
    monkeypatch.undo()
    assert sources.exact_cwd_transcripts(tmp_path, Path(source.cwd)) == []


@pytest.mark.parametrize("boundary", ["missing", None, False, -1, 0, "78"])
def test_child_without_usable_boundary_retains_ambiguity_and_resolved_deliveries(
    boundary, tmp_path
):
    path = _write_rollout(
        tmp_path / "child.jsonl",
        fixture.CHILD_TRANSCRIPT,
        {"subagent_history_start_ordinal": boundary},
        omitted_fields=("subagent_history_start_ordinal",)
        if boundary == "missing"
        else (),
    )
    source = codex.read_source(path)
    native = fixture.human_message(source.entries)
    session = codex.interpret_source(source)
    message = _message(session, native)
    assert message["text"] == native["payload"]["content"][0]["text"]
    assert _sources(message) == ["unknown"]
    delivery = fixture.response_items(source.entries, "agent_message")[-1]
    assert _sources(_message(session, delivery)) == ["other_agent"]
    injected = 0
    for entry in fixture.response_items(source.entries, "message"):
        kinds = entry["payload"].get(META, {}).get("content_item_kinds")
        if kinds == ["environments.environment_context"]:
            assert _sources(_message(session, entry)) == ["harness"]
            injected += 1
    assert injected > 0
    exported = export_session(session)
    assert exported["events"][message["i"]]["authorship"] == message["authorship"]
    for event in session["events"]:
        assert "candidate" not in _sources(event)


@pytest.mark.parametrize("ordinal", ["boundary", None, False, -1, "78"])
def test_child_prompt_ownership_requires_recorded_position(ordinal, tmp_path):
    entries = sources.load_entries(fixture.CHILD_TRANSCRIPT)
    # Controlled compatibility case: a parent task delivered as user.text after
    # the recorded boundary, rather than the capture's explicit agent_message.
    prompt = deepcopy(fixture.human_message(entries))
    boundary = entries[0]["payload"]["subagent_history_start_ordinal"]
    prompt["ordinal"] = boundary if ordinal == "boundary" else ordinal
    prompt["payload"]["id"] = "controlled-parent-task"
    entries.append(prompt)
    path = tmp_path / "child.jsonl"
    lines = []
    for entry in entries:
        lines.append(json.dumps(entry))
    path.write_text("\n".join(lines))
    session = codex.interpret_source(codex.read_source(path))
    message = _message(session, prompt)
    expected_source = "other_agent" if ordinal == "boundary" else "unknown"
    assert _sources(message) == [expected_source]
    assert message["text"] == prompt["payload"]["content"][0]["text"]


@pytest.mark.parametrize(
    "metadata",
    [
        {"thread_source": "future", "source": "future"},
        {"source": {"subagent": None}},
        {"thread_source": False, "source": {"subagent": "future"}},
    ],
)
def test_unfamiliar_metadata_does_not_reclassify_normal_inputs(metadata, tmp_path):
    path = _write_rollout(tmp_path / "normal.jsonl", fixture.TRANSCRIPT, metadata)
    source = codex.read_source(path)
    session = codex.interpret_source(source)
    message = _message(session, fixture.human_message(source.entries))
    assert _sources(message) == ["candidate"]


@pytest.mark.parametrize(
    "metadata",
    [
        {"thread_source": "subagent", "source": None},
        {"thread_source": None, "source": {"subagent": {"thread_spawn": {}}}},
    ],
)
def test_independent_child_indicators_keep_injections_and_agent_deliveries(
    metadata, tmp_path
):
    path = _write_rollout(tmp_path / "child.jsonl", fixture.CHILD_TRANSCRIPT, metadata)
    source = codex.read_source(path)
    session = codex.interpret_source(source)
    delivery = fixture.response_items(source.entries, "agent_message")[-1]
    assert _sources(_message(session, delivery)) == ["other_agent"]
    for event in session["events"]:
        assert "candidate" not in _sources(event)
    matching = sources.exact_cwd_transcripts(tmp_path, Path(source.cwd))
    assert matching == [path]


def test_guardian_legacy_event_without_content_provenance_is_harness(tmp_path):
    entries = sources.load_entries(fixture.TRANSCRIPT)
    # Legacy events carry no block provenance. Rollout identity establishes the
    # internal reviewer even without a response_item view to merge.
    entries[0]["payload"].update(GUARDIAN_METADATA)
    user_event = {
        "type": "event_msg",
        "payload": {"type": "user_message", "message": "approval request"},
    }
    entries = [entries[0], user_event]
    path = tmp_path / "guardian.jsonl"
    lines = []
    for entry in entries:
        lines.append(json.dumps(entry))
    path.write_text("\n".join(lines))
    session = codex.interpret_source(codex.read_source(path))
    assert len(session["events"]) == 1
    assert session["events"][0]["text"] == "approval request"
    assert _sources(session["events"][0]) == ["harness"]
