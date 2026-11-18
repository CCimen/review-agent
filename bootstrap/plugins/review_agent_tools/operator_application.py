"""PostgreSQL application boundary for operator inspection and recovery."""

from __future__ import annotations

import psycopg
from psycopg.rows import TupleRow

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .domain.coaching import (
    CoachCandidateInput,
    CoachInterventionOutcome,
    CoachInterventionOutcomeInput,
    CoachRunInput,
    resolve_coach_run,
    resolve_intervention_outcome,
)
from .domain.feedback import resolve_repository
from .domain.review import JsonObject, ReviewRunId, ReviewStatus
from . import failure_codes, operator_input
from .review import run_application as review_run_application
from .github import app_auth, app_inventory
from .postgres import coaching as postgres_coaching
from .postgres import github_app as postgres_github_app
from .postgres import jobs as postgres_jobs
from .postgres import publications as postgres_publications
from .postgres import quality_reporting as postgres_quality_reporting
from .postgres import retention as postgres_retention
from .postgres import reporting as postgres_reporting
from .postgres import review_runs as postgres_review_runs
from .postgres.runtime import PostgreSQLRuntime
from .postgres import audit as postgres_audit, team_access, admin_operations


MAX_COACH_INTERVENTION_HISTORY_ITEMS = 100


@dataclass(frozen=True, slots=True)
class QueueHealth:
    review_queue: postgres_jobs.ReviewQueueHealth
    publication_queue: postgres_publications.PublicationQueueHealth
    progress: tuple[admin_operations.QueueStatus, ...]
    generated_at: datetime
    stale_after_seconds: int


@dataclass(frozen=True, slots=True)
class DeploymentHealth(QueueHealth):
    github_app: postgres_github_app.GitHubAppAccessHealth


@dataclass(frozen=True, slots=True)
class RetentionReceipt:
    result: postgres_retention.RetentionResult
    actor: str
    reason: str


@dataclass(frozen=True, slots=True)
class RepositoryOnboardingResult:
    reconciliation: postgres_github_app.InstallationReconciliationResult
    access: postgres_github_app.RepositoryAccessState


def queue_health(runtime: PostgreSQLRuntime) -> QueueHealth:
    """Read queue counters and progress through the shared operational reader."""
    with runtime.transaction() as connection:
        return _queue_health(connection)


def _queue_health(connection: psycopg.Connection[TupleRow]) -> QueueHealth:
    now = datetime.now(timezone.utc)
    progress = admin_operations.queue_status(connection, now=now)
    kinds = {queue.kind: queue for queue in progress}
    reviews, publications = kinds["review"], kinds["publisher"]
    return QueueHealth(
        review_queue=postgres_jobs.ReviewQueueHealth(
            active=reviews.waiting + reviews.leased + reviews.awaiting_publication,
            queued=reviews.waiting,
            leased=reviews.leased,
            expired_leases=reviews.expired_leases,
            dead_letters=reviews.dead_letters,
        ),
        publication_queue=postgres_publications.PublicationQueueHealth(
            pending=publications.waiting,
            posting=publications.leased,
            expired_recoverable=publications.expired_leases - publications.expired_exhausted,
            expired_exhausted=publications.expired_exhausted,
        ),
        progress=progress,
        generated_at=now,
        stale_after_seconds=admin_operations.WORKER_STALE_SECONDS,
    )


def deployment_health(runtime: PostgreSQLRuntime, *, profile: str) -> DeploymentHealth:
    """Read one consistent, set-oriented operator health snapshot."""
    with runtime.transaction() as connection:
        queues = _queue_health(connection)
        return DeploymentHealth(
            github_app=postgres_github_app.access_health(
                connection,
                profile_key=profile,
            ),
            review_queue=queues.review_queue,
            publication_queue=queues.publication_queue,
            progress=queues.progress,
            generated_at=queues.generated_at,
            stale_after_seconds=queues.stale_after_seconds,
        )


@dataclass(frozen=True, slots=True)
class CoachInterventionOutcomeRequest:
    repository: str
    proposal_set_id: str
    candidate_key: str
    target_owner: str
    proposal_content_hash: str
    base_contract_hash: str
    diff_hash: str
    validation_receipt_hash: str
    outcome: CoachInterventionOutcome
    reason: str
    actor: str


@dataclass(frozen=True, slots=True)
class StaleRunsResult:
    older_than_minutes: int
    cutoff: datetime
    runs: tuple[postgres_reporting.ReviewRunReport, ...]

    @property
    def failed_count(self) -> int:
        return len(self.runs)


