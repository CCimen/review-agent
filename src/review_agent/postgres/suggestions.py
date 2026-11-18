"""Concrete PostgreSQL operations for optional finding suggestions."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

import psycopg
from psycopg.pq import TransactionStatus
from psycopg.rows import TupleRow

from ..domain.finding import FindingId, FindingOccurrenceId
from ..suggestion_validation import ValidatedSuggestion
from .findings import FindingBatch


class SuggestionStoreError(ValueError):
    """A suggestion operation violates its persisted finding scope."""


@dataclass(frozen=True, slots=True)
class SuggestionContext:
    repository: str
    pr_number: int
    head_sha: str
    canonical_by_finding_id: Mapping[FindingId, ValidatedSuggestion]


def _require_transaction(connection: psycopg.Connection[TupleRow]) -> None:
    if connection.info.transaction_status != TransactionStatus.INTRANS:
        raise SuggestionStoreError("suggestion operations require an active transaction")


def _occurrence_paths(
    connection: psycopg.Connection[TupleRow], batch: FindingBatch
) -> dict[FindingOccurrenceId, tuple[FindingId, str]]:
    occurrence_ids = [item.occurrence_id for item in batch.items]
    if not occurrence_ids:
        return {}
    rows = connection.execute(
        """
        SELECT occurrence.id, occurrence.finding_id, identity.path
        FROM review_agent.finding_occurrences AS occurrence
        JOIN review_agent.finding_identities AS identity
          ON identity.id = occurrence.finding_id
        WHERE occurrence.review_run_id = %s
          AND occurrence.repository_id = %s
          AND occurrence.pull_request_id = %s
          AND occurrence.id = ANY(%s::bigint[])
        """,
        (
            batch.run_id,
            batch.repository_id,
            batch.pull_request_id,
            occurrence_ids,
        ),
    ).fetchall()
    stored = {
        FindingOccurrenceId(int(row[0])): (FindingId(int(row[1])), str(row[2]))
        for row in rows
    }
    expected = {
        item.occurrence_id: item.finding_id
        for item in batch.items
    }
    if len(stored) != len(expected) or any(
        stored.get(occurrence_id, (FindingId(0), ""))[0] != finding_id
        for occurrence_id, finding_id in expected.items()
    ):
        raise SuggestionStoreError(
            "suggestion occurrences do not match the recorded finding batch"
        )
    return stored


def load_context(
    connection: psycopg.Connection[TupleRow], batch: FindingBatch
) -> SuggestionContext:
    """Load exact scope and reusable same-head suggestions for one batch."""
    _require_transaction(connection)
    # Validate scope here; the later store transaction checks its own snapshot.
    _occurrence_paths(connection, batch)
    scope = connection.execute(
        """
        SELECT repository.full_name, pull_request.number, subject.head_sha
        FROM review_agent.review_runs AS run
        JOIN review_agent.pull_requests AS pull_request
          ON pull_request.id = run.pull_request_id
        JOIN review_agent.repositories AS repository
          ON repository.id = pull_request.repository_id
        JOIN review_agent.review_subjects AS subject
          ON subject.id = run.review_subject_id
        WHERE run.id = %s
          AND pull_request.id = %s
          AND repository.id = %s
        """,
        (batch.run_id, batch.pull_request_id, batch.repository_id),
    ).fetchone()
    if scope is None:
        raise SuggestionStoreError("suggestion batch review scope does not exist")

    finding_ids = [item.finding_id for item in batch.items]
    canonical: dict[FindingId, ValidatedSuggestion] = {}
    if finding_ids:
        rows = connection.execute(
            """
            SELECT DISTINCT ON (occurrence.finding_id)
                   occurrence.finding_id, identity.path,
                   suggestion.start_line, suggestion.end_line,
                   suggestion.expected_hash, suggestion.replacement_text,
                   suggestion.suggestion_key
            FROM review_agent.finding_suggestions AS suggestion
            JOIN review_agent.finding_occurrences AS occurrence
              ON occurrence.id = suggestion.finding_occurrence_id
            JOIN review_agent.finding_identities AS identity
              ON identity.id = occurrence.finding_id
            JOIN review_agent.review_runs AS run
              ON run.id = occurrence.review_run_id
            JOIN review_agent.review_subjects AS subject
              ON subject.id = run.review_subject_id
            WHERE occurrence.repository_id = %s
              AND occurrence.pull_request_id = %s
              AND occurrence.finding_id = ANY(%s::bigint[])
              AND subject.head_sha = %s
            ORDER BY occurrence.finding_id, suggestion.recorded_at, suggestion.id
            """,
            (
                batch.repository_id,
                batch.pull_request_id,
                finding_ids,
                str(scope[2]),
            ),
        ).fetchall()
        for row in rows:
            canonical[FindingId(int(row[0]))] = {
                "path": str(row[1]),
                "start_line": int(row[2]),
                "end_line": int(row[3]),
                "expected_hash": str(row[4]),
                "replacement_text": str(row[5]),
                "suggestion_key": str(row[6]),
            }
    return SuggestionContext(
        repository=str(scope[0]),
        pr_number=int(scope[1]),
        head_sha=str(scope[2]),
        canonical_by_finding_id=canonical,
    )


def replace_suggestions(
    connection: psycopg.Connection[TupleRow],
    *,
    batch: FindingBatch,
    selected: Mapping[FindingOccurrenceId, ValidatedSuggestion],
) -> None:
    """Replace the batch suggestions inside the caller's short transaction."""
    _require_transaction(connection)
    occurrence_paths = _occurrence_paths(connection, batch)
    unexpected = set(selected).difference(occurrence_paths)
    if unexpected:
        raise SuggestionStoreError("suggestion does not belong to the finding batch")
    for occurrence_id, suggestion in selected.items():
        if occurrence_paths[occurrence_id][1] != suggestion["path"]:
            raise SuggestionStoreError("suggestion path does not match its finding")
        if not re.fullmatch(r"[0-9a-f]{64}", suggestion["expected_hash"]):
            raise SuggestionStoreError("suggestion expected_hash is invalid")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", suggestion["suggestion_key"]):
            raise SuggestionStoreError("suggestion_key is invalid")

    occurrence_ids = list(occurrence_paths)
    if occurrence_ids:
        connection.execute(
            "DELETE FROM review_agent.finding_suggestions "
            "WHERE finding_occurrence_id = ANY(%s::bigint[])",
            (occurrence_ids,),
        )
    if selected:
        ordered = sorted(selected.items(), key=lambda item: int(item[0]))
        connection.execute(
            """
            INSERT INTO review_agent.finding_suggestions (
                finding_occurrence_id, start_line, end_line, expected_hash,
                replacement_text, suggestion_key, recorded_at
            )
            SELECT incoming.finding_occurrence_id, incoming.start_line,
                   incoming.end_line, incoming.expected_hash,
                   incoming.replacement_text, incoming.suggestion_key,
                   statement_timestamp()
            FROM unnest(
                %s::bigint[], %s::integer[], %s::integer[],
                %s::text[], %s::text[], %s::text[]
            ) AS incoming(
                finding_occurrence_id, start_line, end_line, expected_hash,
                replacement_text, suggestion_key
            )
            """,
            (
                [occurrence_id for occurrence_id, _suggestion in ordered],
                [suggestion["start_line"] for _occurrence_id, suggestion in ordered],
                [suggestion["end_line"] for _occurrence_id, suggestion in ordered],
                [suggestion["expected_hash"] for _occurrence_id, suggestion in ordered],
                [suggestion["replacement_text"] for _occurrence_id, suggestion in ordered],
                [suggestion["suggestion_key"] for _occurrence_id, suggestion in ordered],
            ),
        )
