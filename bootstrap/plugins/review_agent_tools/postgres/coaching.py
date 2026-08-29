"""Concrete PostgreSQL operations for immutable reviewer-coaching evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import NewType

import psycopg
from psycopg.pq import TransactionStatus
from psycopg.rows import TupleRow, class_row

from ..domain.coaching import (
    CoachInterventionOutcome,
    CoachInterventionOutcomeDefinition,
    CoachRunDefinition,
    CoachRunDecision,
)
from ..domain.review import RepositoryId


CoachRunId = NewType("CoachRunId", int)
CoachCandidateId = NewType("CoachCandidateId", int)
CoachInterventionOutcomeId = NewType("CoachInterventionOutcomeId", int)


class CoachingStoreError(ValueError):
    """A coach-run operation violates its durable evidence contract."""


class CoachRepositoryMismatch(CoachingStoreError):
    """The selected repository does not match the normalized coach input."""


class CoachCandidateConflict(CoachingStoreError):
    """One coach run contains the same candidate identity more than once."""


class CoachCandidateNotFound(CoachingStoreError):
    """No stored candidate matches the verified proposal identity."""


class CoachCandidateProvenanceMismatch(CoachingStoreError):
    """A candidate exists but its repository or proposal provenance disagrees."""


class CoachInterventionConflict(CoachingStoreError):
    """The exact intervention outcome was already recorded."""


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
class CoachInterventionOutcomeRecord:
    id: CoachInterventionOutcomeId
    coach_candidate_id: CoachCandidateId
    candidate_key: str
    target_owner: str
    proposal_set_id: str
    intervention_key: str
    proposal_content_hash: str
    base_contract_hash: str
    diff_hash: str | None
    validation_receipt_hash: str | None
    outcome: CoachInterventionOutcome
    reason: str
    actor: str
    recorded_at: datetime


@dataclass(frozen=True, slots=True)
class CoachInterventionHistory:
    repository: str
    candidate_key: str
    target_owners: tuple[str, ...]
    coach_run_count: int
    maximum_independent_episodes: int
    interventions: tuple[CoachInterventionOutcomeRecord, ...]


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


@dataclass(frozen=True, slots=True)
class _CoachInterventionOutcomeRow:
    id: CoachInterventionOutcomeId
    coach_candidate_id: CoachCandidateId
    candidate_key: str
    target_owner: str
    proposal_set_id: str
    intervention_key: str
    proposal_content_hash: str
    base_contract_hash: str
    diff_hash: str | None
    validation_receipt_hash: str | None
    outcome: CoachInterventionOutcome
    reason: str
    actor: str
    recorded_at: datetime


@dataclass(frozen=True, slots=True)
class _CoachHistorySummaryRow:
    coach_run_count: int
    maximum_independent_episodes: int
    target_owners: list[str]


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
_CANDIDATE_PROVENANCE_COLUMNS = """
    candidate.id, candidate.coach_run_id, candidate.candidate_key,
    candidate.target_owner, candidate.suggested_route, candidate.event_type,
    candidate.independent_episode_count, candidate.evidence_event_ids,
    candidate.evidence_events_total
"""
_INTERVENTION_COLUMNS = """
    intervention.id, intervention.coach_candidate_id,
    candidate.candidate_key, candidate.target_owner, run.proposal_set_id,
    intervention.intervention_key, intervention.proposal_content_hash,
    intervention.base_contract_hash, intervention.diff_hash,
    intervention.validation_receipt_hash, intervention.outcome,
    intervention.reason, intervention.actor, intervention.recorded_at
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


