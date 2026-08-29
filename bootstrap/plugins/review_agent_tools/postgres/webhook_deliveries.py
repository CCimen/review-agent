"""Durable PostgreSQL intake and fenced processing for GitHub webhooks.

The next processor slice consumes the lease interface defined here. A delivery
GUID makes transport replay idempotent; a redelivery with a new GUID is a new
delivery and downstream effects must keep their own business identity.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
import json
import re
from typing import TypeAlias, cast
from uuid import UUID

import psycopg
from psycopg.pq import TransactionStatus
from psycopg.rows import TupleRow, class_row
from psycopg.types.json import Jsonb

from ..domain.review import JsonObject, JsonValue


class WebhookDeliveryError(ValueError):
    """A webhook-delivery operation violates its durable contract."""


class NormalizedPayloadTooLarge(WebhookDeliveryError):
    """The resumable envelope exceeds its durable storage/privacy guard."""


class DeliveryNotFound(WebhookDeliveryError):
    """The requested webhook delivery does not exist."""


class DeliveryConflict(WebhookDeliveryError):
    """A delivery GUID was reused for a different transport payload."""

    current_delivery: "WebhookDelivery"

    def __init__(self, current_delivery: "WebhookDelivery") -> None:
        super().__init__("delivery GUID conflicts with the stored event or payload")
        self.current_delivery = current_delivery


class DeliveryLeaseLost(WebhookDeliveryError):
    """A processor no longer owns the exact live delivery generation."""

    current_delivery: "WebhookDelivery"

    def __init__(self, current_delivery: "WebhookDelivery") -> None:
        super().__init__("webhook delivery lease is no longer current")
        self.current_delivery = current_delivery


class CommandCategory(StrEnum):
    REVIEW = "review"
    FEEDBACK = "feedback"
    INSTALLATION = "installation"
    REPOSITORY_ACCESS = "repository_access"
    IGNORED = "ignored"


class DeliveryStatus(StrEnum):
    RECEIVED = "received"
    PROCESSING = "processing"
    ACCEPTED = "accepted"
    IGNORED = "ignored"
    REJECTED = "rejected"
    FAILED = "failed"


class TerminalStatus(StrEnum):
    ACCEPTED = "accepted"
    IGNORED = "ignored"
    REJECTED = "rejected"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class DeliveryDefinition:
    delivery_guid: str
    event: str
    action: str | None
    payload_sha256: str
    provider_installation_id: int | None
    provider_repository_id: int | None
    repository_full_name: str | None
    command_category: CommandCategory
    normalized_schema_version: int
    normalized_payload: JsonObject


@dataclass(frozen=True, slots=True)
class WebhookDelivery:
    id: int
    delivery_guid: str
    event: str
    action: str | None
    payload_sha256: str
    provider_installation_id: int | None
    provider_repository_id: int | None
    repository_full_name: str | None
    command_category: CommandCategory
    normalized_schema_version: int
    normalized_payload: dict[str, JsonValue] | None
    status: DeliveryStatus
    attempt_count: int
    max_attempts: int
    available_at: datetime
    lease_owner: str | None
    lease_generation: int
    lease_expires_at: datetime | None
    last_heartbeat_at: datetime | None
    failure_code: str | None
    failure_actor: str | None
    completed_by: str | None
    received_at: datetime
    started_at: datetime | None
    processed_at: datetime | None


@dataclass(frozen=True, slots=True)
class _DeliveryRow:
    id: int
    delivery_guid: str
    event: str
    action: str | None
    payload_sha256: str
    provider_installation_id: int | None
    provider_repository_id: int | None
    repository_full_name: str | None
    command_category: str
    normalized_schema_version: int
    normalized_payload: object | None
    status: str
    attempt_count: int
    max_attempts: int
    available_at: datetime
    lease_owner: str | None
    lease_generation: int
    lease_expires_at: datetime | None
    last_heartbeat_at: datetime | None
    failure_code: str | None
    failure_actor: str | None
    completed_by: str | None
    received_at: datetime
    started_at: datetime | None
    processed_at: datetime | None


@dataclass(frozen=True, slots=True)
class RegisteredDelivery:
    delivery: WebhookDelivery


@dataclass(frozen=True, slots=True)
class DuplicateDelivery:
    delivery: WebhookDelivery


DeliveryRegistration: TypeAlias = RegisteredDelivery | DuplicateDelivery


_EVENT_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_FAILURE_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_DELIVERY_COLUMNS = """
    id, delivery_guid::text AS delivery_guid, event_name AS event, action,
    payload_sha256, provider_installation_id, provider_repository_id,
    repository_full_name, command_category, normalized_schema_version,
    normalized_payload, status, attempt_count, max_attempts, available_at,
    lease_owner, lease_generation, lease_expires_at, last_heartbeat_at,
    failure_code, failure_actor, completed_by, received_at, started_at,
    processed_at
