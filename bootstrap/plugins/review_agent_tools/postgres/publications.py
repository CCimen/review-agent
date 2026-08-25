"""PostgreSQL publication plan and delivery-state operations."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

import psycopg
from psycopg import errors
from psycopg.pq import TransactionStatus
from psycopg.rows import TupleRow, class_row
from psycopg.types.json import Jsonb

from ..domain.publication import (
    CanonicalPayload,
    PublicationDomainError,
    PublicationFindingDefinition,
    PublicationFindingOutcome,
    PublicationDelivery,
    PublicationId,
    PublicationPartDefinition,
    PublicationPartStatus,
    PublicationPartType,
    PublicationPlan,
    PublicationStatus,
    decode_publication_delivery,
    resolve_rendered_blocks,
)
from ..domain.review import PullRequestId, ReviewRunId, ReviewStatus


class PublicationStoreError(ValueError):
    """A publication operation violates its durable lifecycle contract."""


class PublicationNotFound(PublicationStoreError):
    """The requested publication does not exist."""


class PublicationConflict(PublicationStoreError):
    """Submitted publication facts conflict with the persisted review."""


class InvalidPublicationTransition(PublicationStoreError):
    """The requested publication state transition is not valid."""


class PublicationBusy(PublicationStoreError):
    """The pull-request publication lifecycle is currently busy."""


@dataclass(frozen=True, slots=True)
class StoredPublicationPart:
    id: int
    part_type: PublicationPartType
    part_number: int
    payload_schema_version: int
    payload_hash: str
    delivery: PublicationDelivery
    status: PublicationPartStatus
    external_id: int | None
    posting_started_at: datetime | None


@dataclass(frozen=True, slots=True)
class StoredPublication:
    id: PublicationId
    pull_request_id: PullRequestId
    review_run_id: ReviewRunId
    review_number: int
    repository: str
    pr_number: int
    base_sha: str
    head_sha: str
    plan: PublicationPlan
    status: PublicationStatus
    posting_started_at: datetime | None
    delivery_attempt_count: int
    delivery_max_attempts: int
    delivery_lease_owner: str | None
    delivery_lease_generation: int
    delivery_lease_expires_at: datetime | None
    delivery_recovery_count: int
    parts: tuple[StoredPublicationPart, ...]


@dataclass(frozen=True, slots=True)
class _RunScope:
    pull_request_id: PullRequestId
    status: str
    phase: str


@dataclass(frozen=True, slots=True)
class _PublicationRow:
    id: PublicationId
    pull_request_id: PullRequestId
    review_run_id: ReviewRunId
    review_number: int
    repository: str
    pr_number: int
    base_sha: str
    head_sha: str
    publication_key: str
    rendered_markdown: str
    rendered_blocks_schema_version: int
    rendered_blocks: object
    rendered_hash: str
    status: str
    posting_started_at: datetime | None
    delivery_attempt_count: int
    delivery_max_attempts: int
    delivery_lease_owner: str | None
    delivery_lease_generation: int
    delivery_lease_expires_at: datetime | None
    delivery_recovery_count: int


@dataclass(frozen=True, slots=True)
class _PartRow:
    id: int
    part_type: str
    part_number: int
    external_id: int | None
    payload_schema_version: int
    payload: object
    payload_hash: str
    status: str
    posting_started_at: datetime | None


@dataclass(frozen=True, slots=True)
class _FindingRow:
    finding_id: int
    source_finding_occurrence_id: int
    source_review_run_id: int
    local_reference: str
    outcome: str
    outcome_evidence: str | None


@dataclass(frozen=True, slots=True)
class PublicationClaim:
    publication: StoredPublication
    acquired: bool


class PublicationLeaseLost(PublicationStoreError):
    """A publisher no longer owns the exact delivery generation."""


_DIRECT_LEASE_OWNER = "direct-publication-call"
_DIRECT_LEASE_DURATION = timedelta(minutes=5)


@dataclass(frozen=True, slots=True)
class HistoricalPublication:
    publication_id: PublicationId
    review_number: int
    repository: str
    pr_number: int
    head_sha: str
    publication_key: str
    rendered_markdown: str
    rendered_blocks_json: str
    comment_ids: tuple[int, ...]
    current_findings_count: int
    superseding_review_number: int
    superseding_comment_id: int


@dataclass(frozen=True, slots=True)
class SupersessionResult:
    publication_id: PublicationId
    rendered_at: datetime
    failure_code: str | None


@dataclass(frozen=True, slots=True)
class PreparationFinding:
    finding_id: int
    occurrence_id: int
    source_run_id: int
    local_reference: str
    fingerprint: str
    rule_id: str
    path: str
    line: int
    title: str
    severity: str
    category: str
    publication_score: int
    confidence: float
    context_hash: str
    evidence: str
    disproof_checks: str
    impact: str
    smallest_fix: str
    suppressed: bool
    suggestion: PreparationSuggestion | None


@dataclass(frozen=True, slots=True)
class PreparationSuggestion:
    path: str
    start_line: int
    end_line: int
    replacement_text: str
    suggestion_key: str


@dataclass(frozen=True, slots=True)
class PreviousPublicationFinding:
    finding_id: int
    occurrence_id: int
    source_run_id: int
    local_reference: str
    fingerprint: str
    title: str
    context_hash: str
    suppressed: bool


@dataclass(frozen=True, slots=True)
class PreparationCoverage:
    state: str
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
class PublicationPreparationContext:
    run_id: int
    repository: str
    pr_number: int
    base_sha: str
    head_sha: str
    policy_revision: str
    review_number: int
    previous_review_number: int | None
    previous_head_sha: str
    current: tuple[PreparationFinding, ...]
    previous: tuple[PreviousPublicationFinding, ...]
    published_fingerprints: frozenset[str]
    dropped_occurrence_ids: frozenset[int]
    dropped_reasons: tuple[tuple[int, str], ...]
    coverage: PreparationCoverage


@dataclass(frozen=True, slots=True)
class _PreparationScopeRow:
    repository: str
    pr_number: int
    base_sha: str
    head_sha: str
    policy_revision: str
    review_number: int
    previous_review_number: int | None
    previous_head_sha: str | None


@dataclass(frozen=True, slots=True)
class _PreparationFindingRow:
    finding_id: int
    occurrence_id: int
    source_run_id: int
    local_reference: str
    fingerprint: str
    rule_id: str
    path: str
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
    suppressed: bool
    suggestion_path: str | None
    suggestion_start_line: int | None
    suggestion_end_line: int | None
    suggestion_replacement_text: str | None
    suggestion_key: str | None


@dataclass(frozen=True, slots=True)
class _PreviousFindingRow:
    finding_id: int
    occurrence_id: int
    source_run_id: int
    local_reference: str
    fingerprint: str
    title: str
    context_hash: str
    suppressed: bool


def preparation_context(
    connection: psycopg.Connection[TupleRow], *, run_id: ReviewRunId
) -> PublicationPreparationContext:
    """Lock one run and load every fact needed to freeze its publication."""
    _require_transaction(connection)
    scope_lock = _run_scope(connection, run_id)
    with connection.cursor(row_factory=class_row(_PreparationScopeRow)) as cursor:
        scope = cursor.execute(
            """
            SELECT repository.full_name AS repository,
                   pull_request.number AS pr_number,
                   subject.base_sha, subject.head_sha, subject.policy_revision,
                   COALESCE(max(all_publication.review_number), 0) + 1 AS review_number,
                   current_publication.review_number AS previous_review_number,
                   previous_subject.head_sha AS previous_head_sha
            FROM review_agent.review_runs AS run
            JOIN review_agent.pull_requests AS pull_request
              ON pull_request.id = run.pull_request_id
            JOIN review_agent.repositories AS repository
              ON repository.id = pull_request.repository_id
            JOIN review_agent.review_subjects AS subject
              ON subject.id = run.review_subject_id
            LEFT JOIN review_agent.publications AS all_publication
              ON all_publication.pull_request_id = run.pull_request_id
            LEFT JOIN review_agent.publications AS current_publication
              ON current_publication.pull_request_id = run.pull_request_id
             AND current_publication.status = 'posted'
             AND current_publication.superseded_by_publication_id IS NULL
            LEFT JOIN review_agent.review_runs AS previous_run
              ON previous_run.id = current_publication.review_run_id
            LEFT JOIN review_agent.review_subjects AS previous_subject
              ON previous_subject.id = previous_run.review_subject_id
            WHERE run.id = %s
            GROUP BY repository.full_name, pull_request.number,
                     subject.base_sha, subject.head_sha, subject.policy_revision,
                     current_publication.review_number, previous_subject.head_sha
            """,
            (run_id,),
        ).fetchone()
    if scope is None:
        raise PublicationConflict("review run scope does not exist")
    if scope_lock.pull_request_id < 1:
        raise PublicationConflict("review run has an invalid pull request")

    with connection.cursor(row_factory=class_row(_PreparationFindingRow)) as cursor:
        current_rows = cursor.execute(
            """
            SELECT identity.id AS finding_id, occurrence.id AS occurrence_id,
                   occurrence.review_run_id AS source_run_id,
                   reference.local_reference, identity.fingerprint,
                   identity.rule_id, identity.path, occurrence.line,
                   occurrence.title, occurrence.severity, occurrence.category,
                   occurrence.publication_score, occurrence.confidence,
                   occurrence.context_hash, occurrence.evidence,
                   occurrence.disproof_checks, occurrence.impact,
                   occurrence.smallest_fix,
                   COALESCE(
                       decision.decision IN (
                           'false_positive', 'intentional_by_design',
                           'accepted_risk', 'duplicate'
                       )
                       AND decision.context_hash = occurrence.context_hash
                       AND (
                           decision.expires_at IS NULL
                           OR decision.expires_at > statement_timestamp()
                       ), false
                   ) AS suppressed,
                   identity.path AS suggestion_path,
                   suggestion.start_line AS suggestion_start_line,
                   suggestion.end_line AS suggestion_end_line,
                   suggestion.replacement_text AS suggestion_replacement_text,
                   suggestion.suggestion_key
            FROM review_agent.finding_occurrences AS occurrence
            JOIN review_agent.finding_identities AS identity
              ON identity.id = occurrence.finding_id
            JOIN review_agent.pull_request_finding_references AS reference
              ON reference.pull_request_id = occurrence.pull_request_id
             AND reference.finding_id = occurrence.finding_id
            LEFT JOIN LATERAL (
                SELECT finding_decision.decision,
                       finding_decision.context_hash,
                       finding_decision.expires_at
                FROM review_agent.finding_decisions AS finding_decision
                WHERE finding_decision.finding_id = occurrence.finding_id
                ORDER BY finding_decision.id DESC LIMIT 1
            ) AS decision ON true
            LEFT JOIN review_agent.finding_suggestions AS suggestion
              ON suggestion.finding_occurrence_id = occurrence.id
            WHERE occurrence.review_run_id = %s
            ORDER BY reference.local_reference
            """,
            (run_id,),
        ).fetchall()

    with connection.cursor(row_factory=class_row(_PreviousFindingRow)) as cursor:
        previous_rows = cursor.execute(
            """
            SELECT identity.id AS finding_id,
                   item.source_finding_occurrence_id AS occurrence_id,
                   item.source_review_run_id AS source_run_id,
                   item.local_reference, identity.fingerprint,
                   occurrence.title, occurrence.context_hash,
                   COALESCE(
                       decision.decision IN (
                           'false_positive', 'intentional_by_design',
                           'accepted_risk', 'duplicate'
                       )
                       AND decision.context_hash = occurrence.context_hash
                       AND (
                           decision.expires_at IS NULL
                           OR decision.expires_at > statement_timestamp()
                       ), false
                   ) AS suppressed
            FROM review_agent.publications AS publication
            JOIN review_agent.publication_findings AS item
              ON item.publication_id = publication.id
            JOIN review_agent.finding_identities AS identity
              ON identity.id = item.finding_id
            JOIN review_agent.finding_occurrences AS occurrence
              ON occurrence.id = item.source_finding_occurrence_id
            LEFT JOIN LATERAL (
                SELECT finding_decision.decision,
                       finding_decision.context_hash,
                       finding_decision.expires_at
                FROM review_agent.finding_decisions AS finding_decision
                WHERE finding_decision.finding_id = item.finding_id
                ORDER BY finding_decision.id DESC LIMIT 1
            ) AS decision ON true
            WHERE publication.pull_request_id = %s
              AND publication.status = 'posted'
              AND publication.superseded_by_publication_id IS NULL
              AND item.outcome IN ('current', 'not_checked')
            ORDER BY item.local_reference
            """,
            (scope_lock.pull_request_id,),
        ).fetchall()

    published_fingerprint_rows = connection.execute(
        """
        SELECT DISTINCT identity.fingerprint
        FROM review_agent.publications AS publication
        JOIN review_agent.publication_findings AS item
          ON item.publication_id = publication.id
        JOIN review_agent.finding_identities AS identity
          ON identity.id = item.finding_id
        WHERE publication.pull_request_id = %s
          AND publication.status = 'posted'
        """,
        (scope_lock.pull_request_id,),
    ).fetchall()

    reconciliation_rows = connection.execute(
        """
        SELECT finding_occurrence_id, COALESCE(reason, '')
        FROM review_agent.candidate_reconciliations
        WHERE review_run_id = %s AND final_decision = 'drop'
        ORDER BY finding_occurrence_id
        """,
        (run_id,),
    ).fetchall()

    from . import coverage as postgres_coverage

    coverage = postgres_coverage.summarize(connection, run_id)
    path_rows = connection.execute(
        """
        SELECT path, diff_state
        FROM review_agent.review_run_files
        WHERE review_run_id = %s
          AND diff_state IN ('unavailable', 'truncated')
        ORDER BY path LIMIT 20
        """,
        (run_id,),
    ).fetchall()

    def suggestion(row: _PreparationFindingRow) -> PreparationSuggestion | None:
        if row.suggestion_key is None:
            return None
        if (
            row.suggestion_path is None
            or row.suggestion_start_line is None
            or row.suggestion_end_line is None
            or row.suggestion_replacement_text is None
        ):
            raise PublicationConflict("stored suggestion is incomplete")
        return PreparationSuggestion(
            path=row.suggestion_path,
            start_line=row.suggestion_start_line,
            end_line=row.suggestion_end_line,
            replacement_text=row.suggestion_replacement_text,
            suggestion_key=row.suggestion_key,
        )

    return PublicationPreparationContext(
        run_id=int(run_id),
        repository=scope.repository,
        pr_number=scope.pr_number,
        base_sha=scope.base_sha,
        head_sha=scope.head_sha,
        policy_revision=scope.policy_revision,
        review_number=scope.review_number,
        previous_review_number=scope.previous_review_number,
        previous_head_sha=scope.previous_head_sha or "",
        current=tuple(
            PreparationFinding(
                finding_id=row.finding_id,
                occurrence_id=row.occurrence_id,
                source_run_id=row.source_run_id,
                local_reference=row.local_reference,
                fingerprint=row.fingerprint,
                rule_id=row.rule_id,
                path=row.path,
                line=row.line,
                title=row.title,
                severity=row.severity,
                category=row.category,
                publication_score=row.publication_score,
                confidence=float(row.confidence),
                context_hash=row.context_hash,
                evidence=row.evidence,
                disproof_checks=row.disproof_checks,
                impact=row.impact,
                smallest_fix=row.smallest_fix,
                suppressed=row.suppressed,
                suggestion=suggestion(row),
            )
            for row in current_rows
        ),
        previous=tuple(
            PreviousPublicationFinding(
                finding_id=row.finding_id,
                occurrence_id=row.occurrence_id,
                source_run_id=row.source_run_id,
                local_reference=row.local_reference,
                fingerprint=row.fingerprint,
                title=row.title,
                context_hash=row.context_hash,
                suppressed=row.suppressed,
            )
            for row in previous_rows
        ),
        published_fingerprints=frozenset(
            str(row[0]) for row in published_fingerprint_rows
        ),
        dropped_occurrence_ids=frozenset(int(row[0]) for row in reconciliation_rows),
        dropped_reasons=tuple(
            (int(row[0]), str(row[1])) for row in reconciliation_rows
        ),
        coverage=PreparationCoverage(
            state=coverage.state.value,
            changed_files_reported=coverage.changed_files_reported,
            changed_files_registered=coverage.changed_files_registered,
            registration_complete=coverage.registration_complete,
            changed_paths_with_complete_diff=coverage.changed_paths_with_complete_diff,
            changed_paths_with_source_reads=coverage.changed_paths_with_source_reads,
            supporting_context_paths_read=coverage.supporting_context_paths_read,
            context_ranges_read=coverage.context_ranges_read,
            unavailable_paths=tuple(
                str(row[0]) for row in path_rows if str(row[1]) == "unavailable"
            ),
            truncated_paths=tuple(
                str(row[0]) for row in path_rows if str(row[1]) == "truncated"
            ),
        ),
    )


def _require_transaction(connection: psycopg.Connection[TupleRow]) -> None:
    if connection.info.transaction_status != TransactionStatus.INTRANS:
        raise PublicationStoreError(
            "publication operations require an active transaction"
        )


def _run_scope(
    connection: psycopg.Connection[TupleRow], run_id: ReviewRunId
) -> _RunScope:
    with connection.cursor(row_factory=class_row(_RunScope)) as cursor:
        scope = cursor.execute(
            """
            SELECT pull_request_id, status, phase
            FROM review_agent.review_runs
            WHERE id = %s
            """,
            (run_id,),
        ).fetchone()
    if scope is None:
        raise PublicationConflict("review run does not exist")
    try:
        connection.execute(
            "SELECT id FROM review_agent.pull_requests WHERE id = %s "
            "FOR NO KEY UPDATE NOWAIT",
            (scope.pull_request_id,),
        ).fetchone()
    except errors.LockNotAvailable as exc:
        raise PublicationBusy("pull request is busy preparing publication") from exc
    with connection.cursor(row_factory=class_row(_RunScope)) as cursor:
        locked = cursor.execute(
            """
            SELECT pull_request_id, status, phase
            FROM review_agent.review_runs
            WHERE id = %s
            FOR UPDATE
            """,
            (run_id,),
        ).fetchone()
    if locked is None or locked.pull_request_id != scope.pull_request_id:
        raise PublicationConflict("review run scope changed during preparation")
    if locked.status != "running" or locked.phase not in {"rendering", "publishing"}:
        raise InvalidPublicationTransition(
            "publication preparation requires a rendering-phase review run"
        )
    return locked


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _part_rows(
    connection: psycopg.Connection[TupleRow], publication_id: PublicationId
) -> tuple[_PartRow, ...]:
    with connection.cursor(row_factory=class_row(_PartRow)) as cursor:
        rows = cursor.execute(
            """
            SELECT id, part_type, part_number, external_id,
                   payload_schema_version, payload, payload_hash, status,
                   posting_started_at
            FROM review_agent.publication_parts
            WHERE publication_id = %s
            ORDER BY
                CASE part_type
                    WHEN 'suggestion_review' THEN 0
                    WHEN 'summary' THEN 1
                    ELSE 2
                END,
                part_number
            """,
            (publication_id,),
        ).fetchall()
    return tuple(rows)


def _finding_rows(
    connection: psycopg.Connection[TupleRow], publication_id: PublicationId
) -> tuple[_FindingRow, ...]:
    with connection.cursor(row_factory=class_row(_FindingRow)) as cursor:
        rows = cursor.execute(
            """
            SELECT finding_id, source_finding_occurrence_id,
                   source_review_run_id, local_reference, outcome,
                   outcome_evidence
            FROM review_agent.publication_findings
            WHERE publication_id = %s
            ORDER BY local_reference
            """,
            (publication_id,),
        ).fetchall()
    return tuple(rows)


def _stored(
    connection: psycopg.Connection[TupleRow], row: _PublicationRow
) -> StoredPublication:
    part_rows = _part_rows(connection, row.id)
    finding_rows = _finding_rows(connection, row.id)
    try:
        rendered_blocks_json = resolve_rendered_blocks(
            row.rendered_blocks,
            schema_version=row.rendered_blocks_schema_version,
            rendered_markdown=row.rendered_markdown,
        )
    except PublicationDomainError as exc:
        raise PublicationConflict("stored rendered blocks are invalid") from exc
    rendered_hash = hashlib.sha256(
        row.rendered_markdown.encode("utf-8")
    ).hexdigest()
    if row.rendered_hash != rendered_hash:
        raise PublicationConflict("stored publication hash does not match Markdown")
    part_definitions: list[PublicationPartDefinition] = []
    for item in part_rows:
        payload_json = _canonical_json(item.payload)
        payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        if item.payload_hash != payload_hash:
            raise PublicationConflict("stored publication part hash does not match payload")
        part_type = PublicationPartType(item.part_type)
        part_definitions.append(
            PublicationPartDefinition(
                part_type=part_type,
                part_number=item.part_number,
                payload=CanonicalPayload(
                    schema_version=item.payload_schema_version,
                    canonical_json=payload_json,
                    sha256=payload_hash,
                ),
                delivery=decode_publication_delivery(
                    part_type=part_type,
                    part_number=item.part_number,
                    payload_schema_version=item.payload_schema_version,
                    payload_json=payload_json,
                    publication_key=row.publication_key,
                ),
            )
        )
    plan = PublicationPlan(
        publication_key=row.publication_key,
        rendered_markdown=row.rendered_markdown,
        rendered_blocks_schema_version=row.rendered_blocks_schema_version,
        rendered_blocks_json=rendered_blocks_json,
        rendered_hash=rendered_hash,
        parts=tuple(part_definitions),
        findings=tuple(
            PublicationFindingDefinition(
                finding_id=item.finding_id,
                source_finding_occurrence_id=item.source_finding_occurrence_id,
                source_review_run_id=item.source_review_run_id,
                local_reference=item.local_reference,
                outcome=PublicationFindingOutcome(item.outcome),
                outcome_evidence=item.outcome_evidence,
            )
            for item in finding_rows
        ),
    )
    return StoredPublication(
        id=row.id,
        pull_request_id=row.pull_request_id,
        review_run_id=row.review_run_id,
        review_number=row.review_number,
        repository=row.repository,
        pr_number=row.pr_number,
        base_sha=row.base_sha,
        head_sha=row.head_sha,
        plan=plan,
        status=PublicationStatus(row.status),
        posting_started_at=row.posting_started_at,
        delivery_attempt_count=row.delivery_attempt_count,
        delivery_max_attempts=row.delivery_max_attempts,
        delivery_lease_owner=row.delivery_lease_owner,
        delivery_lease_generation=row.delivery_lease_generation,
        delivery_lease_expires_at=row.delivery_lease_expires_at,
        delivery_recovery_count=row.delivery_recovery_count,
        parts=tuple(
            StoredPublicationPart(
                id=item.id,
                part_type=PublicationPartType(item.part_type),
                part_number=item.part_number,
                payload_schema_version=item.payload_schema_version,
                payload_hash=item.payload_hash,
                delivery=definition.delivery,
                status=PublicationPartStatus(item.status),
                external_id=item.external_id,
                posting_started_at=item.posting_started_at,
            )
            for item, definition in zip(part_rows, part_definitions, strict=True)
        ),
    )


def _publication_row(
    connection: psycopg.Connection[TupleRow],
    publication_id: PublicationId,
    *,
    for_update: bool = False,
) -> _PublicationRow | None:
    lock = " FOR UPDATE OF publication" if for_update else ""
    with connection.cursor(row_factory=class_row(_PublicationRow)) as cursor:
        return cursor.execute(
            f"""
            SELECT publication.id, publication.pull_request_id,
                   publication.review_run_id, publication.review_number,
                   repository.full_name AS repository,
                   pull_request.number AS pr_number,
                   subject.base_sha, subject.head_sha,
                   publication.publication_key,
                   publication.rendered_markdown,
                   publication.rendered_blocks_schema_version,
                   publication.rendered_blocks, publication.rendered_hash,
                   publication.status, publication.posting_started_at,
                   publication.delivery_attempt_count,
                   publication.delivery_max_attempts,
                   publication.delivery_lease_owner,
                   publication.delivery_lease_generation,
                   publication.delivery_lease_expires_at,
                   publication.delivery_recovery_count
            FROM review_agent.publications AS publication
            JOIN review_agent.pull_requests AS pull_request
              ON pull_request.id = publication.pull_request_id
            JOIN review_agent.repositories AS repository
              ON repository.id = pull_request.repository_id
            JOIN review_agent.review_runs AS run
              ON run.id = publication.review_run_id
            JOIN review_agent.review_subjects AS subject
              ON subject.id = run.review_subject_id
            WHERE publication.id = %s{lock}
            """,
            (publication_id,),
        ).fetchone()


def get_publication(
    connection: psycopg.Connection[TupleRow], publication_id: PublicationId
) -> StoredPublication:
    """Load one exact immutable plan and its current delivery state."""
    _require_transaction(connection)
    row = _publication_row(connection, publication_id)
    if row is None:
        raise PublicationNotFound("publication does not exist")
    return _stored(connection, row)


def publication_for_supersession(
    connection: psycopg.Connection[TupleRow],
    *,
    superseding_publication_id: PublicationId,
) -> HistoricalPublication | None:
    """Load the oldest pending historical rewrite in a posted publication's PR."""
    _require_transaction(connection)
    current = get_publication(connection, superseding_publication_id)
    if current.status is not PublicationStatus.POSTED:
        raise InvalidPublicationTransition(
            "supersession recovery requires a posted publication"
        )
    row = connection.execute(
        """
        SELECT id, superseded_by_publication_id
        FROM review_agent.publications
        WHERE pull_request_id = %s
          AND superseded_by_publication_id IS NOT NULL
          AND (
              supersession_rendered_at IS NULL
              OR supersession_failure_code IS NOT NULL
          )
        ORDER BY (superseded_by_publication_id = %s) DESC, superseded_at, id
        LIMIT 1
        """,
        (current.pull_request_id, superseding_publication_id),
    ).fetchone()
    if row is None:
        return None
    prior = get_publication(connection, PublicationId(int(row[0])))
    superseding = get_publication(connection, PublicationId(int(row[1])))
    if (
        prior.status is not PublicationStatus.POSTED
        or superseding.status is not PublicationStatus.POSTED
    ):
        raise PublicationConflict("supersession requires two posted publications")
    issue_parts = tuple(
        part
        for part in prior.parts
        if part.part_type
        in {PublicationPartType.SUMMARY, PublicationPartType.CONTINUATION}
    )
    comment_ids = tuple(
        part.external_id for part in issue_parts if part.external_id is not None
    )
    if len(comment_ids) != len(issue_parts) or not comment_ids:
        raise PublicationConflict("superseded publication has incomplete comment IDs")
    superseding_summary = next(
        (
            part
            for part in superseding.parts
            if part.part_type is PublicationPartType.SUMMARY
            and part.part_number == 1
        ),
        None,
    )
    if superseding_summary is None or superseding_summary.external_id is None:
        raise PublicationConflict("superseding publication has no summary comment")
    return HistoricalPublication(
        publication_id=prior.id,
        review_number=prior.review_number,
        repository=prior.repository,
        pr_number=prior.pr_number,
        head_sha=prior.head_sha,
        publication_key=prior.plan.publication_key,
        rendered_markdown=prior.plan.rendered_markdown,
        rendered_blocks_json=prior.plan.rendered_blocks_json,
        comment_ids=comment_ids,
        current_findings_count=sum(
            finding.outcome is PublicationFindingOutcome.CURRENT
            for finding in prior.plan.findings
        ),
        superseding_review_number=superseding.review_number,
        superseding_comment_id=superseding_summary.external_id,
    )


