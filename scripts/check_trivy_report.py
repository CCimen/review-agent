#!/usr/bin/env python3
"""Apply the repository's release-blocking policy to Trivy JSON reports."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import cast


class TrivyReportError(ValueError):
    """Raised when a scanner report does not satisfy the expected contract."""


@dataclass(frozen=True, slots=True)
class ReportSummary:
    targets: frozenset[str]
    vulnerabilities: int
    blocking_vulnerabilities: int


def _mapping(value: object, *, context: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TrivyReportError(f"{context} must be a JSON object")
    raw_mapping = cast(dict[object, object], value)
    if not all(isinstance(key, str) for key in raw_mapping):
        raise TrivyReportError(f"{context} keys must be text")
    return cast(dict[str, object], value)


def _sequence(value: object, *, context: str) -> list[object]:
    if not isinstance(value, list):
        raise TrivyReportError(f"{context} must be a JSON array")
    return cast(list[object], value)


def _load_report(path: Path) -> ReportSummary:
    try:
        document: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TrivyReportError(f"cannot read {path}: {error}") from error

    root = _mapping(document, context=str(path))
    results = _sequence(root.get("Results"), context=f"{path}: Results")
    if not results:
        raise TrivyReportError(f"{path}: Results must not be empty")

    targets: set[str] = set()
    vulnerability_count = 0
    blocking_count = 0
    for index, raw_result in enumerate(results):
        result = _mapping(raw_result, context=f"{path}: Results[{index}]")
        target = result.get("Target")
        if not isinstance(target, str) or not target:
            raise TrivyReportError(f"{path}: Results[{index}].Target must be text")
        targets.add(target)

        raw_vulnerabilities = result.get("Vulnerabilities")
        if raw_vulnerabilities is None:
            continue
        vulnerabilities = _sequence(
            raw_vulnerabilities,
            context=f"{path}: Results[{index}].Vulnerabilities",
        )
        for vulnerability_index, raw_vulnerability in enumerate(vulnerabilities):
            vulnerability = _mapping(
                raw_vulnerability,
                context=(
                    f"{path}: Results[{index}].Vulnerabilities"
                    f"[{vulnerability_index}]"
                ),
            )
            severity = vulnerability.get("Severity")
            if not isinstance(severity, str):
                raise TrivyReportError(
                    f"{path}: vulnerability Severity must be text"
                )
            if severity not in {"HIGH", "CRITICAL"}:
                raise TrivyReportError(
                    f"{path}: unsupported vulnerability Severity {severity!r}"
                )
            fixed_version = vulnerability.get("FixedVersion", "")
            if not isinstance(fixed_version, str):
                raise TrivyReportError(
                    f"{path}: vulnerability FixedVersion must be text"
                )
            vulnerability_count += 1
            if severity == "CRITICAL" or bool(fixed_version.strip()):
                blocking_count += 1

    return ReportSummary(
        targets=frozenset(targets),
        vulnerabilities=vulnerability_count,
        blocking_vulnerabilities=blocking_count,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Block critical vulnerabilities and high vulnerabilities with an "
            "available fix."
        )
    )
    parser.add_argument(
        "--require-target",
        action="append",
        default=[],
        metavar="TARGET",
        help="require an exact Trivy result target; repeat for multiple targets",
    )
    parser.add_argument("reports", nargs="+", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    required_targets = frozenset(arguments.require_target)
    try:
        summaries = [_load_report(path) for path in arguments.reports]
        observed_targets = frozenset(
            target for summary in summaries for target in summary.targets
        )
        missing_targets = sorted(required_targets - observed_targets)
        if missing_targets:
            raise TrivyReportError(
                "missing required target(s): " + ", ".join(missing_targets)
            )
    except TrivyReportError as error:
        print(f"Trivy report rejected: {error}", file=sys.stderr)
        return 1

    result = {
        "blocking_vulnerabilities": sum(
            summary.blocking_vulnerabilities for summary in summaries
        ),
        "reports": len(summaries),
        "required_targets": len(required_targets),
        "vulnerabilities": sum(
            summary.vulnerabilities for summary in summaries
        ),
    }
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    if result["blocking_vulnerabilities"]:
        print(
            "Trivy report rejected: release-blocking vulnerabilities found",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
