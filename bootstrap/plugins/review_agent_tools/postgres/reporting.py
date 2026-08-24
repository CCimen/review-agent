"""Bounded PostgreSQL read models for the operator surface."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import LiteralString, cast

import psycopg
from psycopg import sql
from psycopg.pq import TransactionStatus
from psycopg.rows import TupleRow, class_row

from ..domain.finding import (
    DecisionKind,
    FindingCategory,
    FindingDecision,
    FindingDecisionId,
    FindingDomainError,
    FindingId,
    FindingOccurrenceId,
    Severity,
    require_explicit_decision_target,
    suppression_is_active,
)
from ..domain.review import (
    CoverageState,
    JsonObject,
    JsonValue,
    RepositoryId,
    ReviewRunId,
)
from . import coverage as postgres_coverage
from . import decisions as postgres_decisions


EXPORT_SCHEMA_VERSION = 16
VERIFICATION_SOURCE_SCHEMA_VERSION = 1


class ReportingError(ValueError):
    """An operator read cannot be resolved from durable PostgreSQL state."""


class RepositoryNotFound(ReportingError):
    """The requested repository is not registered."""


class FindingNotFound(ReportingError):
    """The requested finding is not registered in the repository."""


class VerificationExportUnavailable(ReportingError):
    """A run lacks the complete publication evidence required for export."""


@dataclass(frozen=True, slots=True)
class RepositoryScope:
    id: RepositoryId
    full_name: str


@dataclass(frozen=True, slots=True)
class FindingReport:
    id: FindingId
    repository: str
    fingerprint: str
    rule_id: str
    path: str
    symbol: str | None
    anchor: str
    first_seen_at: datetime
    last_seen_at: datetime
    occurrence_id: FindingOccurrenceId
    occurrence_count: int
    line: int
    title: str
    severity: Severity
    category: FindingCategory
    publication_score: int
    confidence: float
    context_hash: str
    evidence: str
    disproof_checks: str
    impact: str
    smallest_fix: str
    latest_decision: DecisionKind | None
    suppressed: bool


@dataclass(frozen=True, slots=True)
class FindingDetail:
    finding: FindingReport
    decisions: tuple[FindingDecision, ...]


@dataclass(frozen=True, slots=True)
class DecisionTarget:
    finding_id: FindingId
    occurrence_id: FindingOccurrenceId


@dataclass(frozen=True, slots=True)
class CountByValue:
    value: str
    count: int


@dataclass(frozen=True, slots=True)
class FindingStats:
    repository: str | None
    generated_at: datetime
    findings_total: int
    findings_without_decision: int
    findings_by_severity: tuple[CountByValue, ...]
    findings_by_category: tuple[CountByValue, ...]
    findings_by_rule: tuple[CountByValue, ...]
    quality_feedback_by_category: tuple[CountByValue, ...]
    latest_decision_by_type: tuple[CountByValue, ...]
    active_suppressions: int
    active_suppressions_expiring_within_days: int
    active_suppressions_nearing_expiry: int
    repeats_after_decision_approx: int


@dataclass(frozen=True, slots=True)
class ReviewRunReport:
    id: ReviewRunId
    repository: str
    pr_number: int
    base_sha: str
    head_sha: str
    status: str
    phase: str
    findings_count: int | None
    failure_code: str | None
    failure_status_comment_id: int | None
    started_at: datetime
    last_heartbeat_at: datetime
    completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class RunStats:
    repository: str | None
    window_days: int
    generated_at: datetime
    total: int
    by_status: tuple[CountByValue, ...]
    stalled_running: int
    time_to_answer_seconds_p50: float | None
    time_to_answer_seconds_p95: float | None
    average_findings_per_completed_run: float | None


@dataclass(frozen=True, slots=True)
class PublicationReport:
    id: int
    review_run_id: ReviewRunId
    review_number: int
    repository: str
    pr_number: int
    status: str
    comment_ids: tuple[int, ...]
    failure_code: str | None
    generated_at: datetime
    posting_started_at: datetime | None
    posted_at: datetime | None
    publish_failed_at: datetime | None
    superseded_at: datetime | None
    superseded_by_publication_id: int | None
    supersession_rendered_at: datetime | None
    supersession_failure_code: str | None
    suggestion_status: str | None
    suggestion_review_id: int | None
    verification_status: str | None
    verification_mode: str | None
    verification_provider: str | None
    verification_failure_code: str | None


@dataclass(frozen=True, slots=True)
class CoverageReport:
    state: CoverageState
    coverage_hash: str
    changed_files_reported: int | None
    changed_files_registered: int
    registration_complete: bool
    changed_paths_with_complete_diff: int
    changed_paths_with_source_reads: int
    supporting_context_paths_read: int
    context_ranges_read: int
    unavailable_paths: tuple[str, ...]
    truncated_paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RepositoryExport:
    repository: str
    exported_at: datetime
    row_limit: int
    truncated_tables: tuple[str, ...]
    rows_by_table: tuple[tuple[str, tuple[JsonObject, ...]], ...]

    def to_json_obj(self) -> dict[str, JsonValue]:
        output: dict[str, JsonValue] = {
            "schema_version": EXPORT_SCHEMA_VERSION,
            "repository": self.repository,
            "exported_at": self.exported_at.isoformat(),
            "row_limit": self.row_limit,
            "complete": not self.truncated_tables,
            "truncated_tables": list(self.truncated_tables),
        }
        for name, rows in self.rows_by_table:
            output[name] = [dict(row) for row in rows]
        return output


@dataclass(frozen=True, slots=True)
class _FindingRow:
    id: FindingId
    repository: str
    fingerprint: str
    rule_id: str
    path: str
    symbol: str | None
    anchor: str
    first_seen_at: datetime
    last_seen_at: datetime
    occurrence_id: FindingOccurrenceId
    occurrence_count: int
    line: int
    title: str
    severity: str
    category: str
    publication_score: int
    confidence: Decimal
    context_hash: str
    evidence: str
    disproof_checks: str
    impact: str
    smallest_fix: str
    decision_id: FindingDecisionId | None
    decision: str | None
    decision_reason: str | None
    decision_actor: str | None
    decision_context_hash: str | None
    decision_adr_id: str | None
    decision_created_at: datetime | None
    decision_expires_at: datetime | None


@dataclass(frozen=True, slots=True)
class _CountRow:
    value: str
    count: int


@dataclass(frozen=True, slots=True)
class _FindingTotalsRow:
    findings_total: int
    findings_without_decision: int
    active_suppressions: int
    active_suppressions_nearing_expiry: int
    repeats_after_decision_approx: int


@dataclass(frozen=True, slots=True)
class _RunRow:
    id: ReviewRunId
    repository: str
    pr_number: int
    base_sha: str
    head_sha: str
    status: str
    phase: str
    findings_count: int | None
    failure_code: str | None
    failure_status_comment_id: int | None
    started_at: datetime
    last_heartbeat_at: datetime
    completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class _RunTotalsRow:
    total: int
    stalled_running: int
    p50: Decimal | None
    p95: Decimal | None
    average_findings: Decimal | None


@dataclass(frozen=True, slots=True)
class _PublicationReportRow:
    id: int
    review_run_id: ReviewRunId
    review_number: int
    repository: str
    pr_number: int
    status: str
    comment_ids: list[int]
    failure_code: str | None
    generated_at: datetime
    posting_started_at: datetime | None
    posted_at: datetime | None
    publish_failed_at: datetime | None
    superseded_at: datetime | None
    superseded_by_publication_id: int | None
    supersession_rendered_at: datetime | None
    supersession_failure_code: str | None
    suggestion_status: str | None
    suggestion_review_id: int | None
    verification_status: str | None
    verification_mode: str | None
    verification_provider: str | None
    verification_failure_code: str | None


_FINDING_SELECT = """
    SELECT identity.id, repository.full_name AS repository,
           identity.fingerprint, identity.rule_id, identity.path,
           identity.symbol, identity.anchor, identity.first_seen_at,
           identity.last_seen_at, occurrence.id AS occurrence_id,
           occurrence.occurrence_count, occurrence.line, occurrence.title,
           occurrence.severity, occurrence.category,
           occurrence.publication_score, occurrence.confidence,
           occurrence.context_hash, occurrence.evidence,
           occurrence.disproof_checks, occurrence.impact,
           occurrence.smallest_fix, decision.id AS decision_id,
           decision.decision, decision.reason AS decision_reason,
           decision.actor AS decision_actor,
           decision.context_hash AS decision_context_hash,
           decision.adr_id AS decision_adr_id,
           decision.created_at AS decision_created_at,
           decision.expires_at AS decision_expires_at
    FROM review_agent.finding_identities AS identity
    JOIN review_agent.repositories AS repository
      ON repository.id = identity.repository_id
    JOIN LATERAL (
        SELECT occurrence.*, count(*) OVER ()::integer AS occurrence_count
        FROM review_agent.finding_occurrences AS occurrence
        WHERE occurrence.finding_id = identity.id
        ORDER BY occurrence.observed_at DESC, occurrence.id DESC
        LIMIT 1
    ) AS occurrence ON true
    LEFT JOIN LATERAL (
        SELECT stored.*
        FROM review_agent.finding_decisions AS stored
        WHERE stored.finding_id = identity.id
        ORDER BY stored.id DESC
        LIMIT 1
    ) AS decision ON true
