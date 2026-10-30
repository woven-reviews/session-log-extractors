"""File-boundary checks for the reusable capture sanitizer."""

import hashlib
import json
import os
import runpy
import subprocess
import sys

import pytest

from session_log_extractors.sanitize_capture_cli import main


def test_cli_produces_repeatable_output_and_reports_with_no_input_changes(
    tmp_path, capsys
):
    source = tmp_path / "native.jsonl"
    source.write_bytes(b'{"uuid":"native-id","text":"private@example.org"}\r\n')
    original = source.read_bytes()
    rules = tmp_path / "rules.json"
    rules.write_text("{}")
    output = tmp_path / "sanitized.jsonl"
    args = [
        str(source),
        "--rules",
        str(rules),
        "--output",
        str(output),
        "--format",
        "jsonl",
    ]
    assert main(args) == 0
    first_report = capsys.readouterr().out
    first_output = output.read_bytes()
    assert main(args) == 0
    assert capsys.readouterr().out == first_report
    assert output.read_bytes() == first_output
    assert source.read_bytes() == original
    assert json.loads(first_output) == {"uuid": "native-id", "text": "[REDACTED_EMAIL]"}
    assert json.loads(first_report) == {
        "source_sha256": hashlib.sha256(original).hexdigest(),
        "sanitized_sha256": hashlib.sha256(first_output).hexdigest(),
        "redactions": [
            {"rule": "api_key", "matches": 0},
            {"rule": "email", "matches": 1},
        ],
    }


def test_script_writes_report_separately(tmp_path):
    source = tmp_path / "screen.txt"
    source.write_text("private@example.org")
    rules = tmp_path / "rules.json"
    rules.write_text("{}")
    output, report = tmp_path / "screen-clean.txt", tmp_path / "report.json"
    result = subprocess.run(
        [
            sys.executable,
            "scripts/sanitize_capture_cli.py",
            str(source),
            "--rules",
            str(rules),
            "--output",
            str(output),
            "--report",
            str(report),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == result.stderr == ""
    assert output.read_text() == "[REDACTED_EMAIL]"
    assert json.loads(report.read_text())["redactions"][1]["matches"] == 1


@pytest.mark.parametrize("entrypoint", ["script", "module"])
def test_entrypoints_use_the_same_rules_and_report(
    tmp_path, monkeypatch, capsys, entrypoint
):
    source, rules = tmp_path / "source.txt", tmp_path / "rules.json"
    output, report = tmp_path / "output.txt", tmp_path / "report.json"
    source.write_text("person@example.org")
    rules.write_text("{}")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "sanitize_capture",
            str(source),
            "--rules",
            str(rules),
            "--output",
            str(output),
            "--report",
            str(report),
        ],
    )
    if entrypoint == "script":
        run = runpy.run_path
        target = "scripts/sanitize_capture_cli.py"
    else:
        monkeypatch.delitem(sys.modules, "session_log_extractors.sanitize_capture_cli")
        run = runpy.run_module
        target = "session_log_extractors.sanitize_capture_cli"
    with pytest.raises(SystemExit) as error:
        run(target, run_name="__main__")
    assert error.value.code == 0
    assert output.read_text() == "[REDACTED_EMAIL]"
    assert json.loads(report.read_text())["redactions"][1]["matches"] == 1
    assert capsys.readouterr().out == ""


@pytest.fixture
def sanitizer_files(tmp_path):
    source = tmp_path / "native.txt"
    rules = tmp_path / "rules.json"
    output = tmp_path / "out.txt"
    report = tmp_path / "report.json"
    source.write_text("private-malformed-content")
    rules.write_text("{}")
    return source, rules, output, report


def test_cli_rejects_source_as_output(sanitizer_files, capsys):
    source, rules, output, report = sanitizer_files
    output = source
    args = [
        str(source),
        "--rules",
        str(rules),
        "--output",
        str(output),
        "--report",
        str(report),
    ]

    assert main(args) == 1

    assert "error:" in capsys.readouterr().err
    assert source.read_text() == "private-malformed-content"
    assert rules.read_text() == "{}"