def _intervention(row: _CoachInterventionOutcomeRow) -> CoachInterventionOutcomeRecord:
    return CoachInterventionOutcomeRecord(
        id=row.id,
        coach_candidate_id=row.coach_candidate_id,
        candidate_key=row.candidate_key,
        target_owner=row.target_owner,
        proposal_set_id=row.proposal_set_id,
        intervention_key=row.intervention_key,
        proposal_content_hash=row.proposal_content_hash,
        base_contract_hash=row.base_contract_hash,
        diff_hash=row.diff_hash,
        validation_receipt_hash=row.validation_receipt_hash,
        outcome=row.outcome,
        reason=row.reason,
        actor=row.actor,
        recorded_at=row.recorded_at,
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


def resolve_intervention_candidate(
    connection: psycopg.Connection[TupleRow],
    *,
    repository: str,
    proposal_set_id: str,
    candidate_key: str,
    target_owner: str,
) -> CoachCandidate:
    """Resolve the canonical candidate for a verified proposal identity."""
    _require_transaction(connection)
    with connection.cursor(row_factory=class_row(_CoachCandidateRow)) as cursor:
        row = cursor.execute(
            f"""
            SELECT {_CANDIDATE_PROVENANCE_COLUMNS}
            FROM review_agent.coach_candidates AS candidate
            JOIN review_agent.coach_runs AS run ON run.id = candidate.coach_run_id
            LEFT JOIN review_agent.repositories AS repository
              ON repository.id = run.repository_id
            WHERE run.proposal_set_id = %s
              AND candidate.candidate_key = %s
              AND lower(repository.full_name) = lower(%s)
              AND candidate.target_owner = %s
            ORDER BY candidate.id
            LIMIT 1
            """,
            (proposal_set_id, candidate_key, repository, target_owner),
        ).fetchone()
    if row is None:
        exists = connection.execute(
            """
            SELECT 1
            FROM review_agent.coach_candidates AS candidate
            JOIN review_agent.coach_runs AS run ON run.id = candidate.coach_run_id
            WHERE run.proposal_set_id = %s AND candidate.candidate_key = %s
            LIMIT 1
            """,
            (proposal_set_id, candidate_key),
        ).fetchone()
        if exists is not None:
            raise CoachCandidateProvenanceMismatch(
                "coach candidate does not match the requested repository and owner"
            )
        raise CoachCandidateNotFound("coach candidate does not exist")
    # Replaying identical coach evidence may record an equivalent run. The
    # earliest exact candidate is stable, so later replays cannot change the
    # intervention identity derived from its database id.
    return _candidate(row)


def record_intervention_outcome(
    connection: psycopg.Connection[TupleRow],
    definition: CoachInterventionOutcomeDefinition,
) -> CoachInterventionOutcomeRecord:
    """Append one final, human-governed intervention evaluation."""
    _require_transaction(connection)
    try:
        row = connection.execute(
            """
            INSERT INTO review_agent.coach_intervention_outcomes (
                coach_candidate_id, intervention_key, proposal_content_hash,
                base_contract_hash, diff_hash, validation_receipt_hash,
                outcome, reason, actor, recorded_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
            RETURNING id
            """,
            (
                definition.coach_candidate_id,
                definition.intervention_key,
                definition.proposal_content_hash,
                definition.base_contract_hash,
                definition.diff_hash,
                definition.validation_receipt_hash,
                definition.outcome,
                definition.reason,
                definition.actor,
            ),
        ).fetchone()
    except psycopg.errors.UniqueViolation as exc:
        raise CoachInterventionConflict(
            "coach intervention outcome already exists"
        ) from exc
    except psycopg.errors.ForeignKeyViolation as exc:
        raise CoachCandidateNotFound("coach candidate does not exist") from exc
    if row is None:
        raise CoachingStoreError("coach intervention insert returned no identity")
    return load_intervention_outcome(
        connection, outcome_id=CoachInterventionOutcomeId(int(row[0]))
    )


def load_intervention_outcome(
    connection: psycopg.Connection[TupleRow],
    *,
    outcome_id: CoachInterventionOutcomeId,
) -> CoachInterventionOutcomeRecord:
    _require_transaction(connection)
    with connection.cursor(
        row_factory=class_row(_CoachInterventionOutcomeRow)
    ) as cursor:
        row = cursor.execute(
            f"""
            SELECT {_INTERVENTION_COLUMNS}
            FROM review_agent.coach_intervention_outcomes AS intervention
            JOIN review_agent.coach_candidates AS candidate
              ON candidate.id = intervention.coach_candidate_id
            JOIN review_agent.coach_runs AS run ON run.id = candidate.coach_run_id
            WHERE intervention.id = %s
            """,
            (outcome_id,),
        ).fetchone()
    if row is None:
        raise CoachingStoreError("coach intervention outcome does not exist")
    return _intervention(row)


def intervention_history(
    connection: psycopg.Connection[TupleRow],
    *,
    repository: str,
    candidate_key: str,
    limit: int,
) -> CoachInterventionHistory:
    """Return bounded newest-first intervention history for one candidate key."""
    _require_transaction(connection)
    with connection.cursor(row_factory=class_row(_CoachHistorySummaryRow)) as cursor:
        summary = cursor.execute(
            """
            SELECT count(DISTINCT run.id)::integer AS coach_run_count,
                   max(candidate.independent_episode_count)::integer
                       AS maximum_independent_episodes,
                   array_agg(DISTINCT candidate.target_owner
                             ORDER BY candidate.target_owner) AS target_owners
            FROM review_agent.coach_candidates AS candidate
            JOIN review_agent.coach_runs AS run ON run.id = candidate.coach_run_id
            JOIN review_agent.repositories AS repository
              ON repository.id = run.repository_id
            WHERE lower(repository.full_name) = lower(%s)
              AND candidate.candidate_key = %s
            HAVING count(*) > 0
            """,
            (repository, candidate_key),
        ).fetchone()
    if summary is None:
        raise CoachCandidateNotFound("coach candidate does not exist")
    with connection.cursor(
        row_factory=class_row(_CoachInterventionOutcomeRow)
    ) as cursor:
        rows = cursor.execute(
            f"""
            SELECT {_INTERVENTION_COLUMNS}
            FROM review_agent.coach_intervention_outcomes AS intervention
            JOIN review_agent.coach_candidates AS candidate
              ON candidate.id = intervention.coach_candidate_id
            JOIN review_agent.coach_runs AS run ON run.id = candidate.coach_run_id
            JOIN review_agent.repositories AS repository
              ON repository.id = run.repository_id
            WHERE lower(repository.full_name) = lower(%s)
              AND candidate.candidate_key = %s
            ORDER BY intervention.recorded_at DESC, intervention.id DESC
            LIMIT %s
            """,
            (repository, candidate_key, limit),
        ).fetchall()
    return CoachInterventionHistory(
        repository=repository.lower(),
        candidate_key=candidate_key,
        target_owners=tuple(summary.target_owners),
        coach_run_count=summary.coach_run_count,
        maximum_independent_episodes=summary.maximum_independent_episodes,
        interventions=tuple(_intervention(row) for row in rows),
    )
