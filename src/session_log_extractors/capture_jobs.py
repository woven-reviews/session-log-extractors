"""Persisted capture requests and explicit host-to-container path translation."""

from __future__ import annotations

import json
import ntpath
import posixpath
import re
from dataclasses import dataclass
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from uuid import uuid4

from session_log_extractors.harness import Harness
from session_log_extractors.json_types import JsonValue, parse_json


def native_path(value: str) -> PurePath:
    """Interpret recorded Windows paths even inside a Linux container."""
    return (
        PureWindowsPath(ntpath.normpath(value))
        if PureWindowsPath(value).drive
        else PurePosixPath(posixpath.normpath(value))
    )


@dataclass(frozen=True)
class SourceMount:
    """One recorded file exposed at one container path."""

    recorded: str
    mounted: str


@dataclass(frozen=True)
class CaptureJob:
    """One recorded session owned by a configured assessment checkout."""

    harness: str
    session_id: str
    transcript: str
    repo: str
    remote: str
    state_log: str = ""
    mounts: tuple[SourceMount, ...] = ()
    checkout_mount: str = ""

    def __post_init__(self) -> None:
        Harness(self.harness)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}", self.session_id):
            raise ValueError("invalid recorded session identifier")
        if not self.repo or not self.remote:
            raise ValueError("capture requires repo and remote")

    @property
    def output_path(self) -> str:
        return f"session_logs/{self.harness}/{self.session_id}.json"

    def resolve(self, recorded: str) -> Path:
        """Map only known mounts, preserving recorded paths in exported records."""
        source = native_path(recorded)
        for mount in self.mounts:
            if source == native_path(mount.recorded):
                return Path(mount.mounted)
        checkout = native_path(self.repo)
        if self.checkout_mount and source.is_relative_to(checkout):
            mounted_root = Path(self.checkout_mount)
            path = mounted_root.joinpath(*source.relative_to(checkout).parts)
            if not path.resolve().is_relative_to(mounted_root.resolve()):
                raise ValueError("source symlink escapes the mounted checkout")
            return path
        if self.mounts or self.checkout_mount:
            raise ValueError("source file is not explicitly mounted")
        return Path(recorded)

    def owns(self, recorded_cwd: str | None) -> bool:
        return recorded_cwd is not None and native_path(recorded_cwd).is_relative_to(
            native_path(self.repo)
        )

    def serialize(self) -> str:
        return json.dumps(
            {
                "harness": self.harness,
                "session_id": self.session_id,
                "transcript": self.transcript,
                "repo": self.repo,
                "remote": self.remote,
                "state_log": self.state_log,
                "checkout_mount": self.checkout_mount,
                "mounts": [
                    {"recorded": mount.recorded, "mounted": mount.mounted}
                    for mount in self.mounts
                ],
            }
        )


def load_job(path: Path) -> CaptureJob:
    value = parse_json(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("capture request must be an object")
    required = ("harness", "session_id", "transcript", "repo", "remote")
    fields: dict[str, str] = {}
    for key in (*required, "state_log", "checkout_mount"):
        item = value.get(key, "" if key not in required else None)
        if not isinstance(item, str):
            raise ValueError(f"capture request requires string {key}")
        fields[key] = item
    return CaptureJob(**fields, mounts=_load_mounts(value.get("mounts", [])))


def _load_mounts(value: JsonValue) -> tuple[SourceMount, ...]:
    if not isinstance(value, list):
        raise ValueError("mounts must be a list")
    mounts = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("mount must be an object")
        recorded, mounted = item.get("recorded"), item.get("mounted")
        if not isinstance(recorded, str) or not isinstance(mounted, str):
            raise ValueError("mount requires recorded and mounted paths")
        mounts.append(SourceMount(recorded, mounted))
    return tuple(mounts)


def stage(state: Path, job: CaptureJob) -> Path:
    pending = state / "pending"
    pending.mkdir(parents=True, exist_ok=True)
    path = pending / f"{uuid4().hex}.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(job.serialize(), encoding="utf-8")
    temporary.replace(path)
    return path
