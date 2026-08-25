"""Review-run lifecycle and objective coverage coordination."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Generic, Literal, TypeVar, cast

import psycopg
from psycopg.rows import TupleRow

from . import changed_files, failure_codes
from .domain.review import (
    DiffState,
    FileDomain,
    FileSide as PostgresFileSide,
    JsonObject,
    PullRequestId,
    ReviewDomainError,
    ReviewMode,
    ReviewPhase,
    ReviewRunId,
    ReviewStatus,
    ReviewSubjectId,
    classify_file_domain,
    classify_review_mode,
    resolve_changed_file,
    resolve_changed_file_count,
    resolve_diff_observation,
    resolve_file_read,
    resolve_review_subject,
)
from .postgres import registry as postgres_registry
from .postgres import coverage as postgres_coverage
from .postgres import jobs as postgres_jobs
from .postgres import review_runs as postgres_review_runs
from .postgres.coverage import RunFileLookup
from .postgres.runtime import PostgreSQLRuntime


PullPayload = TypeVar("PullPayload")
FileSide = Literal["head", "base"]
RunPhase = Literal[
    "accepted",
    "fetching_pr",
    "collecting_diff",
    "reviewing",
    "rendering",
    "publishing",
    "posted",
    "failed",
]


class ReviewRunError(ValueError):
    """The requested operation does not belong to an active review run."""


class ReviewRunTerminal(Exception):
    """Expected stop for a review whose persisted snapshot is no longer current."""

    run_id: int
    newly_terminalized: bool

    def __init__(self, run_id: int, *, newly_terminalized: bool) -> None:
        super().__init__(failure_codes.SNAPSHOT_SUPERSEDED)
        self.run_id = run_id
        self.newly_terminalized = newly_terminalized


@dataclass(frozen=True, slots=True)
class PostgresRunRequest:
    provider: str
    provider_repository_id: int
    repository: str
    pr_number: int
    base_sha: str
    head_sha: str
    policy_revision: str
    resolved_config_schema_version: int
    resolved_config: JsonObject
    request_key: str
    trigger_comment_id: int | None = None
    trigger_user: str = ""


@dataclass(frozen=True, slots=True)
class PostgresChangedFile:
    path: str
    change_status: str
    previous_path: str | None = None
    domain: FileDomain = FileDomain.GENERAL
    review_mode: ReviewMode = ReviewMode.NORMAL


@dataclass(frozen=True, slots=True)
class PostgresFileRead:
    path: str
    side: PostgresFileSide
    start_line: int
    end_line: int


@dataclass(frozen=True, slots=True)
class RunSubject:
    repository: str
    pr_number: int
    run_id: int


@dataclass(frozen=True, slots=True)
class StartedRun:
    run_id: int
    status: Literal["running"]
    phase: RunPhase
    started_at: str


@dataclass(frozen=True, slots=True)
class DuplicateRun:
    existing_run_id: int
    status: Literal["duplicate"]
    phase: RunPhase
    started_at: str
    last_heartbeat_at: str
    message: str


RunStart = StartedRun | DuplicateRun


@dataclass(frozen=True, slots=True)
class LiveRunState:
    """Persisted continuation point for one exact active review."""

    run_id: int
    phase: RunPhase
    started_at: str
    file_index: postgres_coverage.FileIndexSummary


@dataclass(frozen=True, slots=True)
class AdmittedReview:
    """Atomic run and durable-job admission result."""

    run: postgres_review_runs.RunStart
    job: postgres_jobs.JobEnqueue


def start_run_in_transaction(
    connection: psycopg.Connection[TupleRow],
    *,
    pull_request_id: PullRequestId,
    review_subject_id: ReviewSubjectId,
    request_key: str,
    trigger_comment_id: int | None = None,
    trigger_user: str = "",
) -> postgres_review_runs.RunStart:
    """Start one run and reconcile any prior exact job in the same transaction."""
    result = postgres_review_runs.start_run(
        connection,
        pull_request_id=pull_request_id,
        review_subject_id=review_subject_id,
        request_key=request_key,
        trigger_comment_id=trigger_comment_id,
        trigger_user=trigger_user,
    )
    if (
        isinstance(result, postgres_review_runs.StartedRun)
        and result.superseded_run_id is not None
    ):
        postgres_jobs.reconcile_run_jobs(
            connection,
            run_ids=(result.superseded_run_id,),
            status=ReviewStatus.SUPERSEDED,
        )
    return result


def complete_run_in_transaction(
    connection: psycopg.Connection[TupleRow],
    run_id: ReviewRunId,
    *,
    findings_count: int,
) -> postgres_review_runs.ReviewRun:
    """Complete one review and its optional durable job atomically."""
    run = postgres_review_runs.complete_run(
        connection, run_id, findings_count=findings_count
    )
    postgres_jobs.reconcile_run_jobs(connection, run_ids=(run.id,), status=run.status)
    return run


def fail_run_in_transaction(
    connection: psycopg.Connection[TupleRow],
    run_id: ReviewRunId,
    *,
    failure_code: str,
    findings_count: int | None = None,
) -> postgres_review_runs.ReviewRun:
    """Fail one review and its optional durable job atomically."""
    run = postgres_review_runs.fail_run(
        connection,
        run_id,
        failure_code=failure_code,
        findings_count=findings_count,
    )
    postgres_jobs.reconcile_run_jobs(connection, run_ids=(run.id,), status=run.status)
    return run


def complete_run_after_publication_in_transaction(
    connection: psycopg.Connection[TupleRow],
    run_id: ReviewRunId,
    *,
    findings_count: int,
) -> postgres_review_runs.ReviewRun:
    """Complete a live run, or preserve a concurrently terminal run."""
    current = postgres_review_runs.lock_run(connection, run_id)
    if current.status is not ReviewStatus.RUNNING:
        postgres_jobs.reconcile_run_jobs(
            connection, run_ids=(current.id,), status=current.status
        )
        return current
    return complete_run_in_transaction(
        connection, run_id, findings_count=findings_count
    )


def fail_run_after_publication_in_transaction(
    connection: psycopg.Connection[TupleRow],
    run_id: ReviewRunId,
    *,
    failure_code: str,
    findings_count: int | None = None,
) -> postgres_review_runs.ReviewRun:
    """Fail a live run, or preserve a concurrently terminal run."""
    current = postgres_review_runs.lock_run(connection, run_id)
    if current.status is not ReviewStatus.RUNNING:
        postgres_jobs.reconcile_run_jobs(
            connection, run_ids=(current.id,), status=current.status
        )
        return current
    return fail_run_in_transaction(
        connection,
        run_id,
        failure_code=failure_code,
        findings_count=findings_count,
    )


def mark_superseded_in_transaction(
    connection: psycopg.Connection[TupleRow], run_id: ReviewRunId
) -> postgres_review_runs.ReviewRun:
    """Supersede one review and its optional durable job atomically."""
    run = postgres_review_runs.mark_superseded(connection, run_id)
    postgres_jobs.reconcile_run_jobs(connection, run_ids=(run.id,), status=run.status)
    return run


def fail_claimed_job_in_transaction(
    connection: psycopg.Connection[TupleRow],
    *,
    job_id: int,
    lease_owner: str,
    lease_generation: int,
    failure_code: str,
    retryable: bool,
    retry_delay: timedelta | None,
) -> postgres_jobs.JobFailureResult:
    """Fail one claim after locking its run before its job row."""
    current_job = postgres_jobs.get_job(connection, job_id)
    run = postgres_review_runs.lock_run(connection, current_job.review_run_id)
    if run.status is not ReviewStatus.RUNNING:
        changed = postgres_jobs.reconcile_run_jobs(
            connection, run_ids=(run.id,), status=run.status
        )
        job = changed[0] if changed else postgres_jobs.get_job(connection, job_id)
        raise postgres_jobs.ReviewJobLeaseLost(job)

    outcome = postgres_jobs.fail_claimed_job(
        connection,
        job_id=job_id,
        lease_owner=lease_owner,
        lease_generation=lease_generation,
        failure_code=failure_code,
        retryable=retryable,
        retry_delay=retry_delay,
    )
    if outcome.run_failure_code is not None:
        postgres_review_runs.fail_active_runs(
            connection,
            run_ids=(run.id,),
            failure_code=outcome.run_failure_code,
        )
    return outcome


def recover_expired_jobs_in_transaction(
    connection: psycopg.Connection[TupleRow], *, limit: int
) -> postgres_jobs.RecoveryBatch:
    """Recover one bounded expiry batch and release exhausted active runs."""
    recovered = postgres_jobs.recover_expired_leases(connection, limit=limit)
    postgres_review_runs.fail_active_runs(
        connection,
        run_ids=recovered.run_ids_to_fail,
        failure_code=failure_codes.JOB_RETRY_EXHAUSTED,
    )
    return recovered


def mark_stale_runs_failed_in_transaction(
    connection: psycopg.Connection[TupleRow],
    *,
    cutoff: datetime,
    repository: str | None,
    pr_number: int | None,
) -> tuple[ReviewRunId, ...]:
    """Fail old running runs that have no durable job or publication owner."""
    return postgres_review_runs.mark_stale_runs_failed(
        connection,
        cutoff=cutoff,
        repository=repository,
        pr_number=pr_number,
    )


def start_postgres_review(
    runtime: PostgreSQLRuntime, request: PostgresRunRequest
) -> postgres_review_runs.RunStart:
    """Compose the first exact PostgreSQL review in one short transaction."""
    with runtime.transaction() as connection:
        pull_request_id, subject_id = _ensure_review_scope(connection, request)
        return start_run_in_transaction(
            connection,
            pull_request_id=pull_request_id,
            review_subject_id=subject_id,
            request_key=request.request_key,
            trigger_comment_id=request.trigger_comment_id,
            trigger_user=request.trigger_user,
        )


def admit_postgres_review(
    runtime: PostgreSQLRuntime,
    request: PostgresRunRequest,
    *,
    priority: int,
    max_attempts: int,
    active_job_limit: int,
) -> AdmittedReview:
    """Persist the exact run and its queue record in one transaction."""
    with runtime.transaction() as connection:
        pull_request_id, subject_id = _ensure_review_scope(connection, request)
        run = start_run_in_transaction(
            connection,
            pull_request_id=pull_request_id,
            review_subject_id=subject_id,
            request_key=request.request_key,
            trigger_comment_id=request.trigger_comment_id,
            trigger_user=request.trigger_user,
        )
        job = postgres_jobs.enqueue_run(
            connection,
            review_run_id=run.run.id,
            priority=priority,
            max_attempts=max_attempts,
            active_job_limit=active_job_limit,
        )
        return AdmittedReview(run=run, job=job)


def _ensure_review_scope(
    connection: psycopg.Connection[TupleRow], request: PostgresRunRequest
) -> tuple[PullRequestId, ReviewSubjectId]:
    """Resolve and persist the repository, pull request, and exact subject."""
    repository_definition = postgres_registry.resolve_repository(
        postgres_registry.RepositoryDefinition(
            provider=request.provider,
            provider_repository_id=request.provider_repository_id,
            full_name=request.repository,
        )
    )
    subject_definition = resolve_review_subject(
        base_sha=request.base_sha,
        head_sha=request.head_sha,
        policy_revision=request.policy_revision,
        resolved_config_schema_version=request.resolved_config_schema_version,
        resolved_config=request.resolved_config,
    )
    if isinstance(request.pr_number, bool) or request.pr_number < 1:
        raise ReviewRunError("pr_number must be positive")
    repository = postgres_registry.ensure_repository(connection, repository_definition)
    pull_request = postgres_registry.ensure_pull_request(
        connection, repository.id, request.pr_number
    )
    subject = postgres_registry.create_or_get_subject(
        connection, pull_request.id, subject_definition
    )
    return pull_request.id, subject.id


def register_postgres_changed_files(
    runtime: PostgreSQLRuntime,
    *,
    run_id: ReviewRunId,
    files: Sequence[PostgresChangedFile],
    changed_files_reported: int,
    registration_complete: bool,
) -> postgres_coverage.CoverageRegistration:
    """Persist one pre-fetched changed-file batch in one short transaction."""
    if len(files) > changed_files.GITHUB_PR_FILES_LIMIT:
        raise ReviewDomainError("changed-file batch exceeds the supported limit")
    resolved = tuple(
        resolve_changed_file(
            path=item.path,
            change_status=item.change_status,
            previous_path=item.previous_path,
            domain=item.domain,
            review_mode=item.review_mode,
        )
        for item in files
    )
    resolved_reported = resolve_changed_file_count(changed_files_reported)
    with runtime.transaction() as connection:
        return postgres_coverage.insert_changed_files(
            connection,
            run_id=run_id,
            files=resolved,
            changed_files_reported=resolved_reported,
            registration_complete=registration_complete,
        )


def register_live_changed_files(
    runtime: PostgreSQLRuntime,
    subject: RunSubject,
    *,
    files: Sequence[Mapping[str, object]],
    changed_files_reported: int,
) -> postgres_coverage.FileIndexSummary:
    resolved_files = tuple(
        PostgresChangedFile(
            path=str(item.get("path", "")),
            change_status=str(item.get("status", "")),
            previous_path=(
                str(previous)
                if (previous := item.get("previous_path")) is not None
                else None
            ),
            domain=classify_file_domain(str(item.get("path", ""))),
            review_mode=classify_review_mode(
                str(item.get("path", "")), str(item.get("status", ""))
            ),
        )
        for item in files
    )
    registration_complete = len(files) >= changed_files_reported
    resolved_reported = resolve_changed_file_count(changed_files_reported)
    with runtime.transaction() as connection:
        postgres_coverage.insert_changed_files(
            connection,
            run_id=ReviewRunId(subject.run_id),
            files=tuple(
                resolve_changed_file(
                    path=item.path,
                    change_status=item.change_status,
                    previous_path=item.previous_path,
                    domain=item.domain,
                    review_mode=item.review_mode,
                )
                for item in resolved_files
            ),
            changed_files_reported=resolved_reported,
            registration_complete=registration_complete,
        )
        return postgres_coverage.file_index_summary(
            connection, run_id=ReviewRunId(subject.run_id)
        )


def record_postgres_diff_observation(
    runtime: PostgreSQLRuntime,
    *,
    run_id: ReviewRunId,
    paths: Sequence[str],
    state: DiffState,
    unavailable_reason: str = "",
) -> int:
    """Persist one pre-fetched diff outcome without a network callback."""
    if len(paths) > changed_files.GITHUB_PR_FILES_LIMIT:
        raise ReviewDomainError("diff observation exceeds the supported limit")
    observation = resolve_diff_observation(
        paths=paths,
        state=state,
        unavailable_reason=unavailable_reason,
    )
    with runtime.transaction() as connection:
        return postgres_coverage.record_diff_observation(
            connection,
            run_id=run_id,
            observation=observation,
        )


def record_postgres_file_reads(
    runtime: PostgreSQLRuntime,
    *,
    run_id: ReviewRunId,
    reads: Sequence[PostgresFileRead],
) -> postgres_coverage.FileReadBatch:
    """Persist pre-resolved source ranges without changing diff coverage."""
    if len(reads) > changed_files.GITHUB_PR_FILES_LIMIT:
        raise ReviewDomainError("source-read batch exceeds the supported limit")
    resolved = tuple(
        resolve_file_read(
            path=item.path,
            side=item.side,
            start_line=item.start_line,
            end_line=item.end_line,
        )
        for item in reads
    )
    with runtime.transaction() as connection:
        return postgres_coverage.insert_file_reads(
            connection, run_id=run_id, reads=resolved
        )


def summarize_postgres_coverage(
    runtime: PostgreSQLRuntime, run_id: ReviewRunId
) -> postgres_coverage.CoverageSummary:
    """Read the normalized coverage summary in one short transaction."""
    with runtime.transaction() as connection:
        return postgres_coverage.summarize(connection, run_id)


def _timestamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def start_live_review(
    runtime: PostgreSQLRuntime, request: PostgresRunRequest
) -> RunStart:
    """Start the deployed PostgreSQL review while preserving the tool contract."""
    result = start_postgres_review(runtime, request)
    run = result.run
    phase = cast(RunPhase, run.phase.value)
    if isinstance(result, postgres_review_runs.DuplicateRun):
        return DuplicateRun(
            existing_run_id=int(run.id),
            status="duplicate",
            phase=phase,
            started_at=_timestamp(run.started_at),
            last_heartbeat_at=_timestamp(run.last_heartbeat_at),
            message="another review is already running for this pull request",
        )
    return StartedRun(
        run_id=int(run.id),
        status="running",
        phase=phase,
        started_at=_timestamp(run.started_at),
    )


def _require_live_scope(
    runtime: PostgreSQLRuntime,
    subject: RunSubject,
    *,
    expected_head_sha: str | None = None,
) -> postgres_review_runs.ReviewRunScope:
    with runtime.transaction() as connection:
        scope = postgres_review_runs.get_run_scope(
            connection, ReviewRunId(subject.run_id)
        )
    if scope.repository != subject.repository or scope.pr_number != subject.pr_number:
        raise ReviewRunError("run_id does not match this pull request")
    if expected_head_sha is not None and scope.head_sha != expected_head_sha:
        raise ReviewRunError("head_sha does not match the active review run")
    if scope.run.status.value == "superseded":
        raise ReviewRunTerminal(subject.run_id, newly_terminalized=False)
    if scope.run.status.value != "running":
        raise ReviewRunError("run_id is not an active review run")
    return scope


def load_live_run_state(
    runtime: PostgreSQLRuntime, subject: RunSubject
) -> LiveRunState:
    """Load the exact active phase and its durable changed-file inventory."""
    scope = _require_live_scope(runtime, subject)
    with runtime.transaction() as connection:
        file_index = postgres_coverage.file_index_summary(
            connection, run_id=ReviewRunId(subject.run_id)
        )
    return LiveRunState(
        run_id=subject.run_id,
        phase=cast(RunPhase, scope.run.phase.value),
        started_at=_timestamp(scope.run.started_at),
        file_index=file_index,
    )


def advance_live_phase(
    runtime: PostgreSQLRuntime, subject: RunSubject, phase: RunPhase
) -> None:
    _require_live_scope(runtime, subject)
    with runtime.transaction() as connection:
        try:
            postgres_review_runs.advance_phase(
                connection,
                ReviewRunId(subject.run_id),
                resolve_review_phase(phase),
            )
        except postgres_review_runs.InvalidReviewTransition as exc:
            raise ReviewRunError(str(exc)) from exc


def reopen_live_finding_collection(
    runtime: PostgreSQLRuntime,
    subject: RunSubject,
    *,
    expected_head_sha: str,
) -> None:
    """Allow an exact run to correct findings after render validation fails."""
    _require_live_scope(runtime, subject, expected_head_sha=expected_head_sha)
    with runtime.transaction() as connection:
        try:
            postgres_review_runs.reopen_finding_collection(
                connection, ReviewRunId(subject.run_id)
            )
        except postgres_review_runs.InvalidReviewTransition as exc:
            raise ReviewRunError(str(exc)) from exc


def load_live_snapshot(
    runtime: PostgreSQLRuntime,
    subject: RunSubject,
    *,
    phase: RunPhase,
    pull_loader: Callable[[], PullSnapshot[PullPayload]],
    expected_head_sha: str | None = None,
) -> SnapshotResult[PullPayload]:
    """Validate one provider snapshot without holding a connection during I/O."""
    _require_live_scope(runtime, subject, expected_head_sha=expected_head_sha)
    pull = pull_loader()
    with runtime.transaction() as connection:
        try:
            scope, current, newly_terminalized = postgres_review_runs.validate_snapshot(
                connection,
                run_id=ReviewRunId(subject.run_id),
                repository=subject.repository,
                pr_number=subject.pr_number,
                base_sha=pull.base_sha,
                head_sha=pull.head_sha,
                expected_head_sha=expected_head_sha,
                phase=resolve_review_phase(phase),
            )
            if newly_terminalized:
                postgres_jobs.reconcile_run_jobs(
                    connection,
                    run_ids=(ReviewRunId(subject.run_id),),
                    status=ReviewStatus.SUPERSEDED,
                )
        except postgres_review_runs.ReviewRunError as exc:
            raise ReviewRunError(str(exc)) from exc
    if not current:
        raise ReviewRunTerminal(subject.run_id, newly_terminalized=newly_terminalized)
    return SnapshotResult(
        pull=pull.payload,
        run=ValidatedRun(base_sha=scope.base_sha, head_sha=scope.head_sha),
    )


def load_live_changed_file_page(
    runtime: PostgreSQLRuntime,
    subject: RunSubject,
    *,
    pull_loader: Callable[[], PullSnapshot[PullPayload]],
    limit: int,
    cursor: str = "",
    domain: str = "",
    review_mode: str = "",
    changed_only: bool = True,
) -> postgres_coverage.RunFilePage:
    load_live_snapshot(
        runtime,
        subject,
        phase="collecting_diff",
        pull_loader=pull_loader,
    )
    with runtime.transaction() as connection:
        return postgres_coverage.list_run_files(
            connection,
            run_id=ReviewRunId(subject.run_id),
            repository=subject.repository,
            pr_number=subject.pr_number,
            limit=limit,
            cursor=cursor,
            domain=domain,
            review_mode=review_mode,
            changed_only=changed_only,
        )


def load_live_file_context(
    runtime: PostgreSQLRuntime,
    subject: RunSubject,
    *,
    path: str,
    pull_loader: Callable[[], PullSnapshot[PullPayload]],
) -> tuple[SnapshotResult[PullPayload], postgres_coverage.RunFileLookup]:
    snapshot = load_live_snapshot(
        runtime,
        subject,
        phase="reviewing",
        pull_loader=pull_loader,
    )
    with runtime.transaction() as connection:
        run_file = postgres_coverage.lookup_run_file(
            connection,
            run_id=ReviewRunId(subject.run_id),
            repository=subject.repository,
            pr_number=subject.pr_number,
            path=path,
        )
    return snapshot, run_file


def record_live_diff_result(
    runtime: PostgreSQLRuntime,
    subject: RunSubject,
    exposure: DiffExposure,
    *,
    phase: RunPhase = "reviewing",
) -> None:
    observations = tuple(
        observation
        for observation in (
            resolve_diff_observation(
                paths=exposure.unavailable_paths,
                state=DiffState.UNAVAILABLE,
                unavailable_reason=exposure.unavailable_reason,
            )
            if exposure.unavailable_paths
            else None,
            resolve_diff_observation(
                paths=exposure.exposed_paths,
                state=DiffState.COMPLETE,
            )
            if exposure.exposed_paths
            else None,
            resolve_diff_observation(
                paths=exposure.truncated_paths,
                state=DiffState.TRUNCATED,
            )
            if exposure.truncated_paths
            else None,
        )
        if observation is not None
    )
    with runtime.transaction() as connection:
        postgres_review_runs.advance_phase(
            connection,
            ReviewRunId(subject.run_id),
            resolve_review_phase(phase),
        )
        for observation in observations:
            postgres_coverage.record_diff_observation(
                connection,
                run_id=ReviewRunId(subject.run_id),
                observation=observation,
            )


def record_live_source_read(
    runtime: PostgreSQLRuntime,
    subject: RunSubject,
    *,
    path: str,
    side: FileSide,
    start_line: int,
    line_count: int,
) -> int:
    if line_count < 0:
        raise ReviewRunError("line_count must not be negative")
    end_line = start_line + line_count - 1
    if line_count == 0:
        return end_line
    read = resolve_file_read(
        path=path,
        side=PostgresFileSide(side),
        start_line=start_line,
        end_line=end_line,
    )
    with runtime.transaction() as connection:
        postgres_coverage.insert_file_reads(
            connection,
            run_id=ReviewRunId(subject.run_id),
            reads=(read,),
        )
    return end_line


def fail_live_run(
    runtime: PostgreSQLRuntime,
    subject: RunSubject,
    *,
    failure_code: str,
    findings_count: int | None = None,
) -> bool:
    try:
        _require_live_scope(runtime, subject)
        with runtime.transaction() as connection:
            fail_run_in_transaction(
                connection,
                ReviewRunId(subject.run_id),
                failure_code=failure_code,
                findings_count=findings_count,
            )
        return True
    except ReviewRunTerminal:
        return False
    except postgres_review_runs.InvalidReviewTransition:
        return False


def resolve_review_phase(value: RunPhase) -> ReviewPhase:
    try:
        return ReviewPhase(value)
    except ValueError as exc:
        raise ReviewRunError(f"unknown review phase: {value}") from exc


@dataclass(frozen=True, slots=True)
class PullSnapshot(Generic[PullPayload]):
    payload: PullPayload
    base_sha: str
    head_sha: str


@dataclass(frozen=True, slots=True)
class ValidatedRun:
    base_sha: str
    head_sha: str


@dataclass(frozen=True, slots=True)
class SnapshotResult(Generic[PullPayload]):
    pull: PullPayload
    run: ValidatedRun


@dataclass(frozen=True, slots=True)
class FileContextResult(Generic[PullPayload]):
    pull: PullPayload
    run: ValidatedRun
    file: RunFileLookup


@dataclass(frozen=True, slots=True)
class DiffExposure:
    exposed_paths: tuple[str, ...] = ()
    truncated_paths: tuple[str, ...] = ()
    unavailable_paths: tuple[str, ...] = ()
    unavailable_reason: str = "patch_unavailable"
