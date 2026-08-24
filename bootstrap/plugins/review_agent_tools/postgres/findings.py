"""PostgreSQL finding identity, occurrence, and local-reference operations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

import psycopg
from psycopg import errors
from psycopg.pq import TransactionStatus
from psycopg.rows import TupleRow, class_row

from ..domain.finding import (
    FindingDefinition,
    FindingId,
    FindingOccurrenceId,
    MAX_FINDINGS_PER_REVIEW,
    FingerprintQuery,
    RepeatFinding,
    FindingCategory,
    Severity,
    require_unique_finding_identities,
)
from ..domain.review import PullRequestId, RepositoryId, ReviewRunId


class FindingStoreError(ValueError):
    """A finding operation violates its durable contract."""


class FindingRunNotActive(FindingStoreError):
    """Finding writes require an active review run."""


class FindingRunBusy(FindingStoreError):
    """Finding writes could not acquire the pull-request lifecycle lock."""


class FindingConflict(FindingStoreError):
    """Stored identity or occurrence data conflicts with the submitted fact."""


class FindingPathNotChanged(FindingStoreError):
    """A finding does not belong to the run's registered changed paths."""


class FingerprintNotFound(FindingStoreError):
    """No finding identity matches within the selected repository."""


class AmbiguousFingerprint(FindingStoreError):
    """A repository-local prefix matches more than one finding."""


@dataclass(frozen=True, slots=True)
class RecordedFinding:
    finding_id: FindingId
    occurrence_id: FindingOccurrenceId
    fingerprint: str
    local_reference: str


@dataclass(frozen=True, slots=True)
class FindingBatch:
    """Recorded findings in the same order as the admitted definitions."""

    repository_id: RepositoryId
    pull_request_id: PullRequestId
    run_id: ReviewRunId
    items: tuple[RecordedFinding, ...]


@dataclass(frozen=True, slots=True)
class _RunScope:
    status: str
    pull_request_id: PullRequestId
    repository_id: RepositoryId
    head_sha: str


@dataclass(frozen=True, slots=True)
class _IdentityRow:
    id: FindingId
    fingerprint: str
    rule_id: str
    path: str
    symbol: str | None
    anchor: str


@dataclass(frozen=True, slots=True)
class _OccurrenceRow:
    id: FindingOccurrenceId
    finding_id: FindingId
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
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class _ReferenceRow:
    finding_id: FindingId
    local_reference: str


@dataclass(frozen=True, slots=True)
class _RepeatRow:
    fingerprint: str
    local_reference: str
    previous_run_id: ReviewRunId
    previous_head: str
    rule_id: str
    path: str
    line: int
    symbol: str | None
    anchor: str
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


def _require_transaction(connection: psycopg.Connection[TupleRow]) -> None:
    if connection.info.transaction_status != TransactionStatus.INTRANS:
        raise FindingStoreError("finding operations require an active transaction")


def _scope(
    connection: psycopg.Connection[TupleRow],
    run_id: ReviewRunId,
    *,
    for_write: bool,
) -> _RunScope:
    with connection.cursor(row_factory=class_row(_RunScope)) as cursor:
        scope = cursor.execute(
            """
            SELECT run.status, run.pull_request_id, pr.repository_id,
                   subject.head_sha
            FROM review_agent.review_runs AS run
            JOIN review_agent.pull_requests AS pr
              ON pr.id = run.pull_request_id
            JOIN review_agent.review_subjects AS subject
              ON subject.id = run.review_subject_id
            WHERE run.id = %s
            """,
            (run_id,),
        ).fetchone()
    if scope is None:
        raise FindingStoreError("review run does not exist")
    if not for_write:
        return scope

    # Review starts lock the pull request before superseding a run. Use the same
    # order while allocating F-numbers to avoid an inverted lock dependency.
    try:
        connection.execute(
            "SELECT id FROM review_agent.pull_requests WHERE id = %s "
            "FOR NO KEY UPDATE",
            (scope.pull_request_id,),
        ).fetchone()
    except errors.LockNotAvailable as exc:
        raise FindingRunBusy("pull request is busy recording findings") from exc
    with connection.cursor(row_factory=class_row(_RunScope)) as cursor:
        locked = cursor.execute(
            """
            SELECT run.status, run.pull_request_id, pr.repository_id,
                   subject.head_sha
            FROM review_agent.review_runs AS run
            JOIN review_agent.pull_requests AS pr
              ON pr.id = run.pull_request_id
            JOIN review_agent.review_subjects AS subject
              ON subject.id = run.review_subject_id
            WHERE run.id = %s
            FOR SHARE OF run
            """,
            (run_id,),
        ).fetchone()
    if locked is None or (
        locked.pull_request_id != scope.pull_request_id
        or locked.repository_id != scope.repository_id
        or locked.head_sha != scope.head_sha
    ):
        raise FindingConflict("review run scope changed while recording findings")
    if locked.status != "running":
        raise FindingRunNotActive("finding writes require an active review run")
    return locked


