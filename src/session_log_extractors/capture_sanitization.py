"""Deterministic capture redaction with explicit rules and match counts.

No environment variables, home directory, credentials, or clock are consulted.
Literal matches take priority over built-in patterns, longest literal first.
All matches are replaced in one pass: replacement text is never reprocessed.
JSON property names and inline image data remain unchanged.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Literal

from session_log_extractors.json_types import JsonValue, parse_json

CaptureFormat = Literal["text", "json", "jsonl"]
_EMAIL_PATTERN = r"[A-Za-z0-9_.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
_API_KEY_PATTERN = r"sk-ant-[A-Za-z0-9_.-]+"
_ORGANIZATION_PLACEHOLDER = "[REDACTED_ORGANIZATION_ID]"


@dataclass(frozen=True)
class LiteralReplacement:
    name: str
    original: str
    replacement: str


@dataclass(frozen=True)
class SanitizationRules:
    literal_replacements: tuple[LiteralReplacement, ...] = ()
    redact_emails: bool = True
    redact_api_keys: bool = True


@dataclass(frozen=True)
class RedactionCount:
    rule: str
    matches: int


@dataclass(frozen=True)
class SanitizedCapture:
    text: str
    redactions: tuple[RedactionCount, ...]


@dataclass(frozen=True)
class _Rule:
    name: str
    pattern: str
    replacement: str


def _is_inline_image(value: dict[str, JsonValue]) -> bool:
    media_type = value.get("media_type")
    return (
        isinstance(media_type, str)
        and media_type.startswith("image/")
        and isinstance(value.get("data"), str)
    )


def _replacement_rules(rules: SanitizationRules) -> list[_Rule]:
    names = {"email", "api_key", "organization_id"}
    originals: set[str] = set()
    compiled: list[_Rule] = []
    ordered = sorted(
        rules.literal_replacements,
        key=lambda rule: (-len(rule.original), rule.original, rule.name),
    )
    for rule in ordered:
        if not rule.name or rule.name in names:
            raise ValueError("Literal rule names must be unique and not reserved")
        if not rule.original or rule.original in originals:
            raise ValueError("Literal originals must be nonempty and unique")
        names.add(rule.name)
        originals.add(rule.original)
        compiled.append(_Rule(rule.name, re.escape(rule.original), rule.replacement))
    if rules.redact_api_keys:
        compiled.append(_Rule("api_key", _API_KEY_PATTERN, "[REDACTED_API_KEY]"))
    if rules.redact_emails:
        compiled.append(_Rule("email", _EMAIL_PATTERN, "[REDACTED_EMAIL]"))
    return compiled


class _CaptureSanitizer:
    """One capture's replacements and accumulated counts, without filesystem access."""

    def __init__(self, rules: SanitizationRules) -> None:
        replacements = _replacement_rules(rules)
        self.rules = {f"rule_{i}": rule for i, rule in enumerate(replacements)}
        self.counts = {rule.name: 0 for rule in replacements}
        pattern = "|".join(
            f"(?P<{group}>{rule.pattern})" for group, rule in self.rules.items()
        )
        self.pattern = re.compile(pattern) if pattern else None

    def _replace(self, match: re.Match[str]) -> str:
        group = match.lastgroup
        if group is None:
            raise RuntimeError("Sanitizer pattern matched without a named rule")
        rule = self.rules[group]
        self.counts[rule.name] += 1
        return rule.replacement

    def text(self, text: str) -> str:
        return self.pattern.sub(self._replace, text) if self.pattern else text

    def value(self, value: JsonValue) -> JsonValue:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self.value(item) for item in value]
        if isinstance(value, dict):
            image = _is_inline_image(value)
            return {
                key: item if image and key == "data" else self._field_value(value, key)
                for key, item in value.items()
            }
        return value

    def _field_value(self, record: dict[str, JsonValue], key: str) -> JsonValue:
        item = record[key]
        if record.get("type") == "credential_org" and key == "organizationUuid":
            self.counts.setdefault("organization_id", 0)
            if item != _ORGANIZATION_PLACEHOLDER:
                self.counts["organization_id"] += 1
            return _ORGANIZATION_PLACEHOLDER
        return self.value(item)

    def redactions(self) -> tuple[RedactionCount, ...]:
        return tuple(
            RedactionCount(name, matches)
            for name, matches in sorted(self.counts.items())
        )


def _sanitize_jsonl(text: str, sanitizer: _CaptureSanitizer) -> str:
    lines = []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            lines.append("")
            continue
        try:
            value = parse_json(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"Invalid JSON on line {number}") from error
        lines.append(json.dumps(sanitizer.value(value), ensure_ascii=False))
    return "".join(line + "\n" for line in lines)


def sanitize_capture(
    text: str, rules: SanitizationRules, *, format: CaptureFormat = "text"
) -> SanitizedCapture:
    """Return sanitized text and rule counts; never mutate source records.

    JSON is serialized as UTF-8-friendly, indented JSON; JSONL has one serialized
    value per line. Both use LF and a trailing newline. Text keeps its original
    formatting. Counts include zero-match rules and contain no matched values.
    """
    sanitizer = _CaptureSanitizer(rules)
    if format == "json":
        value = sanitizer.value(parse_json(text))
        sanitized = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    elif format == "jsonl":
        sanitized = _sanitize_jsonl(text, sanitizer)
    elif format == "text":
        sanitized = sanitizer.text(text)
    else:
        raise ValueError("Unsupported capture format")
    return SanitizedCapture(sanitized, sanitizer.redactions())


def _literal_from_json(value: JsonValue) -> LiteralReplacement:
    fields = {"name", "original", "replacement"}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("Each literal rule needs name, original and replacement")
    name, original, replacement = (
        value[field] for field in ("name", "original", "replacement")
    )
    if (
        isinstance(name, str)
        and isinstance(original, str)
        and isinstance(replacement, str)
    ):
        return LiteralReplacement(name, original, replacement)
    raise ValueError("Literal rule fields must be strings")


def rules_from_json(value: JsonValue) -> SanitizationRules:
    """Read explicit configuration, rejecting typos rather than guessing settings."""
    allowed = {"literal_replacements", "redact_emails", "redact_api_keys"}
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError("Expected a sanitization rules object with known fields")
    literals = value.get("literal_replacements", [])
    emails = value.get("redact_emails", True)
    keys = value.get("redact_api_keys", True)
    if not isinstance(literals, list):
        raise ValueError("literal_replacements must be a list")
    if not isinstance(emails, bool) or not isinstance(keys, bool):
        raise ValueError("redact_emails and redact_api_keys must be booleans")
    rules = SanitizationRules(
        tuple(_literal_from_json(item) for item in literals), emails, keys
    )
    _replacement_rules(rules)
    return rules