"""


def _require_transaction(connection: psycopg.Connection[TupleRow]) -> None:
    if connection.info.transaction_status != TransactionStatus.INTRANS:
        raise ReportingError("reporting operations require an active transaction")


def _require_repeatable_read(connection: psycopg.Connection[TupleRow]) -> None:
    _require_transaction(connection)
    isolation = connection.execute("SHOW transaction_isolation").fetchone()
    if isolation != ("repeatable read",):
        raise ReportingError(
            "repository export requires a repeatable-read transaction"
        )


def _trusted_sql(value: str) -> sql.SQL:
    """Mark SQL assembled only from module-owned static fragments as trusted."""
    return sql.SQL(cast(LiteralString, value))


def repository_scope(
    connection: psycopg.Connection[TupleRow], *, repository: str
) -> RepositoryScope:
    """Resolve one current GitHub repository name to its stable identity."""
    _require_transaction(connection)
    row = connection.execute(
        "SELECT id, full_name FROM review_agent.repositories "
        "WHERE provider = 'github' AND lower(full_name) = lower(%s)",
        (repository,),
    ).fetchone()
    if row is None:
        raise RepositoryNotFound("repository is not registered")
    return RepositoryScope(id=RepositoryId(int(row[0])), full_name=str(row[1]))


def _finding(row: _FindingRow, *, now: datetime) -> FindingReport:
    try:
        severity = Severity(row.severity)
        category = FindingCategory(row.category)
        latest = DecisionKind(row.decision) if row.decision is not None else None
    except ValueError as exc:
        raise ReportingError("stored finding classification is invalid") from exc
    suppressed = False
    if latest is not None and row.decision_context_hash is not None:
        suppressed = suppression_is_active(
            decision=latest,
            decision_context_hash=row.decision_context_hash,
            current_context_hash=row.context_hash,
            expires_at=row.decision_expires_at,
            now=now,
        )
    return FindingReport(
        id=row.id,
        repository=row.repository,
        fingerprint=row.fingerprint,
        rule_id=row.rule_id,
        path=row.path,
        symbol=row.symbol,
        anchor=row.anchor,
        first_seen_at=row.first_seen_at,
        last_seen_at=row.last_seen_at,
        occurrence_id=row.occurrence_id,
        occurrence_count=row.occurrence_count,
        line=row.line,
        title=row.title,
        severity=severity,
        category=category,
        publication_score=row.publication_score,
        confidence=float(row.confidence),
        context_hash=row.context_hash,
        evidence=row.evidence,
        disproof_checks=row.disproof_checks,
        impact=row.impact,
        smallest_fix=row.smallest_fix,
        latest_decision=latest,
        suppressed=suppressed,
    )


def list_findings(
    connection: psycopg.Connection[TupleRow],
    *,
    repository: str | None,
    limit: int,
    include_suppressed: bool,
    now: datetime,
) -> tuple[FindingReport, ...]:
    """List latest finding state with one set-oriented decision lookup."""
    _require_transaction(connection)
    where = ""
    parameters: tuple[object, ...]
    if repository is None:
        parameters = (limit,)
    else:
        where = "WHERE lower(repository.full_name) = lower(%s)"
        parameters = (repository, limit)
    with connection.cursor(row_factory=class_row(_FindingRow)) as cursor:
        rows = cursor.execute(
            f"{_FINDING_SELECT} {where} "
            "ORDER BY identity.last_seen_at DESC, identity.id DESC LIMIT %s",
            parameters,
        ).fetchall()
    items = tuple(_finding(row, now=now) for row in rows)
    if include_suppressed:
        return items
    return tuple(item for item in items if not item.suppressed)


def finding_detail(
    connection: psycopg.Connection[TupleRow],
    *,
    repository_id: RepositoryId,
    fingerprint: str,
    now: datetime,
) -> FindingDetail:
    """Return latest evidence and the complete decision chain for one identity."""
    _require_transaction(connection)
    with connection.cursor(row_factory=class_row(_FindingRow)) as cursor:
        row = cursor.execute(
            f"{_FINDING_SELECT} WHERE identity.repository_id = %s "
            "AND identity.fingerprint = %s",
            (repository_id, fingerprint),
        ).fetchone()
    if row is None:
        raise FindingNotFound("finding is not registered in the repository")
    return FindingDetail(
        finding=_finding(row, now=now),
        decisions=postgres_decisions.decision_history(
            connection, finding_id=row.id
        ),
    )


def decision_target(
    connection: psycopg.Connection[TupleRow],
    *,
    repository_id: RepositoryId,
    fingerprint: str,
    occurrence_id: FindingOccurrenceId | None,
    pr_number: int | None,
    local_reference: str | None,
    latest: bool,
) -> DecisionTarget:
    """Resolve one explicit operator target without guessing between occurrences."""
    _require_transaction(connection)
    try:
        require_explicit_decision_target(
            occurrence_id=occurrence_id,
            pr_number=pr_number,
            local_reference=local_reference,
            latest=latest,
        )
    except FindingDomainError as exc:
        raise ReportingError(str(exc)) from exc
    identity = connection.execute(
        "SELECT id FROM review_agent.finding_identities "
        "WHERE repository_id = %s AND fingerprint = %s",
        (repository_id, fingerprint),
    ).fetchone()
    if identity is None:
        raise FindingNotFound("finding is not registered in the repository")
    finding_id = FindingId(int(identity[0]))
    if occurrence_id is not None:
        occurrence = connection.execute(
            "SELECT id FROM review_agent.finding_occurrences "
            "WHERE id = %s AND finding_id = %s",
            (occurrence_id, finding_id),
        ).fetchone()
    elif latest:
        occurrence = connection.execute(
            "SELECT id FROM review_agent.finding_occurrences "
            "WHERE finding_id = %s ORDER BY observed_at DESC, id DESC LIMIT 1",
            (finding_id,),
        ).fetchone()
    else:
        occurrence = connection.execute(
            """
            SELECT occurrence.id
            FROM review_agent.pull_requests AS pull_request
            JOIN review_agent.pull_request_finding_references AS reference
              ON reference.pull_request_id = pull_request.id
            JOIN LATERAL (
                SELECT stored.id
                FROM review_agent.finding_occurrences AS stored
                JOIN review_agent.review_runs AS run
                  ON run.id = stored.review_run_id
                WHERE stored.finding_id = reference.finding_id
                  AND run.pull_request_id = pull_request.id
                ORDER BY stored.observed_at DESC, stored.id DESC LIMIT 1
            ) AS occurrence ON true
            WHERE pull_request.repository_id = %s
              AND pull_request.number = %s
              AND reference.finding_id = %s
              AND reference.local_reference = %s
            """,
            (repository_id, pr_number, finding_id, local_reference),
        ).fetchone()
    if occurrence is None:
        raise FindingNotFound("explicit decision occurrence could not be resolved")
    return DecisionTarget(
        finding_id=finding_id,
        occurrence_id=FindingOccurrenceId(int(occurrence[0])),
    )


def _counts(
    connection: psycopg.Connection[TupleRow],
    query: str,
    parameters: tuple[object, ...],
) -> tuple[CountByValue, ...]:
    with connection.cursor(row_factory=class_row(_CountRow)) as cursor:
        rows = cursor.execute(_trusted_sql(query), parameters).fetchall()
    return tuple(CountByValue(value=row.value, count=row.count) for row in rows)


def finding_stats(
    connection: psycopg.Connection[TupleRow],
    *,
    repository: str | None,
    expiring_at: datetime,
    expiring_within_days: int,
    now: datetime,
) -> FindingStats:
    """Compute repository finding and feedback metrics in set-oriented queries."""
    _require_transaction(connection)
    repository_filter = (
        "" if repository is None else "AND lower(repository.full_name) = lower(%s)"
    )
    parameters: tuple[object, ...] = () if repository is None else (repository,)
    with connection.cursor(row_factory=class_row(_FindingTotalsRow)) as cursor:
        totals = cursor.execute(
            f"""
            WITH scoped_identity AS (
                SELECT identity.id
                FROM review_agent.finding_identities AS identity
                JOIN review_agent.repositories AS repository
                  ON repository.id = identity.repository_id
                WHERE true {repository_filter}
            ), latest_occurrence AS (
                SELECT DISTINCT ON (occurrence.finding_id)
                       occurrence.finding_id, occurrence.context_hash,
                       occurrence.observed_at,
                       count(*) OVER (PARTITION BY occurrence.finding_id)::integer
                           AS occurrence_count
                FROM review_agent.finding_occurrences AS occurrence
                JOIN scoped_identity AS identity
                  ON identity.id = occurrence.finding_id
                ORDER BY occurrence.finding_id, occurrence.observed_at DESC,
                         occurrence.id DESC
            ), latest_decision AS (
                SELECT DISTINCT ON (decision.finding_id) decision.*
                FROM review_agent.finding_decisions AS decision
                JOIN scoped_identity AS identity
                  ON identity.id = decision.finding_id
                ORDER BY decision.finding_id, decision.id DESC
            )
            SELECT count(*)::integer AS findings_total,
                   count(*) FILTER (WHERE decision.id IS NULL)::integer
                       AS findings_without_decision,
                   count(*) FILTER (
                       WHERE decision.decision IN (
                           'false_positive', 'intentional_by_design',
                           'accepted_risk', 'duplicate'
                       ) AND decision.expires_at > %s
                         AND decision.context_hash = occurrence.context_hash
                   )::integer AS active_suppressions,
                   count(*) FILTER (
                       WHERE decision.decision IN (
                           'false_positive', 'intentional_by_design',
                           'accepted_risk', 'duplicate'
                       ) AND decision.expires_at > %s
                         AND decision.expires_at <= %s
                         AND decision.context_hash = occurrence.context_hash
                   )::integer AS active_suppressions_nearing_expiry,
                   count(*) FILTER (
                       WHERE occurrence.occurrence_count > 1
                         AND decision.id IS NOT NULL
                         AND decision.created_at <= occurrence.observed_at
                   )::integer AS repeats_after_decision_approx
            FROM scoped_identity AS identity
            JOIN latest_occurrence AS occurrence
              ON occurrence.finding_id = identity.id
            LEFT JOIN latest_decision AS decision
              ON decision.finding_id = identity.id
            """,
            (*parameters, now, now, expiring_at),
        ).fetchone()
    if totals is None:
        raise ReportingError("finding statistics could not be computed")
    classification_base = f"""
        FROM review_agent.finding_identities AS identity
        JOIN review_agent.repositories AS repository
          ON repository.id = identity.repository_id
        JOIN LATERAL (
            SELECT occurrence.* FROM review_agent.finding_occurrences AS occurrence
            WHERE occurrence.finding_id = identity.id
            ORDER BY occurrence.observed_at DESC, occurrence.id DESC LIMIT 1
        ) AS occurrence ON true
        WHERE true {repository_filter}
    """
    severity = _counts(
        connection,
        "SELECT occurrence.severity AS value, count(*)::integer AS count "
        + classification_base
        + " GROUP BY occurrence.severity ORDER BY occurrence.severity",
        parameters,
    )
    category = _counts(
        connection,
        "SELECT occurrence.category AS value, count(*)::integer AS count "
        + classification_base
        + " GROUP BY occurrence.category ORDER BY occurrence.category",
        parameters,
    )
    rule = _counts(
        connection,
        "SELECT identity.rule_id AS value, count(*)::integer AS count "
        + classification_base
        + " GROUP BY identity.rule_id ORDER BY count DESC, identity.rule_id",
        parameters,
    )
    decision = _counts(
        connection,
        f"""
        SELECT latest.decision AS value, count(*)::integer AS count
        FROM review_agent.finding_identities AS identity
        JOIN review_agent.repositories AS repository
          ON repository.id = identity.repository_id
        JOIN LATERAL (
            SELECT stored.decision
            FROM review_agent.finding_decisions AS stored
            WHERE stored.finding_id = identity.id
            ORDER BY stored.id DESC LIMIT 1
        ) AS latest ON true
        WHERE true {repository_filter}
        GROUP BY latest.decision ORDER BY latest.decision
        """,
        parameters,
    )
    feedback_filter = (
        "" if repository is None else "WHERE lower(repository.full_name) = lower(%s)"
    )
    feedback = _counts(
        connection,
        f"""
        SELECT feedback.category AS value, count(*)::integer AS count
        FROM review_agent.review_quality_feedback AS feedback
        JOIN review_agent.pull_requests AS pull_request
          ON pull_request.id = feedback.pull_request_id
        JOIN review_agent.repositories AS repository
          ON repository.id = pull_request.repository_id
        {feedback_filter}
        GROUP BY feedback.category ORDER BY feedback.category
        """,
        parameters,
    )
    return FindingStats(
        repository=repository,
        generated_at=now,
        findings_total=totals.findings_total,
        findings_without_decision=totals.findings_without_decision,
        findings_by_severity=severity,
        findings_by_category=category,
        findings_by_rule=rule,
        quality_feedback_by_category=feedback,
        latest_decision_by_type=decision,
        active_suppressions=totals.active_suppressions,
        active_suppressions_expiring_within_days=expiring_within_days,
        active_suppressions_nearing_expiry=totals.active_suppressions_nearing_expiry,
        repeats_after_decision_approx=totals.repeats_after_decision_approx,
    )


def _run(row: _RunRow) -> ReviewRunReport:
    return ReviewRunReport(
        id=row.id,
        repository=row.repository,
        pr_number=row.pr_number,
        base_sha=row.base_sha,
        head_sha=row.head_sha,
        status=row.status,
        phase=row.phase,
        findings_count=row.findings_count,
        failure_code=row.failure_code,
        failure_status_comment_id=row.failure_status_comment_id,
        started_at=row.started_at,
        last_heartbeat_at=row.last_heartbeat_at,
        completed_at=row.completed_at,
    )


_RUN_SELECT = """
    SELECT run.id, repository.full_name AS repository,
           pull_request.number AS pr_number, subject.base_sha, subject.head_sha,
           run.status, run.phase, run.findings_count, run.failure_code,
           run.failure_status_comment_id, run.started_at,
           run.last_heartbeat_at, run.completed_at
    FROM review_agent.review_runs AS run
    JOIN review_agent.pull_requests AS pull_request
      ON pull_request.id = run.pull_request_id
    JOIN review_agent.repositories AS repository
      ON repository.id = pull_request.repository_id
    JOIN review_agent.review_subjects AS subject
      ON subject.id = run.review_subject_id