def _identities(
    connection: psycopg.Connection[TupleRow],
    repository_id: RepositoryId,
    definitions: tuple[FindingDefinition, ...],
) -> dict[str, _IdentityRow]:
    if definitions:
        connection.execute(
            """
            INSERT INTO review_agent.finding_identities (
                repository_id, fingerprint, rule_id, path, symbol, anchor,
                first_seen_at, last_seen_at
            )
            SELECT %s, incoming.fingerprint, incoming.rule_id, incoming.path,
                   incoming.symbol, incoming.anchor,
                   statement_timestamp(), statement_timestamp()
            FROM unnest(
                %s::text[], %s::text[], %s::text[], %s::text[], %s::text[]
            ) AS incoming(fingerprint, rule_id, path, symbol, anchor)
            ON CONFLICT (repository_id, fingerprint) DO NOTHING
            """,
            (
                repository_id,
                [item.fingerprint for item in definitions],
                [item.rule_id for item in definitions],
                [item.path for item in definitions],
                [item.symbol for item in definitions],
                [item.anchor for item in definitions],
            ),
        )
    with connection.cursor(row_factory=class_row(_IdentityRow)) as cursor:
        rows = cursor.execute(
            """
            SELECT id, fingerprint, rule_id, path, symbol, anchor
            FROM review_agent.finding_identities
            WHERE repository_id = %s AND fingerprint = ANY(%s::text[])
            """,
            (repository_id, [item.fingerprint for item in definitions]),
        ).fetchall()
    stored = {row.fingerprint: row for row in rows}
    for item in definitions:
        row = stored.get(item.fingerprint)
        if row is None or (
            row.rule_id != item.rule_id
            or row.path != item.path
            or row.symbol != item.symbol
            or row.anchor != item.anchor
        ):
            raise FindingConflict(
                f"finding identity conflicts for {item.fingerprint}"
            )
    return stored


def _occurrences(
    connection: psycopg.Connection[TupleRow],
    *,
    scope: _RunScope,
    run_id: ReviewRunId,
    definitions: tuple[FindingDefinition, ...],
    identities: dict[str, _IdentityRow],
) -> dict[FindingId, _OccurrenceRow]:
    finding_ids = [identities[item.fingerprint].id for item in definitions]
    if definitions:
        connection.execute(
            """
            INSERT INTO review_agent.finding_occurrences (
                review_run_id, pull_request_id, repository_id, finding_id,
                line, title, severity, category, publication_score, confidence,
                context_hash, evidence, disproof_checks, impact, smallest_fix,
                observed_at
            )
            SELECT %s, %s, %s, incoming.finding_id, incoming.line,
                   incoming.title, incoming.severity, incoming.category,
                   incoming.publication_score, incoming.confidence,
                   incoming.context_hash, incoming.evidence,
                   incoming.disproof_checks, incoming.impact,
                   incoming.smallest_fix, statement_timestamp()
            FROM unnest(
                %s::bigint[], %s::integer[], %s::text[], %s::text[],
                %s::text[], %s::integer[], %s::numeric[], %s::text[],
                %s::text[], %s::text[], %s::text[], %s::text[]
            ) AS incoming(
                finding_id, line, title, severity, category,
                publication_score, confidence, context_hash, evidence,
                disproof_checks, impact, smallest_fix
            )
            ON CONFLICT (review_run_id, finding_id) DO NOTHING
            """,
            (
                run_id,
                scope.pull_request_id,
                scope.repository_id,
                finding_ids,
                [item.line for item in definitions],
                [item.title for item in definitions],
                [item.severity.value for item in definitions],
                [item.category.value for item in definitions],
                [item.publication_score for item in definitions],
                [Decimal(str(item.confidence)) for item in definitions],
                [item.context_hash for item in definitions],
                [item.evidence for item in definitions],
                [item.disproof_checks for item in definitions],
                [item.impact for item in definitions],
                [item.smallest_fix for item in definitions],
            ),
        )
    with connection.cursor(row_factory=class_row(_OccurrenceRow)) as cursor:
        rows = cursor.execute(
            """
            SELECT id, finding_id, line, title, severity, category,
                   publication_score, confidence, context_hash, evidence,
                   disproof_checks, impact, smallest_fix, observed_at
            FROM review_agent.finding_occurrences
            WHERE review_run_id = %s AND finding_id = ANY(%s::bigint[])
            """,
            (run_id, finding_ids),
        ).fetchall()
    stored = {row.finding_id: row for row in rows}
    for item in definitions:
        finding_id = identities[item.fingerprint].id
        row = stored.get(finding_id)
        if row is None or (
            row.line != item.line
            or row.title != item.title
            or row.severity != item.severity.value
            or row.category != item.category.value
            or row.publication_score != item.publication_score
            or row.confidence != Decimal(str(item.confidence))
            or row.context_hash != item.context_hash
            or row.evidence != item.evidence
            or row.disproof_checks != item.disproof_checks
            or row.impact != item.impact
            or row.smallest_fix != item.smallest_fix
        ):
            raise FindingConflict(
                f"finding occurrence conflicts for {item.fingerprint}"
            )
    connection.execute(
        """
        UPDATE review_agent.finding_identities AS identity
        SET last_seen_at = latest.observed_at
        FROM (
            SELECT finding_id, max(observed_at) AS observed_at
            FROM review_agent.finding_occurrences
            WHERE review_run_id = %s AND finding_id = ANY(%s::bigint[])
            GROUP BY finding_id
        ) AS latest
        WHERE identity.id = latest.finding_id
          AND identity.last_seen_at < latest.observed_at
        """,
        (run_id, finding_ids),
    )
    return stored


