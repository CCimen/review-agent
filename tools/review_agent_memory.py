#!/usr/bin/env python3
"""Human administration for the Review Agent PostgreSQL store."""

from __future__ import annotations

import argparse
from dataclasses import asdict, is_dataclass
from datetime import datetime
from enum import Enum
import json
import os
from pathlib import Path
import sys

import psycopg

from review_agent_private_io import write_private_file


def _plugin_parent() -> Path:
    candidates = (
        Path("/opt/review-agent-bootstrap/plugins"),
        Path(os.environ.get("HERMES_HOME", "/opt/data")) / "plugins",
        Path(__file__).resolve().parents[1] / "bootstrap" / "plugins",
    )
    for candidate in candidates:
        if (candidate / "review_agent_tools" / "operator_application.py").exists():
            sys.path.insert(0, str(candidate))
            return candidate
    raise SystemExit("Could not locate the review_agent_tools package")


_plugin_parent()

from review_agent_tools import operator_application, operator_input  # noqa: E402
from review_agent_tools.review import decision_application  # noqa: E402
from review_agent_tools.domain.feedback import (  # noqa: E402
    FeedbackDomainError,
    FeedbackTriageStatus,
)
from review_agent_tools.domain.finding import FindingDomainError  # noqa: E402
from review_agent_tools.postgres import (  # noqa: E402
    findings as postgres_findings,
    quality_reporting,
    quality_triage,
    reporting as postgres_reporting,
    review_runs as postgres_review_runs,
)
from review_agent_tools.postgres.runtime import (  # noqa: E402
    PostgreSQLNotReady,
    PostgreSQLRuntime,
    PostgreSQLRuntimeRole,
    PostgreSQLUnavailable,
)
from review_agent_tools.settings import (  # noqa: E402
    ReviewAgentSettings,
    SettingsError,
)


class _MemoryCommandError(ValueError):
    """One local command failed a stable operator contract."""

    def __init__(self, code: str, *, exit_code: int = os.EX_DATAERR) -> None:
        self.code = code
        self.exit_code = exit_code
        super().__init__(code)


_EXPECTED_COMMAND_ERRORS = (
    operator_input.OperatorInputError,
    FeedbackDomainError,
    FindingDomainError,
    postgres_findings.FingerprintNotFound,
    postgres_findings.AmbiguousFingerprint,
    quality_triage.QualityFeedbackNotFound,
    quality_triage.QualityFeedbackNotTriageable,
    postgres_reporting.RepositoryNotFound,
    postgres_reporting.FindingNotFound,
    postgres_review_runs.ReviewRunNotFound,
)

_DATABASE_BUSY_ERRORS = (
    psycopg.errors.DeadlockDetected,
    psycopg.errors.QueryCanceled,
    psycopg.errors.LockNotAvailable,
)


