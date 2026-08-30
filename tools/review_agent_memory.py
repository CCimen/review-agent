#!/usr/bin/env python3
"""Human administration for the Review Agent PostgreSQL store."""

from __future__ import annotations

import argparse
from dataclasses import asdict, is_dataclass
from datetime import datetime
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import cast

import psycopg

from review_agent_coach_run import build_coach_run_artifacts
from review_agent_coach_proposals import CandidateProposal, ProposalBundle
from review_agent_export import repository_key
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

from review_agent_tools import operator_application  # noqa: E402
from review_agent_tools.domain.coaching import (  # noqa: E402
    COACH_INTERVENTION_OUTCOMES,
    CoachingDomainError,
    CoachCandidateInput,
    CoachInterventionOutcome,
)
from review_agent_tools.domain.feedback import (  # noqa: E402
    FeedbackDomainError,
    FeedbackTriageStatus,
)
from review_agent_tools.domain.finding import FindingDomainError  # noqa: E402
from review_agent_tools.postgres import (  # noqa: E402
    coaching as postgres_coaching,
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
    """One local command or artifact failed a stable operator contract."""

    def __init__(self, code: str, *, exit_code: int = os.EX_DATAERR) -> None:
        self.code = code
        self.exit_code = exit_code
        super().__init__(code)


_EXPECTED_COMMAND_ERRORS = (
    operator_application.OperatorInputError,
    CoachingDomainError,
    FeedbackDomainError,
    FindingDomainError,
    postgres_coaching.CoachRepositoryMismatch,
    postgres_coaching.CoachCandidateNotFound,
    postgres_coaching.CoachCandidateProvenanceMismatch,
    postgres_coaching.CoachInterventionConflict,
    postgres_findings.FingerprintNotFound,
    postgres_findings.AmbiguousFingerprint,
    quality_triage.QualityFeedbackNotFound,
    quality_triage.QualityFeedbackNotTriageable,
    postgres_reporting.RepositoryNotFound,
    postgres_reporting.FindingNotFound,
    postgres_reporting.VerificationExportUnavailable,
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


def _complete_export(
    path: str,
    *,
    repository: str | None = None,
) -> dict[str, object]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise _MemoryCommandError("export_invalid")
    state = cast(dict[str, object], value)
    if state.get("complete") is not True:
        raise _MemoryCommandError("export_incomplete")
    if repository is not None:
        try:
            requested = repository_key(repository)
        except ValueError as exc:
            raise _MemoryCommandError("repository_scope_invalid") from exc
        exported_repository = state.get("repository")
        if not isinstance(exported_repository, str):
            raise _MemoryCommandError("export_repository_missing")
        try:
            exported = repository_key(exported_repository)
        except ValueError as exc:
            raise _MemoryCommandError("export_repository_invalid") from exc
        if exported != requested:
            raise _MemoryCommandError("export_repository_mismatch")
    return state


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
    runs.add_argument("--stats", action="store_true")
    runs.add_argument("--days", type=int, default=30)
    runs.add_argument("--stale-after-minutes", type=int, default=30)
    runs.add_argument("--mark-stalled", action="store_true")
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
    verification = commands.add_parser(
        "verification-export", help="Write a private verification bundle."
    )
    verification.add_argument("--run-id", type=int, required=True)
    verification.add_argument("--output", required=True)
    learning = commands.add_parser(
        "learning-report", help="Build a report from a complete export."
    )
    learning.add_argument("--export", required=True)
    learning.add_argument("--repo")
    learning.add_argument("--output")
    replay = commands.add_parser("validate-replay", help="Validate replay fixtures.")
    replay.add_argument("path", type=Path)
    coach_export = commands.add_parser("coach-export", help="Build coach events.")
    coach_export.add_argument("--export", required=True)
    coach_export.add_argument("--repo")
    coach_export.add_argument("--after-decision-id", type=int, default=0)
    coach_export.add_argument("--after-feedback-id", type=int, default=0)
    coach_export.add_argument("--after-triage-id", type=int, default=0)
    coach_export.add_argument("--include-incomplete", action="store_true")
    coach_export.add_argument("--output", required=True)
    propose = commands.add_parser("coach-propose", help="Build coach proposals.")
    propose.add_argument("--events", required=True)
    propose.add_argument("--output-dir", required=True)
    propose.add_argument("--max-candidates", type=int, default=3)
    propose.add_argument("--min-independent-episodes", type=int, default=2)
    verify = commands.add_parser(
        "coach-verify-proposal", help="Verify a coach proposal."
    )
    verify.add_argument("--proposal", required=True)
    coach_run = commands.add_parser("coach-run", help="Run and record the coach.")
    coach_run.add_argument("--export")
    coach_run.add_argument("--repo", required=True)
    coach_run.add_argument("--row-limit", type=_positive_argument, default=10_000)
    coach_run.add_argument("--output-dir", required=True)
    coach_run.add_argument("--after-decision-id", type=int, default=0)
    coach_run.add_argument("--after-feedback-id", type=int, default=0)
    coach_run.add_argument("--after-triage-id", type=int, default=0)
    coach_run.add_argument("--include-incomplete", action="store_true")
    coach_run.add_argument("--max-candidates", type=int, default=3)
    coach_run.add_argument("--min-independent-episodes", type=int, default=2)
    coach_record = commands.add_parser(
        "coach-record-outcome",
        help="Record one final evaluated coach intervention.",
    )
    coach_record.add_argument("--proposal", required=True)
    coach_record.add_argument("--candidate-key", required=True)
    coach_record.add_argument("--base-contract-sha256", required=True)
    coach_record.add_argument("--diff", default="")
    coach_record.add_argument("--validation-receipt", default="")
    coach_record.add_argument(
        "--outcome", required=True, choices=tuple(sorted(COACH_INTERVENTION_OUTCOMES))
    )
    coach_record.add_argument("--actor", required=True)
    coach_record.add_argument("--reason", required=True)
    coach_history = commands.add_parser(
        "coach-history",
        help="List bounded intervention outcomes for one coach candidate.",
    )
    coach_history.add_argument("--repo", required=True)
    coach_history.add_argument("--candidate-key", required=True)
    coach_history.add_argument("--limit", type=_positive_argument, required=True)
    return parser


def _validate_high_cost_limits(args: argparse.Namespace) -> None:
    settings = ReviewAgentSettings.from_environment()
    uses_page_limit = args.command in {"list", "publications"} or (
        args.command == "runs" and not args.stats and not args.mark_stalled
    )
    if uses_page_limit:
        if args.limit > settings.operator_page_max_items:
            raise _MemoryCommandError(
                "invalid_command_input",
                exit_code=os.EX_USAGE,
            )
        return
    if args.command == "coach-history":
        maximum = min(
            settings.operator_page_max_items,
            operator_application.MAX_COACH_INTERVENTION_HISTORY_ITEMS,
        )
        if args.limit > maximum:
            raise _MemoryCommandError(
                "invalid_command_input",
                exit_code=os.EX_USAGE,
            )
        return
    requires_export = args.command == "export" or (
        args.command == "coach-run" and not args.export
    )
    if requires_export and args.row_limit > settings.operator_export_max_rows:
        raise _MemoryCommandError(
            "invalid_command_input",
            exit_code=os.EX_USAGE,
        )


def _offline_command(args: argparse.Namespace) -> int | None:
    if args.command == "learning-report":
        import review_agent_learning as learning
        report = learning.build_learning_report(
            _complete_export(args.export, repository=args.repo),
            repository=args.repo,
        )
        _write_or_print(learning.render_markdown(report), args.output)
        return 0
    if args.command == "validate-replay":
        import review_agent_replay as replay
        results = replay.validate_replay_path(args.path)
        for result in results:
            print(f"Replay OK: {result.fixture_id} ({result.path})")
        print(f"Validated {len(results)} replay fixture(s).")
        return 0
    if args.command == "coach-export":
        import review_agent_coach as coach
        payload = coach.build_coach_export(
            _complete_export(args.export, repository=args.repo),
            repository=args.repo,
            after_decision_id=args.after_decision_id,
            after_feedback_id=args.after_feedback_id,
            after_triage_id=args.after_triage_id,
            include_incomplete=args.include_incomplete,
        )
        _write_or_print(coach.dumps_coach_export(payload), args.output)
        return 0
    if args.command == "coach-propose":
        import review_agent_coach_proposals as proposals
        bundle = proposals.build_proposal(
            proposals.load_coach_export(Path(args.events)),
            max_candidates=args.max_candidates,
            min_independent_episodes=args.min_independent_episodes,
        )
        output_dir = Path(args.output_dir)
        write_private_file(
            output_dir / "proposal.json", proposals.dumps_proposal_bundle(bundle)
        )
        write_private_file(output_dir / "SUMMARY.md", proposals.render_markdown(bundle))
        print(output_dir)
        return 0
    if args.command == "coach-verify-proposal":
        import review_agent_coach_proposals as proposals
        bundle = proposals.load_proposal_bundle(Path(args.proposal))
        print(_json(proposals.verify_proposal_bundle(bundle).to_json_obj(), pretty=True))
        return 0
    return None


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
    if isinstance(exc, json.JSONDecodeError):
        _json_error(code="artifact_invalid", retryable=False)
        return os.EX_DATAERR
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


def _sha256_file(raw_path: str) -> str:
    if not raw_path:
        return ""
    path = Path(raw_path)
    if path.is_symlink() or not path.is_file():
        raise _MemoryCommandError("intervention_artifact_invalid")
    with path.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    return f"sha256:{digest}"


def _proposal_intervention(
    proposal_path: str,
    candidate_key: str,
) -> tuple[ProposalBundle, CandidateProposal, str]:
    import review_agent_coach_proposals as proposals

    bundle = proposals.load_proposal_bundle(Path(proposal_path))
    candidates = tuple(
        candidate
        for candidate in bundle.candidates
        if candidate.candidate_key == candidate_key
    )
    if len(candidates) != 1:
        raise _MemoryCommandError("coach_candidate_not_found")
    candidate = candidates[0]
    canonical = json.dumps(
        {
            "schema_version": bundle.schema_version,
            "proposal_set_id": bundle.proposal_set_id,
            "candidate_key": candidate.candidate_key,
            "target_owner": candidate.target_owner,
            "proposed_change": candidate.proposed_change,
            "required_validation": list(candidate.required_validation),
            "risk": candidate.risk,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return bundle, candidate, "sha256:" + hashlib.sha256(canonical).hexdigest()


def _run_live(args: argparse.Namespace, runtime: PostgreSQLRuntime) -> int:
    result: object
    if args.command == "list":
        result = operator_application.list_findings(
            runtime, repository=args.repo, limit=args.limit,
            include_suppressed=not args.open_only,
        )
    elif args.command == "show":
        result = operator_application.show_finding(
            runtime, repository=args.repo, fingerprint=args.fingerprint
        )
    elif args.command == "decide":
        result = operator_application.decide_finding(
            runtime,
            operator_application.OperatorDecisionRequest(
                repository=args.repo, fingerprint=args.fingerprint,
                decision=args.decision, reason=args.reason, actor=args.actor,
                occurrence_id=args.occurrence_id, pr_number=args.pr,
                local_reference=args.local_reference, latest=args.latest,
                expires_days=args.expires_days, adr_id=args.adr_id,
            ),
        )
    elif args.command == "triage-feedback":
        result = operator_application.triage_review_feedback(
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
        elif args.stats:
            result = operator_application.run_stats(
                runtime, repository=args.repo, days=args.days,
                stale_after_minutes=args.stale_after_minutes,
            )
        else:
            result = operator_application.list_runs(
                runtime, repository=args.repo, limit=args.limit,
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
    elif args.command == "verification-export":
        import review_agent_verification as verification
        source = operator_application.verification_export_source(
            runtime, run_id=args.run_id
        )
        payload = verification.build_verification_export(source, coverage=None)
        _write_or_print(verification.dumps_verification_export(payload), args.output)
        return 0
    elif args.command == "coach-run":
        if args.export:
            state = _complete_export(args.export, repository=args.repo)
        else:
            export = operator_application.export_repository(
                runtime, repository=args.repo, row_limit=args.row_limit
            )
            state = cast(dict[str, object], export.to_json_obj())
            if state.get("complete") is not True:
                raise _MemoryCommandError("coach_export_incomplete")
        artifacts = build_coach_run_artifacts(
            state=state, output_dir=Path(args.output_dir), repository=args.repo,
            after_decision_id=args.after_decision_id,
            after_feedback_id=args.after_feedback_id,
            after_triage_id=args.after_triage_id,
            include_incomplete=args.include_incomplete,
            max_candidates=args.max_candidates,
            min_independent_episodes=args.min_independent_episodes,
        )
        candidates = tuple(
            CoachCandidateInput(
                candidate_key=item.candidate_key, target_owner=item.target_owner,
                suggested_route=item.suggested_route, event_type=item.event_type,
                independent_episode_count=item.independent_episode_count,
                evidence_event_ids=item.evidence_event_ids,
                evidence_events_total=item.evidence_events_total,
            ) for item in artifacts.bundle.candidates
        )
        run = operator_application.record_coach_run(
            runtime, repository=artifacts.bundle.repository_untrusted,
            source_event_set_id=artifacts.bundle.source_event_set_id,
            source_snapshot_id=artifacts.bundle.source_snapshot_id,
            proposal_set_id=artifacts.bundle.proposal_set_id,
            events_considered=artifacts.bundle.events_considered,
            artifact_dir=str(artifacts.paths.output_dir), candidates=candidates,
        )
        result = {"run": run, "artifacts": artifacts.paths.to_json_obj()}
    elif args.command == "coach-record-outcome":
        bundle, candidate, proposal_content_hash = _proposal_intervention(
            args.proposal, args.candidate_key
        )
        result = operator_application.record_coach_intervention(
            runtime,
            operator_application.CoachInterventionOutcomeRequest(
                repository=bundle.repository_untrusted,
                proposal_set_id=bundle.proposal_set_id,
                candidate_key=candidate.candidate_key,
                target_owner=candidate.target_owner,
                proposal_content_hash=proposal_content_hash,
                base_contract_hash=args.base_contract_sha256,
                diff_hash=_sha256_file(args.diff),
                validation_receipt_hash=_sha256_file(args.validation_receipt),
                outcome=cast(CoachInterventionOutcome, args.outcome),
                reason=args.reason,
                actor=args.actor,
            ),
        )
    elif args.command == "coach-history":
        result = operator_application.coach_intervention_history(
            runtime,
            repository=args.repo,
            candidate_key=args.candidate_key,
            limit=args.limit,
        )
    else:
        raise RuntimeError("parser accepted an unsupported command")
    print(_json(result, pretty=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        _validate_high_cost_limits(args)
        offline = _offline_command(args)
    except Exception as exc:
        return _failure_exit(exc)
    if offline is not None:
        return offline
    try:
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