ACTIVE_JOB_STATUSES = (
    postgres_jobs.ReviewJobStatus.QUEUED,
    postgres_jobs.ReviewJobStatus.LEASED,
)


def _console_admin_access(
    connection: psycopg.Connection[TupleRow],
    access: team_access.AccessRequest | None,
    *,
    write: bool = False,
) -> team_access.AccessScope | None:
    if access is None:
        return None
    scope = team_access.resolve_scope(connection, access, write=write)
    team_access.require_admin(scope)
    return scope


def _record_access_change(
    connection: psycopg.Connection[TupleRow],
    scope: team_access.AccessScope | None,
    *,
    operation: str,
    subject: str,
    reason: str,
    repository_id: int | None = None,
) -> None:
    if scope is not None:
        postgres_audit.record(
            connection,
            scope,
            action=postgres_audit.AuditAction.ACCESS_UPDATED,
            subject=subject,
            reason=reason,
            details={"operation": operation},
            repository_id=repository_id,
        )


def list_github_app_installations(
    runtime: PostgreSQLRuntime,
    *,
    limit: int,
    after_provider_installation_id: int = 0,
    access_request: team_access.AccessRequest | None = None,
) -> tuple[postgres_github_app.GitHubAppInstallation, ...]:
    """Return one stable installation inventory page for operators."""
    with runtime.transaction() as connection:
        _console_admin_access(connection, access_request)
        return postgres_github_app.list_installations(
            connection,
            limit=limit,
            after_provider_installation_id=after_provider_installation_id,
        )


def list_github_app_repositories(
    runtime: PostgreSQLRuntime,
    *,
    limit: int,
    after_provider_repository_id: int = 0,
    access_request: team_access.AccessRequest | None = None,
) -> tuple[postgres_github_app.RepositoryAccessState, ...]:
    """Return one stable repository access inventory page for operators."""
    with runtime.transaction() as connection:
        _console_admin_access(connection, access_request)
        return postgres_github_app.list_repository_access(
            connection,
            limit=limit,
            after_provider_repository_id=after_provider_repository_id,
        )


def sync_github_app_installation(
    runtime: PostgreSQLRuntime,
    authenticator: app_auth.GitHubAppAuthenticator,
    *,
    provider_installation_id: int,
    actor: str,
    reason: str,
    now: datetime | None = None,
    access_request: team_access.AccessRequest | None = None,
) -> postgres_github_app.InstallationReconciliationResult:
    """Fetch a complete provider snapshot, then reconcile it in one transaction."""
    installation_id = operator_input.positive(
        provider_installation_id, field="provider_installation_id"
    )
    inventory = app_inventory.read_installation_inventory(
        authenticator,
        provider_installation_id=installation_id,
        now=now,
    )
    with runtime.transaction() as connection:
        scope = _console_admin_access(connection, access_request, write=True)
        actor = scope.actor if scope is not None else actor
        result = postgres_github_app.reconcile_selected_installation(
            connection,
            definition=inventory.definition,
            status=inventory.status,
            repositories=inventory.repositories,
            actor=actor,
            reason=reason,
        )

        _record_access_change(
            connection,
            scope,
            operation="installation_synced",
            subject=str(provider_installation_id),
            reason=reason,
        )
        return result


def approve_github_app_installation(
    runtime: PostgreSQLRuntime,
    authenticator: app_auth.GitHubAppAuthenticator,
    *,
    provider_installation_id: int,
    policy: postgres_github_app.RepositoryActivationPolicy,
    actor: str,
    reason: str,
    now: datetime | None = None,
    access_request: team_access.AccessRequest | None = None,
) -> postgres_github_app.GitHubAppInstallation:
    """Approve or restrict lazy repository activation for one installation."""
    installation_id = operator_input.positive(
        provider_installation_id, field="provider_installation_id"
    )
    metadata = app_inventory.read_installation_metadata(
        authenticator,
        provider_installation_id=installation_id,
        now=now,
    )
    with runtime.transaction() as connection:
        scope = _console_admin_access(connection, access_request, write=True)
        actor = scope.actor if scope is not None else actor
        installation = postgres_github_app.sync_installation(
            connection, metadata.definition
        )
        if installation.status is not metadata.status:
            installation = postgres_github_app.set_installation_status(
                connection,
                installation_id=installation.id,
                status=metadata.status,
                actor=actor,
                reason=reason,
            )
        result = postgres_github_app.set_repository_activation_policy(
            connection,
            installation_id=installation.id,
            policy=policy,
            actor=actor,
            reason=reason,
        )

        _record_access_change(
            connection,
            scope,
            operation="activation_policy_changed",
            subject=str(provider_installation_id),
            reason=reason,
        )
        return result