def _json_default(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    raise TypeError(f"{type(value).__name__} is not JSON serializable")


def _json(value: object, *, pretty: bool = False) -> str:
    return json.dumps(
        value,
        default=_json_default,
        ensure_ascii=False,
        indent=2 if pretty else None,
        separators=None if pretty else (",", ":"),
        sort_keys=True,
    )


def _runtime() -> PostgreSQLRuntime:
    runtime = PostgreSQLRuntime(
        ReviewAgentSettings.from_environment().postgres_database_url,
        role=PostgreSQLRuntimeRole.OPERATOR,
    )
    runtime.open()
    return runtime


def _write_or_print(content: str, output: str | None) -> None:
    if output:
        destination = Path(output)
        write_private_file(destination, content)
        print(destination)
    else:
        print(content, end="" if content.endswith("\n") else "\n")


def _positive_argument(value: str) -> int:
    try:
        resolved = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if resolved < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return resolved


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="List recent findings.")
    listing.add_argument("--repo")
    listing.add_argument("--limit", type=_positive_argument, default=50)
    listing.add_argument("--open-only", action="store_true")
    show = commands.add_parser("show", help="Show one finding and its decisions.")
    show.add_argument("fingerprint")
    show.add_argument("--repo", required=True)
    decide = commands.add_parser("decide", help="Append a human decision.")
    decide.add_argument("fingerprint")
    decide.add_argument("decision")
    decide.add_argument("--repo", required=True)
    decide.add_argument("--reason", required=True)
    decide.add_argument("--actor", required=True)
    decide.add_argument("--expires-days", type=int)
    decide.add_argument("--adr-id", default="")
    decide.add_argument("--occurrence-id", type=int)
    decide.add_argument("--pr", type=int)
    decide.add_argument("--local-reference", default="")
    decide.add_argument("--latest", action="store_true")
    triage = commands.add_parser(
        "triage-feedback",
        help="Append operator triage for one missed-issue signal.",
    )
    triage.add_argument("feedback_id", type=int)
    triage.add_argument(
        "--status",
        required=True,
        choices=tuple(item.value for item in FeedbackTriageStatus),
    )
    triage.add_argument("--stable-key", default="")
    triage.add_argument("--target-owner", default="")
    triage.add_argument("--evidence-reference", default="")
    triage.add_argument("--path", default="")
    triage.add_argument("--category", default="")
    triage.add_argument("--actor", required=True)
    triage.add_argument("--reason", required=True)
    export = commands.add_parser("export", help="Export one repository as JSON.")
    export.add_argument("--repo", required=True)
    export.add_argument("--row-limit", type=_positive_argument, required=True)
    export.add_argument("--output")
    stats = commands.add_parser("stats", help="Summarize finding state.")
    stats.add_argument("--repo")
    stats.add_argument("--expiring-within-days", type=int, default=30)
    runs = commands.add_parser("runs", help="Inspect or recover review runs.")
    runs.add_argument("--repo")
    runs.add_argument("--pr", type=int)
    runs.add_argument("--limit", type=_positive_argument, default=50)
    runs.add_argument("--failed", action="store_true")
    # One mode per call: a run-scoped read must never fall into the mutation.
    runs_mode = runs.add_mutually_exclusive_group()
    runs_mode.add_argument("--stats", action="store_true")
    runs_mode.add_argument("--run-id", type=_positive_argument)
    runs_mode.add_argument("--mark-stalled", action="store_true")
    runs.add_argument("--days", type=int, default=30)
    runs.add_argument("--stale-after-minutes", type=int, default=30)
    publications = commands.add_parser("publications", help="List publications.")
    publications.add_argument("--repo")
    publications.add_argument("--pr", type=int)
    publications.add_argument("--limit", type=_positive_argument, default=50)
    coverage = commands.add_parser("coverage", help="Show run coverage.")
    coverage.add_argument("--run-id", type=int, required=True)
    quality = commands.add_parser(
        "quality", help="Report explicit review-quality signals."
    )
    quality.add_argument("--days", type=int, default=30)
    quality.add_argument("--repo")
    quality_format = quality.add_mutually_exclusive_group()
    quality_format.add_argument(
        "--json", dest="quality_format", action="store_const", const="json"
    )
    quality_format.add_argument(
        "--markdown",
        dest="quality_format",
        action="store_const",
        const="markdown",
    )
    quality.set_defaults(quality_format="markdown")
    return parser


def _validate_high_cost_limits(args: argparse.Namespace) -> None:
    settings = ReviewAgentSettings.from_environment()
    uses_page_limit = args.command in {"list", "publications"} or (
        args.command == "runs"
        and not args.stats
        and not args.mark_stalled
        and args.run_id is None
    )
    if uses_page_limit:
        if args.limit > settings.operator_page_max_items:
            raise _MemoryCommandError(
                "invalid_command_input",
                exit_code=os.EX_USAGE,
            )
        return
    if args.command == "export" and args.row_limit > settings.operator_export_max_rows:
        raise _MemoryCommandError(
            "invalid_command_input",
            exit_code=os.EX_USAGE,
        )


