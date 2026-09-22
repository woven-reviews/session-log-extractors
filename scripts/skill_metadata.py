"""Resolve and summarize local ``SKILL.md`` definitions for log extractors."""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


def _skill_slug(name: str) -> str:
    """Return the filesystem-facing portion of a possibly namespaced name."""
    return name.rsplit(":", 1)[-1].strip()


def _candidate_paths(
    name: str, explicit_path: Optional[str], project_root: Path
) -> Iterable[Path]:
    if explicit_path:
        yield Path(explicit_path).expanduser()

    slug = _skill_slug(name)
    if not slug or slug in (".", "..") or "/" in slug or "\\" in slug:
        return

    home = Path.home()
    roots = (
        project_root / ".agents" / "skills",
        project_root / ".claude" / "skills",
        project_root / ".codex" / "skills",
        home / ".claude" / "skills",
        home / ".codex" / "skills",
    )
    for root in roots:
        yield root / slug / "SKILL.md"

    # Installed plugin skills (Claude or Codex) are versioned several levels
    # below cache/. Limit the recursive scan to files whose parent dir matches
    # the invoked name.
    namespace = name.split(":", 1)[0].casefold() if ":" in name else ""
    for cache in (
        home / ".claude" / "plugins" / "cache",
        home / ".codex" / "plugins" / "cache",
    ):
        if not cache.is_dir():
            continue
        try:
            matches = [
                path
                for path in cache.rglob("SKILL.md")
                if path.parent.name.casefold() == slug.casefold()
            ]
            matches.sort(
                key=lambda path: (
                    0
                    if namespace
                    and namespace in (part.casefold() for part in path.parts)
                    else 1,
                    str(path),
                )
            )
            yield from matches
        except OSError:
            pass


