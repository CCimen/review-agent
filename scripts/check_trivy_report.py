#!/usr/bin/env python3
"""Apply the repository's release-blocking policy to Trivy JSON reports."""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import cast


class TrivyReportError(ValueError):
    """Raised when a scanner report does not satisfy the expected contract."""


_MAX_EXCEPTIONS = 200
_MAX_FIELD_CHARACTERS = 500
_VULNERABILITY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


@dataclass(frozen=True, slots=True, order=True)
class VulnerabilityIdentity:
    vulnerability_id: str
    package_name: str
    installed_version: str


@dataclass(frozen=True, slots=True)
class CriticalException:
    identity: VulnerabilityIdentity
    reason: str


@dataclass(frozen=True, slots=True)
class CriticalExceptionPolicy:
    expires_on: date
    exceptions: dict[VulnerabilityIdentity, CriticalException]


@dataclass(frozen=True, slots=True)
class ReportSummary:
    targets: frozenset[str]
    vulnerabilities: int
    blocking_vulnerabilities: int
    accepted_critical_occurrences: int
    accepted_critical_vulnerabilities: frozenset[VulnerabilityIdentity]


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


def _bounded_text(value: object, *, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TrivyReportError(f"{context} must be non-empty text")
    if len(value) > _MAX_FIELD_CHARACTERS:
        raise TrivyReportError(
            f"{context} must not exceed {_MAX_FIELD_CHARACTERS} characters"
        )
    return value


def _load_critical_exceptions(path: Path) -> CriticalExceptionPolicy:
    try:
        document: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TrivyReportError(f"cannot read {path}: {error}") from error

    root = _mapping(document, context=str(path))
    expected_root_keys = {"schema_version", "scope", "expires_on", "exceptions"}
    if set(root) != expected_root_keys:
        raise TrivyReportError(
            f"{path} must contain exactly {sorted(expected_root_keys)!r}"
        )
    if root.get("schema_version") != 1:
        raise TrivyReportError(f"{path}: schema_version must be 1")
    if root.get("scope") != "release-image":
        raise TrivyReportError(f"{path}: scope must be 'release-image'")
    expires_on_text = _bounded_text(
        root.get("expires_on"), context=f"{path}: expires_on"
    )
    try:
        expires_on = date.fromisoformat(expires_on_text)
    except ValueError as error:
        raise TrivyReportError(
            f"{path}: expires_on must use YYYY-MM-DD"
        ) from error
    today = datetime.now(UTC).date()
    if expires_on < today:
        raise TrivyReportError(
            f"{path}: critical exceptions expired on {expires_on.isoformat()}"
        )

    raw_exceptions = _sequence(
        root.get("exceptions"), context=f"{path}: exceptions"
    )
    if len(raw_exceptions) > _MAX_EXCEPTIONS:
        raise TrivyReportError(
            f"{path}: exceptions must not exceed {_MAX_EXCEPTIONS} entries"
        )

    exceptions: dict[VulnerabilityIdentity, CriticalException] = {}
    for index, raw_exception in enumerate(raw_exceptions):
        context = f"{path}: exceptions[{index}]"
        exception = _mapping(raw_exception, context=context)
        expected_keys = {
            "vulnerability_id",
            "package_name",
            "installed_version",
            "reason",
        }
        if set(exception) != expected_keys:
            raise TrivyReportError(
                f"{context} must contain exactly {sorted(expected_keys)!r}"
            )
        vulnerability_id = _bounded_text(
            exception.get("vulnerability_id"),
            context=f"{context}.vulnerability_id",
        )
        if _VULNERABILITY_ID_RE.fullmatch(vulnerability_id) is None:
            raise TrivyReportError(
                f"{context}.vulnerability_id has an invalid format"
            )
        identity = VulnerabilityIdentity(
            vulnerability_id=vulnerability_id,
            package_name=_bounded_text(
                exception.get("package_name"), context=f"{context}.package_name"
            ),
            installed_version=_bounded_text(
                exception.get("installed_version"),
                context=f"{context}.installed_version",
            ),
        )
        if identity in exceptions:
            raise TrivyReportError(f"{context} duplicates a prior exception")
        exceptions[identity] = CriticalException(
            identity=identity,
            reason=_bounded_text(
                exception.get("reason"), context=f"{context}.reason"
            ),
        )
    return CriticalExceptionPolicy(expires_on=expires_on, exceptions=exceptions)


def _load_report(
    path: Path,
    *,
    critical_exceptions: dict[VulnerabilityIdentity, CriticalException],
) -> ReportSummary:
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
    accepted_critical_occurrences = 0
    accepted_critical_vulnerabilities: set[VulnerabilityIdentity] = set()
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
            vulnerability_id = vulnerability.get("VulnerabilityID")
            package_name = vulnerability.get("PkgName")
            installed_version = vulnerability.get("InstalledVersion")
            if severity == "CRITICAL":
                identity = VulnerabilityIdentity(
                    vulnerability_id=_bounded_text(
                        vulnerability_id,
                        context=f"{path}: vulnerability VulnerabilityID",
                    ),
                    package_name=_bounded_text(
                        package_name,
                        context=f"{path}: vulnerability PkgName",
                    ),
                    installed_version=_bounded_text(
                        installed_version,
                        context=f"{path}: vulnerability InstalledVersion",
                    ),
                )
            else:
                identity = None
            if fixed_version.strip() or (
                severity == "CRITICAL" and identity not in critical_exceptions
            ):
                blocking_count += 1
            elif severity == "CRITICAL":
                if identity is None:
                    raise AssertionError("critical vulnerability identity is required")
                accepted_critical_occurrences += 1
                accepted_critical_vulnerabilities.add(identity)

    return ReportSummary(
        targets=frozenset(targets),
        vulnerabilities=vulnerability_count,
        blocking_vulnerabilities=blocking_count,
        accepted_critical_occurrences=accepted_critical_occurrences,
        accepted_critical_vulnerabilities=frozenset(
            accepted_critical_vulnerabilities
        ),
    )


def _write_markdown_summary(
    path: Path,
    *,
    policy: CriticalExceptionPolicy,
    accepted: frozenset[VulnerabilityIdentity],
) -> None:
    lines = [
        "<!-- review-agent-vulnerability-summary:start -->",
        "## Image vulnerability review",
        "",
        "The exact release images have no fixable high or critical findings.",
    ]
    if accepted:
        lines.extend(
            [
                f"The following {len(accepted)} critical package findings have "
                "no published vendor fix and are accepted only until "
                f"{policy.expires_on.isoformat()}:",
                "",
            ]
        )
        for identity in sorted(accepted):
            exception = policy.exceptions[identity]
            lines.append(
                f"- `{identity.vulnerability_id}` in `{identity.package_name}` "
                f"`{identity.installed_version}` — {exception.reason}"
            )
    else:
        lines.append("No critical package findings require a temporary exception.")
    lines.extend(
        [
            "",
            "Full per-platform Trivy reports are attached to this release.",
            "<!-- review-agent-vulnerability-summary:end -->",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Block every critical vulnerability and high vulnerabilities with "
            "an available fix; permit only explicit release-image exceptions."
        )
    )
    parser.add_argument(
        "--require-target",
        action="append",
        default=[],
        metavar="TARGET",
        help="require an exact Trivy result target; repeat for multiple targets",
    )
    parser.add_argument(
        "--critical-exceptions",
        type=Path,
        help=(
            "permit only exact, unexpired, release-image critical findings "
            "from this reviewed JSON policy"
        ),
    )
    parser.add_argument(
        "--markdown-output",
        type=Path,
        help="write a deterministic release-note summary after policy passes",
    )
    parser.add_argument("reports", nargs="+", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    required_targets = frozenset(arguments.require_target)
    try:
        exception_policy = (
            _load_critical_exceptions(arguments.critical_exceptions)
            if arguments.critical_exceptions is not None
            else None
        )
        if arguments.markdown_output is not None and exception_policy is None:
            raise TrivyReportError(
                "--markdown-output requires --critical-exceptions"
            )
        critical_exceptions = (
            exception_policy.exceptions if exception_policy is not None else {}
        )
        summaries = [
            _load_report(path, critical_exceptions=critical_exceptions)
            for path in arguments.reports
        ]
        observed_targets = frozenset(
            target for summary in summaries for target in summary.targets
        )
        missing_targets = sorted(required_targets - observed_targets)
        if missing_targets:
            raise TrivyReportError(
                "missing required target(s): " + ", ".join(missing_targets)
            )
        accepted = frozenset(
            identity
            for summary in summaries
            for identity in summary.accepted_critical_vulnerabilities
        )
        if exception_policy is not None:
            unused = sorted(set(exception_policy.exceptions) - accepted)
            if unused:
                rendered = ", ".join(
                    f"{identity.vulnerability_id}/{identity.package_name}/"
                    f"{identity.installed_version}"
                    for identity in unused
                )
                raise TrivyReportError(
                    "critical exceptions did not match the reports: " + rendered
                )
    except TrivyReportError as error:
        print(f"Trivy report rejected: {error}", file=sys.stderr)
        return 1

    result = {
        "accepted_critical_occurrences": sum(
            summary.accepted_critical_occurrences for summary in summaries
        ),
        "accepted_critical_vulnerabilities": len(accepted),
        "blocking_vulnerabilities": sum(
            summary.blocking_vulnerabilities for summary in summaries
        ),
        "reports": len(summaries),
        "required_targets": len(required_targets),
        "vulnerabilities": sum(
            summary.vulnerabilities for summary in summaries
        ),
    }
    if result["blocking_vulnerabilities"]:
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        print(
            "Trivy report rejected: release-blocking vulnerabilities found",
            file=sys.stderr,
        )
        return 1
    if arguments.markdown_output is not None:
        if exception_policy is None:
            raise AssertionError("exception policy is required for markdown output")
        try:
            _write_markdown_summary(
                arguments.markdown_output,
                policy=exception_policy,
                accepted=accepted,
            )
        except OSError as error:
            print(
                f"Trivy report rejected: cannot write markdown summary: {error}",
                file=sys.stderr,
            )
            return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
