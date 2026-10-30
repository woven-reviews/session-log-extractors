"""File entrypoint for deterministic capture sanitization, separate from extraction."""

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Sequence

from session_log_extractors.capture_sanitization import (
    SanitizedCapture,
    rules_from_json,
    sanitize_capture,
)
from session_log_extractors.json_types import JsonValue, parse_json


def _check_destinations(inputs: list[Path], outputs: list[Path]) -> None:
    resolved_inputs = {path.resolve() for path in inputs}
    resolved_outputs = [path.resolve() for path in outputs]
    if any(path in resolved_inputs for path in resolved_outputs):
        raise ValueError("Output and report must not overwrite the source or rules")
    if len(set(resolved_outputs)) != len(resolved_outputs):
        raise ValueError("Output and report must use different paths")
    if any(
        first.exists() and second.exists() and first.samefile(second)
        for index, first in enumerate(outputs)
        for second in outputs[index + 1 :]
    ):
        raise ValueError("Output and report must use different files")
    input_files = [path for path in inputs if path.exists()]
    if any(
        output.exists() and output.samefile(source)
        for output in outputs
        for source in input_files
    ):
        raise ValueError("Output and report must not overwrite an input through a link")


def _report(source: bytes, result: SanitizedCapture) -> str:
    report: JsonValue = {
        "source_sha256": hashlib.sha256(source).hexdigest(),
        "sanitized_sha256": hashlib.sha256(result.text.encode("utf-8")).hexdigest(),
        "redactions": [
            {"rule": item.rule, "matches": item.matches} for item in result.redactions
        ],
    }
    return json.dumps(report, ensure_ascii=False, indent=2) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--rules", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--format", choices=("text", "json", "jsonl"), default="text")
    parser.add_argument(
        "--report", type=Path, help="Write counts/hashes here instead of stdout"
    )
    args = parser.parse_args(argv)
    outputs = [args.output] + ([args.report] if args.report else [])
    try:
        _check_destinations([args.source, args.rules], outputs)
        rules = rules_from_json(parse_json(args.rules.read_bytes().decode("utf-8")))
        source = args.source.read_bytes()
        result = sanitize_capture(source.decode("utf-8"), rules, format=args.format)
        report = _report(source, result)
        args.output.write_bytes(result.text.encode("utf-8"))
        if args.report:
            args.report.write_bytes(report.encode("utf-8"))
        else:
            sys.stdout.write(report)
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
