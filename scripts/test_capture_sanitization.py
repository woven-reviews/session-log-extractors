"""Deterministic redaction of captured text and native JSON records."""

import json
from pathlib import Path

import pytest

from session_log_extractors.capture_sanitization import (
    LiteralReplacement,
    SanitizationRules,
    rules_from_json,
    sanitize_capture,
)
from session_log_extractors.json_types import parse_json

ROOT = Path(__file__).parent / "fixtures" / "claude" / "2.1.312"
TRANSCRIPT = ROOT / "native/65036844-09ed-4e91-8ed7-8636dfd163db.jsonl"
SUBAGENT = TRANSCRIPT.with_suffix("") / "subagents/agent-a2923ceb80fc3e3ac.jsonl"


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
    assert {item.rule: item.matches for item in result.redactions} == {
        "home": 1,
        "workspace": 1,
        "placeholder": 0,
        "email": 0,
        "api_key": 0,
    }
    assert (
        sanitize_capture(
            text, SanitizationRules(literal_replacements=tuple(reversed(replacements)))
        )
        == result
    )


def test_patterns_redact_only_actual_matches_and_report_no_secret_values():
    result = sanitize_capture(
        "café 雪 private@example.org noreply@anthropic.com sk-ant-...fragment-123",
        SanitizationRules(),
    )
    assert result.text == (
        "café 雪 [REDACTED_EMAIL] [REDACTED_EMAIL] [REDACTED_API_KEY]"
    )
    assert [(item.rule, item.matches) for item in result.redactions] == [
        ("api_key", 1),
        ("email", 2),
    ]
    assert "private@example.org" not in repr(result.redactions)
    assert sanitize_capture(result.text, SanitizationRules()).text == result.text


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
    assert [(item.rule, item.matches) for item in result.redactions] == [
        ("known_secret", 1)
    ]


@pytest.mark.parametrize("transcript", [TRANSCRIPT, SUBAGENT])
def test_native_records_keep_structure_identities_and_payloads(transcript):
    original = transcript.read_text()
    rules = SanitizationRules(redact_emails=False, redact_api_keys=False)
    result = sanitize_capture(original, rules, format="jsonl")
    assert [json.loads(line) for line in result.text.splitlines()] == [
        json.loads(line) for line in original.splitlines()
    ]
    assert [(item.rule, item.matches) for item in result.redactions] == [
        ("organization_id", 0)
    ]
    assert sanitize_capture(result.text, rules, format="jsonl") == result


def test_image_data_is_not_redacted_even_when_a_literal_matches_base64():
    original = TRANSCRIPT.read_text()
    entries = [json.loads(line) for line in original.splitlines()]
    image = next(
        block["source"]
        for entry in entries
        for block in entry.get("message", {}).get("content", [])
        if isinstance(block, dict) and block.get("type") == "image"
    )
    rules = SanitizationRules(
        literal_replacements=(
            LiteralReplacement("base64_collision", image["data"], "[REDACTED]"),
        ),
        redact_emails=False,
        redact_api_keys=False,
    )
    result = sanitize_capture(original, rules, format="jsonl")
    assert [json.loads(line) for line in result.text.splitlines()] == entries
    assert result.redactions[0].matches == 0


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


def test_jsonl_preserves_record_order_and_blank_lines():
    result = sanitize_capture(
        '{"id":"first","email":"a@example.org"}\r\n\r\n{"id":"second"}',
        SanitizationRules(),
        format="jsonl",
    )
    assert result.text == (
        '{"id": "first", "email": "[REDACTED_EMAIL]"}\n\n{"id": "second"}\n'
    )
    assert sanitize_capture("", SanitizationRules(), format="jsonl").text == ""


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
    assert [(item.rule, item.matches) for item in result.redactions] == [
        ("organization_id", 1)
    ]
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
    assert sanitize_capture("private-host", rules).text == "PILOT_HOST"
    assert rules_from_json({}) == SanitizationRules()


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
