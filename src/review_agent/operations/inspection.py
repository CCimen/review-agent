"""PostgreSQL application boundary for operator inspection and recovery."""

from __future__ import annotations

import psycopg
from psycopg.rows import TupleRow

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ..domain.feedback import resolve_repository
from ..domain.review import ReviewRunId, ReviewStatus
from ..domain import failure_codes, input_checks
from ..review import runs as review_runs
from ..github import app_auth, app_inventory
from ..postgres import github_app as postgres_github_app
from ..postgres import jobs as postgres_jobs
from ..postgres import publications as postgres_publications
from ..postgres import quality_report as postgres_quality_report
from ..postgres import retention as postgres_retention
from ..postgres import reporting as postgres_reporting
from ..postgres import review_runs as postgres_review_runs
from ..postgres.runtime import PostgreSQLRuntime
from ..postgres import audit as postgres_audit, team_access, operations as postgres_operations


@dataclass(frozen=True, slots=True)
class QueueHealth:
    review_queue: postgres_jobs.ReviewQueueHealth
    publication_queue: postgres_publications.PublicationQueueHealth
    progress: tuple[postgres_operations.QueueStatus, ...]
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
    progress = postgres_operations.queue_status(connection, now=now)
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
        stale_after_seconds=postgres_operations.WORKER_STALE_SECONDS,
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
    installation_id = input_checks.positive(
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
    installation_id = input_checks.positive(
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
            input_checks.positive(provider_repository_id, field="provider_repository_id"),
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
            input_checks.positive(provider_repository_id, field="provider_repository_id"),
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


def _repository(value: str | None) -> str | None:
    return resolve_repository(value) if value is not None else None


def _audit_text(value: str, *, field: str, maximum: int) -> str:
    normalized = " ".join(value.strip().split())
    if not normalized or len(normalized) > maximum:
        raise input_checks.OperatorInputError(f"{field} must be 1 to {maximum} characters")
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
    cutoff = input_checks.now(before)
    batch_limit = input_checks.positive(limit, field="limit")
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
    row_limit = input_checks.positive(limit, field="limit")
    moment = input_checks.now(now)
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
        raise input_checks.OperatorInputError("expiring_within_days must be zero or greater")
    moment = input_checks.now(now)
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
    budget = input_checks.positive(row_limit, field="row_limit")
    moment = input_checks.now(now)
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
    normalized_pr = input_checks.positive(pr_number, field="pr_number") if pr_number is not None else None
    row_limit = input_checks.positive(limit, field="limit")
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
            limit=input_checks.positive(limit, field="limit"),
        )


def retry_review_job(
    runtime: PostgreSQLRuntime, *, job_id: int
) -> postgres_jobs.ReviewJob:
    """Release a delayed queued retry without changing its attempt fence."""
    with runtime.transaction() as connection:
        return postgres_jobs.retry_queued_job(
            connection, job_id=input_checks.positive(job_id, field="job_id")
        )


def cancel_review_job(
    runtime: PostgreSQLRuntime, *, job_id: int
) -> postgres_jobs.ReviewJob:
    """Fail the active owning run so queued and leased work loses authority."""
    resolved_job_id = input_checks.positive(job_id, field="job_id")
    with runtime.transaction() as connection:
        current = postgres_jobs.get_job(connection, resolved_job_id)
        run = postgres_review_runs.lock_run(connection, current.review_run_id)
        if run.status is ReviewStatus.RUNNING:
            review_runs.fail_run_in_transaction(
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
    normalized_pr = input_checks.positive(pr_number, field="pr_number") if pr_number is not None else None
    window_days = input_checks.positive(days, field="days")
    stale_minutes = input_checks.positive(stale_after_minutes, field="stale_after_minutes")
    moment = input_checks.now(now)
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
) -> postgres_quality_report.QualityReport:
    """Build a bounded report from explicit review-quality signals."""
    normalized_repository = _repository(repository)
    window_days = input_checks.positive(days, field="days")
    moment = input_checks.now(now)
    with runtime.transaction() as connection:
        return postgres_quality_report.build_report(
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
        raise input_checks.OperatorInputError("pr_number requires repository scope")
    normalized_pr = (
        input_checks.positive(pr_number, field="pr_number") if pr_number is not None else None
    )
    age = input_checks.positive(older_than_minutes, field="older_than_minutes")
    moment = input_checks.now(now)
    cutoff = moment - timedelta(minutes=age)
    with runtime.transaction() as connection:
        run_ids = review_runs.mark_stale_runs_failed_in_transaction(
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
        raise input_checks.OperatorInputError("pr_number requires repository scope")
    normalized_pr = (
        input_checks.positive(pr_number, field="pr_number") if pr_number is not None else None
    )
    row_limit = input_checks.positive(limit, field="limit")
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
    normalized_run_id = ReviewRunId(input_checks.positive(run_id, field="run_id"))
    with runtime.transaction() as connection:
        return postgres_reporting.run_diagnostics(connection, run_id=normalized_run_id)


def coverage(
    runtime: PostgreSQLRuntime, *, run_id: int
) -> postgres_reporting.CoverageReport:
    normalized_run_id = ReviewRunId(input_checks.positive(run_id, field="run_id"))
    with runtime.transaction() as connection:
        return postgres_reporting.coverage_report(connection, run_id=normalized_run_id)