"""


def list_runs(
    connection: psycopg.Connection[TupleRow],
    *,
    repository: str | None,
    limit: int,
    failed_only: bool,
) -> tuple[ReviewRunReport, ...]:
    _require_transaction(connection)
    conditions: list[str] = []
    parameters: list[object] = []
    if repository is not None:
        conditions.append("lower(repository.full_name) = lower(%s)")
        parameters.append(repository)
    if failed_only:
        # Superseded runs also need operator recovery because they carry a
        # terminal snapshot-superseded failure code.
        conditions.append("run.status IN ('failed', 'superseded')")
    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    parameters.append(limit)
    with connection.cursor(row_factory=class_row(_RunRow)) as cursor:
        rows = cursor.execute(
            _trusted_sql(
                f"{_RUN_SELECT} {where} "
                "ORDER BY run.started_at DESC, run.id DESC LIMIT %s"
            ),
            tuple(parameters),
        ).fetchall()
    return tuple(_run(row) for row in rows)


def runs_by_id(
    connection: psycopg.Connection[TupleRow],
    *,
    run_ids: tuple[ReviewRunId, ...],
) -> tuple[ReviewRunReport, ...]:
    """Return exact run reports in the caller's order."""
    _require_transaction(connection)
    if not run_ids:
        return ()
    with connection.cursor(row_factory=class_row(_RunRow)) as cursor:
        rows = cursor.execute(
            f"{_RUN_SELECT} WHERE run.id = ANY(%s::bigint[])",
            ([int(run_id) for run_id in run_ids],),
        ).fetchall()
    by_id = {row.id: _run(row) for row in rows}
    if len(by_id) != len(run_ids):
        raise ReportingError("one or more review runs disappeared")
    return tuple(by_id[run_id] for run_id in run_ids)


