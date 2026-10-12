"""Run one large code review as parts with one final reconciliation."""

from __future__ import annotations

from dataclasses import dataclass

import psycopg
from psycopg.rows import TupleRow

from .domain.finding import MAX_FINDINGS_PER_REVIEW
from .domain.review import (
    MAX_REVIEW_PARTS,
    JsonObject,
    ReviewPartPlan,
    ReviewPhase,
    ReviewRunId,
)
from .postgres import (
    jobs as postgres_jobs,
    parts as postgres_parts,
    review_runs as postgres_review_runs,
)
from .postgres.runtime import PostgreSQLRuntime

# Every part may stage an equal share of the findings one review can record.
PART_MAX_CANDIDATES = MAX_FINDINGS_PER_REVIEW // MAX_REVIEW_PARTS
PART_MAX_NOTES = 10
PART_NOTE_MAX_CHARS = 300

ReviewPartError = postgres_parts.ReviewPartError
PartReport = postgres_parts.PartReport


class PartReadingIncomplete(ValueError):
    """A part tried to report before reading every readable assigned diff."""

    def __init__(self, *, unread: int, assigned: int) -> None:
        super().__init__(
            f"{unread} of this part's {assigned} changed files still have unread diff"
        )
        self.unread = unread
        self.assigned = assigned


@dataclass(frozen=True, slots=True)
class PartBriefing:
    part: int
    parts: int
    assigned_files: int
    earlier: tuple[PartReport, ...]


def _leased(
    connection: psycopg.Connection[TupleRow],
    *,
    run_id: ReviewRunId,
    lease: postgres_jobs.WorkerLeaseSession,
) -> postgres_jobs.ReviewJob:
    return postgres_jobs.require_live_lease(
        connection,
        job_id=lease.job_id,
        review_run_id=run_id,
        lease_generation=lease.lease_generation,
    )


def split_review(
    runtime: PostgreSQLRuntime,
    *,
    run_id: ReviewRunId,
    lease: postgres_jobs.WorkerLeaseSession,
    plan: ReviewPartPlan,
) -> int:
    """Turn the leased main job into part jobs and return how many were created.

    Assignments, queued part jobs, the phase, and the release of the main job
    commit together, so a part can never start against a half-planned review.
    """
    if not plan.parts:
        return 0
    with runtime.transaction() as connection:
        # The run row serializes planning with every other lifecycle change.
        postgres_review_runs.advance_phase(
            connection, run_id, ReviewPhase.COLLECTING_DIFF
        )
        main = _leased(connection, run_id=run_id, lease=lease)
        if main.part != 0 or postgres_parts.planned_parts(connection, run_id=run_id):
            return 0
        postgres_parts.assign(connection, run_id=run_id, parts=plan.parts)
        postgres_jobs.enqueue_part_jobs(connection, main=main, parts=len(plan.parts))
        postgres_jobs.requeue_unstarted_job(
            connection,
            job_id=main.id,
            lease_owner=main.lease_owner or "",
            lease_generation=main.lease_generation,
        )
    return len(plan.parts)


def planned_parts(runtime: PostgreSQLRuntime, *, run_id: ReviewRunId) -> int:
    with runtime.transaction() as connection:
        return postgres_parts.planned_parts(connection, run_id=run_id)


def begin_part(
    runtime: PostgreSQLRuntime,
    *,
    run_id: ReviewRunId,
    lease: postgres_jobs.WorkerLeaseSession,
) -> PartBriefing:
    """Start a part session's reading and report what earlier parts found."""
    with runtime.transaction() as connection:
        job = _leased(connection, run_id=run_id, lease=lease)
        postgres_parts.begin_reading(
            connection,
            run_id=run_id,
            part=job.part,
            lease_generation=job.lease_generation,
        )
        assigned, _ = postgres_parts.assigned_files(
            connection, run_id=run_id, part=job.part
        )
        return PartBriefing(
            part=job.part,
            parts=postgres_parts.planned_parts(connection, run_id=run_id),
            assigned_files=assigned,
            earlier=tuple(
                report
                for report in postgres_parts.reports(connection, run_id=run_id)
                if report.part != job.part
            ),
        )


def stage_report(
    runtime: PostgreSQLRuntime,
    *,
    run_id: ReviewRunId,
    lease: postgres_jobs.WorkerLeaseSession,
    report: JsonObject,
) -> int:
    """Commit one part's report together with its job's success; return the part."""
    with runtime.transaction() as connection:
        job = _leased(connection, run_id=run_id, lease=lease)
        if job.part == 0:
            raise ReviewPartError("only a review part stages a report")
        assigned, unread = postgres_parts.assigned_files(
            connection, run_id=run_id, part=job.part
        )
        if unread:
            raise PartReadingIncomplete(unread=unread, assigned=assigned)
        postgres_parts.store_report(
            connection, run_id=run_id, part=job.part, report=report
        )
        postgres_jobs.succeed_part_job(
            connection,
            job_id=job.id,
            review_run_id=run_id,
            lease_generation=job.lease_generation,
        )
        return job.part


def load_report(
    runtime: PostgreSQLRuntime, *, run_id: ReviewRunId, part: int
) -> JsonObject:
    with runtime.transaction() as connection:
        for item in postgres_parts.reports(connection, run_id=run_id):
            if item.part == part:
                return item.report
    raise ReviewPartError("this review has no report for that part")


def confirm_report_read(
    runtime: PostgreSQLRuntime,
    *,
    run_id: ReviewRunId,
    lease: postgres_jobs.WorkerLeaseSession,
    part: int,
) -> None:
    """Remember that the final session of this lease received one part report."""
    with runtime.transaction() as connection:
        job = _leased(connection, run_id=run_id, lease=lease)
        if job.part != 0:
            raise ReviewPartError("part reports are read by the final session")
        postgres_parts.mark_report_read(
            connection,
            run_id=run_id,
            part=part,
            lease_generation=job.lease_generation,
        )


def unread_reports(
    runtime: PostgreSQLRuntime,
    *,
    run_id: ReviewRunId,
    lease: postgres_jobs.WorkerLeaseSession,
) -> tuple[int, ...]:
    with runtime.transaction() as connection:
        return postgres_parts.unread_reports(
            connection, run_id=run_id, lease_generation=lease.lease_generation
        )
