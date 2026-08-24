"""Normalized changed-file inventory and content-read coverage operations."""

from __future__ import annotations

from dataclasses import dataclass

import psycopg
from psycopg import sql
from psycopg.pq import TransactionStatus
from psycopg.rows import TupleRow, class_row

from ..domain.review import (
    ChangedFileDefinition,
    CoverageState,
    DiffObservation,
    DiffState,
    FileReadDefinition,
    ReviewRunId,
    resolve_changed_file_count,
    resolve_review_path,
)


class CoverageError(ValueError):
    """A coverage operation violates its persisted contract."""


class CoverageConflict(CoverageError):
    """A persisted inventory fact conflicts with the submitted definition."""


class CoverageRunNotActive(CoverageError):
    """Coverage writes require an active review run."""


class CoverageFileNotFound(CoverageError):
    """A diff observation does not belong to a registered changed path."""


class InvalidCoverageTransition(CoverageError):
    """A diff observation would weaken a completed coverage fact."""


@dataclass(frozen=True, slots=True)
class CoverageRegistration:
    changed_files_reported: int
    changed_files_registered: int
    registration_complete: bool


@dataclass(frozen=True, slots=True)
class FileReadBatch:
    submitted: int
    inserted: int


@dataclass(frozen=True, slots=True)
class CoverageSummary:
    state: CoverageState
    changed_files_reported: int | None
    changed_files_registered: int
    registration_complete: bool
    changed_paths_with_complete_diff: int
    changed_paths_with_source_reads: int
    supporting_context_paths_read: int
    context_ranges_read: int
    unavailable_paths: int
    truncated_paths: int


@dataclass(frozen=True, slots=True)
class RunFile:
    path: str
    change_status: str
    previous_path: str
    domain: str
    review_mode: str
    diff_state: DiffState
    is_changed_path: bool


@dataclass(frozen=True, slots=True)
class RunFilePage:
    run_id: ReviewRunId
    repository: str
    pr_number: int
    limit: int
    next_cursor: str | None
    total_matching: int
    items: tuple[RunFile, ...]


@dataclass(frozen=True, slots=True)
class RunFileLookup:
    item: RunFile | None
    registration_complete: bool


@dataclass(frozen=True, slots=True)
class FileIndexSummary:
    changed_files_reported: int | None
    changed_files_registered: int
    registration_complete: bool
    by_domain: tuple[tuple[str, int], ...]
    by_review_mode: tuple[tuple[str, int], ...]
    by_change_status: tuple[tuple[str, int], ...]
    sample_paths: tuple[RunFile, ...]


@dataclass(frozen=True, slots=True)
class _RunCoverageRow:
    status: str
    changed_files_reported: int | None
    changed_file_registration_complete: bool


@dataclass(frozen=True, slots=True)
class _RunFileRow:
    id: int
    path: str
    change_status: str | None
    previous_path: str | None
    is_changed_path: bool
    domain: str | None
    review_mode: str
    diff_state: str


@dataclass(frozen=True, slots=True)
class _FileIdRow:
    id: int
    path: str


@dataclass(frozen=True, slots=True)
class _SummaryRow:
    changed_files_reported: int | None
    registration_complete: bool
    changed_files_registered: int
    changed_paths_with_complete_diff: int
    changed_paths_with_source_reads: int
    supporting_context_paths_read: int
    context_ranges_read: int
    unavailable_paths: int
    truncated_paths: int


@dataclass(frozen=True, slots=True)
class _RunIdentityRow:
    repository: str
    pr_number: int
    status: str
    changed_files_reported: int | None
    registration_complete: bool


def _file(row: _RunFileRow) -> RunFile:
    try:
        state = DiffState(row.diff_state)
    except ValueError as exc:
        raise CoverageError("stored changed file has an invalid diff state") from exc
    return RunFile(
        path=row.path,
        change_status=row.change_status or "",
        previous_path=row.previous_path or "",
        domain=row.domain or "general",
        review_mode=row.review_mode,
        diff_state=state,
        is_changed_path=row.is_changed_path,
    )


def _require_transaction(connection: psycopg.Connection[TupleRow]) -> None:
    if connection.info.transaction_status != TransactionStatus.INTRANS:
        raise CoverageError("coverage operations require an active transaction")