def _frontmatter(text: str) -> Dict[str, str]:
    """Parse the simple top-level YAML fields commonly used by skills.

    This deliberately avoids a YAML dependency. It supports scalar values and
    folded/literal continuation blocks, which covers skill name/description
    frontmatter while leaving nested configuration alone.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}

    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return {}

    result: Dict[str, str] = {}
    i = 1
    while i < end:
        match = re.match(r"^([A-Za-z][A-Za-z0-9_-]*):(?:\s*(.*))?$", lines[i])
        if not match:
            i += 1
            continue
        key, raw = match.group(1), (match.group(2) or "").strip()
        if raw in ("|", "|-", "|+", ">", ">-", ">+"):
            block: List[str] = []
            i += 1
            while i < end and (not lines[i].strip() or lines[i][:1].isspace()):
                block.append(lines[i].strip())
                i += 1
            separator = "\n" if raw.startswith("|") else " "
            result[key] = separator.join(part for part in block if part).strip()
            continue
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
            raw = raw[1:-1]
        result[key] = raw
        i += 1
    return result


def _heading(text: str) -> str:
    for line in text.splitlines():
        match = re.match(r"^#\s+(.+?)\s*$", line)
        if match:
            return match.group(1)
    return ""


def _body_lines(text: str) -> List[str]:
    """The markdown body with any leading ``---`` frontmatter block removed."""
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
        if end is not None:
            return lines[end + 1 :]
    return lines


_LIST_MARKER = re.compile(r"^(?:[-*+]|\d+[.)])\s+")


def _sections(text: str) -> List[Dict[str, str]]:
    """``##`` sections as an annotated outline of the skill's steps.

    Each entry is ``{"heading": ..., "summary": ...}`` where the summary is the
    first prose line of that section (list markers stripped, truncated). Only
    second-level headings are collected: the ``#`` title and deeper ``###``
    sub-steps are skipped so the outline stays high-level. Fenced code blocks
    are ignored so ``#`` comments inside them aren't mistaken for steps.
    """
    steps: List[Dict[str, str]] = []
    in_code = False
    for line in _body_lines(text):
        if line.strip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        match = re.match(r"^##\s+(.+?)\s*#*$", line)
        if match:
            steps.append({"heading": match.group(1).strip(), "summary": ""})
            continue
        # First prose line after a heading becomes that step's summary. Skip
        # markdown table rows/separators — a table header reads as noise here.
        stripped = line.strip()
        if steps and not steps[-1]["summary"] and stripped and stripped[0] not in "#|":
            summary = _LIST_MARKER.sub("", stripped)
            if len(summary) > 120:
                summary = summary[:117].rstrip() + "..."
            steps[-1]["summary"] = summary
    return steps


def _prose_steps(text: str) -> List[Dict[str, str]]:
    """Fallback step outline for definitions with no ``##`` headings.

    Many slash commands write their procedure as prose paragraphs (often with a
    ``**bold**`` lead-in per step) rather than headings. Each top-level
    paragraph becomes one step; ``!`` command-exec lines and fenced code are
    skipped. Callers only use this when there are 2+ paragraphs, so a one-line
    body still renders as a single ``_Overview:_`` instead.
    """
    steps: List[Dict[str, str]] = []
    cur: List[str] = []

    def flush() -> None:
        if cur:
            para = " ".join(cur)
            if len(para) > 120:
                para = para[:117].rstrip() + "..."
            steps.append({"heading": para, "summary": ""})
            cur.clear()

    in_code = False
    for line in _body_lines(text):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code = not in_code
            flush()
            continue
        if in_code:
            continue
        if not stripped or stripped[0] in "#!":
            flush()
            continue
        cur.append(stripped)
    flush()
    return steps


# Frontmatter fields worth surfacing, in display order, with their log labels.
_META_FIELDS = (
    ("argument-hint", "Arguments"),
    ("allowed-tools", "Allowed tools"),
    ("model", "Model"),
    ("homepage", "Homepage"),
)


def _overview(text: str) -> str:
    """The first body paragraph — a fallback purpose line for heading-less defs
    (e.g. slash commands whose body is prose rather than ``##`` steps)."""
    para: List[str] = []
    in_code = False
    for line in _body_lines(text):
        stripped = line.strip()
        if stripped.startswith("```"):
            if para:
                break
            in_code = not in_code
            continue
        if in_code:
            continue
        if stripped.startswith("#") or not stripped:
            if para:
                break
            continue
        para.append(stripped)
    return " ".join(para).strip()


@lru_cache(maxsize=256)
def _read_definition(path_text: str) -> Dict[str, Any]:
    path = Path(path_text)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return {}
    metadata = _frontmatter(text)
    # Prefer ``##`` headings; fall back to prose paragraphs (2+ only, so a
    # one-line body still reads as a single overview).
    sections = _sections(text)
    if not sections:
        prose = _prose_steps(text)
        if len(prose) >= 2:
            sections = prose
    return {
        "path": str(path.resolve()),
        "name": metadata.get("name", ""),
        "description": metadata.get("description", ""),
        "title": _heading(text),
        "sections": sections,
        "overview": _overview(text),
        "meta": {key: metadata[key] for key, _ in _META_FIELDS if metadata.get(key)},
    }


@lru_cache(maxsize=256)
def _load_skill_details_cached(
    name: str, explicit_path: Optional[str], root_text: str
) -> Dict[str, Any]:
    root = Path(root_text)
    seen = set()
    for candidate in _candidate_paths(name, explicit_path, root):
        candidate_key = str(candidate)
        if candidate_key in seen:
            continue
        seen.add(candidate_key)
        if candidate.is_file() and candidate.name == "SKILL.md":
            details = _read_definition(candidate_key)
            if details:
                return details
    return {}


def load_skill_details(
    name: str, explicit_path: Optional[str] = None, project_root: Optional[Path] = None
) -> Dict[str, Any]:
    """Return concise details from the first matching local skill definition."""
    root = (project_root or Path.cwd()).resolve()
    # Return a copy so callers cannot mutate the cached representation.
    return dict(_load_skill_details_cached(name, explicit_path, str(root)))


def _command_candidate_paths(name: str, project_root: Path) -> Iterable[Path]:
    """Slash commands live as ``<slug>.md`` under a ``commands/`` dir, project
    then user. Nested/namespaced commands are left unresolved (name-only)."""
    slug = _skill_slug(name).lstrip("/")
    if not slug or slug in (".", "..") or "/" in slug or "\\" in slug:
        return
    home = Path.home()
    for root in (project_root / ".claude" / "commands", home / ".claude" / "commands"):
        yield root / f"{slug}.md"


@lru_cache(maxsize=256)
def _load_command_details_cached(name: str, root_text: str) -> Dict[str, Any]:
    root = Path(root_text)
    for candidate in _command_candidate_paths(name, root):
        if candidate.is_file():
            details = _read_definition(str(candidate))
            if details:
                return details
    return {}


def load_command_details(
    name: str, project_root: Optional[Path] = None
) -> Dict[str, Any]:
    """Return concise details from the local slash-command definition, if any."""
    root = (project_root or Path.cwd()).resolve()
    return dict(_load_command_details_cached(name, str(root)))


def _compact(text: str, limit: int = 400) -> str:
    out = " ".join(text.split())
    return out if len(out) <= limit else out[: limit - 3].rstrip() + "..."


def render_skill_lines(
    name: str,
    details: Optional[Dict[str, Any]] = None,
    indent: str = "",
    kind: str = "Skill",
) -> List[str]:
    """Render a skill/command invocation with any available definition metadata.

    ``kind`` labels the line ("Skill" or "Command"). Beyond name/description the
    output includes a high-level step overview: the ``##`` section headings, or
    the first body paragraph when the definition has no such headings.
    """
    info = details or {}
    description = info.get("description")
    line = f"{indent}_{kind} used:_ **{name}**"
    if isinstance(description, str) and description:
        line += f" — {_compact(description)}"
    lines = [line]

    path = info.get("path")
    if isinstance(path, str) and path:
        lines.append(f"{indent}  _Definition:_ `{path}`")
    title = info.get("title")
    defined_name = info.get("name")
    if isinstance(title, str) and title and title not in (name, defined_name):
        lines.append(f"{indent}  _Title:_ {title}")

    meta = info.get("meta")
    if isinstance(meta, dict):
        for key, label in _META_FIELDS:
            value = meta.get(key)
            if isinstance(value, str) and value:
                lines.append(f"{indent}  _{label}:_ {_compact(value)}")

    sections = info.get("sections")
    overview = info.get("overview")
    if isinstance(sections, list) and sections:
        lines.append(f"{indent}  _Steps:_")
        for i, step in enumerate(sections, 1):
            heading = step.get("heading", "")
            summary = step.get("summary", "")
            row = f"{indent}    {i}. {heading}"
            if summary:
                row += f" — {summary}"
            lines.append(row)
    elif isinstance(overview, str) and overview and overview != description:
        lines.append(f"{indent}  _Overview:_ {_compact(overview)}")
    return lines