def enable_github_app_repository(
    runtime: PostgreSQLRuntime,
    *,
    provider_repository_id: int,
    profile: str,
    actor: str,
    reason: str,
    access_request: team_access.AccessRequest | None = None,
) -> postgres_github_app.RepositoryAccessState:
    """Enable direct-App admission by GitHub's stable repository ID."""
    with runtime.transaction() as connection:
        scope = _console_admin_access(connection, access_request, write=True)
        actor = scope.actor if scope is not None else actor
        current = postgres_github_app.get_repository_access_by_provider_id(
            connection,
            operator_input.positive(provider_repository_id, field="provider_repository_id"),
        )
        result = postgres_github_app.enable_repository(
            connection,
            repository_id=current.repository_id,
            profile_key=profile,
            trigger_mode=postgres_github_app.TriggerMode.MANUAL,
            actor=actor,
            reason=reason,
        )

        _record_access_change(
            connection,
            scope,
            operation="repository_enabled",
            subject=str(provider_repository_id),
            reason=reason,
            repository_id=int(current.repository_id),
        )
        return result


def onboard_github_app_repository(
    runtime: PostgreSQLRuntime,
    authenticator: app_auth.GitHubAppAuthenticator,
    *,
    repository: str,
    profile: str,
    actor: str,
    reason: str,
    access_request: team_access.AccessRequest | None = None,
) -> RepositoryOnboardingResult:
    """Verify and enable exactly one repository in either installation scope."""
    with runtime.transaction() as connection:
        _console_admin_access(connection, access_request)
        observation = postgres_github_app.observe_repository_access(
            connection, repository=repository
        )
    inventory = app_inventory.read_repository_inventory(
        authenticator, repository=repository
    )
    metadata = inventory.installation
    with runtime.transaction() as connection:
        scope = _console_admin_access(connection, access_request, write=True)
        actor = scope.actor if scope is not None else actor
        installation, access = postgres_github_app.accept_verified_repository(
            connection,
            definition=metadata.definition,
            status=metadata.status,
            repository=inventory.repository,
            observation=observation,
            profile=profile,
            actor=actor,
            reason=reason,
        )
        _record_access_change(
            connection,
            scope,
            operation="repository_onboarded",
            subject=repository,
            reason=reason,
            repository_id=int(access.repository_id),
        )
    return RepositoryOnboardingResult(
        reconciliation=postgres_github_app.InstallationReconciliationResult(
            installation=installation,
            repositories_seen=1,
            repositories_removed=0,
            repositories_enabled=1,
        ),
        access=access,
    )


def disable_github_app_repository(
    runtime: PostgreSQLRuntime,
    *,
    provider_repository_id: int,
    actor: str,
    reason: str,
    access_request: team_access.AccessRequest | None = None,
) -> postgres_github_app.RepositoryAccessState:
    """Disable direct-App admission by GitHub's stable repository ID."""
    with runtime.transaction() as connection:
        scope = _console_admin_access(connection, access_request, write=True)
        actor = scope.actor if scope is not None else actor
        current = postgres_github_app.get_repository_access_by_provider_id(
            connection,
            operator_input.positive(provider_repository_id, field="provider_repository_id"),
        )
        result = postgres_github_app.disable_repository(
            connection,
            repository_id=current.repository_id,
            actor=actor,
            reason=reason,
        )

        _record_access_change(
            connection,
            scope,
            operation="repository_disabled",
            subject=str(provider_repository_id),
            reason=reason,
            repository_id=int(current.repository_id),
        )
        return result


def _nonnegative(value: int, *, field: str) -> int:
    if isinstance(value, bool) or value < 0:
        raise operator_input.OperatorInputError(f"{field} must be zero or greater")
    return value


def _repository(value: str | None) -> str | None:
    return resolve_repository(value) if value is not None else None


def _audit_text(value: str, *, field: str, maximum: int) -> str:
    normalized = " ".join(value.strip().split())
    if not normalized or len(normalized) > maximum:
        raise operator_input.OperatorInputError(f"{field} must be 1 to {maximum} characters")
    return normalized


def prune_receipt_history(
    runtime: PostgreSQLRuntime,
    *,
    target: postgres_retention.RetentionTarget,
    before: datetime,
    limit: int,
    apply: bool,
    actor: str,
    reason: str,
) -> RetentionReceipt:
    """Preview or apply one explicitly approved receipt batch."""
    cutoff = operator_input.now(before)
    batch_limit = operator_input.positive(limit, field="limit")
    normalized_actor = _audit_text(actor, field="actor", maximum=120)
    normalized_reason = _audit_text(reason, field="reason", maximum=500)
    with runtime.transaction() as connection:
        result = postgres_retention.prune_receipts(
            connection,
            target=target,
            before=cutoff,
            limit=batch_limit,
            apply=apply,
        )
    return RetentionReceipt(
        result=result,
        actor=normalized_actor,
        reason=normalized_reason,
    )