def _run_for_write(
    connection: psycopg.Connection[TupleRow],
    run_id: ReviewRunId,
    *,
    exclusive: bool,
) -> _RunCoverageRow:
    lock = "UPDATE" if exclusive else "SHARE"
    with connection.cursor(row_factory=class_row(_RunCoverageRow)) as cursor:
        row = cursor.execute(
            "SELECT status, changed_files_reported, "
            "changed_file_registration_complete "
            "FROM review_agent.review_runs WHERE id = %s FOR " + lock,
            (run_id,),
        ).fetchone()
    if row is None:
        raise CoverageRunNotActive("review run does not exist")
    if row.status != "running":
        raise CoverageRunNotActive("coverage writes require an active review run")
    return row


def _run_identity(
    connection: psycopg.Connection[TupleRow],
    *,
    run_id: ReviewRunId,
    repository: str | None = None,
    pr_number: int | None = None,
    active: bool,
) -> _RunIdentityRow:
    with connection.cursor(row_factory=class_row(_RunIdentityRow)) as cursor:
        row = cursor.execute(
            """
            SELECT repository.full_name AS repository,
                   pull_request.number AS pr_number, run.status,
                   run.changed_files_reported,
                   run.changed_file_registration_complete AS registration_complete
            FROM review_agent.review_runs AS run
            JOIN review_agent.pull_requests AS pull_request
              ON pull_request.id = run.pull_request_id
            JOIN review_agent.repositories AS repository
              ON repository.id = pull_request.repository_id
            WHERE run.id = %s
            """,
            (run_id,),
        ).fetchone()
    if row is None:
        raise CoverageError("review run does not exist")
    if repository is not None and row.repository != repository:
        raise CoverageError("run_id does not match this repository and PR")
    if pr_number is not None and row.pr_number != pr_number:
        raise CoverageError("run_id does not match this repository and PR")
    if active and row.status != "running":
        raise CoverageRunNotActive("coverage reads require an active review run")
    return row


def _counter(
    connection: psycopg.Connection[TupleRow],
    *,
    run_id: ReviewRunId,
    column: str,
) -> tuple[tuple[str, int], ...]:
    allowed = {"domain", "review_mode", "change_status"}
    if column not in allowed:
        raise CoverageError("unsupported changed-file counter")
    field = sql.Identifier(column)
    rows = connection.execute(
        sql.SQL(
            "SELECT {field}, count(*)::integer "
            "FROM review_agent.review_run_files "
            "WHERE review_run_id = %s AND is_changed_path "
            "GROUP BY {field} ORDER BY {field}"
        ).format(field=field),
        (run_id,),
    ).fetchall()
    return tuple((str(row[0] or ""), int(row[1])) for row in rows)


def file_index_summary(
    connection: psycopg.Connection[TupleRow],
    *,
    run_id: ReviewRunId,
    sample_limit: int = 40,
) -> FileIndexSummary:
    _require_transaction(connection)
    identity = _run_identity(connection, run_id=run_id, active=True)
    limit = max(0, min(sample_limit, 80))
    with connection.cursor(row_factory=class_row(_RunFileRow)) as cursor:
        rows = cursor.execute(
            """
            SELECT id, path, change_status, previous_path, is_changed_path,
                   domain, review_mode, diff_state
            FROM review_agent.review_run_files
            WHERE review_run_id = %s AND is_changed_path
            ORDER BY path LIMIT %s
            """,
            (run_id, limit),
        ).fetchall()
    registered = connection.execute(
        "SELECT count(*)::integer FROM review_agent.review_run_files "
        "WHERE review_run_id = %s AND is_changed_path",
        (run_id,),
    ).fetchone()
    return FileIndexSummary(
        changed_files_reported=identity.changed_files_reported,
        changed_files_registered=int(registered[0]) if registered else 0,
        registration_complete=identity.registration_complete,
        by_domain=_counter(connection, run_id=run_id, column="domain"),
        by_review_mode=_counter(connection, run_id=run_id, column="review_mode"),
        by_change_status=_counter(connection, run_id=run_id, column="change_status"),
        sample_paths=tuple(_file(row) for row in rows),
    )


