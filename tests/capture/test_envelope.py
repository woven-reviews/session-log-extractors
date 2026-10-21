"""The built-in envelope mode: the existing extractor's output is all that is ever saved."""

import json
import shutil
from pathlib import Path

import pytest

from session_log_extractors.capture import envelope, export, locate

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
FIXTURES = SCRIPTS / "fixtures"
HELPERS = ("session_log_envelope.py", "skill_metadata.py")


def builder(scripts=SCRIPTS, deferred=False):
    return envelope.EnvelopeBuilder(scripts, deferred=deferred)


def starter_scripts(tmp_path, name, source):
    """A starter repo's scripts folder holding one extractor and the helpers it imports."""
    scripts = tmp_path / "starter-scripts"
    scripts.mkdir()
    (scripts / envelope.SCRIPTS[name]).write_text(source)
    for helper in HELPERS:
        shutil.copy(SCRIPTS / helper, scripts / helper)
    return scripts


def test_valid_envelope_checks_schema_and_harness():
    good = json.dumps(
        {"schema": "raw-session-log/1", "harness": "claude", "events": []}
    )

    assert envelope.valid_envelope(good, "claude")
    assert envelope.valid_envelope(good)
    assert envelope.valid_envelope(good, "codex") is None
    assert envelope.valid_envelope("not json") is None
    assert envelope.valid_envelope("[1]") is None
    assert envelope.valid_envelope('{"schema": "other/1"}') is None


@pytest.mark.parametrize(
    "fixture", sorted((FIXTURES / "claude").glob("*.jsonl")), ids=lambda p: p.stem
)
def test_claude_fixtures_become_envelopes(fixture):
    try:
        text = builder().claude(fixture)
    except envelope.EnvelopeError:
        pytest.skip("fixture has no exportable events")

    assert envelope.valid_envelope(text, "claude")


@pytest.mark.parametrize(
    "fixture", sorted((FIXTURES / "codex").glob("*.jsonl")), ids=lambda p: p.stem
)
def test_codex_fixtures_become_envelopes(fixture):
    assert envelope.valid_envelope(builder().codex(fixture), "codex")


def test_copilot_sessions_become_envelopes(tmp_path):
    manifest = json.loads((FIXTURES / "copilot" / "manifest.json").read_text())
    case, session_id = sorted(manifest["cases"].items())[0]
    home = tmp_path / "home"
    native = home / ".copilot"
    (native / "session-state" / session_id).mkdir(parents=True)
    shutil.copy(
        FIXTURES / "copilot" / case / "events.jsonl",
        native / "session-state" / session_id / "events.jsonl",
    )
    shutil.copy(FIXTURES / "copilot" / "session-store.db", native / "session-store.db")

    text = builder().copilot(session_id, home, native)

    assert envelope.valid_envelope(text, "copilot")


def big_result_transcript(path):
    entries = [
        {
            "type": "user",
            "uuid": "u1",
            "timestamp": "2026-01-01T00:00:00Z",
            "message": {"role": "user", "content": "read it"},
        },
        {
            "type": "assistant",
            "uuid": "a1",
            "timestamp": "2026-01-01T00:00:01Z",
            "message": {
                "role": "assistant",
                "model": "m",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Bash",
                        "input": {"command": "cat big"},
                    }
                ],
            },
        },
        {
            "type": "user",
            "uuid": "u2",
            "timestamp": "2026-01-01T00:00:02Z",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": "SECRET" * 5000,
                    }
                ],
            },
        },
        {
            "type": "assistant",
            "uuid": "a2",
            "timestamp": "2026-01-01T00:00:03Z",
            "message": {
                "role": "assistant",
                "model": "m",
                "content": [{"type": "text", "text": "done"}],
            },
        },
    ]
    path.write_text("".join(json.dumps(e) + "\n" for e in entries))
    return path


def test_tool_results_in_the_envelope_are_capped_at_4kb(tmp_path):
    transcript = big_result_transcript(tmp_path / "big.jsonl")

    text = builder().claude(transcript)

    calls = [c for e in json.loads(text)["events"] for c in e.get("tool_calls") or []]
    assert calls[0]["result_truncated"] is True
    assert calls[0]["result_bytes"] == 30000
    assert len(calls[0]["result"].encode()) <= 4096
    assert "SECRET" * 1000 not in text


def test_claude_subagents_beside_the_transcript_are_read(tmp_path):
    copied = tmp_path / "subagent.jsonl"
    shutil.copy(FIXTURES / "claude" / "subagent.jsonl", copied)
    shutil.copytree(
        FIXTURES / "claude" / "subagent" / "subagents",
        tmp_path / "subagent" / "subagents",
    )

    assert envelope.valid_envelope(builder().claude(copied), "claude")


def test_a_deferred_builder_returns_placeholders_without_running_anything(tmp_path):
    deferred = builder(tmp_path / "nowhere", deferred=True)

    assert deferred.claude(tmp_path / "x.jsonl") == ""
    assert deferred.codex(tmp_path / "x.jsonl") == ""
    assert deferred.copilot("s", tmp_path, tmp_path) == ""


def test_a_missing_extractor_fails_closed(tmp_path):
    with pytest.raises(envelope.EnvelopeError, match="missing"):
        builder(tmp_path).claude(tmp_path / "x.jsonl")


def test_a_failing_extractor_fails_closed(tmp_path):
    scripts = starter_scripts(
        tmp_path, "claude", "import sys\nsys.exit('bad transcript')\n"
    )

    with pytest.raises(envelope.EnvelopeError, match="bad transcript"):
        builder(scripts).claude(tmp_path / "x.jsonl")


