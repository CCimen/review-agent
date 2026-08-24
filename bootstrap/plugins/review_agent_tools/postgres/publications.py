"""PostgreSQL publication plan and delivery-state operations."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime

import psycopg
from psycopg import errors
from psycopg.pq import TransactionStatus
from psycopg.rows import TupleRow, class_row
from psycopg.types.json import Jsonb

from ..domain.publication import (
    CanonicalPayload,
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
)
from ..domain.review import PullRequestId, ReviewRunId


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
                    WHEN 'summary' THEN 0
                    WHEN 'continuation' THEN 1
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
    rendered_blocks_json = _canonical_json(row.rendered_blocks)
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
                   publication.status, publication.posting_started_at
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


def claim_publication(
    connection: psycopg.Connection[TupleRow], publication_id: PublicationId
) -> PublicationClaim:
    """Claim generated delivery once; posting retries reuse the same recovery state."""
    _require_transaction(connection)
    row = _publication_row(connection, publication_id, for_update=True)
    if row is None:
        raise PublicationNotFound("publication does not exist")
    if row.status in {
        PublicationStatus.GENERATED.value,
        PublicationStatus.PUBLISH_FAILED.value,
    }:
        connection.execute(
            """
            UPDATE review_agent.publications
            SET status = 'posting', posting_started_at = statement_timestamp(),
                publish_failed_at = NULL, failure_code = NULL
            WHERE id = %s AND status IN ('generated', 'publish_failed')
            """,
            (publication_id,),
        )
        connection.execute(
            """
            UPDATE review_agent.publication_parts
            SET status = 'posting', posting_started_at = statement_timestamp(),
                failure_at = NULL, failure_code = NULL
            WHERE publication_id = %s
              AND status IN ('pending', 'publish_failed')
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


def acknowledge_part(
    connection: psycopg.Connection[TupleRow],
    *,
    publication_id: PublicationId,
    part_type: PublicationPartType,
    part_number: int,
    external_id: int,
    posting_started_at: datetime,
) -> StoredPublicationPart:
    """Persist one provider ID after a successful write or marker recovery."""
    _require_transaction(connection)
    if isinstance(external_id, bool) or external_id < 1:
        raise PublicationStoreError("external_id must be positive")
    row = _publication_row(connection, publication_id, for_update=True)
    if row is None:
        raise PublicationNotFound("publication does not exist")
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
) -> StoredPublication:
    """Mark delivery posted only after every exact part has an external ID."""
    _require_transaction(connection)
    row = _publication_row(connection, publication_id, for_update=True)
    if row is None:
        raise PublicationNotFound("publication does not exist")
    if row.status == PublicationStatus.POSTED.value:
        return _stored(connection, row)
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
            SET status = 'posted', posted_at = statement_timestamp()
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
) -> StoredPublication:
    """Terminalize the posting generation and all of its unfinished parts."""
    _require_transaction(connection)
    code = failure_code.strip()
    if not code or len(code) > 80:
        raise PublicationStoreError("failure_code must contain at most 80 characters")
    row = _publication_row(connection, publication_id, for_update=True)
    if row is None:
        raise PublicationNotFound("publication does not exist")
    if (
        row.status != PublicationStatus.POSTING.value
        or row.posting_started_at != posting_started_at
    ):
        raise InvalidPublicationTransition("publication is not in this posting generation")
    status = PublicationStatus.STALE if stale else PublicationStatus.PUBLISH_FAILED
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
            failure_code = %s
        WHERE id = %s AND status = 'posting'
        """,
        (status.value, code, publication_id),
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
) -> StoredPublication:
    """Atomically freeze one exact plan and advance its run to publishing."""
    _require_transaction(connection)
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
                rendered_blocks, rendered_hash, status, generated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'generated',
                      statement_timestamp())
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