"""
_QUALIFIED_DELIVERY_COLUMNS = """
    delivery.id, delivery.delivery_guid::text AS delivery_guid,
    delivery.event_name AS event, delivery.action, delivery.payload_sha256,
    delivery.provider_installation_id, delivery.provider_repository_id,
    delivery.repository_full_name, delivery.command_category,
    delivery.normalized_schema_version, delivery.normalized_payload,
    delivery.status, delivery.attempt_count, delivery.max_attempts,
    delivery.available_at, delivery.lease_owner, delivery.lease_generation,
    delivery.lease_expires_at, delivery.last_heartbeat_at,
    delivery.failure_code, delivery.failure_actor, delivery.completed_by,
    delivery.received_at, delivery.started_at, delivery.processed_at
"""
# The normalized envelope must be enough to resume large repository-access
# events but must never become a raw webhook archive. This independent 1 MiB
# storage/privacy ceiling is not an ingress or review-size throughput cap.
NORMALIZED_PAYLOAD_MAX_BYTES = 1_048_576


def _require_transaction(connection: psycopg.Connection[TupleRow]) -> None:
    if connection.info.transaction_status != TransactionStatus.INTRANS:
        raise WebhookDeliveryError(
            "webhook-delivery operations require an active transaction"
        )


def _integer(value: object, *, field: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise WebhookDeliveryError(f"{field} must be at least {minimum}")
    return value


def _optional_positive(value: int | None, *, field: str) -> int | None:
    if value is None:
        return None
    return _integer(value, field=field, minimum=1)


def _bounded_text(
    value: object,
    *,
    field: str,
    maximum: int,
    pattern: re.Pattern[str] | None = None,
) -> str:
    if not isinstance(value, str):
        raise WebhookDeliveryError(f"{field} must be text")
    normalized = value.strip()
    if (
        not normalized
        or "\x00" in normalized
        or len(normalized) > maximum
        or (pattern is not None and pattern.fullmatch(normalized) is None)
    ):
        raise WebhookDeliveryError(f"{field} is invalid")
    return normalized


def _optional_text(
    value: str | None,
    *,
    field: str,
    maximum: int,
    pattern: re.Pattern[str] | None = None,
) -> str | None:
    if value is None:
        return None
    return _bounded_text(value, field=field, maximum=maximum, pattern=pattern)


def _actor(value: str, *, field: str) -> str:
    return _bounded_text(value, field=field, maximum=120)


def _failure_code(value: str) -> str:
    return _bounded_text(
        value,
        field="failure_code",
        maximum=64,
        pattern=_FAILURE_CODE_RE,
    )


def _normalized_payload(value: JsonObject) -> dict[str, JsonValue]:
    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
        decoded = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise WebhookDeliveryError(
            "normalized_payload must contain finite JSON values"
        ) from exc
    if not isinstance(decoded, dict):
        raise WebhookDeliveryError("normalized_payload must be an object")
    if len(encoded) > NORMALIZED_PAYLOAD_MAX_BYTES:
        raise NormalizedPayloadTooLarge(
            "normalized_payload exceeds the storage guard"
        )
    return cast(dict[str, JsonValue], decoded)


def _definition(
    definition: DeliveryDefinition,
) -> tuple[
    str,
    str,
    str | None,
    str,
    int | None,
    int | None,
    str | None,
    CommandCategory,
    int,
    dict[str, JsonValue],
]:
    try:
        delivery_guid = str(UUID(definition.delivery_guid.strip()))
    except (AttributeError, ValueError) as exc:
        raise WebhookDeliveryError("delivery_guid must be a UUID") from exc
    event = _bounded_text(
        definition.event.lower(), field="event", maximum=64, pattern=_EVENT_RE
    )
    action = _optional_text(
        definition.action.lower() if definition.action is not None else None,
        field="action",
        maximum=64,
        pattern=_EVENT_RE,
    )
    digest = _bounded_text(
        definition.payload_sha256,
        field="payload_sha256",
        maximum=64,
        pattern=_DIGEST_RE,
    )
    installation_id = _optional_positive(
        definition.provider_installation_id, field="provider_installation_id"
    )
    repository_id = _optional_positive(
        definition.provider_repository_id, field="provider_repository_id"
    )
    repository_label = _optional_text(
        definition.repository_full_name,
        field="repository_full_name",
        maximum=255,
    )
    schema_version = _integer(
        definition.normalized_schema_version,
        field="normalized_schema_version",
        minimum=1,
    )
    payload = _normalized_payload(definition.normalized_payload)
    return (
        delivery_guid,
        event,
        action,
        digest,
        installation_id,
        repository_id,
        repository_label,
        definition.command_category,
        schema_version,
        payload,
    )


def _delivery(row: _DeliveryRow) -> WebhookDelivery:
    try:
        category = CommandCategory(row.command_category)
        status = DeliveryStatus(row.status)
    except ValueError as exc:
        raise WebhookDeliveryError(
            "stored webhook delivery has an unknown enum value"
        ) from exc
    payload = row.normalized_payload
    if payload is not None and not isinstance(payload, dict):
        raise WebhookDeliveryError(
            "stored webhook delivery has an invalid normalized payload"
        )
    return WebhookDelivery(
        id=row.id,
        delivery_guid=row.delivery_guid,
        event=row.event,
        action=row.action,
        payload_sha256=row.payload_sha256,
        provider_installation_id=row.provider_installation_id,
        provider_repository_id=row.provider_repository_id,
        repository_full_name=row.repository_full_name,
        command_category=category,
        normalized_schema_version=row.normalized_schema_version,
        normalized_payload=cast(dict[str, JsonValue] | None, payload),
        status=status,
        attempt_count=row.attempt_count,
        max_attempts=row.max_attempts,
        available_at=row.available_at,
        lease_owner=row.lease_owner,
        lease_generation=row.lease_generation,
        lease_expires_at=row.lease_expires_at,
        last_heartbeat_at=row.last_heartbeat_at,
        failure_code=row.failure_code,
        failure_actor=row.failure_actor,
        completed_by=row.completed_by,
        received_at=row.received_at,
        started_at=row.started_at,
        processed_at=row.processed_at,
    )


def _by_guid(
    connection: psycopg.Connection[TupleRow], delivery_guid: str
) -> WebhookDelivery | None:
    with connection.cursor(row_factory=class_row(_DeliveryRow)) as cursor:
        row = cursor.execute(
            f"""
            SELECT {_DELIVERY_COLUMNS}
            FROM review_agent.github_webhook_deliveries
            WHERE delivery_guid = %s::uuid
            """,
            (delivery_guid,),
        ).fetchone()
    return _delivery(row) if row is not None else None


def get_delivery(
    connection: psycopg.Connection[TupleRow], delivery_id: int
) -> WebhookDelivery:
    """Return one delivery at any durable lifecycle state."""
    _require_transaction(connection)
    resolved_id = _integer(delivery_id, field="delivery_id", minimum=1)
    with connection.cursor(row_factory=class_row(_DeliveryRow)) as cursor:
        row = cursor.execute(
            f"""
            SELECT {_DELIVERY_COLUMNS}
            FROM review_agent.github_webhook_deliveries
            WHERE id = %s
            """,
            (resolved_id,),
        ).fetchone()
    if row is None:
        raise DeliveryNotFound("webhook delivery does not exist")
    return _delivery(row)


def require_live_delivery(
    connection: psycopg.Connection[TupleRow],
    *,
    delivery_id: int,
    lease_owner: str,
    lease_generation: int,
) -> WebhookDelivery:
    """Return one exact unexpired processing lease or fail closed."""
    _require_transaction(connection)
    resolved_id = _integer(delivery_id, field="delivery_id", minimum=1)
    owner = _actor(lease_owner, field="lease_owner")
    generation = _integer(lease_generation, field="lease_generation", minimum=1)
    with connection.cursor(row_factory=class_row(_DeliveryRow)) as cursor:
        row = cursor.execute(
            f"""
            SELECT {_DELIVERY_COLUMNS}
            FROM review_agent.github_webhook_deliveries
            WHERE id = %s
              AND status = 'processing'
              AND lease_owner = %s
              AND lease_generation = %s
              AND lease_expires_at > statement_timestamp()
            FOR SHARE
            """,
            (resolved_id, owner, generation),
        ).fetchone()
    if row is None:
        raise DeliveryLeaseLost(get_delivery(connection, resolved_id))
    return _delivery(row)


def register_delivery(
    connection: psycopg.Connection[TupleRow],
    *,
    definition: DeliveryDefinition,
    max_attempts: int,
) -> DeliveryRegistration:
    """Persist one normalized delivery or resolve an immutable redelivery."""
    _require_transaction(connection)
    (
        delivery_guid,
        event,
        action,
        payload_sha256,
        installation_id,
        repository_id,
        repository_label,
        category,
        schema_version,
        payload,
    ) = _definition(definition)
    attempt_limit = _integer(max_attempts, field="max_attempts", minimum=1)
    with connection.cursor(row_factory=class_row(_DeliveryRow)) as cursor:
        row = cursor.execute(
            f"""
            INSERT INTO review_agent.github_webhook_deliveries (
                delivery_guid, event_name, action, payload_sha256,
                provider_installation_id, provider_repository_id,
                repository_full_name, command_category,
                normalized_schema_version, normalized_payload, status,
                attempt_count, max_attempts, available_at, lease_generation,
                received_at
            ) VALUES (
                %s::uuid, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                'received', 0, %s, statement_timestamp(), 0,
                statement_timestamp()
            )
            ON CONFLICT (delivery_guid) DO NOTHING
            RETURNING {_DELIVERY_COLUMNS}
            """,
            (
                delivery_guid,
                event,
                action,
                payload_sha256,
                installation_id,
                repository_id,
                repository_label,
                category.value,
                schema_version,
                Jsonb(payload),
                attempt_limit,
            ),
        ).fetchone()
    if row is not None:
        return RegisteredDelivery(_delivery(row))

    current = _by_guid(connection, delivery_guid)
    if current is None:
        raise WebhookDeliveryError(
            "webhook delivery could not be resolved after registration"
        )
    if current.event != event or current.payload_sha256 != payload_sha256:
        raise DeliveryConflict(current)
    return DuplicateDelivery(current)


def claim_next_delivery(
    connection: psycopg.Connection[TupleRow],
    *,
    lease_owner: str,
    lease_duration: timedelta,
) -> WebhookDelivery | None:
    """Claim one ready delivery with a single fenced ``SKIP LOCKED`` update."""
    _require_transaction(connection)
    owner = _actor(lease_owner, field="lease_owner")
    if lease_duration <= timedelta(0):
        raise WebhookDeliveryError("lease_duration must be positive")
    with connection.cursor(row_factory=class_row(_DeliveryRow)) as cursor:
        row = cursor.execute(
            f"""
            WITH candidate AS MATERIALIZED (
                SELECT id
                FROM review_agent.github_webhook_deliveries
                WHERE status = 'received'
                  AND available_at <= statement_timestamp()
                  AND attempt_count < max_attempts
                ORDER BY available_at, id
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            UPDATE review_agent.github_webhook_deliveries AS delivery
            SET status = 'processing',
                attempt_count = delivery.attempt_count + 1,
                lease_owner = %s,
                lease_generation = delivery.lease_generation + 1,
                lease_expires_at = statement_timestamp() + %s,
                last_heartbeat_at = statement_timestamp(),
                failure_code = NULL,
                failure_actor = NULL,
                started_at = COALESCE(delivery.started_at, statement_timestamp())
            FROM candidate
            WHERE delivery.id = candidate.id
              AND delivery.status = 'received'
            RETURNING {_QUALIFIED_DELIVERY_COLUMNS}
            """,
            (owner, lease_duration),
        ).fetchone()
    return _delivery(row) if row is not None else None


def heartbeat_delivery(
    connection: psycopg.Connection[TupleRow],
    *,
    delivery_id: int,
    lease_owner: str,
    lease_generation: int,
    lease_duration: timedelta,
) -> WebhookDelivery:
    """Extend one exact unexpired processing lease."""
    _require_transaction(connection)
    resolved_id = _integer(delivery_id, field="delivery_id", minimum=1)
    owner = _actor(lease_owner, field="lease_owner")
    generation = _integer(lease_generation, field="lease_generation", minimum=1)
    if lease_duration <= timedelta(0):
        raise WebhookDeliveryError("lease_duration must be positive")
    with connection.cursor(row_factory=class_row(_DeliveryRow)) as cursor:
        row = cursor.execute(
            f"""
            UPDATE review_agent.github_webhook_deliveries
            SET lease_expires_at = statement_timestamp() + %s,
                last_heartbeat_at = statement_timestamp()
            WHERE id = %s
              AND status = 'processing'
              AND lease_owner = %s
              AND lease_generation = %s
              AND lease_expires_at > statement_timestamp()
            RETURNING {_DELIVERY_COLUMNS}
            """,
            (lease_duration, resolved_id, owner, generation),
        ).fetchone()
    if row is None:
        raise DeliveryLeaseLost(get_delivery(connection, resolved_id))
    return _delivery(row)


def finish_delivery(
    connection: psycopg.Connection[TupleRow],
    *,
    delivery_id: int,
    lease_owner: str,
    lease_generation: int,
    status: TerminalStatus,
    actor: str,
    failure_code: str | None = None,
) -> WebhookDelivery:
    """Finish one exact lease and erase its normalized processing payload."""
    _require_transaction(connection)
    resolved_id = _integer(delivery_id, field="delivery_id", minimum=1)
    owner = _actor(lease_owner, field="lease_owner")
    generation = _integer(lease_generation, field="lease_generation", minimum=1)
    completed_by = _actor(actor, field="actor")
    if status is TerminalStatus.ACCEPTED:
        if failure_code is not None:
            raise WebhookDeliveryError("accepted delivery cannot have a failure_code")
        code = None
    else:
        if failure_code is None:
            raise WebhookDeliveryError(
                "non-accepted deliveries require a failure_code"
            )
        code = _failure_code(failure_code)

    with connection.cursor(row_factory=class_row(_DeliveryRow)) as cursor:
        row = cursor.execute(
            f"""
            UPDATE review_agent.github_webhook_deliveries
            SET status = %s,
                normalized_payload = NULL,
                lease_owner = NULL,
                lease_expires_at = NULL,
                last_heartbeat_at = NULL,
                failure_code = %s,
                failure_actor = %s,
                completed_by = %s,
                processed_at = statement_timestamp()
            WHERE id = %s
              AND status = 'processing'
              AND lease_owner = %s
              AND lease_generation = %s
              AND lease_expires_at > statement_timestamp()
            RETURNING {_DELIVERY_COLUMNS}
            """,
            (
                status.value,
                code,
                completed_by if code is not None else None,
                completed_by,
                resolved_id,
                owner,
                generation,
            ),
        ).fetchone()
    if row is None:
        raise DeliveryLeaseLost(get_delivery(connection, resolved_id))
    return _delivery(row)


def retry_or_fail_delivery(
    connection: psycopg.Connection[TupleRow],
    *,
    delivery_id: int,
    lease_owner: str,
    lease_generation: int,
    actor: str,
    failure_code: str,
    retry_delay: timedelta,
) -> WebhookDelivery:
    """Retry one exact lease or fail it after its caller-set attempt budget."""
    _require_transaction(connection)
    resolved_id = _integer(delivery_id, field="delivery_id", minimum=1)
    owner = _actor(lease_owner, field="lease_owner")
    generation = _integer(lease_generation, field="lease_generation", minimum=1)
    failure_actor = _actor(actor, field="actor")
    code = _failure_code(failure_code)
    if retry_delay < timedelta(0):
        raise WebhookDeliveryError("retry_delay must not be negative")

    with connection.cursor(row_factory=class_row(_DeliveryRow)) as cursor:
        row = cursor.execute(
            f"""
            UPDATE review_agent.github_webhook_deliveries AS delivery
            SET status = CASE
                    WHEN delivery.attempt_count < delivery.max_attempts
                    THEN 'received'
                    ELSE 'failed'
                END,
                normalized_payload = CASE
                    WHEN delivery.attempt_count < delivery.max_attempts
                    THEN delivery.normalized_payload
                    ELSE NULL
                END,
                available_at = CASE
                    WHEN delivery.attempt_count < delivery.max_attempts
                    THEN statement_timestamp() + %s
                    ELSE delivery.available_at
                END,
                lease_owner = NULL,
                lease_expires_at = NULL,
                last_heartbeat_at = NULL,
                failure_code = %s,
                failure_actor = %s,
                completed_by = CASE
                    WHEN delivery.attempt_count < delivery.max_attempts
                    THEN NULL
                    ELSE %s
                END,
                processed_at = CASE
                    WHEN delivery.attempt_count < delivery.max_attempts
                    THEN NULL
                    ELSE statement_timestamp()
                END
            WHERE delivery.id = %s
              AND delivery.status = 'processing'
              AND delivery.lease_owner = %s
              AND delivery.lease_generation = %s
              AND delivery.lease_expires_at > statement_timestamp()
            RETURNING {_DELIVERY_COLUMNS}
            """,
            (
                retry_delay,
                code,
                failure_actor,
                failure_actor,
                resolved_id,
                owner,
                generation,
            ),
        ).fetchone()
    if row is None:
        raise DeliveryLeaseLost(get_delivery(connection, resolved_id))
    return _delivery(row)


def recover_expired_deliveries(
    connection: psycopg.Connection[TupleRow],
    *,
    limit: int,
    actor: str,
) -> tuple[WebhookDelivery, ...]:
    """Recover a bounded batch of expired leases without blocking live work."""
    _require_transaction(connection)
    row_limit = _integer(limit, field="limit", minimum=1)
    recovery_actor = _actor(actor, field="actor")
    with connection.cursor(row_factory=class_row(_DeliveryRow)) as cursor:
        rows = cursor.execute(
            f"""
            WITH candidate AS MATERIALIZED (
                SELECT id
                FROM review_agent.github_webhook_deliveries
                WHERE status = 'processing'
                  AND lease_expires_at <= statement_timestamp()
                ORDER BY lease_expires_at, id
                FOR UPDATE SKIP LOCKED
                LIMIT %s
            )
            UPDATE review_agent.github_webhook_deliveries AS delivery
            SET status = CASE
                    WHEN delivery.attempt_count < delivery.max_attempts
                    THEN 'received'
                    ELSE 'failed'
                END,
                normalized_payload = CASE
                    WHEN delivery.attempt_count < delivery.max_attempts
                    THEN delivery.normalized_payload
                    ELSE NULL
                END,
                available_at = CASE
                    WHEN delivery.attempt_count < delivery.max_attempts
                    THEN statement_timestamp()
                    ELSE delivery.available_at
                END,
                lease_owner = NULL,
                lease_expires_at = NULL,
                last_heartbeat_at = NULL,
                failure_code = 'processing_lease_expired',
                failure_actor = %s,
                completed_by = CASE
                    WHEN delivery.attempt_count < delivery.max_attempts
                    THEN NULL
                    ELSE %s
                END,
                processed_at = CASE
                    WHEN delivery.attempt_count < delivery.max_attempts
                    THEN NULL
                    ELSE statement_timestamp()
                END
            FROM candidate
            WHERE delivery.id = candidate.id
              AND delivery.status = 'processing'
              AND delivery.lease_expires_at <= statement_timestamp()
            RETURNING {_QUALIFIED_DELIVERY_COLUMNS}
            """,
            (row_limit, recovery_actor, recovery_actor),
        ).fetchall()
    return tuple(sorted((_delivery(row) for row in rows), key=lambda item: item.id))