def record_supersession_result(
    connection: psycopg.Connection[TupleRow],
    *,
    publication_id: PublicationId,
    failure_code: str | None,
) -> SupersessionResult:
    """Record one completed historical rewrite attempt for a superseded review."""
    _require_transaction(connection)
    code = failure_code.strip() if failure_code is not None else None
    if code is not None and (not code or len(code) > 80):
        raise PublicationStoreError(
            "failure_code must contain at most 80 characters"
        )
    row = connection.execute(
        """
        UPDATE review_agent.publications
        SET supersession_rendered_at = statement_timestamp(),
            supersession_failure_code = %s
        WHERE id = %s
          AND status = 'posted'
          AND superseded_by_publication_id IS NOT NULL
        RETURNING supersession_rendered_at, supersession_failure_code
        """,
        (code, publication_id),
    ).fetchone()
    if row is None:
        raise InvalidPublicationTransition(
            "only a superseded posted publication can record a rewrite result"
        )
    rendered_at = row[0]
    if not isinstance(rendered_at, datetime):
        raise PublicationConflict("supersession result has no timestamp")
    return SupersessionResult(
        publication_id=publication_id,
        rendered_at=rendered_at,
        failure_code=str(row[1]) if row[1] is not None else None,
    )


def claim_publication(
    connection: psycopg.Connection[TupleRow],
    publication_id: PublicationId,
    *,
    lease_owner: str = _DIRECT_LEASE_OWNER,
    lease_duration: timedelta = _DIRECT_LEASE_DURATION,
    recover_expired: bool = False,
) -> PublicationClaim:
    """Claim one exact delivery or recover it only after its lease expires."""
    _require_transaction(connection)
    owner = lease_owner.strip()
    if not owner:
        raise PublicationStoreError("lease_owner is required")
    if lease_duration <= timedelta(0):
        raise PublicationStoreError("lease_duration must be positive")
    initial = _publication_row(connection, publication_id)
    if initial is None:
        raise PublicationNotFound("publication does not exist")
    from . import review_runs

    run = review_runs.lock_run(connection, initial.review_run_id)
    row = _publication_row(connection, publication_id, for_update=True)
    if row is None:
        raise PublicationNotFound("publication does not exist")
    if run.status is not ReviewStatus.RUNNING:
        stale = run.status is ReviewStatus.SUPERSEDED
        status = PublicationStatus.STALE if stale else PublicationStatus.FAILED
        part_status = (
            PublicationPartStatus.STALE
            if stale
            else PublicationPartStatus.PUBLISH_FAILED
        )
        failure_code = (
            "snapshot_superseded" if stale else "review_run_terminal"
        )
        connection.execute(
            """
            UPDATE review_agent.publication_parts
            SET status = %s,
                posting_started_at = COALESCE(
                    posting_started_at, statement_timestamp()
                ),
                failure_at = statement_timestamp(), failure_code = %s
            WHERE publication_id = %s AND status <> 'posted'
            """,
            (part_status.value, failure_code, publication_id),
        )
        connection.execute(
            """
            UPDATE review_agent.publications
            SET status = %s,
                posting_started_at = COALESCE(
                    posting_started_at, statement_timestamp()
                ),
                publish_failed_at = statement_timestamp(), failure_code = %s,
                delivery_lease_owner = NULL,
                delivery_lease_expires_at = NULL,
                delivery_last_heartbeat_at = NULL,
                delivery_completed_at = statement_timestamp()
            WHERE id = %s
              AND status IN ('generated', 'posting', 'publish_failed')
            """,
            (status.value, failure_code, publication_id),
        )
        terminal = _publication_row(connection, publication_id)
        if terminal is None:
            raise PublicationNotFound("publication disappeared during terminalization")
        return PublicationClaim(
            publication=_stored(connection, terminal), acquired=False
        )
    clock = connection.execute("SELECT statement_timestamp()").fetchone()
    if clock is None or not isinstance(clock[0], datetime):
        raise PublicationStoreError("database clock could not be read")
    expired = (
        row.status == PublicationStatus.POSTING.value
        and row.delivery_lease_expires_at is not None
        and row.delivery_lease_expires_at <= clock[0]
    )
    if row.status in {
        PublicationStatus.GENERATED.value,
        PublicationStatus.PUBLISH_FAILED.value,
    } or (recover_expired and expired):
        recovered = row.status == PublicationStatus.POSTING.value
        updated = connection.execute(
            """
            UPDATE review_agent.publications
            SET status = 'posting', posting_started_at = statement_timestamp(),
                publish_failed_at = NULL, failure_code = NULL,
                delivery_attempt_count = delivery_attempt_count + 1,
                delivery_lease_owner = %s,
                delivery_lease_generation = delivery_lease_generation + 1,
                delivery_lease_expires_at = statement_timestamp() + %s,
                delivery_last_heartbeat_at = statement_timestamp(),
                delivery_recovery_count = delivery_recovery_count + %s
            WHERE id = %s
              AND delivery_attempt_count < delivery_max_attempts
            RETURNING id
            """,
            (owner, lease_duration, int(recovered), publication_id),
        )
        if updated.fetchone() is None:
            raise InvalidPublicationTransition(
                "publication exhausted its delivery attempts"
            )
        connection.execute(
            """
            UPDATE review_agent.publication_parts
            SET status = 'posting', posting_started_at = statement_timestamp(),
                failure_at = NULL, failure_code = NULL
            WHERE publication_id = %s
              AND status IN ('pending', 'posting', 'publish_failed')
            """,
            (publication_id,),
        )
        claimed = _publication_row(connection, publication_id)
        if claimed is None:
            raise PublicationNotFound("publication disappeared after claim")
        return PublicationClaim(publication=_stored(connection, claimed), acquired=True)
    if row.status in {PublicationStatus.POSTING.value, PublicationStatus.POSTED.value}:
        return PublicationClaim(publication=_stored(connection, row), acquired=False)
    raise InvalidPublicationTransition(
        f"cannot claim a publication in {row.status} state"
    )


