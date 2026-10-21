import json
import runpy
import sys
from pathlib import Path

import pytest

from session_log_extractors.capture import sync

SOURCE_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def target(tmp_path):
    path = tmp_path / "starter"
    (path / ".git").mkdir(parents=True)
    return path


def test_sync_installs_scripts_package_and_hook_configs(target):
    changed = sync.sync(target, SOURCE_ROOT)

    assert "scripts/capture_session.py" in changed
    package = target / "scripts" / "session_log_capture"
    assert (package / "cli.py").exists()
    assert (package / "__init__.py").exists()
    assert not (package / "sync.py").exists()
    claude = json.loads((target / ".claude" / "settings.json").read_text())
    assert set(claude["hooks"]) == {"SessionStart", "Stop"}
    assert "Stop" in json.loads((target / ".codex" / "hooks.json").read_text())["hooks"]
    assert (
        "agentStop"
        in json.loads(
            (target / ".github" / "hooks" / "woven-capture.json").read_text()
        )["hooks"]
    )
    assert "codex_hooks = true" in (target / ".codex" / "config.toml").read_text()


def test_sync_is_idempotent(target):
    sync.sync(target, SOURCE_ROOT)

    assert [
        line
        for line in sync.sync(target, SOURCE_ROOT)
        if not line.startswith("WARNING")
    ] == []


def test_sync_keeps_existing_hooks_and_does_not_duplicate_ours(target):
    settings = target / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    mine = {
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo mine"}]}]},
        "model": "x",
    }
    settings.write_text(json.dumps(mine))

    sync.sync(target, SOURCE_ROOT)
    sync.sync(target, SOURCE_ROOT)

    merged = json.loads(settings.read_text())
    commands = [h["command"] for g in merged["hooks"]["Stop"] for h in g["hooks"]]
    assert commands.count("echo mine") == 1
    assert sum("capture_session.py" in c for c in commands) == 1
    assert merged["model"] == "x"


def test_sync_removes_stale_package_files_and_pycache(target):
    sync.sync(target, SOURCE_ROOT)
    package = target / "scripts" / "session_log_capture"
    (package / "old_module.py").write_text("x")
    (package / "__pycache__").mkdir()
    (package / "__pycache__" / "x.pyc").write_text("x")

    changed = sync.sync(target, SOURCE_ROOT)
    sync.remove_pycache(target)

    assert [line for line in changed if not line.startswith("WARNING")] == [
        "session_log_capture/old_module.py (removed)"
    ]
    assert not (package / "__pycache__").exists()


def test_sync_updates_changed_files(target):
    sync.sync(target, SOURCE_ROOT)
    (target / "scripts" / "capture_session.py").write_text("tampered")

    assert [
        line
        for line in sync.sync(target, SOURCE_ROOT)
        if not line.startswith("WARNING")
    ] == ["scripts/capture_session.py"]


def test_sync_warns_when_codex_config_lacks_the_flag(target):
    config = target / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text('model = "x"\n')

    changed = sync.sync(target, SOURCE_ROOT)

    assert any(line.startswith("WARNING") for line in changed)
    assert config.read_text() == 'model = "x"\n'


def test_sync_ignores_a_non_object_settings_file(target):
    settings = target / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text("[1]")

    sync.sync(target, SOURCE_ROOT)

    assert "hooks" in json.loads(settings.read_text())


def test_sync_script_reports_changes_and_rejects_non_checkouts(
    target, tmp_path, monkeypatch, capsys
):
    script = SOURCE_ROOT / "scripts" / "sync_capture_hooks.py"

    def run(*argv):
        monkeypatch.setattr(sys, "argv", ["sync_capture_hooks.py", *argv])
        with pytest.raises(SystemExit) as info:
            runpy.run_path(str(script), run_name="__main__")
        return info.value.code

    assert run(str(target)) == 0
    assert "scripts/capture_session.py" in capsys.readouterr().out
    (target / "scripts" / "session_log_capture" / "session_log_filter.py").write_text(
        ""
    )
    assert run(str(target)) == 0
    assert capsys.readouterr().out.strip() == "already up to date"
    assert run(str(tmp_path / "nowhere")) == 1
    assert "not a git checkout" in capsys.readouterr().err


def test_sync_never_touches_a_starter_repos_own_filter(target):
    sync.sync(target, SOURCE_ROOT)
    plugin = target / "scripts" / "session_log_capture" / "session_log_filter.py"
    plugin.write_text("# mine\n")

    changed = sync.sync(target, SOURCE_ROOT)

    assert changed == []
    assert plugin.read_text() == "# mine\n"


def test_sync_warns_when_a_starter_repo_has_neither_a_filter_nor_extractors(target):
    changed = sync.sync(target, SOURCE_ROOT)

    warnings = [line for line in changed if line.startswith("WARNING")]
    assert len(warnings) == 3
    assert "scripts/extract_claude_session_log.py is missing" in warnings[0]
    assert not (
        target / "scripts" / "session_log_capture" / "session_log_filter.py"
    ).exists()


def test_sync_is_silent_and_idempotent_when_the_starter_repo_ships_the_extractors(
    target,
):
    scripts = target / "scripts"
    scripts.mkdir()
    for name in sync.SCRIPTS.values():
        (scripts / name).write_text("# the starter repo's own extractor\n")

    sync.sync(target, SOURCE_ROOT)

    assert sync.sync(target, SOURCE_ROOT) == []
    for name in sync.SCRIPTS.values():
        assert (scripts / name).read_text() == "# the starter repo's own extractor\n"
