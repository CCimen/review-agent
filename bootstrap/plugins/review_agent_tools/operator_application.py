"""PostgreSQL application boundary for operator inspection and recovery."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .domain.coaching import CoachCandidateInput, CoachRunInput, resolve_coach_run
from .domain.feedback import resolve_positive_int, resolve_repository
from .domain.finding import (
    DecisionKind,
    FindingDecisionId,
    FindingDomainError,
    FindingOccurrenceId,
    require_explicit_decision_target,
    resolve_decision,
    resolve_fingerprint_query,
)
from .domain.review import JsonObject, ReviewRunId
from .postgres import coaching as postgres_coaching
from .postgres import decisions as postgres_decisions
from .postgres import findings as postgres_findings
from .postgres import reporting as postgres_reporting
from .postgres import review_runs as postgres_review_runs
from .postgres.runtime import PostgreSQLRuntime


class OperatorInputError(ValueError):
    """An operator request is ambiguous or unsafe to execute."""


@dataclass(frozen=True, slots=True)
class OperatorDecisionRequest:
    repository: str
    fingerprint: str
    decision: str
    reason: str
    actor: str
    occurrence_id: int | None = None
    pr_number: int | None = None
    local_reference: str = ""
    latest: bool = False
    expires_days: int | None = None
    adr_id: str = ""


@dataclass(frozen=True, slots=True)
class OperatorDecisionResult:
    id: FindingDecisionId
    fingerprint: str
    occurrence_id: FindingOccurrenceId
    decision: DecisionKind
    reason: str
    actor: str
    context_hash: str
    adr_id: str | None
    created_at: datetime
    expires_at: datetime | None


@dataclass(frozen=True, slots=True)
class StaleRunsResult:
    older_than_minutes: int
    cutoff: datetime
    runs: tuple[postgres_reporting.ReviewRunReport, ...]

    @property
    def failed_count(self) -> int:
        return len(self.runs)


@dataclass(frozen=True, slots=True)
class FailureStatusQueue:
    marked: StaleRunsResult
    targets: tuple[postgres_review_runs.FailureStatusTarget, ...]


def _now(value: datetime | None) -> datetime:
    moment = value or datetime.now(timezone.utc)
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise OperatorInputError("now must include a timezone")
    return moment


def _positive(value: int, *, field: str) -> int:
    try:
        return resolve_positive_int(value, field=field)
    except ValueError as exc:
        raise OperatorInputError(str(exc)) from exc


def _nonnegative(value: int, *, field: str) -> int:
    if isinstance(value, bool) or value < 0:
        raise OperatorInputError(f"{field} must be zero or greater")
    return value


def _repository(value: str | None) -> str | None:
    return resolve_repository(value) if value is not None else None


def list_findings(
    runtime: PostgreSQLRuntime,
    *,
    repository: str | None,
    limit: int,
    include_suppressed: bool,
    now: datetime | None = None,
) -> tuple[postgres_reporting.FindingReport, ...]:
    normalized_repository = _repository(repository)
    row_limit = _positive(limit, field="limit")
    moment = _now(now)
    with runtime.transaction() as connection:
        return postgres_reporting.list_findings(
            connection,
            repository=normalized_repository,
            limit=row_limit,
            include_suppressed=include_suppressed,
            now=moment,
        )


def show_finding(
    runtime: PostgreSQLRuntime,
    *,
    repository: str,
    fingerprint: str,
    now: datetime | None = None,
) -> postgres_reporting.FindingDetail:
    normalized_repository = resolve_repository(repository)
    query = resolve_fingerprint_query(fingerprint)
    moment = _now(now)
    with runtime.transaction() as connection:
        scope = postgres_reporting.repository_scope(
            connection, repository=normalized_repository
        )
        resolved = postgres_findings.resolve_fingerprint(
            connection,
            repository_id=scope.id,
            query=query,
        )
        return postgres_reporting.finding_detail(
            connection,
            repository_id=scope.id,
            fingerprint=resolved,
            now=moment,
        )


def decide_finding(
    runtime: PostgreSQLRuntime,
    request: OperatorDecisionRequest,
    *,
    now: datetime | None = None,
) -> OperatorDecisionResult:
    repository = resolve_repository(request.repository)
    query = resolve_fingerprint_query(request.fingerprint)
    moment = _now(now)
    definition = resolve_decision(
        decision=request.decision,
        reason=request.reason,
        actor=request.actor,
        adr_id=request.adr_id,
        expires_days=request.expires_days,
        now=moment,
    )
    occurrence_id = (
        FindingOccurrenceId(_positive(request.occurrence_id, field="occurrence_id"))
        if request.occurrence_id is not None
        else None
    )
    pr_number = (
        _positive(request.pr_number, field="pr_number")
        if request.pr_number is not None
        else None
    )
    local_reference = request.local_reference.strip() or None
    try:
        require_explicit_decision_target(
            occurrence_id=occurrence_id,
            pr_number=pr_number,
            local_reference=local_reference,
            latest=request.latest,
        )
    except FindingDomainError as exc:
        raise OperatorInputError(str(exc)) from exc
    with runtime.transaction() as connection:
        scope = postgres_reporting.repository_scope(
            connection, repository=repository
        )
        fingerprint = postgres_findings.resolve_fingerprint(
            connection,
            repository_id=scope.id,
            query=query,
        )
        target = postgres_reporting.decision_target(
            connection,
            repository_id=scope.id,
            fingerprint=fingerprint,
            occurrence_id=occurrence_id,
            pr_number=pr_number,
            local_reference=local_reference,
            latest=request.latest,
        )
        stored = postgres_decisions.append_operator_decision(
            connection,
            finding_id=target.finding_id,
            occurrence_id=target.occurrence_id,
            definition=definition,
        )
    return OperatorDecisionResult(
        id=stored.id,
        fingerprint=fingerprint,
        occurrence_id=target.occurrence_id,
        decision=stored.decision,
        reason=stored.reason,
        actor=stored.actor,
        context_hash=stored.context_hash,
        adr_id=stored.adr_id,
        created_at=stored.created_at,
        expires_at=stored.expires_at,
    )


def finding_stats(
    runtime: PostgreSQLRuntime,
    *,
    repository: str | None,
    expiring_within_days: int,
    now: datetime | None = None,
) -> postgres_reporting.FindingStats:
    normalized_repository = _repository(repository)
    if isinstance(expiring_within_days, bool) or expiring_within_days < 0:
        raise OperatorInputError("expiring_within_days must be zero or greater")
    moment = _now(now)
    with runtime.transaction() as connection:
        return postgres_reporting.finding_stats(
            connection,
            repository=normalized_repository,
            expiring_at=moment + timedelta(days=expiring_within_days),
            expiring_within_days=expiring_within_days,
            now=moment,
        )


def export_repository(
    runtime: PostgreSQLRuntime,
    *,
    repository: str,
    row_limit: int,
    now: datetime | None = None,
) -> postgres_reporting.RepositoryExport:
    normalized_repository = resolve_repository(repository)
    budget = _positive(row_limit, field="row_limit")
    moment = _now(now)
    with runtime.transaction() as connection:
        connection.execute(
            "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
        )
        scope = postgres_reporting.repository_scope(
            connection, repository=normalized_repository
        )
        return postgres_reporting.export_repository(
            connection,
            scope=scope,
            row_limit=budget,
            now=moment,
        )


def list_runs(
    runtime: PostgreSQLRuntime,
    *,
    repository: str | None,
    limit: int,
    failed_only: bool = False,
) -> tuple[postgres_reporting.ReviewRunReport, ...]:
    normalized_repository = _repository(repository)
    row_limit = _positive(limit, field="limit")
    with runtime.transaction() as connection:
        return postgres_reporting.list_runs(
            connection,
            repository=normalized_repository,
            limit=row_limit,
            failed_only=failed_only,
        )


def run_stats(
    runtime: PostgreSQLRuntime,
    *,
    repository: str | None,
    days: int,
    stale_after_minutes: int,
    now: datetime | None = None,
) -> postgres_reporting.RunStats:
    normalized_repository = _repository(repository)
    window_days = _positive(days, field="days")
    stale_minutes = _positive(stale_after_minutes, field="stale_after_minutes")
    moment = _now(now)
    with runtime.transaction() as connection:
        return postgres_reporting.run_stats(
            connection,
            repository=normalized_repository,
            since=moment - timedelta(days=window_days),
            stale_before=moment - timedelta(minutes=stale_minutes),
            window_days=window_days,
            now=moment,
        )


def mark_stalled_runs(
    runtime: PostgreSQLRuntime,
    *,
    repository: str | None,
    pr_number: int | None,
    older_than_minutes: int,
    now: datetime | None = None,
) -> StaleRunsResult:
    normalized_repository = _repository(repository)
    if pr_number is not None and normalized_repository is None:
        raise OperatorInputError("pr_number requires repository scope")
    normalized_pr = (
        _positive(pr_number, field="pr_number") if pr_number is not None else None
    )
    age = _positive(older_than_minutes, field="older_than_minutes")
    moment = _now(now)
    cutoff = moment - timedelta(minutes=age)
    with runtime.transaction() as connection:
        run_ids = postgres_review_runs.mark_stale_runs_failed(
            connection,
            cutoff=cutoff,
            repository=normalized_repository,
            pr_number=normalized_pr,
        )
        reports = postgres_reporting.runs_by_id(
            connection,
            run_ids=run_ids,
        )
    return StaleRunsResult(
        older_than_minutes=age,
        cutoff=cutoff,
        runs=reports,
    )


def prepare_failure_status_queue(
    runtime: PostgreSQLRuntime,
    *,
    repository: str | None,
    pr_number: int | None,
    older_than_minutes: int,
    limit: int = 100,
    now: datetime | None = None,
) -> FailureStatusQueue:
    """Fail stale runs, then return the bounded external-delivery queue."""
    marked = mark_stalled_runs(
        runtime,
        repository=repository,
        pr_number=pr_number,
        older_than_minutes=older_than_minutes,
        now=now,
    )
    normalized_repository = _repository(repository)
    normalized_pr = (
        _positive(pr_number, field="pr_number") if pr_number is not None else None
    )
    with runtime.transaction() as connection:
        targets = postgres_review_runs.failed_runs_needing_status(
            connection,
            repository=normalized_repository,
            pr_number=normalized_pr,
            limit=_positive(limit, field="limit"),
        )
    return FailureStatusQueue(marked=marked, targets=targets)


def list_publications(
    runtime: PostgreSQLRuntime,
    *,
    repository: str | None,
    pr_number: int | None,
    limit: int,
) -> tuple[postgres_reporting.PublicationReport, ...]:
    normalized_repository = _repository(repository)
    if pr_number is not None and normalized_repository is None:
        raise OperatorInputError("pr_number requires repository scope")
    normalized_pr = (
        _positive(pr_number, field="pr_number") if pr_number is not None else None
    )
    row_limit = _positive(limit, field="limit")
    with runtime.transaction() as connection:
        return postgres_reporting.list_publications(
            connection,
            repository=normalized_repository,
            pr_number=normalized_pr,
            limit=row_limit,
        )


def coverage(
    runtime: PostgreSQLRuntime, *, run_id: int
) -> postgres_reporting.CoverageReport:
    normalized_run_id = ReviewRunId(_positive(run_id, field="run_id"))
    with runtime.transaction() as connection:
        return postgres_reporting.coverage_report(
            connection, run_id=normalized_run_id
        )


def verification_export_source(
    runtime: PostgreSQLRuntime, *, run_id: int
) -> JsonObject:
    normalized_run_id = ReviewRunId(_positive(run_id, field="run_id"))
    with runtime.transaction() as connection:
        return postgres_reporting.verification_export_source(
            connection, run_id=normalized_run_id
        )


def record_coach_run(
    runtime: PostgreSQLRuntime,
    *,
    repository: str,
    source_event_set_id: str,
    source_snapshot_id: str,
    proposal_set_id: str,
    events_considered: int,
    artifact_dir: str,
    candidates: tuple[CoachCandidateInput, ...],
) -> postgres_coaching.CoachRun:
    normalized_repository = resolve_repository(repository)
    definition = resolve_coach_run(
        CoachRunInput(
            repository=normalized_repository,
            source_event_set_id=source_event_set_id,
            source_snapshot_id=source_snapshot_id,
            proposal_set_id=proposal_set_id,
            decision="propose" if candidates else "no_change",
            events_considered=_nonnegative(
                events_considered, field="events_considered"
            ),
            artifact_dir=artifact_dir,
            candidates=candidates,
        )
    )
    with runtime.transaction() as connection:
        repository_id = postgres_reporting.repository_scope(
            connection, repository=normalized_repository
        ).id
        return postgres_coaching.record_run(
            connection,
            repository_id=repository_id,
            definition=definition,
        )
