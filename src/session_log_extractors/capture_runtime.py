"""Drain persisted capture requests, serialized across temporary containers."""

from __future__ import annotations

import argparse
import fcntl
import subprocess
import time
from pathlib import Path

from session_log_extractors.capture_extraction import extract as extract
from session_log_extractors.capture_jobs import CaptureJob as CaptureJob
from session_log_extractors.capture_jobs import load_job
from session_log_extractors.capture_jobs import stage as stage
from session_log_extractors.capture_upload import UploadError, upload
from session_log_extractors.session_log_envelope import serialize_envelope

_ATTEMPTS = 3


def _pending(state: Path, request: str | None = None) -> list[Path]:
    if request is not None:
        path = state / "pending" / request
        return [path] if path.is_file() else []
    return sorted(
        (state / "pending").glob("*.json"), key=lambda path: path.stat().st_mtime_ns
    )


def _push_batch(state: Path, paths: list[Path]) -> None:
    files: dict[str, bytes] = {}
    remote = None
    for path in paths:
        job = load_job(path)
        if remote is not None and job.remote != remote:
            raise ValueError("capture queue contains different assessment remotes")
        remote = job.remote
        files[job.output_path] = serialize_envelope(extract(job)).encode("utf-8")
    if remote is not None:
        upload(state, remote, files)
    for path in paths:
        path.unlink()


def _attempt_batch(state: Path, paths: list[Path], retry_seconds: float) -> bool:
    for attempt in range(_ATTEMPTS):
        try:
            _push_batch(state, paths)
            (state / "last_error").unlink(missing_ok=True)
            return True
        except UploadError as error:
            if error.secret_rejection:
                rejected = state / "rejected"
                rejected.mkdir(exist_ok=True)
                for path in paths:
                    path.replace(rejected / path.name)
                (state / "last_error").write_text(
                    "Git rejected capture as containing secrets; inspect rejected work before retrying."
                )
                return False
        except (OSError, ValueError, subprocess.TimeoutExpired):
            (state / "last_error").write_text(
                "Capture could not read its source; pending work retained."
            )
            return False
        if attempt + 1 < _ATTEMPTS:
            time.sleep(retry_seconds)
    (state / "last_error").write_text(
        "Upload failed; pending work retained for the next capture."
    )
    return False


def drain(
    state: Path,
    quiet_seconds: float = 10,
    retry_seconds: float = 15,
    *,
    request: str | None = None,
) -> None:
    """Only remove the immutable requests included in a successful upload.

    A waiting container takes the lock after the current worker exits, closing
    the idle-exit race without stale lock stealing during a slow upload.
    """
    state.mkdir(parents=True, exist_ok=True)
    if request is not None and (
        Path(request).name != request or not request.endswith(".json")
    ):
        raise ValueError("request must be a pending filename")
    with (state / "worker.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        while paths := _quiet_batch(state, quiet_seconds, request):
            if not _attempt_batch(state, paths, retry_seconds):
                return


def _quiet_batch(state: Path, quiet_seconds: float, request: str | None) -> list[Path]:
    deadline = time.monotonic() + 60
    while paths := _pending(state, request):
        delay = min(
            quiet_seconds - (time.time() - paths[-1].stat().st_mtime),
            deadline - time.monotonic(),
        )
        if delay <= 0:
            return paths
        time.sleep(delay)
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("state", type=Path)
    parser.add_argument("--request")
    args = parser.parse_args(argv)
    drain(args.state, request=args.request)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
