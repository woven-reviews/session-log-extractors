"""Repository selection through real Git boundaries and native capture copies."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from codex_fixture import CHILD_TRANSCRIPT as CODEX_CHILD
from repository_fixture import (
    _git,
    make_checkouts,
)
from repository_fixture import (
    copy_native_session as _capture,
)
from repository_fixture import (
    run_cli as _cli,
)
from repository_fixture import (
    session_store_root as _store,
)

from session_log_extractors import (
    claude_sources,
    codex_sources,
    copilot_sources,
    session_selection,
)
from session_log_extractors.extract_claude_session_log import encode_project_path


@pytest.fixture
def checkouts(tmp_path):
    return make_checkouts(tmp_path)


@pytest.fixture(params=["claude", "codex", "copilot"])
def harness(request):
    return request.param


def _output_names(directory):
    names = []
    for path in directory.glob("*.json"):
        names.append(path.name)
    return sorted(names)


@pytest.mark.parametrize("location", ["root", "child", "linked", "linked-child"])
@pytest.mark.parametrize("batch_flags", [[], ["--all"]])
def test_repository_batch_and_newest_share_worktree_boundary(
    tmp_path, checkouts, harness, location, batch_flags
):
    main, linked = checkouts
    invocation = {
        "root": main,
        "child": main / "backend",
        "linked": linked,
        "linked-child": linked / "frontend",
    }[location]
    home = tmp_path / "home"
    directories = [
        main,
        main / "backend",
        linked,
        linked / "frontend",
        tmp_path,
        home,
        tmp_path / "assessment-2",
        tmp_path / "separate-clone",
    ]
    for index, directory in enumerate(directories):
        _capture(home, harness, f"id{index}-session", str(directory), index + 1)
    output = tmp_path / "exports"

    batch = _cli(harness, home, invocation, *batch_flags, "--output", str(output))

    assert batch.returncode == 0, batch.stderr
    expected = []
    for index in range(4):
        ident = f"id{index}-session"
        if harness == "copilot":
            ident = f"2026-10-30_id{index}-sess"
        expected.append(f"{harness}_session_log_raw_{ident}.json")
    assert _output_names(output) == expected
    latest = _cli(harness, home, invocation, "--output", "-")
    assert latest.returncode == 0, latest.stderr
    selected = json.loads(latest.stdout)
    expected_latest = json.loads((output / expected[-1]).read_text())
    assert selected == expected_latest


@pytest.mark.parametrize("destination", ["stdout", "batch", "existing"])
def test_no_match_never_falls_back_or_writes(tmp_path, checkouts, harness, destination):
    main, _ = checkouts
    home = tmp_path / "home"
    _capture(home, harness, "ancestor", str(tmp_path))
    output = tmp_path / "exports"
    output.mkdir()
    existing = output / f"{harness}_session_log_raw_ancestor.json"
    existing.write_text("existing export")
    if destination == "stdout":
        args = ["--output", "-"]
    elif destination == "existing":
        args = ["--output", str(existing)]
    else:
        args = ["--output", str(output)]

    result = _cli(harness, home, main, *args)

    assert result.returncode == 1, result.stderr
    assert result.stdout == ""
    assert "no " in result.stderr.lower()
    assert str(main) in result.stderr
    assert existing.read_text() == "existing export"
    assert _output_names(output) == [existing.name]


def test_missing_directory_is_diagnosed_and_excluded(tmp_path, checkouts, harness):
    main, _ = checkouts
    home = tmp_path / "home"
    path = _capture(home, harness, "missing-cwd", None)

    result = _cli(harness, home, main, "--output", "-")

    assert result.returncode == 1
    assert result.stdout == ""
    assert "directory evidence" in result.stderr
    assert str(path) in result.stderr


@pytest.mark.parametrize("batch", [False, True])
def test_repository_requires_git_boundary(tmp_path, harness, batch):
    home = tmp_path / "home"
    _capture(home, harness, "would-match", str(tmp_path))

    output = tmp_path / "exports"
    destination = str(output) if batch else "-"
    result = _cli(harness, home, tmp_path, "--output", destination)

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr.startswith("error: cannot establish Git repository boundary")
    assert not output.exists()


def test_repository_flag_is_not_part_of_the_cli(tmp_path, harness):
    result = _cli(harness, tmp_path, tmp_path, "--repository")
    assert result.returncode == 2
    assert "unrecognized arguments: --repository" in result.stderr


def test_explicit_selection_bypasses_repository_discovery(tmp_path, harness):
    home = tmp_path / "home"
    transcript = _capture(home, harness, "outside", None)
    if harness == "copilot":
        args = ["outside", "--state-root", str(_store(home, harness))]
    else:
        args = ["--transcript", str(transcript)]

    baseline = _cli(harness, home, tmp_path, "--strict", *args, "--output", "-")
    selected = _cli(harness, home, tmp_path, *args, "--output", "-")

    assert baseline.returncode == 0, baseline.stderr
    assert selected.returncode == 0, selected.stderr
    assert json.loads(selected.stdout) == json.loads(baseline.stdout)
    assert json.loads(selected.stdout)["events"]


def test_shared_boundary_is_identical_from_all_checkout_locations(checkouts):
    main, linked = checkouts
    expected = (main.resolve(), linked.resolve())
    for invocation in (main, main / "backend", linked, linked / "frontend"):
        boundary = session_selection.RepositoryBoundary.discover(invocation)
        assert boundary.roots == expected
        for included in (main, main / "backend", linked, linked / "frontend"):
            assert boundary.matches(str(included)) is True
        for excluded in (
            main.parent,
            main.parent / "assessment-2",
            None,
            "",
            "relative",
        ):
            assert boundary.matches(excluded) is False


def test_shared_boundary_resolves_aliases_and_overlapping_roots(checkouts, tmp_path):
    main, linked = checkouts
    alias = tmp_path / "alias"
    alias.symlink_to(main, target_is_directory=True)
    boundary = session_selection.RepositoryBoundary.discover(alias / "backend")
    assert boundary.matches(str(alias / "backend")) is True
    assert boundary.matches(str(linked / "frontend")) is True


def test_harness_readers_consume_the_same_established_boundary(
    tmp_path, checkouts, monkeypatch
):
    main, linked = checkouts
    home = tmp_path / "home"
    for harness in ("claude", "codex", "copilot"):
        _capture(home, harness, "main", str(main))
        _capture(home, harness, "linked", str(linked / "frontend"))
        _capture(home, harness, "ancestor", str(tmp_path))
    boundary = session_selection.RepositoryBoundary.discover(main)

    def rediscovery_forbidden(*args):
        pytest.fail("source filtering must consume the established boundary")

    monkeypatch.setattr(
        session_selection.RepositoryBoundary, "discover", rediscovery_forbidden
    )

    claude_paths = claude_sources.repository_transcripts(
        _store(home, "claude"), boundary
    )
    codex_paths = codex_sources.repository_transcripts(_store(home, "codex"), boundary)
    copilot_candidates = copilot_sources.discover_sessions(_store(home, "copilot"))
    copilot_matches = copilot_sources.repository_sessions(copilot_candidates, boundary)

    for paths in (claude_paths, codex_paths):
        selected_ids = []
        for path in paths:
            selected_ids.append(path.stem)
        assert sorted(selected_ids) == ["linked", "main"]
    selected_ids = []
    for source in copilot_matches:
        selected_ids.append(source.session.session_id)
    assert sorted(selected_ids) == ["linked", "main"]


def test_stale_worktree_is_excluded_and_diagnosed(checkouts, capsys):
    main, linked = checkouts
    shutil.rmtree(linked)

    boundary = session_selection.RepositoryBoundary.discover(main)

    assert boundary.roots == (main.resolve(),)
    assert boundary.matches(str(linked)) is False
    assert "unavailable registered worktree" in capsys.readouterr().err


def test_replaced_worktree_cannot_admit_an_unrelated_checkout(checkouts, capsys):
    main, linked = checkouts
    shutil.rmtree(linked)
    linked.mkdir()
    _git(linked, "init")

    boundary = session_selection.RepositoryBoundary.discover(main)

    assert boundary.roots == (main.resolve(),)
    assert boundary.matches(str(linked)) is False
    assert "registered worktree" in capsys.readouterr().err


def test_bare_repository_is_not_a_checkout_root(tmp_path, checkouts):
    main, _ = checkouts
    bare = tmp_path / "bare.git"
    _git(tmp_path, "clone", "--bare", str(main), str(bare))
    checkout = tmp_path / "bare-linked"
    _git(bare, "worktree", "add", "-b", "bare-feature", str(checkout))

    boundary = session_selection.RepositoryBoundary.discover(checkout)

    assert boundary.roots == (checkout.resolve(),)
    assert boundary.matches(str(bare)) is False


def test_claude_encoded_collision_cannot_establish_ownership(tmp_path, checkouts):
    main, _ = checkouts
    home = tmp_path / "home"
    eligible = main / "backend"
    collision = Path(str(eligible).replace("/backend", "-backend"))
    eligible_log = _capture(home, "claude", "included", str(eligible))
    unrelated_log = _capture(home, "claude", "excluded", str(collision), 2)
    # Both original cwd strings encode to the same Claude project-store name.
    encoded = encode_project_path(eligible)
    assert encoded == encode_project_path(collision)
    store = _store(home, "claude") / encoded
    store.mkdir()
    eligible_log.rename(store / "included.jsonl")
    unrelated_log.rename(store / "excluded.jsonl")
    nested = store / "included" / "subagents"
    nested.mkdir(parents=True)
    shutil.copyfile(store / "included.jsonl", nested / "agent-child.jsonl")

    output = tmp_path / "exports"
    result = _cli("claude", home, main, "--output", str(output))

    assert result.returncode == 0, result.stderr
    assert _output_names(output) == ["claude_session_log_raw_included.json"]


def test_claude_cross_store_filename_collisions_retain_both_sources(
    tmp_path, checkouts
):
    main, linked = checkouts
    home = tmp_path / "home"
    _capture(home, "claude", "main", str(main))
    other = _capture(home, "claude", "linked", str(linked))
    other.rename(other.with_name("main.jsonl"))
    output = tmp_path / "exports"

    result = _cli("claude", home, main, "--output", str(output))

    assert result.returncode == 0, result.stderr
    assert _output_names(output) == [
        "claude_session_log_raw_main-2.json",
        "claude_session_log_raw_main.json",
    ]
    identities = []
    for path in sorted(output.glob("*.json")):
        identities.append(json.loads(path.read_text())["session_id"])
    assert sorted(identities) == ["linked", "main"]


def test_source_alias_is_exported_once(tmp_path, checkouts, harness):
    main, _ = checkouts
    home = tmp_path / "home"
    transcript = _capture(home, harness, "included", str(main))
    alias_store = transcript.parent.parent / "alias-store"
    if harness == "codex":
        # Codex discovery uses rglob, which does not enter linked directories,
        # so only a linked transcript file presents Codex with a second path.
        alias_store.mkdir()
        alias = alias_store / transcript.name
        alias.symlink_to(transcript)
    else:
        alias_store.symlink_to(transcript.parent, target_is_directory=True)
    output = tmp_path / "exports"

    result = _cli(harness, home, main, "--output", str(output))

    assert result.returncode == 0, result.stderr
    assert len(_output_names(output)) == 1


def test_separate_repository_inside_checkout_is_excluded(tmp_path, checkouts, harness):
    main, _ = checkouts
    nested_clone = main / "vendor" / "other"
    nested_clone.mkdir(parents=True)
    _git(nested_clone, "init")
    (nested_clone / "src").mkdir()
    nested_worktree = main / ".worktrees" / "inner"
    _git(main, "worktree", "add", "-b", "inner", str(nested_worktree))
    home = tmp_path / "home"
    _capture(home, harness, "main", str(main), 1)
    _capture(home, harness, "inner", str(nested_worktree), 2)
    _capture(home, harness, "clone", str(nested_clone / "src"), 3)
    output = tmp_path / "exports"

    result = _cli(harness, home, main, "--output", str(output))

    assert result.returncode == 0, result.stderr
    exported_ids = []
    for path in output.glob("*.json"):
        exported_ids.append(json.loads(path.read_text())["session_id"])
    sorted_ids = sorted(exported_ids)
    assert sorted_ids == ["inner", "main"]


def test_looping_store_entry_does_not_stop_export(tmp_path, checkouts, harness):
    main, _ = checkouts
    home = tmp_path / "home"
    transcript = _capture(home, harness, "included", str(main))
    # A transcript-named symlink to itself is a controlled filesystem fault that
    # no native capture contains; resolving it raises before Python 3.13.
    loop = transcript.parent.parent / "looping" / transcript.name
    loop.parent.mkdir()
    loop.symlink_to(loop)
    output = tmp_path / "exports"

    result = _cli(harness, home, main, "--output", str(output))

    assert result.returncode == 0, result.stderr
    assert len(_output_names(output)) == 1


def test_copilot_resume_does_not_override_start_directory(tmp_path, checkouts):
    main, _ = checkouts
    home = tmp_path / "home"
    transcript = _capture(home, "copilot", "outside", str(tmp_path))
    entries = []
    resume_count = 0
    for line in transcript.read_text().splitlines():
        entry = json.loads(line)
        if entry["type"] == "session.resume":
            entry["data"]["context"]["cwd"] = str(main)
            resume_count += 1
        entries.append(json.dumps(entry))
    assert resume_count > 0
    transcript.write_text("\n".join(entries))

    result = _cli("copilot", home, main, "--output", "-")

    assert result.returncode == 1
    assert result.stdout == ""


def test_strict_still_selects_only_invocation_directory(tmp_path, checkouts, harness):
    main, _ = checkouts
    home = tmp_path / "home"
    child = main / "backend"
    parent_log = _capture(home, harness, "parent", str(main), 2)
    child_log = _capture(home, harness, "child", str(child))
    if harness == "claude":
        # Legacy strict mode uses the exact encoded project store, unchanged.
        for directory, log in ((main, parent_log), (child, child_log)):
            store = _store(home, harness) / encode_project_path(directory)
            store.mkdir()
            log.rename(store / log.name)

    strict = _cli(harness, home, child, "--strict", "--output", "-")
    repository = _cli(harness, home, child, "--output", "-")

    assert strict.returncode == 0, strict.stderr
    assert repository.returncode == 0, repository.stderr
    assert json.loads(strict.stdout)["cwd"] == str(child)
    assert json.loads(repository.stdout)["cwd"] == str(main)


def test_nested_registered_checkout_does_not_duplicate_exports(
    tmp_path, checkouts, harness
):
    main, _ = checkouts
    nested = main / "nested"
    _git(main, "worktree", "add", "-b", "nested", str(nested))
    home = tmp_path / "home"
    _capture(home, harness, "included", str(nested))
    output = tmp_path / "exports"

    result = _cli(harness, home, main, "--output", str(output))

    assert result.returncode == 0, result.stderr
    assert len(_output_names(output)) == 1


def test_explicit_id_selection_keeps_legacy_scope_without_git(tmp_path, harness):
    home = tmp_path / "home"
    transcript = _capture(
        home, harness, "selected-session", str(tmp_path / "elsewhere")
    )
    _capture(home, harness, "newer-session", str(tmp_path), 2)
    if harness == "claude":
        store_args = ["--project-dir", str(transcript.parent)]
    elif harness == "codex":
        store_args = ["--sessions-root", str(_store(home, harness))]
    else:
        store_args = ["--state-root", str(_store(home, harness))]

    result = _cli(
        harness,
        home,
        tmp_path,
        "selected",
        *store_args,
        "--output",
        "-",
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["cwd"] == str(tmp_path / "elsewhere")


def test_script_exports_repository_session_from_child_directory(
    tmp_path, checkouts, harness
):
    main, linked = checkouts
    home = tmp_path / "home"
    _capture(home, harness, "included", str(linked / "frontend"))
    script = Path(__file__).parent / f"extract_{harness}_session_log.py"

    result = subprocess.run(
        [sys.executable, str(script), "--output", "-"],
        cwd=main / "backend",
        env=dict(os.environ, HOME=str(home)),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["cwd"] == str(linked / "frontend")


def test_repository_keeps_codex_child_and_excludes_guardian(tmp_path, checkouts):
    main, _ = checkouts
    home = tmp_path / "home"
    _capture(home, "codex", "main", str(main))
    guardian = _capture(home, "codex", "guardian", str(main), 3)
    lines = guardian.read_text().splitlines()
    header = json.loads(lines[0])
    header["payload"]["thread_source"] = "guardian_review"
    lines[0] = json.dumps(header)
    guardian.write_text("\n".join(lines))
    os.utime(guardian, (3, 3))
    child = guardian.parent / "child.jsonl"
    child_lines = CODEX_CHILD.read_text().splitlines()
    # Preserve recorded child ownership/history fields; change only its
    # starting cwd and identity in the owning metadata header.
    header = json.loads(child_lines[0])
    header["payload"]["cwd"] = str(main / "backend")
    header["payload"]["id"] = "child"
    child_lines[0] = json.dumps(header)
    child.write_text("\n".join(child_lines))
    os.utime(child, (2, 2))
    output = tmp_path / "exports"

    batch = _cli("codex", home, main, "--output", str(output))
    latest = _cli("codex", home, main, "--output", "-")

    assert batch.returncode == 0, batch.stderr
    assert _output_names(output) == [
        "codex_session_log_raw_child.json",
        "codex_session_log_raw_main.json",
    ]
    assert latest.returncode == 0, latest.stderr
    assert json.loads(latest.stdout)["session_id"] == "child"