def run_stats(
    connection: psycopg.Connection[TupleRow],
    *,
    repository: str | None,
    since: datetime,
    stale_before: datetime,
    window_days: int,
    now: datetime,
) -> RunStats:
    _require_transaction(connection)
    repository_filter = (
        "" if repository is None else "AND lower(repository.full_name) = lower(%s)"
    )
    parameters: tuple[object, ...] = (
        (since,) if repository is None else (since, repository)
    )
    with connection.cursor(row_factory=class_row(_RunTotalsRow)) as cursor:
        totals = cursor.execute(
            f"""
            SELECT count(*)::integer AS total,
                   count(*) FILTER (
                       WHERE run.status = 'running'
                         AND run.last_heartbeat_at < %s
                   )::integer AS stalled_running,
                   percentile_disc(0.5) WITHIN GROUP (
                       ORDER BY extract(epoch FROM run.completed_at - run.started_at)
                   ) FILTER (WHERE run.status = 'completed') AS p50,
                   percentile_disc(0.95) WITHIN GROUP (
                       ORDER BY extract(epoch FROM run.completed_at - run.started_at)
                   ) FILTER (WHERE run.status = 'completed') AS p95,
                   avg(run.findings_count) FILTER (
                       WHERE run.status = 'completed'
                         AND run.findings_count IS NOT NULL
                   ) AS average_findings
            FROM review_agent.review_runs AS run
            JOIN review_agent.pull_requests AS pull_request
              ON pull_request.id = run.pull_request_id
            JOIN review_agent.repositories AS repository
              ON repository.id = pull_request.repository_id
            WHERE run.started_at >= %s {repository_filter}
            """,
            (stale_before, *parameters),
        ).fetchone()
    if totals is None:
        raise ReportingError("run statistics could not be computed")
    status = _counts(
        connection,
        f"""
        SELECT run.status AS value, count(*)::integer AS count
        FROM review_agent.review_runs AS run
        JOIN review_agent.pull_requests AS pull_request
          ON pull_request.id = run.pull_request_id
        JOIN review_agent.repositories AS repository
          ON repository.id = pull_request.repository_id
        WHERE run.started_at >= %s {repository_filter}
        GROUP BY run.status ORDER BY run.status
        """,
        parameters,
    )
    return RunStats(
        repository=repository,
        window_days=window_days,
        generated_at=now,
        total=totals.total,
        by_status=status,
        stalled_running=totals.stalled_running,
        time_to_answer_seconds_p50=(
            float(totals.p50) if totals.p50 is not None else None
        ),
        time_to_answer_seconds_p95=(
            float(totals.p95) if totals.p95 is not None else None
        ),
        average_findings_per_completed_run=(
            round(float(totals.average_findings), 2)
            if totals.average_findings is not None
            else None
        ),
    )


