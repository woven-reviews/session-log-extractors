"""Shared transcript identity, directory matching, and file selection."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from session_log_extractors.json_types import JsonValue, parse_json

Source = TypeVar("Source")

_GIT_WORKTREE_PREFIX = "worktree "
_GIT_BARE_FIELD = "bare"
_GIT_PRUNABLE_FIELD = "prunable"
_GIT_ENTRY = ".git"


def _source_identity(path: Path) -> Path:
    """Resolve aliases; an unresolvable link remains its own unreadable source."""
    try:
        return path.resolve()
    except (OSError, RuntimeError):
        return path


def unique_paths(paths: Iterable[Path]) -> list[Path]:
    """Retain first discovery order while collapsing resolved source aliases."""
    unique = []
    seen = set()
    for path in paths:
        resolved = _source_identity(path)
        if resolved not in seen:
            seen.add(resolved)
            unique.append(path)
    return unique


def _resolved_directory(recorded_cwd: str | None) -> Path | None:
    if not recorded_cwd:
        return None
    try:
        path = Path(recorded_cwd).expanduser()
        return path.resolve() if path.is_absolute() else None
    except (OSError, ValueError, RuntimeError):
        return None


def _git_output(cwd: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "--no-optional-locks", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = str(exc)
        if isinstance(exc, subprocess.CalledProcessError) and isinstance(
            exc.stderr, str
        ):
            detail = exc.stderr.strip() or detail
        raise FileNotFoundError(
            f"cannot establish Git repository boundary from {cwd}: {detail}"
        ) from exc
    return result.stdout


def _git_common_directory(cwd: Path) -> Path:
    recorded = _git_output(
        cwd, "rev-parse", "--path-format=absolute", "--git-common-dir"
    )
    return Path(recorded.removesuffix("\n")).resolve()


def _available_worktree(root: Path, fields: list[str], common_directory: Path) -> bool:
    unavailable = not root.is_dir() or any(
        field.partition(" ")[0] == _GIT_PRUNABLE_FIELD for field in fields
    )
    if not unavailable:
        try:
            unavailable = _git_common_directory(root) != common_directory
        except OSError:
            unavailable = True
    if unavailable:
        print(f"warning: unavailable registered worktree: {root}", file=sys.stderr)
    return not unavailable


@dataclass(frozen=True)
class RepositoryBoundary:
    """Resolved main and registered, available linked checkout roots."""

    roots: tuple[Path, ...]

    @classmethod
    def discover(cls, cwd: Path) -> RepositoryBoundary:
        """Use Git's worktree registry, never checkout names or path overlap."""
        _git_output(cwd, "rev-parse", "--show-toplevel")
        common_directory = _git_common_directory(cwd)
        listing = _git_output(cwd, "worktree", "list", "--porcelain", "-z")
        roots = []
        for block in listing.split("\0\0"):
            fields = block.split("\0")
            if not fields[0].startswith(_GIT_WORKTREE_PREFIX):
                continue
            root = Path(fields[0].removeprefix(_GIT_WORKTREE_PREFIX)).resolve()
            if _GIT_BARE_FIELD in fields:
                continue
            if not _available_worktree(root, fields, common_directory):
                continue
            if root not in roots:
                roots.append(root)
        if not roots:
            raise FileNotFoundError(f"no available Git repository checkouts from {cwd}")
        return cls(tuple(roots))

    def matches(self, recorded_cwd: str | None, source: Path | None = None) -> bool:
        """Accept only session directories at or below a registered checkout."""
        directory = _resolved_directory(recorded_cwd)
        if directory is None:
            if source is not None:
                print(
                    f"warning: skipping {source}: no usable recorded directory evidence; "
                    "select the session explicitly if needed",
                    file=sys.stderr,
                )
            return False
        containing = [root for root in self.roots if directory.is_relative_to(root)]
        if not containing:
            return False
        checkout = max(containing, key=lambda root: len(root.parts))
        return not _inside_separate_repository(directory, checkout)


def _inside_separate_repository(directory: Path, checkout: Path) -> bool:
    """A Git entry between a session directory and its checkout marks another repo.

    Registered nested worktrees are checkouts themselves, so the walk stops at
    the deepest containing root. A removed directory leaves no such evidence.
    """
    current = directory
    while current != checkout:
        if os.path.lexists(current / _GIT_ENTRY):
            return True
        current = current.parent
    return False


def select_recorded_id(
    candidates: Iterable[tuple[str, Source]], selector: str
) -> Source | None:
    """Prefer a unique exact ID, otherwise require a unique literal prefix."""
    exact = []
    prefixes = []
    for session_id, source in candidates:
        if session_id == selector:
            exact.append((session_id, source))
        elif session_id.startswith(selector):
            prefixes.append((session_id, source))
    matches = exact or prefixes
    if len(matches) > 1:
        ids = ", ".join(session_id for session_id, _ in matches)
        raise FileNotFoundError(
            f"session selector {selector!r} is ambiguous; recorded IDs: {ids}; "
            "use a unique ID/prefix or resolve duplicate recorded IDs"
        )
    return matches[0][1] if matches else None


def read_recorded_id(
    path: Path, identify: Callable[[dict[str, JsonValue]], str | None]
) -> str | None:
    """Scan all records without retaining content or choosing conflicting IDs.

    Invalid JSON and non-object lines cannot provide identity evidence. Unreadable
    files remain unavailable to ID discovery, as with missing ID evidence.
    """
    session_id = None
    try:
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    record = parse_json(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                recorded_id = identify(record)
                if not recorded_id:
                    continue
                if session_id is not None and recorded_id != session_id:
                    raise ValueError(
                        f"conflicting recorded session IDs in {path}: "
                        f"{session_id!r}, {recorded_id!r}; "
                        "inspect the transcript or select an explicit file path"
                    )
                session_id = recorded_id
    except OSError:
        return None
    return session_id


def exact_cwd_match(recorded_cwd: str | None, cwd: Path) -> bool:
    """Match a recorded working directory to the exact invocation directory."""
    if not recorded_cwd:
        return False
    return Path(recorded_cwd).expanduser().resolve() == cwd.resolve()


def newest(paths: Iterable[Path]) -> Path | None:
    """Select the latest modification time, retaining input order on ties."""
    paths = list(paths)
    if not paths:
        return None
    return max(paths, key=lambda path: path.stat().st_mtime)