def list_findings(
    runtime: PostgreSQLRuntime,
    *,
    repository: str | None,
    limit: int,
    include_suppressed: bool,
    now: datetime | None = None,
) -> tuple[postgres_reporting.FindingReport, ...]:
    normalized_repository = _repository(repository)
    row_limit = operator_input.positive(limit, field="limit")
    moment = operator_input.now(now)
    with runtime.transaction() as connection:
        return postgres_reporting.list_findings(
            connection,
            repository=normalized_repository,
            limit=row_limit,
            include_suppressed=include_suppressed,
            now=moment,
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
        raise operator_input.OperatorInputError("expiring_within_days must be zero or greater")
    moment = operator_input.now(now)
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
    budget = operator_input.positive(row_limit, field="row_limit")
    moment = operator_input.now(now)
    with runtime.transaction() as connection:
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
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
    pr_number: int | None = None,
) -> tuple[postgres_reporting.ReviewRunReport, ...]:
    normalized_repository = _repository(repository)
    normalized_pr = operator_input.positive(pr_number, field="pr_number") if pr_number is not None else None
    row_limit = operator_input.positive(limit, field="limit")
    with runtime.transaction() as connection:
        return postgres_reporting.list_runs(
            connection,
            repository=normalized_repository,
            limit=row_limit,
            failed_only=failed_only,
            pr_number=normalized_pr,
        )


def list_review_jobs(
    runtime: PostgreSQLRuntime,
    *,
    statuses: tuple[postgres_jobs.ReviewJobStatus, ...] = ACTIVE_JOB_STATUSES,
    limit: int,
) -> tuple[postgres_jobs.ReviewJobReport, ...]:
    """Return one bounded queue snapshot for operator inspection."""
    with runtime.transaction() as connection:
        return postgres_jobs.list_jobs(
            connection,
            statuses=statuses,
            limit=operator_input.positive(limit, field="limit"),
        )


def retry_review_job(
    runtime: PostgreSQLRuntime, *, job_id: int
) -> postgres_jobs.ReviewJob:
    """Release a delayed queued retry without changing its attempt fence."""
    with runtime.transaction() as connection:
        return postgres_jobs.retry_queued_job(
            connection, job_id=operator_input.positive(job_id, field="job_id")
        )


def cancel_review_job(
    runtime: PostgreSQLRuntime, *, job_id: int
) -> postgres_jobs.ReviewJob:
    """Fail the active owning run so queued and leased work loses authority."""
    resolved_job_id = operator_input.positive(job_id, field="job_id")
    with runtime.transaction() as connection:
        current = postgres_jobs.get_job(connection, resolved_job_id)
        run = postgres_review_runs.lock_run(connection, current.review_run_id)
        if run.status is ReviewStatus.RUNNING:
            review_run_application.fail_run_in_transaction(
                connection,
                run.id,
                failure_code=failure_codes.OPERATOR_CANCELLED,
            )
        return postgres_jobs.get_job(connection, resolved_job_id)


def run_stats(
    runtime: PostgreSQLRuntime,
    *,
    repository: str | None,
    days: int,
    stale_after_minutes: int,
    now: datetime | None = None,
    pr_number: int | None = None,
) -> postgres_reporting.RunStats:
    normalized_repository = _repository(repository)
    normalized_pr = operator_input.positive(pr_number, field="pr_number") if pr_number is not None else None
    window_days = operator_input.positive(days, field="days")
    stale_minutes = operator_input.positive(stale_after_minutes, field="stale_after_minutes")
    moment = operator_input.now(now)
    with runtime.transaction() as connection:
        return postgres_reporting.run_stats(
            connection,
            repository=normalized_repository,
            pr_number=normalized_pr,
            since=moment - timedelta(days=window_days),
            stale_before=moment - timedelta(minutes=stale_minutes),
            window_days=window_days,
            now=moment,
        )