def lookup_run_file(
    connection: psycopg.Connection[TupleRow],
    *,
    run_id: ReviewRunId,
    repository: str,
    pr_number: int,
    path: str,
) -> RunFileLookup:
    _require_transaction(connection)
    identity = _run_identity(
        connection,
        run_id=run_id,
        repository=repository,
        pr_number=pr_number,
        active=True,
    )
    resolved_path = resolve_review_path(path)
    with connection.cursor(row_factory=class_row(_RunFileRow)) as cursor:
        row = cursor.execute(
            """
            SELECT id, path, change_status, previous_path, is_changed_path,
                   domain, review_mode, diff_state
            FROM review_agent.review_run_files
            WHERE review_run_id = %s AND path = %s
            """,
            (run_id, resolved_path),
        ).fetchone()
    return RunFileLookup(
        item=_file(row) if row is not None else None,
        registration_complete=identity.registration_complete,
    )


def list_run_files(
    connection: psycopg.Connection[TupleRow],
    *,
    run_id: ReviewRunId,
    repository: str,
    pr_number: int,
    limit: int,
    cursor: str = "",
    domain: str = "",
    review_mode: str = "",
    changed_only: bool = True,
) -> RunFilePage:
    _require_transaction(connection)
    _run_identity(
        connection,
        run_id=run_id,
        repository=repository,
        pr_number=pr_number,
        active=True,
    )
    if isinstance(limit, bool) or limit < 1 or limit > 200:
        raise CoverageError("limit must be between 1 and 200")
    cursor_path = resolve_review_path(cursor) if cursor else ""
    clauses = [sql.SQL("review_run_id = %s")]
    parameters: list[object] = [run_id]
    if changed_only:
        clauses.append(sql.SQL("is_changed_path"))
    if domain:
        clauses.append(sql.SQL("domain = %s"))
        parameters.append(domain)
    if review_mode:
        clauses.append(sql.SQL("review_mode = %s"))
        parameters.append(review_mode)
    base_where = sql.SQL(" AND ").join(clauses)
    total = connection.execute(
        sql.SQL(
            "SELECT count(*)::integer FROM review_agent.review_run_files WHERE "
        )
        + base_where,
        tuple(parameters),
    ).fetchone()
    page_clauses = list(clauses)
    page_parameters = list(parameters)
    if cursor_path:
        page_clauses.append(sql.SQL("path > %s"))
        page_parameters.append(cursor_path)
    page_parameters.append(limit + 1)
    with connection.cursor(row_factory=class_row(_RunFileRow)) as page_cursor:
        rows = page_cursor.execute(
            sql.SQL(
                "SELECT id, path, change_status, previous_path, is_changed_path, "
                "domain, review_mode, diff_state "
                "FROM review_agent.review_run_files WHERE "
            )
            + sql.SQL(" AND ").join(page_clauses)
            + sql.SQL(" ORDER BY path LIMIT %s"),
            tuple(page_parameters),
        ).fetchall()
    has_next = len(rows) > limit
    items = tuple(_file(row) for row in rows[:limit])
    return RunFilePage(
        run_id=run_id,
        repository=repository,
        pr_number=pr_number,
        limit=limit,
        next_cursor=items[-1].path if has_next and items else None,
        total_matching=int(total[0]) if total else 0,
        items=items,
    )


