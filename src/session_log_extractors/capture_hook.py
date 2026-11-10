"""Decode native hook transport and own capture staging inside Docker."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

from session_log_extractors.capture_jobs import CaptureJob, SourceMount, load_job, stage
from session_log_extractors.capture_runtime import drain
from session_log_extractors.harness import Harness
from session_log_extractors.json_types import JsonValue, parse_json, string_value

TRANSCRIPT_PATH = "/source/transcript"
DATABASE_PATH = "/source/session-store.db"
STATE_LOG_PATH = "/source/events.jsonl"
CHECKOUT_PATH = "/source/repo"


@dataclass(frozen=True)
class HookContext:
    harness: Harness
    repo: str
    remote: str
    copilot_home: str = ""


def describe(payload: JsonValue, context: HookContext) -> CaptureJob:
    """Read recorded IDs and paths without accessing the host filesystem."""
    if not isinstance(payload, dict):
        raise ValueError("hook payload must be an object")
    session_id = string_value(payload.get("session_id")) or string_value(
        payload.get("sessionId")
    )
    transcript = string_value(payload.get("transcript_path")) or string_value(
        payload.get("transcriptPath")
    )
    state_log = ""
    destination = TRANSCRIPT_PATH
    if context.harness == Harness.COPILOT:
        if not context.copilot_home:
            raise ValueError("Copilot capture requires its host store path")
        # Use native separators; these strings name host files, not Linux files.
        separator = "\\" if "\\" in context.copilot_home else "/"
        store = context.copilot_home.rstrip("/\\")
        transcript = store + separator + "session-store.db"
        state_log = separator.join(
            (store, "session-state", session_id or "", "events.jsonl")
        )
        destination = DATABASE_PATH
    if not session_id or not transcript:
        raise ValueError("hook must record a session ID and transcript path")
    for value in (session_id, transcript, state_log):
        if "\n" in value or "\r" in value:
            raise ValueError("hook source fields cannot contain line breaks")
    mounts = [SourceMount(transcript, destination)]
    if state_log:
        mounts.append(SourceMount(state_log, STATE_LOG_PATH))
    return CaptureJob(
        harness=context.harness,
        session_id=session_id,
        transcript=transcript,
        repo=context.repo,
        remote=context.remote,
        state_log=state_log,
        mounts=tuple(mounts),
        checkout_mount=CHECKOUT_PATH,
    )


def _same_session(path: Path, job: CaptureJob) -> bool:
    try:
        candidate = load_job(path)
    except (OSError, ValueError):
        return False
    return _same_sources(candidate, job)


def _same_sources(candidate: CaptureJob, job: CaptureJob) -> bool:
    return (
        candidate.harness == job.harness
        and candidate.session_id == job.session_id
        and candidate.repo == job.repo
        and candidate.remote == job.remote
        and candidate.transcript == job.transcript
        and candidate.state_log == job.state_log
    )


def _consume_signals(state: Path, job: CaptureJob, context: HookContext) -> None:
    # The staged job rereads this session after its quiet interval. Only consume
    # signals whose exact sources are available to this container.
    for path in (state / "incoming").glob("*.json"):
        try:
            candidate = describe(
                parse_json(path.read_text(encoding="utf-8-sig")), context
            )
        except (OSError, ValueError):
            continue
        if _same_sources(candidate, job):
            path.unlink(missing_ok=True)


def consume(state: Path, request: Path, context: HookContext) -> None:
    """Persist work before upload; recover pending work for this recorded session."""
    job = load_job(request)
    if (
        job.repo != context.repo
        or job.remote != context.remote
        or job.harness != context.harness
    ):
        raise ValueError("capture request does not match the configured assessment")
    stage(state, job)
    _consume_signals(state, job, context)
    request.unlink()
    for path in (state / "requests").glob("*.json"):
        if _same_session(path, job):
            stage(state, job)
            path.unlink(missing_ok=True)
    requests = []
    for path in (state / "pending").glob("*.json"):
        if _same_session(path, job):
            requests.append(path)
    for path in sorted(requests):
        drain(state, request=path.name)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("describe", "run"))
    parser.add_argument("input", type=Path)
    parser.add_argument("--harness", type=Harness, required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--remote", required=True)
    parser.add_argument("--copilot-home", default="")
    parser.add_argument("--state", type=Path)
    args = parser.parse_args(argv)
    context = HookContext(args.harness, args.repo, args.remote, args.copilot_home)
    if args.mode == "describe":
        job = describe(parse_json(args.input.read_text(encoding="utf-8-sig")), context)
        print(job.session_id)
        print(job.transcript)
        print(job.state_log)
        print(job.serialize())
    else:
        if args.state is None:
            parser.error("run requires --state")
        consume(args.state, args.input, context)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