def quality_report(
    runtime: PostgreSQLRuntime,
    *,
    repository: str | None,
    days: int,
    now: datetime | None = None,
) -> postgres_quality_reporting.QualityReport:
    """Build a bounded report from explicit review-quality signals."""
    normalized_repository = _repository(repository)
    window_days = operator_input.positive(days, field="days")
    moment = operator_input.now(now)
    with runtime.transaction() as connection:
        return postgres_quality_reporting.build_report(
            connection,
            repository=normalized_repository,
            window_started_at=moment - timedelta(days=window_days),
            window_ended_at=moment,
            window_days=window_days,
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
        raise operator_input.OperatorInputError("pr_number requires repository scope")
    normalized_pr = (
        operator_input.positive(pr_number, field="pr_number") if pr_number is not None else None
    )
    age = operator_input.positive(older_than_minutes, field="older_than_minutes")
    moment = operator_input.now(now)
    cutoff = moment - timedelta(minutes=age)
    with runtime.transaction() as connection:
        run_ids = review_run_application.mark_stale_runs_failed_in_transaction(
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


def list_publications(
    runtime: PostgreSQLRuntime,
    *,
    repository: str | None,
    pr_number: int | None,
    limit: int,
) -> tuple[postgres_reporting.PublicationReport, ...]:
    normalized_repository = _repository(repository)
    if pr_number is not None and normalized_repository is None:
        raise operator_input.OperatorInputError("pr_number requires repository scope")
    normalized_pr = (
        operator_input.positive(pr_number, field="pr_number") if pr_number is not None else None
    )
    row_limit = operator_input.positive(limit, field="limit")
    with runtime.transaction() as connection:
        return postgres_reporting.list_publications(
            connection,
            repository=normalized_repository,
            pr_number=normalized_pr,
            limit=row_limit,
        )


def run_diagnostics(
    runtime: PostgreSQLRuntime, *, run_id: int
) -> postgres_reporting.RunDiagnostics:
    normalized_run_id = ReviewRunId(operator_input.positive(run_id, field="run_id"))
    with runtime.transaction() as connection:
        return postgres_reporting.run_diagnostics(connection, run_id=normalized_run_id)


def coverage(
    runtime: PostgreSQLRuntime, *, run_id: int
) -> postgres_reporting.CoverageReport:
    normalized_run_id = ReviewRunId(operator_input.positive(run_id, field="run_id"))
    with runtime.transaction() as connection:
        return postgres_reporting.coverage_report(connection, run_id=normalized_run_id)


def verification_export_source(
    runtime: PostgreSQLRuntime, *, run_id: int
) -> JsonObject:
    normalized_run_id = ReviewRunId(operator_input.positive(run_id, field="run_id"))
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


def record_coach_intervention(
    runtime: PostgreSQLRuntime,
    request: CoachInterventionOutcomeRequest,
) -> postgres_coaching.CoachInterventionOutcomeRecord:
    """Append one evaluated intervention for an exact verified coach candidate."""
    repository = resolve_repository(request.repository)
    with runtime.transaction() as connection:
        candidate = postgres_coaching.resolve_intervention_candidate(
            connection,
            repository=repository,
            proposal_set_id=request.proposal_set_id,
            candidate_key=request.candidate_key,
            target_owner=request.target_owner,
        )
        definition = resolve_intervention_outcome(
            CoachInterventionOutcomeInput(
                coach_candidate_id=int(candidate.id),
                candidate_key=candidate.candidate_key,
                target_owner=candidate.target_owner,
                proposal_content_hash=request.proposal_content_hash,
                base_contract_hash=request.base_contract_hash,
                diff_hash=request.diff_hash,
                validation_receipt_hash=request.validation_receipt_hash,
                outcome=request.outcome,
                reason=request.reason,
                actor=request.actor,
            )
        )
        return postgres_coaching.record_intervention_outcome(connection, definition)


def coach_intervention_history(
    runtime: PostgreSQLRuntime,
    *,
    repository: str,
    candidate_key: str,
    limit: int,
) -> postgres_coaching.CoachInterventionHistory:
    """Read one explicitly bounded private intervention history."""
    normalized_repository = resolve_repository(repository)
    normalized_candidate_key = " ".join(candidate_key.strip().split())
    if not normalized_candidate_key or len(normalized_candidate_key) > 160:
        raise operator_input.OperatorInputError("candidate_key must be 1 to 160 characters")
    row_limit = operator_input.positive(limit, field="limit")
    if row_limit > MAX_COACH_INTERVENTION_HISTORY_ITEMS:
        raise operator_input.OperatorInputError(
            "limit must not exceed "
            f"{MAX_COACH_INTERVENTION_HISTORY_ITEMS} intervention outcomes"
        )
    with runtime.transaction() as connection:
        return postgres_coaching.intervention_history(
            connection,
            repository=normalized_repository,
            candidate_key=normalized_candidate_key,
            limit=row_limit,
        )
