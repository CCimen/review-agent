"""Concrete PostgreSQL operations for immutable reviewer-coaching evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import NewType

import psycopg
from psycopg.pq import TransactionStatus
from psycopg.rows import TupleRow, class_row

from ..domain.coaching import CoachRunDefinition, CoachRunDecision
from ..domain.review import RepositoryId


CoachRunId = NewType("CoachRunId", int)
CoachCandidateId = NewType("CoachCandidateId", int)


class CoachingStoreError(ValueError):
    """A coach-run operation violates its durable evidence contract."""


class CoachRepositoryMismatch(CoachingStoreError):
    """The selected repository does not match the normalized coach input."""


class CoachCandidateConflict(CoachingStoreError):
    """One coach run contains the same candidate identity more than once."""


@dataclass(frozen=True, slots=True)
class CoachCandidate:
    id: CoachCandidateId
    coach_run_id: CoachRunId
    candidate_key: str
    target_owner: str
    suggested_route: str
    event_type: str
    independent_episode_count: int
    evidence_event_ids: tuple[str, ...]
    evidence_events_total: int


@dataclass(frozen=True, slots=True)
class CoachRun:
    id: CoachRunId
    repository_id: RepositoryId | None
    repository: str | None
    source_event_set_id: str
    source_snapshot_id: str | None
    proposal_set_id: str
    decision: CoachRunDecision
    events_considered: int
    artifact_dir: str | None
    recorded_at: datetime
    candidates: tuple[CoachCandidate, ...]


@dataclass(frozen=True, slots=True)
class _CoachRunRow:
    id: CoachRunId
    repository_id: RepositoryId | None
    repository: str | None
    source_event_set_id: str
    source_snapshot_id: str | None
    proposal_set_id: str
    events_considered: int
    artifact_dir: str | None
    recorded_at: datetime


@dataclass(frozen=True, slots=True)
class _CoachCandidateRow:
    id: CoachCandidateId
    coach_run_id: CoachRunId
    candidate_key: str
    target_owner: str
    suggested_route: str
    event_type: str
    independent_episode_count: int
    evidence_event_ids: list[str]
    evidence_events_total: int


_RUN_COLUMNS = """
    run.id, run.repository_id, repository.full_name AS repository,
    run.source_event_set_id, run.source_snapshot_id, run.proposal_set_id,
    run.events_considered, run.artifact_dir, run.recorded_at
"""
_CANDIDATE_COLUMNS = """
    id, coach_run_id, candidate_key, target_owner, suggested_route,
    event_type, independent_episode_count, evidence_event_ids,
    evidence_events_total
"""


def _require_transaction(connection: psycopg.Connection[TupleRow]) -> None:
    if connection.info.transaction_status != TransactionStatus.INTRANS:
        raise CoachingStoreError("coaching operations require an active transaction")


def _candidate(row: _CoachCandidateRow) -> CoachCandidate:
    return CoachCandidate(
        id=row.id,
        coach_run_id=row.coach_run_id,
        candidate_key=row.candidate_key,
        target_owner=row.target_owner,
        suggested_route=row.suggested_route,
        event_type=row.event_type,
        independent_episode_count=row.independent_episode_count,
        evidence_event_ids=tuple(row.evidence_event_ids),
        evidence_events_total=row.evidence_events_total,
    )


def _repository_scope(
    connection: psycopg.Connection[TupleRow],
    *,
    repository_id: RepositoryId | None,
    expected_name: str | None,
) -> None:
    if repository_id is None:
        if expected_name is not None:
            raise CoachRepositoryMismatch(
                "repository-scoped coach input requires a repository identity"
            )
        return
    row = connection.execute(
        "SELECT lower(full_name) FROM review_agent.repositories "
        "WHERE id = %s FOR KEY SHARE",
        (repository_id,),
    ).fetchone()
    if row is None or expected_name is None or row[0] != expected_name:
        raise CoachRepositoryMismatch(
            "coach repository identity does not match its normalized full name"
        )


def record_run(
    connection: psycopg.Connection[TupleRow],
    *,
    repository_id: RepositoryId | None,
    definition: CoachRunDefinition,
) -> CoachRun:
    """Persist one coach run and its exact candidate set atomically."""
    _require_transaction(connection)
    has_candidates = bool(definition.candidates)
    if (definition.decision == "propose") != has_candidates:
        raise CoachingStoreError(
            "coach run decision must match whether candidates are present"
        )
    _repository_scope(
        connection,
        repository_id=repository_id,
        expected_name=definition.repository,
    )
    row = connection.execute(
        """
        INSERT INTO review_agent.coach_runs (
            repository_id, source_event_set_id, source_snapshot_id,
            proposal_set_id, events_considered, artifact_dir, recorded_at
        ) VALUES (%s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
        RETURNING id
        """,
        (
            repository_id,
            definition.source_event_set_id,
            definition.source_snapshot_id,
            definition.proposal_set_id,
            definition.events_considered,
            definition.artifact_dir,
        ),
    ).fetchone()
    if row is None:
        raise CoachingStoreError("coach run insert returned no identity")
    run_id = CoachRunId(int(row[0]))
    if definition.candidates:
        try:
            with connection.cursor() as cursor:
                cursor.executemany(
                    """
                    INSERT INTO review_agent.coach_candidates (
                        coach_run_id, candidate_key, target_owner,
                        suggested_route, event_type, independent_episode_count,
                        evidence_event_ids, evidence_events_total
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        (
                            run_id,
                            candidate.candidate_key,
                            candidate.target_owner,
                            candidate.suggested_route,
                            candidate.event_type,
                            candidate.independent_episode_count,
                            list(candidate.evidence_event_ids),
                            candidate.evidence_events_total,
                        )
                        for candidate in definition.candidates
                    ),
                )
        except psycopg.errors.UniqueViolation as exc:
            raise CoachCandidateConflict(
                "coach run contains a duplicate candidate key"
            ) from exc
    return load_run(connection, run_id=run_id)


def load_run(
    connection: psycopg.Connection[TupleRow], *, run_id: CoachRunId
) -> CoachRun:
    """Return one run; its decision is derived from the exact candidate set."""
    _require_transaction(connection)
    with connection.cursor(row_factory=class_row(_CoachRunRow)) as cursor:
        row = cursor.execute(
            f"""
            SELECT {_RUN_COLUMNS}
            FROM review_agent.coach_runs AS run
            LEFT JOIN review_agent.repositories AS repository
              ON repository.id = run.repository_id
            WHERE run.id = %s
            """,
            (run_id,),
        ).fetchone()
    if row is None:
        raise CoachingStoreError("coach run does not exist")
    with connection.cursor(row_factory=class_row(_CoachCandidateRow)) as cursor:
        candidate_rows = cursor.execute(
            f"SELECT {_CANDIDATE_COLUMNS} "
            "FROM review_agent.coach_candidates "
            "WHERE coach_run_id = %s ORDER BY candidate_key, id",
            (run_id,),
        ).fetchall()
    candidates = tuple(_candidate(item) for item in candidate_rows)
    return CoachRun(
        id=row.id,
        repository_id=row.repository_id,
        repository=row.repository,
        source_event_set_id=row.source_event_set_id,
        source_snapshot_id=row.source_snapshot_id,
        proposal_set_id=row.proposal_set_id,
        decision="propose" if candidates else "no_change",
        events_considered=row.events_considered,
        artifact_dir=row.artifact_dir,
        recorded_at=row.recorded_at,
        candidates=candidates,
    )
