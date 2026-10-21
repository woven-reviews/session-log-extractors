"""Command line for the capture hook.

Hook mode reads one harness hook payload from stdin, stages the filtered session, and
starts a background worker. It always exits 0: a capture problem must never block or fail
the candidate's agent session, so errors become a single warning line instead.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from . import gitpush, locate, pusher, session_filter
from .envelope import EnvelopeBuilder, EnvelopeError

NOTICE = "Session logs for this assessment are saved to a session-logs branch of your repository."


def repo_root(script: Path, override: str | None) -> Path:
    """The clone this hook belongs to: ``--repo`` if given, else the checkout holding the script."""
    if override:
        return Path(override)
    return Path(
        gitpush.git(script.parent, "rev-parse", "--show-toplevel").decode().strip()
    )


def _parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="capture_session.py")
    parser.add_argument("--harness", choices=["claude", "codex", "copilot"])
    parser.add_argument(
        "--repo",
        help="clone to capture for (default: the checkout containing this script)",
    )
    parser.add_argument(
        "--worker", action="store_true", help="run the background push worker"
    )
    parser.add_argument(
        "--notice",
        action="store_true",
        help="print the capture notice for a SessionStart hook",
    )
    return parser.parse_args(argv)


def _payload(text: str) -> dict[str, object]:
    try:
        loaded = json.loads(text)
    except ValueError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def package_dir(script: Path) -> Path:
    """Where the filter plug-in lives: the ``session_log_capture`` folder beside the entry script."""
    return script.parent / "session_log_capture"


def choose_filter(
    filter_dir: Path, script: Path, deferred: bool
) -> session_filter.SessionFilter | EnvelopeBuilder:
    """The filter plug-in if the starter repo provides one, else the built-in envelope mode."""
    if (filter_dir / session_filter.MODULE_FILE).is_file():
        return session_filter.load(filter_dir)
    return EnvelopeBuilder(script.parent, deferred=deferred)


def refresher(root: Path, home: Path, filter_dir: Path, script: Path) -> pusher.Refresh:
    """Rebuild a staged session from its source at push time, filtering it again."""

    def refresh(snap: locate.Snapshot) -> locate.Snapshot | None:
        flt = choose_filter(filter_dir, script, deferred=False)
        source = snap.source or {}
        return locate.build_snapshot(
            source.get("harness", snap.harness),
            source.get("payload", {}),
            root,
            home,
            flt,
        )

    return refresh


@dataclass
class Hook:
    """Where one hook run happens: the entry script, home, clone, and filter plug-in directory."""

    script: Path
    home: Path
    root: Path
    filter_dir: Path


def capture(args: argparse.Namespace, stdin_text: str, hook: Hook) -> None:
    warning = pusher.take_last_error(hook.root)
    if warning:
        print(warning, file=sys.stderr)
    if pusher.is_disabled(hook.root):
        return
    payload = _payload(stdin_text)
    harness = args.harness or locate.detect_harness(payload)
    if harness is None:
        return
    try:
        flt = choose_filter(hook.filter_dir, hook.script, deferred=True)
        snap = locate.build_snapshot(harness, payload, hook.root, hook.home, flt)
    except (session_filter.FilterError, EnvelopeError) as err:
        # Fail closed: without a working filter nothing is staged, so nothing is pushed.
        print(f"woven-capture: nothing was saved, {err}", file=sys.stderr)
        return
    if snap is None:
        return
    pusher.stage(hook.root, snap)
    pusher.spawn_worker(hook.root, hook.script)


def main(
    argv: list[str],
    stdin_text: str,
    script: Path,
    home: Path | None = None,
    filter_dir: Path | None = None,
) -> int:
    """Entry point shared by every harness hook. Returns the process exit code (always 0)."""
    args = _parse(argv)
    if args.notice:
        print(json.dumps({"systemMessage": NOTICE}))
        return 0
    try:
        root = repo_root(script, args.repo)
        where = filter_dir or package_dir(script)
        if args.worker:
            pusher.run_worker(
                root, refresh=refresher(root, home or Path.home(), where, script)
            )
        else:
            capture(args, stdin_text, Hook(script, home or Path.home(), root, where))
    except Exception as err:
        # A hook must never fail the agent session, so every error becomes one warning line.
        print(f"woven-capture: skipped ({type(err).__name__}: {err})", file=sys.stderr)
    return 0