def _json_error(
    *,
    code: str,
    retryable: bool,
    exception_type: str = "",
) -> None:
    error: dict[str, object] = {"code": code, "retryable": retryable}
    if exception_type:
        error["exception_type"] = exception_type[:120]
    print(
        json.dumps(
            {"error": error},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
        file=sys.stderr,
    )


def _failure_exit(exc: Exception) -> int:
    if isinstance(exc, _MemoryCommandError):
        _json_error(code=exc.code, retryable=False)
        return exc.exit_code
    if isinstance(exc, SettingsError):
        _json_error(code="invalid_configuration", retryable=False)
        return os.EX_CONFIG
    if isinstance(exc, PostgreSQLNotReady):
        _json_error(code="database_not_ready", retryable=False)
        return os.EX_CONFIG
    if isinstance(exc, _DATABASE_BUSY_ERRORS):
        _json_error(code="database_busy", retryable=True)
        return os.EX_TEMPFAIL
    if isinstance(exc, (PostgreSQLUnavailable, psycopg.OperationalError)):
        _json_error(code="database_unavailable", retryable=True)
        return os.EX_TEMPFAIL
    if isinstance(exc, OSError):
        _json_error(code="artifact_io_failed", retryable=False)
        return os.EX_IOERR
    if isinstance(exc, _EXPECTED_COMMAND_ERRORS):
        _json_error(code="command_rejected", retryable=False)
        return os.EX_DATAERR
    if isinstance(exc, psycopg.Error):
        _json_error(code="database_operation_failed", retryable=False)
        return os.EX_DATAERR
    _json_error(
        code="internal_error",
        retryable=False,
        exception_type=type(exc).__name__,
    )
    return os.EX_SOFTWARE


def _run_live(args: argparse.Namespace, runtime: PostgreSQLRuntime) -> int:
    result: object
    if args.command == "list":
        result = operator_application.list_findings(
            runtime, repository=args.repo, limit=args.limit,
            include_suppressed=not args.open_only,
        )
    elif args.command == "show":
        result = decision_application.show_finding(
            runtime, repository=args.repo, fingerprint=args.fingerprint
        )
    elif args.command == "decide":
        result = decision_application.decide_finding(
            runtime,
            decision_application.OperatorDecisionRequest(
                repository=args.repo, fingerprint=args.fingerprint,
                decision=args.decision, reason=args.reason, actor=args.actor,
                occurrence_id=args.occurrence_id, pr_number=args.pr,
                local_reference=args.local_reference, latest=args.latest,
                expires_days=args.expires_days, adr_id=args.adr_id,
            ),
        )
    elif args.command == "triage-feedback":
        result = decision_application.triage_review_feedback(
            runtime,
            feedback_id=args.feedback_id,
            status=args.status,
            stable_key=args.stable_key,
            target_owner=args.target_owner,
            evidence_reference=args.evidence_reference,
            path=args.path,
            category=args.category,
            actor=args.actor,
            reason=args.reason,
        )
    elif args.command == "export":
        export = operator_application.export_repository(
            runtime, repository=args.repo, row_limit=args.row_limit
        )
        _write_or_print(_json(export.to_json_obj(), pretty=True) + "\n", args.output)
        return 0
    elif args.command == "stats":
        result = operator_application.finding_stats(
            runtime, repository=args.repo,
            expiring_within_days=args.expiring_within_days,
        )
    elif args.command == "runs":
        if args.mark_stalled:
            result = operator_application.mark_stalled_runs(
                runtime, repository=args.repo, pr_number=args.pr,
                older_than_minutes=args.stale_after_minutes,
            )
        elif args.run_id is not None:
            result = operator_application.run_diagnostics(runtime, run_id=args.run_id)
        elif args.stats:
            result = operator_application.run_stats(
                runtime, repository=args.repo, pr_number=args.pr, days=args.days,
                stale_after_minutes=args.stale_after_minutes,
            )
        else:
            result = operator_application.list_runs(
                runtime, repository=args.repo, pr_number=args.pr, limit=args.limit,
                failed_only=args.failed,
            )
    elif args.command == "publications":
        result = operator_application.list_publications(
            runtime, repository=args.repo, pr_number=args.pr, limit=args.limit
        )
    elif args.command == "coverage":
        result = operator_application.coverage(runtime, run_id=args.run_id)
    elif args.command == "quality":
        report = operator_application.quality_report(
            runtime,
            repository=args.repo,
            days=args.days,
        )
        if args.quality_format == "markdown":
            print(quality_reporting.render_markdown(report), end="")
        else:
            print(_json(report, pretty=True))
        return 0
    else:
        raise RuntimeError("parser accepted an unsupported command")
    print(_json(result, pretty=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        _validate_high_cost_limits(args)
        runtime = _runtime()
    except Exception as exc:
        return _failure_exit(exc)
    try:
        return _run_live(args, runtime)
    except Exception as exc:
        return _failure_exit(exc)
    finally:
        # The command transaction has already committed or rolled back, and some
        # commands emit their receipt before returning. Pool shutdown is local
        # process cleanup and must not replace that authoritative outcome.
        try:
            runtime.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