def insert_changed_files(
    connection: psycopg.Connection[TupleRow],
    *,
    run_id: ReviewRunId,
    files: tuple[ChangedFileDefinition, ...],
    changed_files_reported: int,
    registration_complete: bool,
) -> CoverageRegistration:
    """Insert one inventory batch and commit completeness only at exact count."""
    _require_transaction(connection)
    reported = resolve_changed_file_count(changed_files_reported)
    if len({item.path for item in files}) != len(files):
        raise CoverageConflict("changed-file batch contains duplicate paths")

    run = _run_for_write(connection, run_id, exclusive=True)
    if run.changed_files_reported not in {None, reported}:
        raise CoverageConflict("changed_files_reported conflicts with the review run")

    if files:
        connection.execute(
            """
            INSERT INTO review_agent.review_run_files (
                review_run_id, path, change_status, previous_path,
                is_changed_path, domain, review_mode, registered_at
            )
            SELECT %s, incoming.path, incoming.change_status,
                   incoming.previous_path, true, incoming.domain,
                   incoming.review_mode, statement_timestamp()
            FROM unnest(
                %s::text[], %s::text[], %s::text[], %s::text[], %s::text[]
            ) AS incoming(
                path, change_status, previous_path, domain, review_mode
            )
            ON CONFLICT (review_run_id, path) DO UPDATE SET
                change_status = EXCLUDED.change_status,
                previous_path = EXCLUDED.previous_path,
                is_changed_path = true,
                domain = EXCLUDED.domain,
                review_mode = EXCLUDED.review_mode
            WHERE NOT review_agent.review_run_files.is_changed_path
            """,
            (
                run_id,
                [item.path for item in files],
                [item.change_status for item in files],
                [item.previous_path for item in files],
                [item.domain.value for item in files],
                [item.review_mode.value for item in files],
            ),
        )

        with connection.cursor(row_factory=class_row(_RunFileRow)) as cursor:
            rows = cursor.execute(
                """
                SELECT id, path, change_status, previous_path, is_changed_path,
                       domain, review_mode, diff_state
                FROM review_agent.review_run_files
                WHERE review_run_id = %s AND path = ANY(%s::text[])
                """,
                (run_id, [item.path for item in files]),
            ).fetchall()
        stored = {row.path: row for row in rows}
        for item in files:
            row = stored.get(item.path)
            if row is None or (
                not row.is_changed_path
                or row.change_status != item.change_status
                or row.previous_path != item.previous_path
                or row.domain != item.domain.value
                or row.review_mode != item.review_mode.value
            ):
                raise CoverageConflict(
                    f"changed-file definition conflicts for {item.path}"
                )

    counted = connection.execute(
        "SELECT count(*) FROM review_agent.review_run_files "
        "WHERE review_run_id = %s AND is_changed_path",
        (run_id,),
    ).fetchone()
    registered = int(counted[0]) if counted is not None else 0
    if registered > reported:
        raise CoverageConflict("registered changed files exceed the reported count")
    if registration_complete and registered != reported:
        raise CoverageConflict(
            "complete registration requires the exact reported file count"
        )
    complete = run.changed_file_registration_complete or registration_complete
    connection.execute(
        """
        UPDATE review_agent.review_runs
        SET changed_files_reported = %s,
            changed_file_registration_complete = %s
        WHERE id = %s
        """,
        (reported, complete, run_id),
    )
    return CoverageRegistration(
        changed_files_reported=reported,
        changed_files_registered=registered,
        registration_complete=complete,
    )


def insert_file_reads(
    connection: psycopg.Connection[TupleRow],
    *,
    run_id: ReviewRunId,
    reads: tuple[FileReadDefinition, ...],
) -> FileReadBatch:
    """Insert normalized source-read ranges without changing diff state."""
    _require_transaction(connection)
    _run_for_write(connection, run_id, exclusive=False)
    if not reads:
        return FileReadBatch(submitted=0, inserted=0)

    paths = sorted({item.path for item in reads})
    connection.execute(
        """
        INSERT INTO review_agent.review_run_files (
            review_run_id, path, is_changed_path, registered_at
        )
        SELECT %s, incoming.path, false, statement_timestamp()
        FROM unnest(%s::text[]) AS incoming(path)
        ON CONFLICT (review_run_id, path) DO NOTHING
        """,
        (run_id, paths),
    )
    with connection.cursor(row_factory=class_row(_FileIdRow)) as cursor:
        file_rows = cursor.execute(
            """
            SELECT id, path
            FROM review_agent.review_run_files
            WHERE review_run_id = %s AND path = ANY(%s::text[])
            """,
            (run_id, paths),
        ).fetchall()
    file_ids = {row.path: row.id for row in file_rows}
    if len(file_ids) != len(paths):
        raise CoverageConflict("source-read paths could not be registered")

    inserted = connection.execute(
        """
        INSERT INTO review_agent.review_file_reads (
            review_run_file_id, side, start_line, end_line, recorded_at
        )
        SELECT incoming.review_run_file_id, incoming.side,
               incoming.start_line, incoming.end_line, statement_timestamp()
        FROM unnest(
            %s::bigint[], %s::text[], %s::integer[], %s::integer[]
        ) AS incoming(review_run_file_id, side, start_line, end_line)
        ON CONFLICT (review_run_file_id, side, start_line, end_line) DO NOTHING
        RETURNING id
        """,
        (
            [file_ids[item.path] for item in reads],
            [item.side.value for item in reads],
            [item.start_line for item in reads],
            [item.end_line for item in reads],
        ),
    ).fetchall()
    return FileReadBatch(submitted=len(reads), inserted=len(inserted))