def list_publications(
    connection: psycopg.Connection[TupleRow],
    *,
    repository: str | None,
    pr_number: int | None,
    limit: int,
) -> tuple[PublicationReport, ...]:
    _require_transaction(connection)
    conditions: list[str] = []
    parameters: list[object] = []
    if repository is not None:
        conditions.append("lower(repository.full_name) = lower(%s)")
        parameters.append(repository)
    if pr_number is not None:
        conditions.append("pull_request.number = %s")
        parameters.append(pr_number)
    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    parameters.append(limit)
    with connection.cursor(row_factory=class_row(_PublicationReportRow)) as cursor:
        rows = cursor.execute(
            _trusted_sql(f"""
            SELECT publication.id, publication.review_run_id,
                   publication.review_number,
                   repository.full_name AS repository,
                   pull_request.number AS pr_number, publication.status,
                   COALESCE(parts.comment_ids, ARRAY[]::bigint[]) AS comment_ids,
                   publication.failure_code, publication.generated_at,
                   publication.posting_started_at, publication.posted_at,
                   publication.publish_failed_at, publication.superseded_at,
                   publication.superseded_by_publication_id,
                   publication.supersession_rendered_at,
                   publication.supersession_failure_code,
                   suggestion.status AS suggestion_status,
                   suggestion.external_id AS suggestion_review_id,
                   verification.status AS verification_status,
                   verification.mode AS verification_mode,
                   verification.provider AS verification_provider,
                   verification.failure_code AS verification_failure_code
            FROM review_agent.publications AS publication
            JOIN review_agent.pull_requests AS pull_request
              ON pull_request.id = publication.pull_request_id
            JOIN review_agent.repositories AS repository
              ON repository.id = pull_request.repository_id
            LEFT JOIN LATERAL (
                SELECT array_agg(part.external_id ORDER BY part.part_number)
                    FILTER (WHERE part.external_id IS NOT NULL) AS comment_ids
                FROM review_agent.publication_parts AS part
                WHERE part.publication_id = publication.id
                  AND part.part_type IN ('summary', 'continuation')
            ) AS parts ON true
            LEFT JOIN LATERAL (
                SELECT part.status, part.external_id
                FROM review_agent.publication_parts AS part
                WHERE part.publication_id = publication.id
                  AND part.part_type = 'suggestion_review'
                ORDER BY part.part_number LIMIT 1
            ) AS suggestion ON true
            LEFT JOIN LATERAL (
                SELECT verification.status, verification.mode,
                       verification.provider, verification.failure_code
                FROM review_agent.verification_runs AS verification
                WHERE verification.review_run_id = publication.review_run_id
                ORDER BY verification.id DESC LIMIT 1
            ) AS verification ON true
            {where}
            ORDER BY publication.generated_at DESC, publication.id DESC
            LIMIT %s
            """),
            tuple(parameters),
        ).fetchall()
    return tuple(
        PublicationReport(
            id=row.id,
            review_run_id=row.review_run_id,
            review_number=row.review_number,
            repository=row.repository,
            pr_number=row.pr_number,
            status=row.status,
            comment_ids=tuple(row.comment_ids),
            failure_code=row.failure_code,
            generated_at=row.generated_at,
            posting_started_at=row.posting_started_at,
            posted_at=row.posted_at,
            publish_failed_at=row.publish_failed_at,
            superseded_at=row.superseded_at,
            superseded_by_publication_id=row.superseded_by_publication_id,
            supersession_rendered_at=row.supersession_rendered_at,
            supersession_failure_code=row.supersession_failure_code,
            suggestion_status=row.suggestion_status,
            suggestion_review_id=row.suggestion_review_id,
            verification_status=row.verification_status,
            verification_mode=row.verification_mode,
            verification_provider=row.verification_provider,
            verification_failure_code=row.verification_failure_code,
        )
        for row in rows
    )