def claim_next_publication(
    connection: psycopg.Connection[TupleRow],
    *,
    lease_owner: str,
    lease_duration: timedelta,
) -> PublicationClaim | None:
    """Claim the oldest ready publication, including an expired delivery."""
    _require_transaction(connection)
    terminal = connection.execute(
        """
        SELECT publication.id
        FROM review_agent.publications AS publication
        JOIN review_agent.review_runs AS run
          ON run.id = publication.review_run_id
        WHERE run.status <> 'running'
          AND publication.status IN ('generated', 'posting', 'publish_failed')
        ORDER BY publication.id
        FOR UPDATE OF run SKIP LOCKED
        LIMIT 1
        """
    ).fetchone()
    terminal_claim = None
    if terminal is not None:
        terminal_claim = claim_publication(
            connection,
            PublicationId(int(terminal[0])),
            lease_owner=lease_owner,
            lease_duration=lease_duration,
            recover_expired=True,
        )
    row = connection.execute(
        """
        SELECT publication.id
        FROM review_agent.publications AS publication
        JOIN review_agent.review_runs AS run
          ON run.id = publication.review_run_id
        WHERE (
              (
                publication.status IN ('generated', 'publish_failed')
                AND publication.delivery_available_at <= statement_timestamp()
              )
           OR (
                publication.status = 'posting'
                AND publication.delivery_lease_expires_at <= statement_timestamp()
              )
              )
          AND run.status = 'running'
          AND publication.delivery_attempt_count
              < publication.delivery_max_attempts
        ORDER BY COALESCE(
            publication.delivery_lease_expires_at,
            publication.delivery_available_at
        ), publication.id
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        return terminal_claim
    claim = claim_publication(
        connection,
        PublicationId(int(row[0])),
        lease_owner=lease_owner,
        lease_duration=lease_duration,
        recover_expired=True,
    )
    return claim if claim.acquired else None


def fail_one_expired_exhausted_publication(
    connection: psycopg.Connection[TupleRow],
) -> StoredPublication | None:
    """Terminalize one final-attempt lease abandoned by a dead publisher."""
    _require_transaction(connection)
    candidate = connection.execute(
        """
        SELECT id, review_run_id
        FROM review_agent.publications
        WHERE status = 'posting'
          AND delivery_lease_expires_at <= statement_timestamp()
          AND delivery_attempt_count >= delivery_max_attempts
        ORDER BY delivery_lease_expires_at, id
        LIMIT 1
        """
    ).fetchone()
    if candidate is None:
        return None
    from . import review_runs

    review_runs.lock_run(connection, ReviewRunId(int(candidate[1])))
    row = connection.execute(
        """
        SELECT id
        FROM review_agent.publications
        WHERE id = %s AND status = 'posting'
          AND delivery_lease_expires_at <= statement_timestamp()
          AND delivery_attempt_count >= delivery_max_attempts
        FOR UPDATE
        """,
        (candidate[0],),
    ).fetchone()
    if row is None:
        return None
    publication_id = PublicationId(int(row[0]))
    connection.execute(
        """
        UPDATE review_agent.publication_parts
        SET status = 'publish_failed', failure_at = statement_timestamp(),
            failure_code = 'publication_attempts_exhausted'
        WHERE publication_id = %s AND status = 'posting'
        """,
        (publication_id,),
    )
    connection.execute(
        """
        UPDATE review_agent.publications
        SET status = 'failed', publish_failed_at = statement_timestamp(),
            failure_code = 'publication_attempts_exhausted',
            delivery_lease_owner = NULL,
            delivery_lease_expires_at = NULL,
            delivery_last_heartbeat_at = NULL,
            delivery_completed_at = statement_timestamp()
        WHERE id = %s AND status = 'posting'
        """,
        (publication_id,),
    )
    return get_publication(connection, publication_id)


def heartbeat_publication(
    connection: psycopg.Connection[TupleRow],
    *,
    publication_id: PublicationId,
    lease_owner: str,
    lease_generation: int,
    lease_duration: timedelta,
) -> StoredPublication:
    """Extend one live publisher lease generation."""
    _require_transaction(connection)
    owner = lease_owner.strip()
    if not owner or lease_generation < 1 or lease_duration <= timedelta(0):
        raise PublicationStoreError("a valid publication lease is required")
    updated = connection.execute(
        """
        UPDATE review_agent.publications
        SET delivery_last_heartbeat_at = statement_timestamp(),
            delivery_lease_expires_at = statement_timestamp() + %s
        WHERE id = %s AND status = 'posting'
          AND delivery_lease_owner = %s
          AND delivery_lease_generation = %s
          AND delivery_lease_expires_at > statement_timestamp()
        RETURNING id
        """,
        (lease_duration, publication_id, owner, lease_generation),
    ).fetchone()
    if updated is None:
        raise PublicationLeaseLost("publication delivery lease is no longer current")
    return get_publication(connection, publication_id)


def _require_delivery_lease(
    connection: psycopg.Connection[TupleRow],
    row: _PublicationRow,
    *,
    lease_owner: str,
    lease_generation: int,
) -> None:
    if (
        row.status != PublicationStatus.POSTING.value
        or row.delivery_lease_owner != lease_owner
        or row.delivery_lease_generation != lease_generation
    ):
        raise PublicationLeaseLost("publication delivery lease is no longer current")
    clock = connection.execute("SELECT statement_timestamp()").fetchone()
    if (
        clock is None
        or not isinstance(clock[0], datetime)
        or row.delivery_lease_expires_at is None
        or row.delivery_lease_expires_at <= clock[0]
    ):
        raise PublicationLeaseLost("publication delivery lease has expired")


def require_live_publication_lease(
    connection: psycopg.Connection[TupleRow],
    *,
    publication_id: PublicationId,
    lease_owner: str,
    lease_generation: int,
) -> None:
    """Prove one exact publisher generation is still live."""
    _require_transaction(connection)
    live = connection.execute(
        """
        SELECT 1
        FROM review_agent.publications
        WHERE id = %s AND status = 'posting'
          AND delivery_lease_owner = %s
          AND delivery_lease_generation = %s
          AND delivery_lease_expires_at > statement_timestamp()
        """,
        (publication_id, lease_owner, lease_generation),
    ).fetchone()
    if live is None:
        raise PublicationLeaseLost("publication delivery lease is no longer current")


def acknowledge_part(
    connection: psycopg.Connection[TupleRow],
    *,
    publication_id: PublicationId,
    part_type: PublicationPartType,
    part_number: int,
    external_id: int,
    posting_started_at: datetime,
    lease_owner: str = _DIRECT_LEASE_OWNER,
    lease_generation: int | None = None,
) -> StoredPublicationPart:
    """Persist one provider ID after a successful write or marker recovery."""
    _require_transaction(connection)
    if isinstance(external_id, bool) or external_id < 1:
        raise PublicationStoreError("external_id must be positive")
    row = _publication_row(connection, publication_id, for_update=True)
    if row is None:
        raise PublicationNotFound("publication does not exist")
    generation = (
        row.delivery_lease_generation
        if lease_generation is None
        else lease_generation
    )
    _require_delivery_lease(
        connection,
        row,
        lease_owner=lease_owner,
        lease_generation=generation,
    )
    if row.posting_started_at != posting_started_at:
        raise InvalidPublicationTransition("publication posting generation changed")
    matching = [
        item
        for item in _part_rows(connection, publication_id)
        if item.part_type == part_type.value and item.part_number == part_number
    ]
    if not matching:
        raise PublicationNotFound("publication part does not exist")
    part = matching[0]
    if part.status == PublicationPartStatus.POSTED.value:
        if part.external_id != external_id:
            raise PublicationConflict(
                "publication part already has a different external ID"
            )
    elif row.status != PublicationStatus.POSTING.value or (
        part.status != PublicationPartStatus.POSTING.value
    ):
        raise InvalidPublicationTransition("publication part is not posting")
    else:
        connection.execute(
            """
            UPDATE review_agent.publication_parts
            SET status = 'posted', external_id = %s,
                posted_at = statement_timestamp()
            WHERE id = %s AND status = 'posting'
            """,
            (external_id, part.id),
        )
    stored = next(
        item
        for item in _stored(connection, row).parts
        if item.part_type is part_type and item.part_number == part_number
    )
    return stored


def complete_publication(
    connection: psycopg.Connection[TupleRow],
    *,
    publication_id: PublicationId,
    posting_started_at: datetime,
    lease_owner: str = _DIRECT_LEASE_OWNER,
    lease_generation: int | None = None,
) -> StoredPublication:
    """Mark delivery posted only after every exact part has an external ID."""
    _require_transaction(connection)
    row = _publication_row(connection, publication_id, for_update=True)
    if row is None:
        raise PublicationNotFound("publication does not exist")
    if row.status == PublicationStatus.POSTED.value:
        return _stored(connection, row)
    generation = (
        row.delivery_lease_generation
        if lease_generation is None
        else lease_generation
    )
    _require_delivery_lease(
        connection,
        row,
        lease_owner=lease_owner,
        lease_generation=generation,
    )
    if (
        row.status != PublicationStatus.POSTING.value
        or row.posting_started_at != posting_started_at
    ):
        raise InvalidPublicationTransition("publication is not in this posting generation")
    incomplete = connection.execute(
        """
        SELECT count(*)
        FROM review_agent.publication_parts
        WHERE publication_id = %s AND status <> 'posted'
        """,
        (publication_id,),
    ).fetchone()
    if incomplete is None:
        raise PublicationConflict("publication part state could not be counted")
    if int(incomplete[0]) != 0:
        raise InvalidPublicationTransition("publication still has unposted parts")
    try:
        connection.execute(
            """
            UPDATE review_agent.publications
            SET superseded_at = statement_timestamp(),
                superseded_by_publication_id = %s
            WHERE pull_request_id = %s
              AND status = 'posted'
              AND superseded_by_publication_id IS NULL
              AND id <> %s
            """,
            (publication_id, row.pull_request_id, publication_id),
        )
        connection.execute(
            """
            UPDATE review_agent.publications
            SET status = 'posted', posted_at = statement_timestamp(),
                delivery_lease_owner = NULL,
                delivery_lease_expires_at = NULL,
                delivery_last_heartbeat_at = NULL,
                delivery_completed_at = statement_timestamp()
            WHERE id = %s AND status = 'posting'
            """,
            (publication_id,),
        )
    except psycopg.IntegrityError as exc:
        raise PublicationConflict(
            "publication could not replace the current posted review"
        ) from exc
    posted = _publication_row(connection, publication_id)
    if posted is None:
        raise PublicationNotFound("publication disappeared during completion")
    return _stored(connection, posted)


def fail_publication(
    connection: psycopg.Connection[TupleRow],
    *,
    publication_id: PublicationId,
    posting_started_at: datetime,
    failure_code: str,
    stale: bool = False,
    retryable: bool = True,
    retry_delay: timedelta = timedelta(seconds=30),
    lease_owner: str = _DIRECT_LEASE_OWNER,
    lease_generation: int | None = None,
) -> StoredPublication:
    """Release a retryable delivery or terminalize its exact lease generation."""
    _require_transaction(connection)
    code = failure_code.strip()
    if not code or len(code) > 80:
        raise PublicationStoreError("failure_code must contain at most 80 characters")
    row = _publication_row(connection, publication_id, for_update=True)
    if row is None:
        raise PublicationNotFound("publication does not exist")
    generation = (
        row.delivery_lease_generation
        if lease_generation is None
        else lease_generation
    )
    _require_delivery_lease(
        connection,
        row,
        lease_owner=lease_owner,
        lease_generation=generation,
    )
    if (
        row.status != PublicationStatus.POSTING.value
        or row.posting_started_at != posting_started_at
    ):
        raise InvalidPublicationTransition("publication is not in this posting generation")
    can_retry = retryable and not stale and (
        row.delivery_attempt_count < row.delivery_max_attempts
    )
    status = (
        PublicationStatus.STALE
        if stale
        else (
            PublicationStatus.PUBLISH_FAILED
            if can_retry
            else PublicationStatus.FAILED
        )
    )
    part_status = (
        PublicationPartStatus.STALE
        if stale
        else PublicationPartStatus.PUBLISH_FAILED
    )
    connection.execute(
        """
        UPDATE review_agent.publication_parts
        SET status = %s, failure_at = statement_timestamp(), failure_code = %s
        WHERE publication_id = %s AND status = 'posting'
        """,
        (part_status.value, code, publication_id),
    )
    connection.execute(
        """
        UPDATE review_agent.publications
        SET status = %s, publish_failed_at = statement_timestamp(),
            failure_code = %s,
            delivery_available_at = CASE
                WHEN %s THEN statement_timestamp() + %s
                ELSE delivery_available_at
            END,
            delivery_lease_owner = NULL,
            delivery_lease_expires_at = NULL,
            delivery_last_heartbeat_at = NULL,
            delivery_completed_at = CASE
                WHEN %s THEN NULL ELSE statement_timestamp()
            END
        WHERE id = %s AND status = 'posting'
        """,
        (status.value, code, can_retry, retry_delay, can_retry, publication_id),
    )
    failed = _publication_row(connection, publication_id)
    if failed is None:
        raise PublicationNotFound("publication disappeared during failure update")
    return _stored(connection, failed)


def prepare_publication(
    connection: psycopg.Connection[TupleRow],
    *,
    run_id: ReviewRunId,
    plan: PublicationPlan,
    delivery_max_attempts: int = 3,
    review_job_id: int | None = None,
    review_lease_generation: int | None = None,
) -> StoredPublication:
    """Atomically freeze one exact plan and advance its run to publishing."""
    _require_transaction(connection)
    if (
        isinstance(delivery_max_attempts, bool)
        or delivery_max_attempts < 1
    ):
        raise PublicationStoreError("delivery_max_attempts must be positive")
    scope = _run_scope(connection, run_id)
    existing_id = connection.execute(
        "SELECT id FROM review_agent.publications WHERE review_run_id = %s",
        (run_id,),
    ).fetchone()
    if existing_id is not None:
        existing = get_publication(connection, PublicationId(int(existing_id[0])))
        if existing.plan != plan:
            raise PublicationConflict(
                "review run already has a different immutable publication plan"
            )
        from . import jobs as postgres_jobs

        postgres_jobs.handoff_to_publication(
            connection,
            review_run_id=run_id,
            job_id=review_job_id,
            lease_generation=review_lease_generation,
        )
        return existing
    if scope.phase != "rendering":
        raise InvalidPublicationTransition(
            "publication preparation requires a rendering-phase review run"
        )

    for finding in plan.findings:
        if (
            finding.outcome is PublicationFindingOutcome.CURRENT
            and finding.source_review_run_id != int(run_id)
        ):
            raise PublicationConflict(
                "current publication finding must come from the publication run"
            )
        if (
            finding.outcome is not PublicationFindingOutcome.CURRENT
            and finding.source_review_run_id == int(run_id)
        ):
            raise PublicationConflict(
                "non-current publication finding must come from a prior run"
            )

    review_number_row = connection.execute(
        """
        SELECT COALESCE(max(review_number), 0) + 1
        FROM review_agent.publications
        WHERE pull_request_id = %s
        """,
        (scope.pull_request_id,),
    ).fetchone()
    if review_number_row is None:
        raise PublicationConflict("publication review number could not be allocated")
    review_number = int(review_number_row[0])
    try:
        publication_id_row = connection.execute(
            """
            INSERT INTO review_agent.publications (
                pull_request_id, review_run_id, review_number, publication_key,
                rendered_markdown, rendered_blocks_schema_version,
                rendered_blocks, rendered_hash, status, generated_at,
                delivery_available_at, delivery_max_attempts
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'generated',
                      statement_timestamp(), statement_timestamp(), %s)
            RETURNING id
            """,
            (
                scope.pull_request_id,
                run_id,
                review_number,
                plan.publication_key,
                plan.rendered_markdown,
                plan.rendered_blocks_schema_version,
                Jsonb(json.loads(plan.rendered_blocks_json)),
                plan.rendered_hash,
                delivery_max_attempts,
            ),
        ).fetchone()
        if publication_id_row is None:
            raise PublicationConflict("publication insert returned no identity")
        publication_id = PublicationId(int(publication_id_row[0]))
        for part in plan.parts:
            connection.execute(
                """
                INSERT INTO review_agent.publication_parts (
                    publication_id, part_type, part_number,
                    payload_schema_version, payload, payload_hash, status
                ) VALUES (%s, %s, %s, %s, %s, %s, 'pending')
                """,
                (
                    publication_id,
                    part.part_type.value,
                    part.part_number,
                    part.payload.schema_version,
                    Jsonb(json.loads(part.payload.canonical_json)),
                    part.payload.sha256,
                ),
            )
        for finding in plan.findings:
            connection.execute(
                """
                INSERT INTO review_agent.publication_findings (
                    publication_id, publication_review_run_id, pull_request_id,
                    finding_id, source_finding_occurrence_id,
                    source_review_run_id, local_reference, outcome,
                    outcome_evidence
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    publication_id,
                    run_id,
                    scope.pull_request_id,
                    finding.finding_id,
                    finding.source_finding_occurrence_id,
                    finding.source_review_run_id,
                    finding.local_reference,
                    finding.outcome.value,
                    finding.outcome_evidence,
                ),
            )
        updated = connection.execute(
            """
            UPDATE review_agent.review_runs
            SET phase = 'publishing', last_heartbeat_at = statement_timestamp()
            WHERE id = %s AND status = 'running' AND phase = 'rendering'
            RETURNING id
            """,
            (run_id,),
        ).fetchone()
        if updated is None:
            raise InvalidPublicationTransition(
                "review run stopped during publication preparation"
            )
        from . import jobs as postgres_jobs

        postgres_jobs.handoff_to_publication(
            connection,
            review_run_id=run_id,
            job_id=review_job_id,
            lease_generation=review_lease_generation,
        )
    except psycopg.IntegrityError as exc:
        if (
            isinstance(exc, errors.CheckViolation)
            and exc.diag.constraint_name == "publication_parts_payload_ck"
        ):
            raise PublicationConflict(
                "publication part payload violates the database storage guard"
            ) from exc
        raise PublicationConflict(
            "publication plan conflicts with persisted review facts"
        ) from exc

    row = _publication_row(connection, publication_id)
    if row is None:
        raise PublicationNotFound("prepared publication could not be reloaded")
    return _stored(connection, row)
