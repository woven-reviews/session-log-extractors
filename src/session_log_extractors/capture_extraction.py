"""Read native session evidence inside capture's explicitly mounted sources."""

from __future__ import annotations

import argparse
import json
import sys
from contextlib import closing
from pathlib import Path

from session_log_extractors import extract_claude_session_log as claude
from session_log_extractors import extract_codex_session_log as codex
from session_log_extractors import extract_copilot_session_log as copilot
from session_log_extractors.capture_jobs import CaptureJob, load_job, native_path
from session_log_extractors.copilot_sources import (
    CopilotSource,
    get_db_connection,
    get_session_by_id,
)
from session_log_extractors.harness import Harness
from session_log_extractors.json_types import string_value
from session_log_extractors.session_log_envelope import Envelope, export_session
from session_log_extractors.session_model import Session

CAPTURE_TOOL_RESULT_MAX_BYTES = 4096


def _codex_session(job: CaptureJob) -> Session:
    def read_image(recorded: str) -> tuple[bytes, str] | None:
        data = _image_bytes(job, recorded)
        if data is None:
            return None
        return data, native_path(recorded).suffix.lstrip(".") or "img"

    extractor = codex.CodexExtractor(image_reader=read_image)
    return extractor.interpret_source(
        extractor.read_source(job.resolve(job.transcript))
    )


def _image_bytes(job: CaptureJob, recorded: str) -> bytes | None:
    try:
        return job.resolve(recorded).read_bytes()
    except (OSError, ValueError):
        return None


def _copilot_source(job: CaptureJob, *, read_images: bool = True) -> CopilotSource:
    def read_image(recorded: str) -> bytes | None:
        return _image_bytes(job, recorded) if read_images else None

    with closing(get_db_connection(job.resolve(job.transcript))) as connection:
        selected = get_session_by_id(connection, job.session_id)
        if selected is None or selected.get("id") != job.session_id:
            raise ValueError("capture requires an exact recorded session ID")
        return copilot.EXTRACTOR.read_source(
            copilot.CopilotRequest(
                connection,
                selected,
                job.resolve(job.state_log) if job.state_log else None,
                read_image,
            )
        )


def _copilot_session(job: CaptureJob) -> Session:
    return copilot.EXTRACTOR.interpret_source(_copilot_source(job))


def extract(job: CaptureJob) -> Envelope:
    if job.harness == Harness.CLAUDE:
        session = claude.EXTRACTOR.interpret_source(
            claude.EXTRACTOR.read_source(job.resolve(job.transcript))
        )
    elif job.harness == Harness.CODEX:
        session = _codex_session(job)
    else:
        session = _copilot_session(job)
    _require_owner(job, session["session_id"], session.get("cwd"))
    if session["unsupported_tools"]:
        print(
            "Capture preserved unsupported tool payloads: "
            + ", ".join(session["unsupported_tools"]),
            file=sys.stderr,
        )
    return export_session(session, CAPTURE_TOOL_RESULT_MAX_BYTES)


def referenced_assets(job: CaptureJob) -> list[str]:
    """Have native readers identify assets before the host exposes those files."""
    if job.harness == Harness.CODEX:
        source = codex.CodexExtractor(image_reader=lambda _recorded: None).read_source(
            job.resolve(job.transcript)
        )
        _require_owner(job, source.session_id, source.cwd)
        return list(source.image_inputs)
    if job.harness == Harness.COPILOT:
        copilot_source = _copilot_source(job, read_images=False)
        _require_owner(
            job,
            string_value(copilot_source.session.get("id")),
            string_value(copilot_source.session.get("cwd")),
        )
        return list(copilot_source.image_inputs)
    session = claude.EXTRACTOR.interpret_source(
        claude.EXTRACTOR.read_source(job.resolve(job.transcript))
    )
    _require_owner(job, session["session_id"], session.get("cwd"))
    return []  # Claude records its image bytes inline.


def _require_owner(job: CaptureJob, session_id: str | None, cwd: str | None) -> None:
    if session_id != job.session_id or not job.owns(cwd):
        raise ValueError("session is not owned by this assessment checkout")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="List recorded image source files")
    parser.add_argument("request", type=Path)
    args = parser.parse_args(argv)
    print(json.dumps(referenced_assets(load_job(args.request))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
