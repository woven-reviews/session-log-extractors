"""Deterministic redaction of captured text and native JSON records."""

import json

import pytest
from claude_fixture import CHILD_TRANSCRIPT as SUBAGENT
from claude_fixture import TRANSCRIPT
from codex_fixture import CHILD_TRANSCRIPT, image_message
from codex_fixture import TRANSCRIPT as CODEX_TRANSCRIPT

from session_log_extractors.capture_sanitization import (
    LiteralReplacement,
    SanitizationRules,
    rules_from_json,
    sanitize_capture,
)
from session_log_extractors.json_types import parse_json


def test_literal_replacements_are_longest_first_and_do_not_cascade():
    replacements = (
        LiteralReplacement("home", "/Users/example", "/Users/PILOT_USER"),
        LiteralReplacement("workspace", "/Users/example/work", "/workspace"),
        LiteralReplacement("placeholder", "/workspace", "should-not-cascade"),
    )
    text = "/Users/example/work/a /Users/example/b"
    rules = SanitizationRules(literal_replacements=replacements)
    result = sanitize_capture(text, rules)
    assert result.text == "/workspace/a /Users/PILOT_USER/b"
    redaction_counts = {}
    for item in result.redactions:
        redaction_counts[item.rule] = item.matches
    assert redaction_counts == {
        "home": 1,
        "workspace": 1,
        "placeholder": 0,
        "email": 0,
        "api_key": 0,
    }
    sanitized = sanitize_capture(
        text, SanitizationRules(literal_replacements=tuple(reversed(replacements)))
    )
    assert sanitized == result


def test_patterns_redact_only_actual_matches_and_report_no_secret_values():
    result = sanitize_capture(
        "café 雪 private@example.org noreply@anthropic.com sk-ant-...fragment-123",
        SanitizationRules(),
    )
    assert result.text == (
        "café 雪 [REDACTED_EMAIL] [REDACTED_EMAIL] [REDACTED_API_KEY]"
    )
    redaction_counts = []
    for item in result.redactions:
        redaction_counts.append((item.rule, item.matches))
    assert redaction_counts == [
        ("api_key", 1),
        ("email", 2),
    ]
    assert "private@example.org" not in repr(result.redactions)
    sanitized = sanitize_capture(result.text, SanitizationRules())
    assert sanitized.text == result.text


def test_configured_secret_does_not_depend_on_the_environment(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "environment-only-value")
    rules = SanitizationRules(
        literal_replacements=(
            LiteralReplacement("known_secret", "listed-sensitive-value", "[SECRET]"),
        ),
        redact_emails=False,
        redact_api_keys=False,
    )
    result = sanitize_capture(
        "listed-sensitive-value environment-only-value person@example.org", rules
    )
    assert result.text == "[SECRET] environment-only-value person@example.org"
    redaction_counts = []
    for item in result.redactions:
        redaction_counts.append((item.rule, item.matches))
    assert redaction_counts == [("known_secret", 1)]


@pytest.mark.parametrize(
    "transcript", [TRANSCRIPT, SUBAGENT, CODEX_TRANSCRIPT, CHILD_TRANSCRIPT]
)
def test_native_records_keep_structure_identities_and_payloads(transcript):
    original = transcript.read_text()
    rules = SanitizationRules(redact_emails=False, redact_api_keys=False)
    result = sanitize_capture(original, rules, format="jsonl")
    actual_records = []
    expected_records = []
    for line in result.text.splitlines():
        actual_records.append(json.loads(line))
    for line in original.splitlines():
        expected_records.append(json.loads(line))
    assert actual_records == expected_records
    for count in result.redactions:
        assert count.matches == 0
    repeated = sanitize_capture(result.text, rules, format="jsonl")
    assert repeated == result