def test_an_extractor_that_writes_no_envelope_fails_closed(tmp_path):
    scripts = starter_scripts(tmp_path, "claude", "print('nothing to write')\n")

    with pytest.raises(envelope.EnvelopeError, match="0 envelopes"):
        builder(scripts).claude(tmp_path / "x.jsonl")


def test_an_extractor_for_another_harness_is_rejected(tmp_path):
    source = (
        "import json, sys\n"
        "out = sys.argv[sys.argv.index('--output') + 1]\n"
        "open(out, 'w').write(json.dumps({'schema': 'raw-session-log/1', 'harness': 'codex'}))\n"
    )
    scripts = starter_scripts(tmp_path, "claude", source)

    with pytest.raises(envelope.EnvelopeError, match="0 envelopes"):
        builder(scripts).claude(tmp_path / "x.jsonl")


def test_a_hung_extractor_times_out_and_fails_closed(tmp_path, monkeypatch):
    scripts = starter_scripts(tmp_path, "claude", "import time\ntime.sleep(30)\n")
    monkeypatch.setattr(envelope, "TIMEOUT_SECONDS", 0.5)

    with pytest.raises(envelope.EnvelopeError, match="could not run"):
        builder(scripts).claude(tmp_path / "x.jsonl")


def test_a_markdown_log_beside_the_envelope_is_never_returned(tmp_path):
    """Starter extractors write a Markdown log and an envelope; only the envelope is kept."""
    source = (
        "import json, sys, pathlib\n"
        "out = pathlib.Path(sys.argv[sys.argv.index('--output') + 1])\n"
        "out.write_text('# Session log\\nSECRET markdown')\n"
        "(out.parent / 'claude_session_log_raw_x.json').write_text(json.dumps("
        "{'schema': 'raw-session-log/1', 'harness': 'claude', 'events': []}))\n"
    )
    scripts = starter_scripts(tmp_path, "claude", source)

    text = builder(scripts).claude(tmp_path / "x.jsonl")

    assert "SECRET" not in text
    assert envelope.valid_envelope(text, "claude")


def test_locate_builds_envelope_snapshots_for_each_harness(tmp_path):
    root = tmp_path / "clone"
    root.mkdir()
    real = builder()
    claude = locate.claude_snapshot(
        {
            "session_id": "abc",
            "transcript_path": str(FIXTURES / "claude" / "baseline.jsonl"),
            "cwd": str(root),
        },
        root,
        real,
    )
    codex_dir = tmp_path / ".codex" / "sessions" / "2026"
    codex_dir.mkdir(parents=True)
    shutil.copy(FIXTURES / "codex" / "model.jsonl", codex_dir / "rollout-1-cx1.jsonl")
    codex = locate.codex_snapshot(
        {"session_id": "cx1", "cwd": str(root)}, root, tmp_path, real
    )

    assert list(claude.files) == ["session_logs/claude/abc.json"]
    assert list(codex.files) == ["session_logs/codex/cx1.json"]
    assert not claude.deferred


def test_locate_builds_a_copilot_envelope_snapshot(tmp_path):
    manifest = json.loads((FIXTURES / "copilot" / "manifest.json").read_text())
    case, session_id = sorted(manifest["cases"].items())[0]
    root = tmp_path / "clone"
    root.mkdir()
    native = tmp_path / ".copilot"
    (native / "session-state" / session_id).mkdir(parents=True)
    shutil.copy(
        FIXTURES / "copilot" / case / "events.jsonl",
        native / "session-state" / session_id / "events.jsonl",
    )
    shutil.copy(FIXTURES / "copilot" / "session-store.db", native / "session-store.db")

    snap = locate.copilot_snapshot(
        {"sessionId": session_id, "cwd": str(root)}, root, tmp_path, builder()
    )

    assert list(snap.files) == [f"session_logs/copilot/{session_id}.json"]
    assert envelope.valid_envelope(
        snap.files[f"session_logs/copilot/{session_id}.json"]
    )


def test_export_passes_valid_envelopes_through_unchanged(tmp_path):
    tree = tmp_path / "tree"
    (tree / "claude").mkdir(parents=True)
    (tree / "copilot").mkdir(parents=True)
    text = json.dumps(
        {"schema": "raw-session-log/1", "harness": "claude", "events": [1]}
    )
    (tree / "claude" / "abc.json").write_text(text)
    (tree / "copilot" / "s1.json").write_text(
        json.dumps({"schema": "raw-session-log/1", "harness": "copilot", "events": []})
    )

    result = export.export_sessions(tree, tmp_path / "out", SCRIPTS)

    assert result.failures == {}
    assert (tmp_path / "out" / "claude_session_log_raw_abc.json").read_text() == text
    assert (tmp_path / "out" / "copilot_session_log_raw_s1.json").exists()


def test_export_reports_envelopes_that_are_not_valid(tmp_path):
    tree = tmp_path / "tree"
    (tree / "codex").mkdir(parents=True)
    (tree / "codex" / "bad.json").write_text("{not json")
    (tree / "codex" / "wrong.json").write_text(
        json.dumps({"schema": "raw-session-log/1", "harness": "claude"})
    )

    result = export.export_sessions(tree, tmp_path / "out", SCRIPTS)

    assert set(result.failures) == {"codex/bad", "codex/wrong"}
    assert result.files == []