def _references(
    connection: psycopg.Connection[TupleRow],
    *,
    scope: _RunScope,
    identities: dict[str, _IdentityRow],
) -> dict[FindingId, str]:
    ordered = sorted(identities.values(), key=lambda row: row.fingerprint)
    finding_ids = [row.id for row in ordered]
    with connection.cursor(row_factory=class_row(_ReferenceRow)) as cursor:
        rows = cursor.execute(
            """
            SELECT finding_id, local_reference
            FROM review_agent.pull_request_finding_references
            WHERE pull_request_id = %s AND finding_id = ANY(%s::bigint[])
            """,
            (scope.pull_request_id, finding_ids),
        ).fetchall()
    existing = {row.finding_id: row.local_reference for row in rows}
    missing = [row for row in ordered if row.id not in existing]
    if missing:
        maximum = connection.execute(
            """
            SELECT COALESCE(max(substring(local_reference FROM 2)::integer), 0)
            FROM review_agent.pull_request_finding_references
            WHERE pull_request_id = %s
            """,
            (scope.pull_request_id,),
        ).fetchone()
        next_number = int(maximum[0]) + 1 if maximum is not None else 1
        references = [f"F{next_number + index}" for index in range(len(missing))]
        connection.execute(
            """
            INSERT INTO review_agent.pull_request_finding_references (
                pull_request_id, repository_id, finding_id, local_reference,
                first_assigned_at
            )
            SELECT %s, %s, incoming.finding_id, incoming.local_reference,
                   statement_timestamp()
            FROM unnest(%s::bigint[], %s::text[])
              AS incoming(finding_id, local_reference)
            """,
            (
                scope.pull_request_id,
                scope.repository_id,
                [row.id for row in missing],
                references,
            ),
        )
        existing.update(
            {row.id: reference for row, reference in zip(missing, references)}
        )
    if len(existing) != len(finding_ids):
        raise FindingConflict("local finding references could not be assigned")
    return existing


def record_findings(
    connection: psycopg.Connection[TupleRow],
    *,
    run_id: ReviewRunId,
    expected_head_sha: str,
    definitions: tuple[FindingDefinition, ...],
) -> FindingBatch:
    """Record identities, occurrences, and deterministic PR references atomically."""
    _require_transaction(connection)
    require_unique_finding_identities(definitions)
    scope = _scope(connection, run_id, for_write=True)
    if scope.head_sha != expected_head_sha:
        raise FindingConflict("head_sha does not match the exact review subject")
    paths = sorted({item.path for item in definitions})
    changed = connection.execute(
        """
        SELECT path FROM review_agent.review_run_files
        WHERE review_run_id = %s AND is_changed_path AND path = ANY(%s::text[])
        """,
        (run_id, paths),
    ).fetchall()
    changed_paths = {str(row[0]) for row in changed}
    missing_paths = [path for path in paths if path not in changed_paths]
    if missing_paths:
        raise FindingPathNotChanged(
            f"finding path is not registered as changed: {missing_paths[0]}"
        )

    identities = _identities(connection, scope.repository_id, definitions)
    occurrences = _occurrences(
        connection,
        scope=scope,
        run_id=run_id,
        definitions=definitions,
        identities=identities,
    )
    references = _references(connection, scope=scope, identities=identities)
    return FindingBatch(
        repository_id=scope.repository_id,
        pull_request_id=scope.pull_request_id,
        run_id=run_id,
        items=tuple(
            RecordedFinding(
                finding_id=identities[item.fingerprint].id,
                occurrence_id=occurrences[identities[item.fingerprint].id].id,
                fingerprint=item.fingerprint,
                local_reference=references[identities[item.fingerprint].id],
            )
            for item in definitions
        ),
    )