def test_cli_rejects_rules_as_output(sanitizer_files, capsys):
    source, rules, output, report = sanitizer_files
    output = rules
    args = [
        str(source),
        "--rules",
        str(rules),
        "--output",
        str(output),
        "--report",
        str(report),
    ]

    assert main(args) == 1

    assert "error:" in capsys.readouterr().err
    assert source.read_text() == "private-malformed-content"
    assert rules.read_text() == "{}"


def test_cli_rejects_report_as_output(sanitizer_files, capsys):
    source, rules, output, report = sanitizer_files
    report = output
    args = [
        str(source),
        "--rules",
        str(rules),
        "--output",
        str(output),
        "--report",
        str(report),
    ]

    assert main(args) == 1

    assert "error:" in capsys.readouterr().err
    assert source.read_text() == "private-malformed-content"
    assert rules.read_text() == "{}"


def test_cli_rejects_symlink_to_source_as_output(sanitizer_files, capsys):
    source, rules, output, report = sanitizer_files
    output.symlink_to(source)
    args = [
        str(source),
        "--rules",
        str(rules),
        "--output",
        str(output),
        "--report",
        str(report),
    ]

    assert main(args) == 1

    assert "error:" in capsys.readouterr().err
    assert source.read_text() == "private-malformed-content"
    assert rules.read_text() == "{}"


def test_cli_rejects_hardlink_to_source_as_output(sanitizer_files, capsys):
    source, rules, output, report = sanitizer_files
    os.link(source, output)
    args = [
        str(source),
        "--rules",
        str(rules),
        "--output",
        str(output),
        "--report",
        str(report),
    ]

    assert main(args) == 1

    assert "error:" in capsys.readouterr().err
    assert source.read_text() == "private-malformed-content"
    assert rules.read_text() == "{}"


def test_cli_reports_missing_source_without_echoing_source(sanitizer_files, capsys):
    source, rules, output, _ = sanitizer_files
    source.unlink()
    args = [str(source), "--rules", str(rules), "--output", str(output)]

    assert main(args) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "error:" in captured.err
    assert "private-malformed-content" not in captured.err


def test_cli_reports_invalid_rules_without_echoing_source(sanitizer_files, capsys):
    source, rules, output, _ = sanitizer_files
    rules.write_text('{"unexpected": true}')
    args = [str(source), "--rules", str(rules), "--output", str(output)]

    assert main(args) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "error:" in captured.err
    assert "private-malformed-content" not in captured.err


def test_cli_reports_invalid_jsonl_without_echoing_source(sanitizer_files, capsys):
    source, rules, output, _ = sanitizer_files

    args = [str(source), "--rules", str(rules), "--output", str(output)] + [
        "--format",
        "jsonl",
    ]

    assert main(args) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "error:" in captured.err
    assert "private-malformed-content" not in captured.err


def test_cli_reports_invalid_utf8_without_echoing_source(sanitizer_files, capsys):
    source, rules, output, _ = sanitizer_files
    source.write_bytes(b"\xff")
    args = [str(source), "--rules", str(rules), "--output", str(output)]

    assert main(args) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "error:" in captured.err
    assert "private-malformed-content" not in captured.err


def test_cli_reports_unwritable_output_without_echoing_source(sanitizer_files, capsys):
    source, rules, output, _ = sanitizer_files
    output.mkdir()
    args = [str(source), "--rules", str(rules), "--output", str(output)]

    assert main(args) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "error:" in captured.err
    assert "private-malformed-content" not in captured.err


def test_cli_rejects_hard_linked_output_and_report_without_writing(tmp_path, capsys):
    source, rules = tmp_path / "native.txt", tmp_path / "rules.json"
    output, report = tmp_path / "out.txt", tmp_path / "report.json"
    source.write_text("private@example.org")
    rules.write_text("{}")
    output.write_text("existing artifact")
    os.link(output, report)
    assert (
        main(
            [
                str(source),
                "--rules",
                str(rules),
                "--output",
                str(output),
                "--report",
                str(report),
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert "Output and report must use different files" in captured.err
    assert captured.out == ""
    assert output.read_text() == report.read_text() == "existing artifact"
    assert source.read_text() == "private@example.org"
    assert rules.read_text() == "{}"