def coverage_report(
    connection: psycopg.Connection[TupleRow], *, run_id: ReviewRunId
) -> CoverageReport:
    _require_transaction(connection)
    summary = postgres_coverage.summarize(connection, run_id)
    rows = connection.execute(
        "SELECT path, diff_state, unavailable_reason FROM "
        "review_agent.review_run_files WHERE review_run_id = %s "
        "ORDER BY path",
        (run_id,),
    ).fetchall()
    material = json.dumps(
        [
            {
                "path": str(row[0]),
                "diff_state": str(row[1]),
                "unavailable_reason": str(row[2]) if row[2] is not None else None,
            }
            for row in rows
        ],
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    unavailable = tuple(str(row[0]) for row in rows if row[1] == "unavailable")
    truncated = tuple(str(row[0]) for row in rows if row[1] == "truncated")
    return CoverageReport(
        state=summary.state,
        coverage_hash="sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest(),
        changed_files_reported=summary.changed_files_reported,
        changed_files_registered=summary.changed_files_registered,
        registration_complete=summary.registration_complete,
        changed_paths_with_complete_diff=summary.changed_paths_with_complete_diff,
        changed_paths_with_source_reads=summary.changed_paths_with_source_reads,
        supporting_context_paths_read=summary.supporting_context_paths_read,
        context_ranges_read=summary.context_ranges_read,
        unavailable_paths=unavailable,
        truncated_paths=truncated,
    )


def verification_export_source(
    connection: psycopg.Connection[TupleRow], *, run_id: ReviewRunId
) -> JsonObject:
    """Build the existing bounded private-verifier input from normalized rows."""
    _require_transaction(connection)
    run = connection.execute(
        """
        SELECT run.id, repository.full_name, pull_request.number,
               subject.base_sha, subject.head_sha, run.status, run.phase,
               run.started_at, run.completed_at
        FROM review_agent.review_runs AS run
        JOIN review_agent.pull_requests AS pull_request
          ON pull_request.id = run.pull_request_id
        JOIN review_agent.repositories AS repository
          ON repository.id = pull_request.repository_id
        JOIN review_agent.review_subjects AS subject
          ON subject.id = run.review_subject_id
        WHERE run.id = %s
        """,
        (run_id,),
    ).fetchone()
    if run is None:
        raise VerificationExportUnavailable("review run was not found")
    publication = connection.execute(
        """
        SELECT id, review_run_id, review_number, status, rendered_hash, generated_at
        FROM review_agent.publications WHERE review_run_id = %s
        """,
        (run_id,),
    ).fetchone()
    if publication is None:
        raise VerificationExportUnavailable(
            "review run has no recorded publication"
        )
    findings = connection.execute(
        """
        SELECT reference.local_reference, occurrence.id, identity.fingerprint,
               identity.rule_id, occurrence.severity, occurrence.category,
               occurrence.publication_score, occurrence.confidence,
               identity.path, occurrence.line, identity.symbol, identity.anchor,
               occurrence.title, occurrence.evidence, occurrence.disproof_checks,
               occurrence.impact, occurrence.smallest_fix,
               occurrence.context_hash
        FROM review_agent.publication_findings AS published
        JOIN review_agent.finding_identities AS identity
          ON identity.id = published.finding_id
        JOIN review_agent.finding_occurrences AS occurrence
          ON occurrence.id = published.source_finding_occurrence_id
        JOIN review_agent.pull_request_finding_references AS reference
          ON reference.pull_request_id = published.pull_request_id
         AND reference.finding_id = published.finding_id
        WHERE published.publication_id = %s AND published.outcome = 'current'
        ORDER BY substring(reference.local_reference FROM 2)::integer
        """,
        (publication[0],),
    ).fetchall()
    return {
        "source_schema_version": VERIFICATION_SOURCE_SCHEMA_VERSION,
        "run": {
            "id": int(run[0]),
            "repository": str(run[1]),
            "pr_number": int(run[2]),
            "base_sha": str(run[3]),
            "head_sha": str(run[4]),
            "status": "generated" if str(run[5]) == "completed" else str(run[5]),
            "phase": str(run[6]),
            "started_at": _iso(run[7]),
            "completed_at": _iso(run[8]),
        },
        "publication": {
            "id": int(publication[0]),
            "review_run_id": int(publication[1]),
            "review_number": int(publication[2]),
            "delivery_status": str(publication[3]),
            "rendered_hash": str(publication[4]),
            "generated_at": _iso(publication[5]),
        },
        "current_findings": [
            {
                "local_reference": str(row[0]),
                "observation_id": int(row[1]),
                "fingerprint": str(row[2]),
                "rule_id": str(row[3]),
                "severity": str(row[4]),
                "category": str(row[5]),
                "publication_score": int(row[6]),
                "confidence": float(cast(Decimal, row[7])),
                "path": str(row[8]),
                "line": int(row[9]),
                "symbol": str(row[10] or ""),
                "anchor": str(row[11]),
                "title": str(row[12]),
                "evidence": str(row[13]),
                "disproof_checks": str(row[14]),
                "impact": str(row[15]),
                "smallest_fix": str(row[16]),
                "introduced_by_diff": 1,
                "context_hash": str(row[17]),
            }
            for row in findings
        ],
    }


def _iso(value: object) -> str:
    return value.isoformat() if isinstance(value, datetime) else ""


def _json_value(value: object) -> JsonValue:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        raw = cast(Mapping[object, object], value)
        output: dict[str, JsonValue] = {}
        for key, item in raw.items():
            if not isinstance(key, str):
                raise ReportingError("export JSON object key is not text")
            output[key] = _json_value(item)
        return output
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        return [_json_value(item) for item in cast(Sequence[object], value)]
    raise ReportingError(
        f"export query returned unsupported {type(value).__name__} value"
    )


def _bounded_rows(
    connection: psycopg.Connection[TupleRow],
    *,
    query: str,
    parameters: tuple[object, ...],
    row_limit: int,
) -> tuple[tuple[JsonObject, ...], bool]:
    cursor = connection.execute(
        _trusted_sql(query), (*parameters, row_limit + 1)
    )
    names = tuple(column.name for column in cursor.description or ())
    fetched = cursor.fetchall()
    rows: list[JsonObject] = []
    for raw_row in fetched[:row_limit]:
        row = cast(tuple[object, ...], raw_row)
        rows.append(
            {name: _json_value(value) for name, value in zip(names, row, strict=True)}
        )
    return tuple(rows), len(fetched) > row_limit


_EXPORT_QUERIES: tuple[tuple[str, str], ...] = (
    (
        "findings",
        """SELECT identity.id, repository.full_name AS repository,
                  identity.fingerprint, identity.rule_id, identity.path,
                  identity.symbol, identity.anchor, identity.first_seen_at,
                  identity.last_seen_at
           FROM review_agent.finding_identities AS identity
           JOIN review_agent.repositories AS repository
             ON repository.id = identity.repository_id
           WHERE identity.repository_id = %s ORDER BY identity.id LIMIT %s""",
    ),
    (
        "review_subjects",
        """SELECT subject.id, pull_request.number AS pr_number,
                  subject.base_sha, subject.head_sha, subject.policy_revision,
                  subject.resolved_config_schema_version,
                  subject.resolved_config, subject.resolved_config_hash,
                  subject.created_at
           FROM review_agent.review_subjects AS subject
           JOIN review_agent.pull_requests AS pull_request
             ON pull_request.id = subject.pull_request_id
           WHERE pull_request.repository_id = %s
           ORDER BY subject.id LIMIT %s""",
    ),
    (
        "finding_observations",
        """SELECT occurrence.id, repository.full_name AS repository,
                  pull_request.number AS pr_number, subject.head_sha,
                  identity.fingerprint, identity.rule_id, identity.path,
                  occurrence.line, identity.symbol, identity.anchor,
                  occurrence.title, occurrence.severity, occurrence.category,
                  occurrence.publication_score, occurrence.confidence,
                  occurrence.context_hash, occurrence.evidence,
                  occurrence.disproof_checks, occurrence.impact,
                  occurrence.smallest_fix, occurrence.observed_at
           FROM review_agent.finding_occurrences AS occurrence
           JOIN review_agent.finding_identities AS identity
             ON identity.id = occurrence.finding_id
           JOIN review_agent.review_runs AS run ON run.id = occurrence.review_run_id
           JOIN review_agent.review_subjects AS subject
             ON subject.id = run.review_subject_id
           JOIN review_agent.pull_requests AS pull_request
             ON pull_request.id = run.pull_request_id
           JOIN review_agent.repositories AS repository
             ON repository.id = pull_request.repository_id
           WHERE repository.id = %s ORDER BY occurrence.id LIMIT %s""",
    ),
    (
        "review_suggestions",
        """SELECT suggestion.id,
                  suggestion.finding_occurrence_id AS observation_id,
                  suggestion.start_line, suggestion.end_line,
                  suggestion.expected_hash, suggestion.replacement_text,
                  suggestion.suggestion_key, suggestion.recorded_at
           FROM review_agent.finding_suggestions AS suggestion
           JOIN review_agent.finding_occurrences AS occurrence
             ON occurrence.id = suggestion.finding_occurrence_id
           WHERE occurrence.repository_id = %s
           ORDER BY suggestion.id LIMIT %s""",
    ),
    (
        "decisions",
        """SELECT decision.id, repository.full_name AS repository,
                  identity.fingerprint,
                  decision.finding_occurrence_id AS observation_id,
                  decision.decision, decision.reason, decision.actor,
                  decision.context_hash, decision.adr_id,
                  decision.created_at, decision.expires_at
           FROM review_agent.finding_decisions AS decision
           JOIN review_agent.finding_identities AS identity
             ON identity.id = decision.finding_id
           JOIN review_agent.repositories AS repository
             ON repository.id = identity.repository_id
           WHERE repository.id = %s ORDER BY decision.id LIMIT %s""",
    ),
    (
        "review_run_files",
        """SELECT file.id, file.review_run_id AS run_id, file.path,
                  file.change_status, file.previous_path, file.is_changed_path,
                  file.domain, file.review_mode, file.diff_state,
                  file.unavailable_reason, file.registered_at,
                  file.diff_observed_at
           FROM review_agent.review_run_files AS file
           JOIN review_agent.review_runs AS run
             ON run.id = file.review_run_id
           JOIN review_agent.pull_requests AS pull_request
             ON pull_request.id = run.pull_request_id
           WHERE pull_request.repository_id = %s
           ORDER BY file.id LIMIT %s""",
    ),
    (
        "pr_finding_references",
        """SELECT reference.id, repository.full_name AS repository,
                  pull_request.number AS pr_number, identity.fingerprint,
                  reference.local_reference, reference.first_assigned_at
           FROM review_agent.pull_request_finding_references AS reference
           JOIN review_agent.pull_requests AS pull_request
             ON pull_request.id = reference.pull_request_id
           JOIN review_agent.repositories AS repository
             ON repository.id = reference.repository_id
           JOIN review_agent.finding_identities AS identity
             ON identity.id = reference.finding_id
           WHERE repository.id = %s ORDER BY reference.id LIMIT %s""",
    ),
    (
        "publication_parts",
        """SELECT part.id, part.publication_id, part.part_type,
                  part.part_number, part.external_id,
                  part.payload_schema_version, part.payload,
                  part.payload_hash, part.status, part.posting_started_at,
                  part.posted_at, part.failure_at, part.failure_code
           FROM review_agent.publication_parts AS part
           JOIN review_agent.publications AS publication
             ON publication.id = part.publication_id
           JOIN review_agent.pull_requests AS pull_request
             ON pull_request.id = publication.pull_request_id
           WHERE pull_request.repository_id = %s
           ORDER BY part.id LIMIT %s""",
    ),
    (
        "publication_findings",
        """SELECT published.id, published.publication_id,
                  published.publication_review_run_id,
                  published.source_review_run_id,
                  published.source_finding_occurrence_id AS observation_id,
                  identity.fingerprint, published.local_reference,
                  published.outcome AS status, published.outcome_evidence
           FROM review_agent.publication_findings AS published
           JOIN review_agent.finding_identities AS identity
             ON identity.id = published.finding_id
           WHERE identity.repository_id = %s
           ORDER BY published.id LIMIT %s""",
    ),
    (
        "review_quality_feedback",
        """SELECT feedback.id, repository.full_name AS repository,
                  pull_request.number AS pr_number, feedback.publication_id,
                  feedback.local_reference, feedback.category, feedback.reason,
                  feedback.actor_user_id, feedback.actor_login,
                  feedback.author_association, feedback.source_comment_id,
                  feedback.source_comment_url, feedback.created_at
           FROM review_agent.review_quality_feedback AS feedback
           JOIN review_agent.pull_requests AS pull_request
             ON pull_request.id = feedback.pull_request_id
           JOIN review_agent.repositories AS repository
             ON repository.id = pull_request.repository_id
           WHERE repository.id = %s ORDER BY feedback.id LIMIT %s""",
    ),
    (
        "review_runs",
        """SELECT run.id, repository.full_name AS repository,
                  pull_request.number AS pr_number, run.status, run.phase,
                  run.findings_count, run.failure_code, run.started_at,
                  run.last_heartbeat_at, run.completed_at
           FROM review_agent.review_runs AS run
           JOIN review_agent.pull_requests AS pull_request
             ON pull_request.id = run.pull_request_id
           JOIN review_agent.repositories AS repository
             ON repository.id = pull_request.repository_id
           WHERE repository.id = %s ORDER BY run.id LIMIT %s""",
    ),
    (
        "review_publications",
        """SELECT publication.id, publication.review_run_id,
                  publication.review_number, repository.full_name AS repository,
                  pull_request.number AS pr_number, publication.status,
                  publication.rendered_hash, publication.generated_at,
                  publication.posted_at, publication.failure_code,
                  publication.superseded_by_publication_id
           FROM review_agent.publications AS publication
           JOIN review_agent.pull_requests AS pull_request
             ON pull_request.id = publication.pull_request_id
           JOIN review_agent.repositories AS repository
             ON repository.id = pull_request.repository_id
           WHERE repository.id = %s ORDER BY publication.id LIMIT %s""",
    ),
    (
        "review_verification_runs",
        """SELECT verification.id, verification.review_run_id,
                  verification.provider, verification.model, verification.mode,
                  verification.status, verification.bundle_hash,
                  verification.failure_code, verification.started_at,
                  verification.completed_at
           FROM review_agent.verification_runs AS verification
           JOIN review_agent.review_runs AS run
             ON run.id = verification.review_run_id
           JOIN review_agent.pull_requests AS pull_request
             ON pull_request.id = run.pull_request_id
           WHERE pull_request.repository_id = %s
           ORDER BY verification.id LIMIT %s""",
    ),
    (
        "candidate_verifications",
        """SELECT candidate.id, candidate.verification_run_id,
                  candidate.review_run_id,
                  candidate.finding_occurrence_id AS observation_id,
                  candidate.verdict, candidate.confidence,
                  candidate.counter_evidence, candidate.notes,
                  candidate.created_at
           FROM review_agent.candidate_verifications AS candidate
           JOIN review_agent.review_runs AS run
             ON run.id = candidate.review_run_id
           JOIN review_agent.pull_requests AS pull_request
             ON pull_request.id = run.pull_request_id
           WHERE pull_request.repository_id = %s
           ORDER BY candidate.id LIMIT %s""",
    ),
    (
        "candidate_reconciliations",
        """SELECT reconciliation.id, reconciliation.review_run_id,
                  reconciliation.finding_occurrence_id AS observation_id,
                  reconciliation.verification_run_id,
                  reconciliation.final_decision,
                  reconciliation.reason, reconciliation.created_at
           FROM review_agent.candidate_reconciliations AS reconciliation
           JOIN review_agent.review_runs AS run
             ON run.id = reconciliation.review_run_id
           JOIN review_agent.pull_requests AS pull_request
             ON pull_request.id = run.pull_request_id
           WHERE pull_request.repository_id = %s
           ORDER BY reconciliation.id LIMIT %s""",
    ),
    (
        "coach_runs",
        """SELECT run.id, repository.full_name AS repository,
                  run.source_event_set_id, run.source_snapshot_id,
                  run.proposal_set_id, run.events_considered,
                  run.artifact_dir, run.recorded_at
           FROM review_agent.coach_runs AS run
           JOIN review_agent.repositories AS repository
             ON repository.id = run.repository_id
           WHERE run.repository_id = %s ORDER BY run.id LIMIT %s""",
    ),
    (
        "coach_candidates",
        """SELECT candidate.id, candidate.coach_run_id,
                  candidate.candidate_key, candidate.target_owner,
                  candidate.suggested_route, candidate.event_type,
                  candidate.independent_episode_count,
                  candidate.evidence_event_ids,
                  candidate.evidence_events_total
           FROM review_agent.coach_candidates AS candidate
           JOIN review_agent.coach_runs AS run
             ON run.id = candidate.coach_run_id
           WHERE run.repository_id = %s
           ORDER BY candidate.id LIMIT %s""",
    ),
)


def export_repository(
    connection: psycopg.Connection[TupleRow],
    *,
    scope: RepositoryScope,
    row_limit: int,
    now: datetime,
) -> RepositoryExport:
    """Export one repository with an explicit per-table row budget."""
    _require_repeatable_read(connection)
    tables: list[tuple[str, tuple[JsonObject, ...]]] = []
    truncated: list[str] = []
    for name, query in _EXPORT_QUERIES:
        rows, was_truncated = _bounded_rows(
            connection,
            query=query,
            parameters=(scope.id,),
            row_limit=row_limit,
        )
        tables.append((name, rows))
        if was_truncated:
            truncated.append(name)
    return RepositoryExport(
        repository=scope.full_name,
        exported_at=now,
        row_limit=row_limit,
        truncated_tables=tuple(truncated),
        rows_by_table=tuple(tables),
    )