def resolve_fingerprint(
    connection: psycopg.Connection[TupleRow],
    *,
    repository_id: RepositoryId,
    query: FingerprintQuery,
) -> str:
    """Resolve a full fingerprint or prefix inside exactly one repository."""
    _require_transaction(connection)
    operator = "=" if query.exact else "LIKE"
    value = query.value if query.exact else query.value + "%"
    rows = connection.execute(
        "SELECT fingerprint FROM review_agent.finding_identities "
        f"WHERE repository_id = %s AND fingerprint {operator} %s "
        "ORDER BY fingerprint LIMIT 2",
        (repository_id, value),
    ).fetchall()
    if not rows:
        raise FingerprintNotFound("unknown fingerprint in this repository")
    if len(rows) > 1:
        raise AmbiguousFingerprint(
            "ambiguous fingerprint prefix in this repository; provide more characters"
        )
    return str(rows[0][0])


def repeat_history(
    connection: psycopg.Connection[TupleRow],
    *,
    run_id: ReviewRunId,
    limit: int,
) -> tuple[RepeatFinding, ...]:
    """Return the latest prior occurrence per identity for the run's pull request."""
    _require_transaction(connection)
    if isinstance(limit, bool) or limit < 1 or limit > MAX_FINDINGS_PER_REVIEW:
        raise FindingStoreError(
            f"repeat history limit must be between 1 and {MAX_FINDINGS_PER_REVIEW}"
        )
    scope = _scope(connection, run_id, for_write=False)
    with connection.cursor(row_factory=class_row(_RepeatRow)) as cursor:
        rows = cursor.execute(
            """
            SELECT fingerprint, local_reference, previous_run_id, previous_head,
                   rule_id, path, line, symbol, anchor, title, severity,
                   category, publication_score, confidence, context_hash,
                   evidence, disproof_checks, impact, smallest_fix
            FROM (
                SELECT DISTINCT ON (occurrence.finding_id)
                       identity.fingerprint, reference.local_reference,
                       occurrence.review_run_id AS previous_run_id,
                       subject.head_sha AS previous_head,
                       identity.rule_id, identity.path, occurrence.line,
                       identity.symbol, identity.anchor, occurrence.title,
                       occurrence.severity, occurrence.category,
                       occurrence.publication_score, occurrence.confidence,
                       occurrence.context_hash, occurrence.evidence,
                       occurrence.disproof_checks, occurrence.impact,
                       occurrence.smallest_fix, occurrence.observed_at,
                       occurrence.id
                FROM review_agent.finding_occurrences AS occurrence
                JOIN review_agent.finding_identities AS identity
                  ON identity.id = occurrence.finding_id
                JOIN review_agent.pull_request_finding_references AS reference
                  ON reference.pull_request_id = occurrence.pull_request_id
                 AND reference.finding_id = occurrence.finding_id
                JOIN review_agent.review_runs AS previous_run
                  ON previous_run.id = occurrence.review_run_id
                JOIN review_agent.review_subjects AS subject
                  ON subject.id = previous_run.review_subject_id
                WHERE occurrence.pull_request_id = %s
                  AND occurrence.repository_id = %s
                  AND occurrence.review_run_id <> %s
                ORDER BY occurrence.finding_id, occurrence.observed_at DESC,
                         occurrence.id DESC
            ) AS latest
            ORDER BY observed_at DESC, id DESC
            LIMIT %s
            """,
            (scope.pull_request_id, scope.repository_id, run_id, limit),
        ).fetchall()
    return tuple(
        RepeatFinding(
            fingerprint=row.fingerprint,
            local_reference=row.local_reference,
            previous_run_id=row.previous_run_id,
            previous_head=row.previous_head,
            rule_id=row.rule_id,
            path=row.path,
            line=row.line,
            symbol=row.symbol,
            anchor=row.anchor,
            title=row.title,
            severity=Severity(row.severity),
            category=FindingCategory(row.category),
            publication_score=row.publication_score,
            confidence=float(row.confidence),
            context_hash=row.context_hash,
            prior_claim=row.evidence,
            prior_disproof_checks=row.disproof_checks,
            prior_impact=row.impact,
            prior_smallest_fix=row.smallest_fix,
        )
        for row in rows
    )