def test_image_data_is_not_redacted_even_when_a_literal_matches_base64():
    original = TRANSCRIPT.read_text()
    entries = []
    image_data = set()
    for line in original.splitlines():
        entry = json.loads(line)
        entries.append(entry)
        for block in entry.get("message", {}).get("content", []):
            if isinstance(block, dict) and block.get("type") == "image":
                image_data.add(block["source"]["data"])
    assert image_data, "Capture must include native image bytes"
    replacements = []
    for index, data in enumerate(sorted(image_data)):
        replacements.append(
            LiteralReplacement(f"base64_collision_{index}", data, "[REDACTED]")
        )
    rules = SanitizationRules(
        literal_replacements=tuple(replacements),
        redact_emails=False,
        redact_api_keys=False,
    )

    result = sanitize_capture(original, rules, format="jsonl")

    actual_records = []
    for line in result.text.splitlines():
        actual_records.append(json.loads(line))
    assert actual_records == entries
    for item in result.redactions:
        assert item.matches == 0


def test_json_redaction_handles_escapes_without_changing_keys_or_scalar_types():
    rules = SanitizationRules(
        literal_replacements=(
            LiteralReplacement("windows_home", r"C:\Users\example", r"C:\Users\PILOT"),
        )
    )
    original = {
        "uuid": "message-1",
        "timestamp": "2026-10-29T19:35:13Z",
        "content": 'PILOT_QUOTES_527, "café 雪" person@example.org',
        "path": r"C:\Users\example\file.txt",
        "data": [None, 17, True, {"literal@example.org": "unchanged key"}],
    }
    result = sanitize_capture(json.dumps(original), rules, format="json")
    assert json.loads(result.text) == {
        **original,
        "content": 'PILOT_QUOTES_527, "café 雪" [REDACTED_EMAIL]',
        "path": r"C:\Users\PILOT\file.txt",
    }
    assert original["path"] == r"C:\Users\example\file.txt"


@pytest.mark.parametrize("format", ["json", "jsonl"])
@pytest.mark.parametrize(
    "representation",
    ["image_url", "url", "path", "file_path", "mime_type", "media_type"],
)
def test_codex_inline_image_representations_preserve_bytes(format, representation):
    captured = []
    for line in CODEX_TRANSCRIPT.read_text().splitlines():
        captured.append(json.loads(line))
    message = image_message(captured)
    entries = [message]
    content = message["payload"]["content"]
    for index, block in enumerate(content):
        if block.get("type") == "input_image":
            image_index = index
            data_url = block["image_url"]
            break
    else:
        pytest.fail("Capture lacks a native inline image")
    # Change one captured block to exercise each legacy representation.
    if representation in {"mime_type", "media_type"}:
        content[image_index] = {
            representation: "image/png",
            "data": data_url.split(",", 1)[1],
        }
    else:
        content[image_index] = {"type": "input_image", representation: data_url}
    content.append({"type": "input_text", "text": "iVBOR"})
    encoded_records = []
    for entry in entries:
        encoded_records.append(json.dumps(entry))
    original = json.dumps(entries) if format == "json" else "\n".join(encoded_records)
    rules = SanitizationRules(
        literal_replacements=(LiteralReplacement("collision", "iVBOR", "[USER]"),)
    )

    result = sanitize_capture(original, rules, format=format)

    content[-1]["text"] = "[USER]"
    if format == "json":
        actual = json.loads(result.text)
    else:
        actual = []
        for line in result.text.split("\n"):
            if line:
                actual.append(json.loads(line))
    assert actual == entries
    counts = {}
    for item in result.redactions:
        counts[item.rule] = item.matches
    assert counts["collision"] == 1


@pytest.mark.parametrize("separator", ["\u2028", "\u0085"])
@pytest.mark.parametrize("ensure_ascii", [True, False])
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_jsonl_unicode_in_strings_is_not_a_record_boundary(
    separator, ensure_ascii, newline
):
    # These legal JSON string characters are also recognized by str.splitlines().
    records = [{"text": f"before{separator}after"}, {"text": "second"}]
    encoded_records = []
    for record in records:
        encoded_records.append(json.dumps(record, ensure_ascii=ensure_ascii))
    original = newline.join(encoded_records) + newline
    rules = SanitizationRules()

    result = sanitize_capture(original, rules, format="jsonl")
    repeated = sanitize_capture(result.text, rules, format="jsonl")

    actual_records = []
    for line in result.text.split("\n"):
        if line:
            actual_records.append(json.loads(line))
    assert actual_records == records
    assert repeated == result