def record_diff_observation(
    connection: psycopg.Connection[TupleRow],
    *,
    run_id: ReviewRunId,
    observation: DiffObservation,
) -> int:
    """Record one batch diff outcome for registered changed paths."""
    _require_transaction(connection)
    _run_for_write(connection, run_id, exclusive=False)
    if not observation.paths:
        return 0

    with connection.cursor(row_factory=class_row(_RunFileRow)) as cursor:
        rows = cursor.execute(
            """
            SELECT id, path, change_status, previous_path, is_changed_path,
                   domain, review_mode, diff_state
            FROM review_agent.review_run_files
            WHERE review_run_id = %s AND path = ANY(%s::text[])
            FOR UPDATE
            """,
            (run_id, list(observation.paths)),
        ).fetchall()
    stored = {row.path: row for row in rows}
    for path in observation.paths:
        row = stored.get(path)
        if row is None or not row.is_changed_path:
            raise CoverageFileNotFound(
                f"diff observation path is not registered as changed: {path}"
            )
        if (
            row.diff_state == DiffState.COMPLETE.value
            and observation.state is not DiffState.COMPLETE
        ):
            raise InvalidCoverageTransition(
                f"complete diff coverage cannot regress for {path}"
            )

    updated = connection.execute(
        """
        UPDATE review_agent.review_run_files
        SET diff_state = %s,
            unavailable_reason = %s,
            diff_observed_at = statement_timestamp()
        WHERE review_run_id = %s AND path = ANY(%s::text[])
          AND is_changed_path
        RETURNING id
        """,
        (
            observation.state.value,
            (
                observation.unavailable_reason
                if observation.state is DiffState.UNAVAILABLE
                else None
            ),
            run_id,
            list(observation.paths),
        ),
    ).fetchall()
    return len(updated)


def summarize(
    connection: psycopg.Connection[TupleRow], run_id: ReviewRunId
) -> CoverageSummary:
    """Summarize normalized coverage without treating source reads as diff."""
    _require_transaction(connection)
    with connection.cursor(row_factory=class_row(_SummaryRow)) as cursor:
        row = cursor.execute(
            """
            SELECT
                run.changed_files_reported,
                run.changed_file_registration_complete AS registration_complete,
                count(DISTINCT file.id) FILTER (
                    WHERE file.is_changed_path
                )::integer AS changed_files_registered,
                count(DISTINCT file.id) FILTER (
                    WHERE file.is_changed_path AND file.diff_state = 'complete'
                )::integer AS changed_paths_with_complete_diff,
                count(DISTINCT file.id) FILTER (
                    WHERE file.is_changed_path AND read.id IS NOT NULL
                )::integer AS changed_paths_with_source_reads,
                count(DISTINCT file.id) FILTER (
                    WHERE NOT file.is_changed_path AND read.id IS NOT NULL
                )::integer AS supporting_context_paths_read,
                count(read.id)::integer AS context_ranges_read,
                count(DISTINCT file.id) FILTER (
                    WHERE file.is_changed_path AND file.diff_state = 'unavailable'
                )::integer AS unavailable_paths,
                count(DISTINCT file.id) FILTER (
                    WHERE file.is_changed_path AND file.diff_state = 'truncated'
                )::integer AS truncated_paths
            FROM review_agent.review_runs AS run
            LEFT JOIN review_agent.review_run_files AS file
                ON file.review_run_id = run.id
            LEFT JOIN review_agent.review_file_reads AS read
                ON read.review_run_file_id = file.id
            WHERE run.id = %s
            GROUP BY run.id
            """,
            (run_id,),
        ).fetchone()
    if row is None:
        raise CoverageError("review run does not exist")
    state = (
        CoverageState.UNKNOWN
        if row.changed_files_reported is None
        else CoverageState.COMPLETE
        if (
            row.registration_complete
            and row.changed_files_reported == row.changed_files_registered
            and row.changed_paths_with_complete_diff
            == row.changed_files_registered
        )
        else CoverageState.INCOMPLETE
    )
    return CoverageSummary(
        state=state,
        changed_files_reported=row.changed_files_reported,
        changed_files_registered=row.changed_files_registered,
        registration_complete=row.registration_complete,
        changed_paths_with_complete_diff=row.changed_paths_with_complete_diff,
        changed_paths_with_source_reads=row.changed_paths_with_source_reads,
        supporting_context_paths_read=row.supporting_context_paths_read,
        context_ranges_read=row.context_ranges_read,
        unavailable_paths=row.unavailable_paths,
        truncated_paths=row.truncated_paths,
    )
