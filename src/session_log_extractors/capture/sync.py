"""Copy the capture script, package, and hook configs into a starter repo checkout.

Idempotent: running it twice changes nothing. Existing hook configs are merged, not
overwritten, so a starter repo's own hooks survive. The starter repo gets a mirror of
this package named ``session_log_capture`` (the package uses relative imports only).

A privacy filter plug-in is not part of this package: a starter repo may own its own
``session_log_capture/session_log_filter.py`` (see ``session_filter``). Syncing never
writes, overwrites, or removes it. Without one, the hook uses the built-in envelope mode,
which runs the starter repo's own ``scripts/extract_*_session_log.py``; syncing warns when
those are missing, because capture pushes nothing without them.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from .envelope import SCRIPTS
from .session_filter import MODULE_FILE

PACKAGE_NAME = "session_log_capture"
# Starter repos only run the hook, so the sync tooling itself stays behind.
EXCLUDED = {"sync.py", MODULE_FILE}
# Files a starter repo owns inside the mirrored package; never touched by a sync.
PRESERVED = {MODULE_FILE}
CODEX_FLAG = "codex_hooks"


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    loaded = json.loads(path.read_text(encoding="utf-8"))
    return loaded if isinstance(loaded, dict) else {}


def _commands(group: dict[str, Any]) -> set[str]:
    return {h.get("command") for h in group.get("hooks", []) if isinstance(h, dict)}


def merge_hooks(existing: dict[str, Any], canonical: dict[str, Any]) -> dict[str, Any]:
    """Add each canonical hook group unless a group with the same command is already there."""
    merged = json.loads(json.dumps(existing))
    events = merged.setdefault("hooks", {})
    for event, groups in canonical["hooks"].items():
        current = events.setdefault(event, [])
        known = set().union(*(_commands(g) for g in current)) if current else set()
        current.extend(g for g in groups if not _commands(g) <= known)
    return merged


def _write_json(path: Path, value: dict[str, Any]) -> bool:
    text = json.dumps(value, indent=2) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return True


def _copy(source: Path, dest: Path) -> bool:
    data = source.read_bytes()
    if dest.exists() and dest.read_bytes() == data:
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return True


def _mirror_package(source: Path, dest: Path) -> list[str]:
    changed = []
    wanted = {p.name for p in source.glob("*.py") if p.name not in EXCLUDED}
    for name in sorted(wanted):
        if _copy(source / name, dest / name):
            changed.append(f"{PACKAGE_NAME}/{name}")
    for stale in sorted(dest.glob("*.py")) if dest.exists() else []:
        if stale.name not in wanted and stale.name not in PRESERVED:
            stale.unlink()
            changed.append(f"{PACKAGE_NAME}/{stale.name} (removed)")
    return changed


def _codex_config(source: Path, dest: Path) -> list[str]:
    if not dest.exists():
        _copy(source, dest)
        return [".codex/config.toml"]
    if CODEX_FLAG not in dest.read_text(encoding="utf-8"):
        return [f"WARNING: {dest} exists without {CODEX_FLAG}; enable it by hand"]
    return []


def _extractor_warning(target: Path) -> list[str]:
    scripts = target / "scripts"
    if (scripts / PACKAGE_NAME / MODULE_FILE).exists():
        return []
    return [
        f"WARNING: scripts/{name} is missing; capture pushes nothing for that harness"
        for name in SCRIPTS.values()
        if not (scripts / name).is_file()
    ]


def sync(target: Path, source_root: Path) -> list[str]:
    """Sync into ``target``; returns a line per file changed or needing attention."""
    changed = []
    if _copy(
        source_root / "scripts" / "capture_session.py",
        target / "scripts" / "capture_session.py",
    ):
        changed.append("scripts/capture_session.py")
    package = source_root / "src" / "session_log_extractors" / "capture"
    changed += _mirror_package(package, target / "scripts" / PACKAGE_NAME)
    hooks = source_root / "hooks"
    for rel in (".claude/settings.json", ".codex/hooks.json"):
        canonical = _load(
            hooks / ("claude/settings.json" if "claude" in rel else "codex/hooks.json")
        )
        if _write_json(target / rel, merge_hooks(_load(target / rel), canonical)):
            changed.append(rel)
    if _copy(
        hooks / "copilot" / "woven-capture.json",
        target / ".github" / "hooks" / "woven-capture.json",
    ):
        changed.append(".github/hooks/woven-capture.json")
    changed += _codex_config(
        hooks / "codex" / "config.toml", target / ".codex" / "config.toml"
    )
    return changed + _extractor_warning(target)


def remove_pycache(target: Path) -> None:
    shutil.rmtree(target / "scripts" / PACKAGE_NAME / "__pycache__", ignore_errors=True)
