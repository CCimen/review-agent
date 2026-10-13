"""Review parts: changed-file ownership, per-attempt reading, and staged reports."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast

import psycopg
from psycopg.pq import TransactionStatus
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb

from ..domain.review import JsonObject, ReviewRunId


class ReviewPartError(ValueError):
    """A review-part operation does not match the stored plan."""


@dataclass(frozen=True, slots=True)
class PartReport:
    part: int
    report: JsonObject


def _require_transaction(connection: psycopg.Connection[TupleRow]) -> None:
    if connection.info.transaction_status != TransactionStatus.INTRANS:
        raise ReviewPartError("review-part operations require an active transaction")


def planned_parts(
    connection: psycopg.Connection[TupleRow], *, run_id: ReviewRunId
) -> int:
    row = connection.execute(
        "SELECT count(*)::integer FROM review_agent.review_parts WHERE review_run_id = %s",
        (run_id,),
    ).fetchone()
    return int(row[0]) if row else 0


def assign(
    connection: psycopg.Connection[TupleRow],
    *,
    run_id: ReviewRunId,
    parts: Sequence[Sequence[str]],
) -> None:
    """Record which part owns each changed file's diff coverage."""
    _require_transaction(connection)
    for number, paths in enumerate(parts, start=1):
        connection.execute(
            "INSERT INTO review_agent.review_parts (review_run_id, part) VALUES (%s, %s)",
            (run_id, number),
        )
        # A planned path without a registered changed-file row has no coverage
        # to own, so it is left out instead of failing the review.
        connection.execute(
            """
            UPDATE review_agent.review_run_files SET part = %s
            WHERE review_run_id = %s AND is_changed_path AND path = ANY(%s::text[])
            """,
            (number, run_id, list(paths)),
        )


def owned_paths(
    connection: psycopg.Connection[TupleRow],
    *,
    run_id: ReviewRunId,
    part: int,
    paths: Sequence[str],
) -> frozenset[str]:
    rows = connection.execute(
        """
        SELECT path FROM review_agent.review_run_files
        WHERE review_run_id = %s AND part = %s AND path = ANY(%s::text[])
        """,
        (run_id, part, list(paths)),
    ).fetchall()
    return frozenset(str(row[0]) for row in rows)


def begin_reading(
    connection: psycopg.Connection[TupleRow],
    *,
    run_id: ReviewRunId,
    part: int,
    lease_generation: int,
) -> None:
    """Start one attempt's reading: an earlier attempt's reads do not count.

    A reclaimed part job is a new reviewer session that has seen none of the
    diffs its predecessor read, so the part's files return to unseen. Calling
    again in the same lease generation keeps what that session has read.
    """
    _require_transaction(connection)
    row = connection.execute(
        """
        SELECT read_generation, report IS NOT NULL FROM review_agent.review_parts
        WHERE review_run_id = %s AND part = %s FOR UPDATE
        """,
        (run_id, part),
    ).fetchone()
    if row is None:
        raise ReviewPartError("this review has no such part")
    if row[1] or row[0] == lease_generation:
        return
    connection.execute(
        """
        UPDATE review_agent.review_run_files
        SET diff_state = 'unseen', unavailable_reason = NULL, diff_observed_at = NULL,
            diff_content_sha256 = NULL, diff_total_chars = NULL,
            diff_read_ranges = '{}'::int8multirange
        WHERE review_run_id = %s AND part = %s
        """,
        (run_id, part),
    )
    connection.execute(
        "UPDATE review_agent.review_parts SET read_generation = %s "
        "WHERE review_run_id = %s AND part = %s",
        (lease_generation, run_id, part),
    )


def assigned_files(
    connection: psycopg.Connection[TupleRow], *, run_id: ReviewRunId, part: int
) -> tuple[int, int]:
    """Return the part's changed files and how many still have a readable diff left."""
    row = connection.execute(
        """
        SELECT count(*)::integer,
               (count(*) FILTER (WHERE diff_state IN ('unseen', 'truncated')))::integer
        FROM review_agent.review_run_files WHERE review_run_id = %s AND part = %s
        """,
        (run_id, part),
    ).fetchone()
    return (int(row[0]), int(row[1])) if row else (0, 0)


def store_report(
    connection: psycopg.Connection[TupleRow],
    *,
    run_id: ReviewRunId,
    part: int,
    report: JsonObject,
) -> None:
    _require_transaction(connection)
    stored = connection.execute(
        """
        UPDATE review_agent.review_parts
        SET report = %s, reported_at = statement_timestamp()
        WHERE review_run_id = %s AND part = %s AND report IS NULL
        """,
        (Jsonb(report), run_id, part),
    ).rowcount
    if stored != 1:
        raise ReviewPartError("this part has already reported")


def reports(
    connection: psycopg.Connection[TupleRow], *, run_id: ReviewRunId
) -> tuple[PartReport, ...]:
    """Return every committed part report in part order."""
    rows = connection.execute(
        """
        SELECT part, report FROM review_agent.review_parts
        WHERE review_run_id = %s AND report IS NOT NULL ORDER BY part
        """,
        (run_id,),
    ).fetchall()
    return tuple(PartReport(int(row[0]), cast(JsonObject, row[1])) for row in rows)


def mark_report_read(
    connection: psycopg.Connection[TupleRow],
    *,
    run_id: ReviewRunId,
    part: int,
    lease_generation: int,
) -> None:
    connection.execute(
        "UPDATE review_agent.review_parts SET report_read_generation = %s "
        "WHERE review_run_id = %s AND part = %s",
        (lease_generation, run_id, part),
    )


def unread_reports(
    connection: psycopg.Connection[TupleRow],
    *,
    run_id: ReviewRunId,
    lease_generation: int,
) -> tuple[int, ...]:
    """Parts whose report the final session of this lease generation has not received."""
    rows = connection.execute(
        """
        SELECT part FROM review_agent.review_parts
        WHERE review_run_id = %s AND report_read_generation IS DISTINCT FROM %s
        ORDER BY part
        """,
        (run_id, lease_generation),
    ).fetchall()
    return tuple(int(row[0]) for row in rows)
