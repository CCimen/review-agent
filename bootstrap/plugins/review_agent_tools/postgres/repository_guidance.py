"""Immutable PostgreSQL aggregates of repository-owned review guidance."""

from __future__ import annotations

from dataclasses import dataclass

import psycopg
from psycopg.pq import TransactionStatus
from psycopg.rows import TupleRow, class_row
from psycopg.types.json import Jsonb

from .. import repository_guidance_context
from ..domain import repository_guidance as guidance_domain
from ..repository_guidance_context import RepositoryGuidanceContext
from .review_runs import ReviewRunId


class RepositoryGuidanceStoreError(ValueError):
    """A repository guidance snapshot conflicts with its review run."""


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
    config_hash: str | None
    snapshot_hash: str
    payload: object
    instructions_present: bool
    context_file_count: int
    failure_code: str | None


def _require_transaction(connection: psycopg.Connection[TupleRow]) -> None:
    if connection.info.transaction_status != TransactionStatus.INTRANS:
        raise RepositoryGuidanceStoreError(
            "repository guidance operations require an active transaction"
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
        raise RepositoryGuidanceStoreError("review run does not exist")
    return row


def _snapshot(
    connection: psycopg.Connection[TupleRow],
    *,
    run_id: ReviewRunId,
) -> _SnapshotRow | None:
    with connection.cursor(row_factory=class_row(_SnapshotRow)) as cursor:
        return cursor.execute(
            """
            SELECT id, base_sha, schema_version, status, config_hash,
                   snapshot_hash, payload, instructions_present,
                   context_file_count, failure_code
            FROM review_agent.review_guidance_snapshots
            WHERE review_run_id = %s
            """,
            (run_id,),
        ).fetchone()


def _restore(row: _SnapshotRow, *, run: _RunRow) -> RepositoryGuidanceContext:
    try:
        context = repository_guidance_context.restore_snapshot(
            snapshot_id=row.id,
            value=row.payload,
            expected_hash=row.snapshot_hash,
        )
    except repository_guidance_context.RepositoryGuidanceContextError as exc:
        raise RepositoryGuidanceStoreError(
            "stored repository guidance snapshot is invalid"
        ) from exc
    if (
        context.base_sha != run.base_sha
        or row.base_sha != context.base_sha
        or row.schema_version != context.schema_version
        or row.status != context.status
        or row.config_hash != context.config_hash
        or row.failure_code != context.failure_code
        or row.instructions_present != (context.instructions is not None)
        or row.context_file_count != len(context.context_files)
    ):
        raise RepositoryGuidanceStoreError(
            "stored guidance columns disagree with the typed aggregate"
        )
    return context


def load_context(
    connection: psycopg.Connection[TupleRow],
    *,
    run_id: ReviewRunId,
) -> RepositoryGuidanceContext:
    """Load the one immutable optional guidance state for a review run."""
    _require_transaction(connection)
    run = _run(connection, run_id=run_id, lock=False)
    row = _snapshot(connection, run_id=run_id)
    if row is None:
        return repository_guidance_context.pending(base_sha=run.base_sha)
    return _restore(row, run=run)


def store_context(
    connection: psycopg.Connection[TupleRow],
    *,
    run_id: ReviewRunId,
    context: RepositoryGuidanceContext,
) -> RepositoryGuidanceContext:
    """Store the first complete aggregate and return it on later retries."""
    _require_transaction(connection)
    run = _run(connection, run_id=run_id, lock=True)
    existing = _snapshot(connection, run_id=run_id)
    if existing is not None:
        return _restore(existing, run=run)
    if run.run_status != "running":
        raise RepositoryGuidanceStoreError(
            "repository guidance requires an active review run"
        )
    if context.status == "pending" or context.snapshot_id is not None:
        raise RepositoryGuidanceStoreError(
            "only a new complete guidance snapshot can be stored"
        )
    if context.base_sha != run.base_sha:
        raise RepositoryGuidanceStoreError(
            "repository guidance base SHA does not match the review run"
        )
    value = repository_guidance_context.snapshot_value(context)
    try:
        repository_guidance_context.restore_snapshot(
            snapshot_id=1,
            value=value,
            expected_hash=context.snapshot_hash,
        )
    except repository_guidance_context.RepositoryGuidanceContextError as exc:
        raise RepositoryGuidanceStoreError(
            "repository guidance context is invalid"
        ) from exc
    with connection.cursor(row_factory=class_row(_SnapshotRow)) as cursor:
        row = cursor.execute(
            """
            INSERT INTO review_agent.review_guidance_snapshots (
                review_run_id, base_sha, schema_version, status, config_path,
                config_hash, snapshot_hash, payload, instructions_present,
                context_file_count, failure_code, loaded_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                statement_timestamp()
            )
            RETURNING id, base_sha, schema_version, status, config_hash,
                      snapshot_hash, payload, instructions_present,
                      context_file_count, failure_code
            """,
            (
                run_id,
                context.base_sha,
                context.schema_version,
                context.status,
                guidance_domain.CONFIG_PATH,
                context.config_hash,
                context.snapshot_hash,
                Jsonb(value),
                context.instructions is not None,
                len(context.context_files),
                context.failure_code,
            ),
        ).fetchone()
    if row is None:
        raise RepositoryGuidanceStoreError(
            "repository guidance snapshot insert returned no row"
        )
    return _restore(row, run=run)


__all__ = [
    "RepositoryGuidanceStoreError",
    "load_context",
    "store_context",
]
