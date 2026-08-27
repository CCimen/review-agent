"""Immutable PostgreSQL aggregates of repository design evidence."""

from __future__ import annotations

from dataclasses import dataclass

import psycopg
from psycopg.pq import TransactionStatus
from psycopg.rows import TupleRow, class_row
from psycopg.types.json import Jsonb

from .. import repository_decision_context
from ..repository_decision_context import RepositoryDecisionContext
from .review_runs import ReviewRunId


class RepositoryDecisionStoreError(ValueError):
    """A repository decision snapshot conflicts with its review run."""


@dataclass(frozen=True, slots=True)
class _RunRow:
    run_status: str
    base_sha: str


@dataclass(frozen=True, slots=True)
class _SnapshotRow:
    id: int
    base_sha: str
    schema_version: int
    status: str
    index_hash: str | None
    snapshot_hash: str
    payload: object
    matched_decision_count: int
    failure_code: str | None


def _require_transaction(connection: psycopg.Connection[TupleRow]) -> None:
    if connection.info.transaction_status != TransactionStatus.INTRANS:
        raise RepositoryDecisionStoreError(
            "repository decision operations require an active transaction"
        )


def _run(
    connection: psycopg.Connection[TupleRow],
    *,
    run_id: ReviewRunId,
    lock: bool,
) -> _RunRow:
    suffix = " FOR UPDATE" if lock else ""
    with connection.cursor(row_factory=class_row(_RunRow)) as cursor:
        row = cursor.execute(
            """
            SELECT run.status AS run_status, subject.base_sha
            FROM review_agent.review_runs AS run
            JOIN review_agent.review_subjects AS subject
              ON subject.id = run.review_subject_id
            WHERE run.id = %s
            """
            + suffix,
            (run_id,),
        ).fetchone()
    if row is None:
        raise RepositoryDecisionStoreError("review run does not exist")
    return row


def _snapshot(
    connection: psycopg.Connection[TupleRow],
    *,
    run_id: ReviewRunId,
) -> _SnapshotRow | None:
    with connection.cursor(row_factory=class_row(_SnapshotRow)) as cursor:
        return cursor.execute(
            """
            SELECT id, base_sha, schema_version, status, index_hash,
                   snapshot_hash, payload, matched_decision_count, failure_code
            FROM review_agent.review_decision_snapshots
            WHERE review_run_id = %s
            """,
            (run_id,),
        ).fetchone()


def _restore(row: _SnapshotRow, *, run: _RunRow) -> RepositoryDecisionContext:
    try:
        context = repository_decision_context.restore_snapshot(
            snapshot_id=row.id,
            value=row.payload,
            expected_hash=row.snapshot_hash,
        )
    except repository_decision_context.RepositoryDecisionContextError as exc:
        raise RepositoryDecisionStoreError("stored decision snapshot is invalid") from exc
    if (
        context.base_sha != run.base_sha
        or row.base_sha != context.base_sha
        or row.schema_version != context.schema_version
        or row.status != context.status
        or row.index_hash != context.index_hash
        or row.failure_code != context.failure_code
        or row.matched_decision_count != len(context.decisions)
    ):
        raise RepositoryDecisionStoreError(
            "stored decision snapshot columns disagree with the typed aggregate"
        )
    return context


def load_context(
    connection: psycopg.Connection[TupleRow], *, run_id: ReviewRunId
) -> RepositoryDecisionContext:
    """Load the one immutable optional context state for a review run."""
    _require_transaction(connection)
    run = _run(connection, run_id=run_id, lock=False)
    row = _snapshot(connection, run_id=run_id)
    if row is None:
        return repository_decision_context.pending(base_sha=run.base_sha)
    return _restore(row, run=run)


def store_context(
    connection: psycopg.Connection[TupleRow],
    *,
    run_id: ReviewRunId,
    context: RepositoryDecisionContext,
) -> RepositoryDecisionContext:
    """Store the first complete aggregate and return it on later retries."""
    _require_transaction(connection)
    run = _run(connection, run_id=run_id, lock=True)
    existing = _snapshot(connection, run_id=run_id)
    if existing is not None:
        return _restore(existing, run=run)
    if run.run_status != "running":
        raise RepositoryDecisionStoreError(
            "repository decision context requires an active review run"
        )
    if context.status == "pending" or context.snapshot_id is not None:
        raise RepositoryDecisionStoreError("only a new complete snapshot can be stored")
    if context.base_sha != run.base_sha:
        raise RepositoryDecisionStoreError(
            "repository decision context base SHA does not match the review run"
        )
    value = repository_decision_context.snapshot_value(context)
    try:
        repository_decision_context.restore_snapshot(
            snapshot_id=1,
            value=value,
            expected_hash=context.snapshot_hash,
        )
    except repository_decision_context.RepositoryDecisionContextError as exc:
        raise RepositoryDecisionStoreError("repository decision context is invalid") from exc
    with connection.cursor(row_factory=class_row(_SnapshotRow)) as cursor:
        row = cursor.execute(
            """
            INSERT INTO review_agent.review_decision_snapshots (
                review_run_id, base_sha, schema_version, status, index_path,
                index_hash, snapshot_hash, payload, matched_decision_count,
                failure_code, loaded_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                statement_timestamp()
            )
            RETURNING id, base_sha, schema_version, status, index_hash,
                      snapshot_hash, payload, matched_decision_count, failure_code
            """,
            (
                run_id,
                context.base_sha,
                context.schema_version,
                context.status,
                repository_decision_context.INDEX_PATH,
                context.index_hash,
                context.snapshot_hash,
                Jsonb(value),
                len(context.decisions),
                context.failure_code,
            ),
        ).fetchone()
    if row is None:
        raise RepositoryDecisionStoreError("decision snapshot insert returned no row")
    return _restore(row, run=run)


__all__ = [
    "RepositoryDecisionStoreError",
    "load_context",
    "store_context",
]
