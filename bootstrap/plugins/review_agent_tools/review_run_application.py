"""Review-run lifecycle and objective coverage coordination."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from typing import TYPE_CHECKING, Generic, Literal, TypeVar

from . import failure_codes, memory_coverage, memory_runs, memory_schema
from .domain.review import JsonObject, resolve_review_subject
from .memory_coverage import FileIndexSummary, RunFileLookup, RunFilePage
from .memory_runs import RunPhase
from .postgres import registry as postgres_registry
from .postgres import review_runs as postgres_review_runs
from .postgres.runtime import PostgreSQLRuntime

if TYPE_CHECKING:
    import sqlite3


PullPayload = TypeVar("PullPayload")
FileSide = Literal["head", "base"]


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
class RunRequest:
    repository: str
    pr_number: int
    base_sha: str
    head_sha: str
    trigger_comment_id: int | None = None
    trigger_user: str = ""


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


def start_postgres_review(
    runtime: PostgreSQLRuntime, request: PostgresRunRequest
) -> postgres_review_runs.RunStart:
    """Compose the first exact PostgreSQL review in one short transaction."""
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

    with runtime.transaction() as connection:
        repository = postgres_registry.ensure_repository(
            connection, repository_definition
        )
        pull_request = postgres_registry.ensure_pull_request(
            connection, repository.id, request.pr_number
        )
        subject = postgres_registry.create_or_get_subject(
            connection, pull_request.id, subject_definition
        )
        return postgres_review_runs.start_run(
            connection,
            pull_request_id=pull_request.id,
            review_subject_id=subject.id,
            request_key=request.request_key,
            trigger_comment_id=request.trigger_comment_id,
            trigger_user=request.trigger_user,
        )


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


def start_run(request: RunRequest) -> RunStart:
    """Start one exact review subject or return the active duplicate."""
    with closing(memory_schema.connect_existing()) as connection:
        run = memory_runs.start_run(
            connection,
            request.repository,
            request.pr_number,
            trigger_comment_id=request.trigger_comment_id,
            trigger_user=request.trigger_user,
            base_sha=request.base_sha,
            head_sha=request.head_sha,
        )
        if str(run["status"]) == "duplicate":
            return DuplicateRun(
                existing_run_id=int(run["existing_run_id"]),
                status="duplicate",
                phase=run["phase"],
                started_at=str(run["started_at"]),
                last_heartbeat_at=str(run["last_heartbeat_at"]),
                message=str(run["message"]),
            )
    return StartedRun(
        run_id=int(run["id"]),
        status="running",
        phase=run["phase"],
        started_at=str(run["started_at"]),
    )


def _ensure_active(
    connection: sqlite3.Connection,
    subject: RunSubject,
    *,
    expected_head_sha: str | None = None,
) -> Mapping[str, object]:
    run = memory_runs.get_run(connection, subject.run_id)
    if run is None:
        raise ReviewRunError("run_id does not match a recorded review run")
    if (
        str(run["repository"]) != subject.repository
        or int(run["pr_number"]) != subject.pr_number
    ):
        raise ReviewRunError("run_id does not match this pull request")
    recorded_head = str(run.get("head_sha") or "").strip().lower()
    if (
        expected_head_sha is not None
        and recorded_head != expected_head_sha
    ):
        raise ReviewRunError("head_sha does not match the active review run")
    if str(run.get("status")) == "running":
        return run
    if str(run.get("failure_code") or "") == failure_codes.SNAPSHOT_SUPERSEDED:
        raise ReviewRunTerminal(subject.run_id, newly_terminalized=False)
    raise ReviewRunError("run_id is not an active review run")


def _advance_phase(
    connection: sqlite3.Connection,
    subject: RunSubject,
    phase: RunPhase,
) -> None:
    updated = memory_runs.update_run_phase(
        connection,
        subject.run_id,
        phase,
        repository=subject.repository,
        pr_number=subject.pr_number,
    )
    if updated is not None:
        return
    _ensure_active(connection, subject)
    raise ReviewRunError("run_id is not an active review run")


def advance_phase(subject: RunSubject, phase: RunPhase) -> None:
    """Heartbeat an active run at one known application phase."""
    with closing(memory_schema.connect_existing()) as connection:
        _advance_phase(connection, subject, phase)


def load_snapshot(
    subject: RunSubject,
    *,
    phase: RunPhase,
    pull_loader: Callable[[], PullSnapshot[PullPayload]],
    expected_head_sha: str | None = None,
) -> SnapshotResult[PullPayload]:
    """Load and heartbeat one exact snapshot, with DB-only terminal reuse."""
    with closing(memory_schema.connect_existing()) as connection:
        _ensure_active(
            connection,
            subject,
            expected_head_sha=expected_head_sha,
        )
    pull = pull_loader()
    with closing(memory_schema.connect_existing()) as connection:
        _ensure_active(
            connection,
            subject,
            expected_head_sha=expected_head_sha,
        )
        try:
            run = memory_runs.validate_run_snapshot(
                connection,
                subject.run_id,
                repository=subject.repository,
                pr_number=subject.pr_number,
                base_sha=pull.base_sha,
                head_sha=pull.head_sha,
            )
        except memory_runs.ReviewSnapshotChangedError:
            completed = memory_runs.complete_run(
                connection,
                subject.run_id,
                repository=subject.repository,
                pr_number=subject.pr_number,
                status="failed",
                failure_code=failure_codes.SNAPSHOT_SUPERSEDED,
            )
            if completed is None:
                _ensure_active(
                    connection,
                    subject,
                    expected_head_sha=expected_head_sha,
                )
                raise ReviewRunError("run_id is not an active review run")
            raise ReviewRunTerminal(subject.run_id, newly_terminalized=True)
        _advance_phase(connection, subject, phase)
    return SnapshotResult(
        pull=pull.payload,
        run=ValidatedRun(
            base_sha=str(run.get("base_sha") or "").strip().lower(),
            head_sha=str(run.get("head_sha") or "").strip().lower(),
        ),
    )


def fail_run(
    subject: RunSubject,
    *,
    failure_code: str,
    findings_count: int | None = None,
) -> bool:
    """Terminalize one active run without owning failure-status publication."""
    with closing(memory_schema.connect_existing()) as connection:
        completed = memory_runs.complete_run(
            connection,
            subject.run_id,
            repository=subject.repository,
            pr_number=subject.pr_number,
            status="failed",
            findings_count=findings_count,
            failure_code=failure_code,
        )
    return completed is not None


def register_changed_files(
    subject: RunSubject,
    *,
    files: Sequence[Mapping[str, object]],
    changed_files_reported: int,
) -> FileIndexSummary:
    """Register the immutable changed-file inventory and return its compact index."""
    with closing(memory_schema.connect_existing()) as connection:
        memory_coverage.register_changed_files(
            connection,
            run_id=subject.run_id,
            repository=subject.repository,
            pr_number=subject.pr_number,
            files=files,
            changed_files_reported=changed_files_reported,
            registration_complete=len(files) >= changed_files_reported,
        )
        return memory_coverage.file_index_summary(connection, run_id=subject.run_id)


def load_changed_file_page(
    subject: RunSubject,
    *,
    pull_loader: Callable[[], PullSnapshot[PullPayload]],
    limit: int,
    cursor: str = "",
    domain: str = "",
    review_mode: str = "",
    changed_only: bool = True,
) -> RunFilePage:
    load_snapshot(
        subject,
        phase="collecting_diff",
        pull_loader=pull_loader,
    )
    with closing(memory_schema.connect_existing()) as connection:
        return memory_coverage.list_run_files(
            connection,
            run_id=subject.run_id,
            repository=subject.repository,
            pr_number=subject.pr_number,
            limit=limit,
            cursor=cursor,
            domain=domain,
            review_mode=review_mode,
            changed_only=changed_only,
        )


def load_file_context(
    subject: RunSubject,
    *,
    path: str,
    pull_loader: Callable[[], PullSnapshot[PullPayload]],
) -> FileContextResult[PullPayload]:
    snapshot = load_snapshot(
        subject,
        phase="reviewing",
        pull_loader=pull_loader,
    )
    with closing(memory_schema.connect_existing()) as connection:
        run_file = memory_coverage.lookup_run_file(
            connection,
            run_id=subject.run_id,
            repository=subject.repository,
            pr_number=subject.pr_number,
            path=path,
        )
    return FileContextResult(
        pull=snapshot.pull,
        run=snapshot.run,
        file=run_file,
    )


def record_diff_result(
    subject: RunSubject,
    exposure: DiffExposure,
    *,
    phase: RunPhase = "reviewing",
) -> None:
    """Record one diff exposure result and advance the owning run phase."""
    with closing(memory_schema.connect_existing()) as connection:
        if exposure.unavailable_paths:
            memory_coverage.record_diff_exposure(
                connection,
                run_id=subject.run_id,
                repository=subject.repository,
                pr_number=subject.pr_number,
                paths=exposure.unavailable_paths,
                truncated=False,
                unavailable_reason=exposure.unavailable_reason,
            )
        if exposure.exposed_paths:
            memory_coverage.record_diff_exposure(
                connection,
                run_id=subject.run_id,
                repository=subject.repository,
                pr_number=subject.pr_number,
                paths=exposure.exposed_paths,
                truncated=False,
            )
        if exposure.truncated_paths:
            memory_coverage.record_diff_exposure(
                connection,
                run_id=subject.run_id,
                repository=subject.repository,
                pr_number=subject.pr_number,
                paths=exposure.truncated_paths,
                truncated=True,
            )
        _advance_phase(connection, subject, phase)


def record_source_read(
    subject: RunSubject,
    *,
    path: str,
    side: FileSide,
    start_line: int,
    line_count: int,
) -> int:
    """Record one non-empty source exposure and return its inclusive end line."""
    if line_count < 0:
        raise ReviewRunError("line_count must not be negative")
    end_line = start_line + line_count - 1
    if line_count == 0:
        return end_line
    with closing(memory_schema.connect_existing()) as connection:
        memory_coverage.record_file_range(
            connection,
            run_id=subject.run_id,
            repository=subject.repository,
            pr_number=subject.pr_number,
            path=path,
            side=side,
            start_line=start_line,
            end_line=end_line,
        )
    return end_line