def test_jsonl_preserves_record_order_and_blank_lines():
    result = sanitize_capture(
        '{"id":"first","email":"a@example.org"}\r\n\r\n{"id":"second"}',
        SanitizationRules(),
        format="jsonl",
    )
    assert result.text == (
        '{"id": "first", "email": "[REDACTED_EMAIL]"}\n\n{"id": "second"}\n'
    )
    sanitized = sanitize_capture("", SanitizationRules(), format="jsonl")
    assert sanitized.text == ""


@pytest.mark.parametrize("format", ["json", "jsonl"])
def test_credential_organization_is_redacted_by_field_without_literal_rules(format):
    # Synthetic identity boundary: the existing native fixtures have no credential_org.
    organization = "00000000-0000-4000-8000-000000000001"
    original = {
        "uuid": "message-1",
        "attachment": {
            "type": "credential_org",
            "organizationUuid": organization,
            "unknown_field": {"kept": True},
        },
        "other": {"type": "session", "organizationUuid": organization},
    }
    rules = SanitizationRules(redact_emails=False, redact_api_keys=False)
    result = sanitize_capture(json.dumps(original), rules, format=format)
    expected = json.loads(json.dumps(original))
    expected["attachment"]["organizationUuid"] = "[REDACTED_ORGANIZATION_ID]"
    assert json.loads(result.text) == expected
    assert original["attachment"]["organizationUuid"] == organization
    redaction_counts = []
    for item in result.redactions:
        redaction_counts.append((item.rule, item.matches))
    assert redaction_counts == [("organization_id", 1)]
    repeated = sanitize_capture(result.text, rules, format=format)
    assert repeated.text == result.text
    assert repeated.redactions[0].matches == 0


def test_credential_organization_without_identity_keeps_other_fields():
    original = {"type": "credential_org", "context": {"kept": True}}
    result = sanitize_capture(json.dumps(original), SanitizationRules(), format="json")
    assert json.loads(result.text) == original


def test_invalid_jsonl_reports_line_number_without_echoing_private_content():
    with pytest.raises(ValueError, match="Invalid JSON on line 2") as error:
        sanitize_capture(
            "{}\nprivate-sensitive-malformed", SanitizationRules(), format="jsonl"
        )
    assert "private-sensitive-malformed" not in str(error.value)


def test_unknown_format_does_not_fall_back_to_raw_text():
    with pytest.raises(ValueError, match="Unsupported capture format"):
        sanitize_capture("{}", SanitizationRules(), format="yaml")


def test_rules_are_explicitly_loaded_from_json():
    rules = rules_from_json(
        parse_json("""{
        "literal_replacements": [
            {"name": "host", "original": "private-host", "replacement": "PILOT_HOST"}
        ],
        "redact_emails": false,
        "redact_api_keys": true
    }""")
    )
    sanitized = sanitize_capture("private-host", rules)
    assert sanitized.text == "PILOT_HOST"
    loaded_rules = rules_from_json({})
    assert loaded_rules == SanitizationRules()


@pytest.mark.parametrize(
    "value",
    [
        [],
        {"unknown_option": True},
        {"redact_emails": "yes"},
        {"redact_api_keys": 1},
        {"literal_replacements": {}},
        {"literal_replacements": [None]},
        {"literal_replacements": [{"name": "missing"}]},
        {"literal_replacements": [{"name": "x", "original": 7, "replacement": "x"}]},
    ],
)
def test_invalid_rule_documents_are_rejected(value):
    with pytest.raises(ValueError, match="Expected|must be|Each literal rule"):
        rules_from_json(value)


@pytest.mark.parametrize(
    "replacements",
    [
        (LiteralReplacement("", "value", "x"),),
        (LiteralReplacement("email", "value", "x"),),
        (LiteralReplacement("organization_id", "value", "x"),),
        (LiteralReplacement("x", "", "x"),),
        (LiteralReplacement("x", "a", "x"), LiteralReplacement("x", "b", "y")),
        (LiteralReplacement("x", "a", "x"), LiteralReplacement("y", "a", "y")),
    ],
)
def test_ambiguous_or_empty_literal_rules_are_rejected(replacements):
    with pytest.raises(ValueError, match="Literal (rule names|originals) must"):
        sanitize_capture("text", SanitizationRules(literal_replacements=replacements))
