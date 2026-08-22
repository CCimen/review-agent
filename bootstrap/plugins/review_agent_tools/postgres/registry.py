"""PostgreSQL repository, pull-request, and exact-subject operations."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

import psycopg
from psycopg import errors
from psycopg.pq import TransactionStatus
from psycopg.rows import TupleRow, class_row

from ..domain.review import (
    PullRequestId,
    RepositoryId,
    ReviewDomainError,
    ReviewSubjectDefinition,
    ReviewSubjectId,
    decode_resolved_config,
)


_PROVIDER_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
_REPOSITORY_PART_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


class RegistryError(ValueError):
    """A repository-registry operation violates its durable contract."""


class RepositoryNameConflict(RegistryError):
    """A provider full name already belongs to another repository identity."""


class SubjectConflict(RegistryError):
    """A stored exact subject disagrees with its canonical configuration hash."""


@dataclass(frozen=True, slots=True)
class RepositoryDefinition:
    provider: str
    provider_repository_id: int
    full_name: str


@dataclass(frozen=True, slots=True)
class Repository:
    id: RepositoryId
    provider: str
    provider_repository_id: int
    owner: str
    name: str
    full_name: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class PullRequest:
    id: PullRequestId
    repository_id: RepositoryId
    number: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ReviewSubject:
    id: ReviewSubjectId
    pull_request_id: PullRequestId
    definition: ReviewSubjectDefinition
    created_at: datetime


@dataclass(frozen=True, slots=True)
class _ReviewSubjectRow:
    id: ReviewSubjectId
    pull_request_id: PullRequestId
    base_sha: str
    head_sha: str
    policy_revision: str
    resolved_config_schema_version: int
    resolved_config_json: str
    resolved_config_hash: str
    created_at: datetime


def _require_transaction(connection: psycopg.Connection[TupleRow]) -> None:
    if connection.info.transaction_status != TransactionStatus.INTRANS:
        raise RegistryError("registry operations require an active transaction")


def _repository_parts(
    definition: RepositoryDefinition,
) -> tuple[str, int, str, str, str]:
    provider = definition.provider.strip().lower()
    if not _PROVIDER_RE.fullmatch(provider):
        raise RegistryError("provider must be a stable lower-case identifier")
    provider_repository_id = definition.provider_repository_id
    if isinstance(provider_repository_id, bool) or provider_repository_id < 1:
        raise RegistryError("provider_repository_id must be positive")
    full_name = definition.full_name.strip()
    if full_name.count("/") != 1:
        raise RegistryError("full_name must be owner/name")
    owner, name = full_name.split("/", maxsplit=1)
    if not _REPOSITORY_PART_RE.fullmatch(owner) or not _REPOSITORY_PART_RE.fullmatch(
        name
    ):
        raise RegistryError("full_name must be owner/name")
    return provider, provider_repository_id, owner, name, full_name


def resolve_repository(definition: RepositoryDefinition) -> RepositoryDefinition:
    """Validate and normalize provider identity before a transaction opens."""
    provider, provider_repository_id, owner, name, _ = _repository_parts(definition)
    return RepositoryDefinition(
        provider=provider,
        provider_repository_id=provider_repository_id,
        full_name=f"{owner}/{name}",
    )


def ensure_repository(
    connection: psycopg.Connection[TupleRow], definition: RepositoryDefinition
) -> Repository:
    """Create or rename one repository by stable provider identity."""
    _require_transaction(connection)
    normalized = resolve_repository(definition)
    provider, provider_repository_id, owner, name, full_name = _repository_parts(
        normalized
    )
    try:
        with connection.transaction():
            with connection.cursor(row_factory=class_row(Repository)) as cursor:
                repository = cursor.execute(
                    """
                    INSERT INTO review_agent.repositories (
                        provider, provider_repository_id, owner, name, full_name,
                        created_at, updated_at
                    ) VALUES (
                        %s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    )
                    ON CONFLICT ON CONSTRAINT repositories_provider_identity_uk
                    DO UPDATE SET
                        owner = EXCLUDED.owner,
                        name = EXCLUDED.name,
                        full_name = EXCLUDED.full_name,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE (
                        review_agent.repositories.owner,
                        review_agent.repositories.name,
                        review_agent.repositories.full_name
                    ) IS DISTINCT FROM (
                        EXCLUDED.owner, EXCLUDED.name, EXCLUDED.full_name
                    )
                    RETURNING id, provider, provider_repository_id, owner, name,
                              full_name, created_at, updated_at
                    """,
                    (provider, provider_repository_id, owner, name, full_name),
                ).fetchone()
    except errors.UniqueViolation as exc:
        raise RepositoryNameConflict(
            f"repository full name is already registered for provider {provider}"
        ) from exc

    if repository is not None:
        return repository
    with connection.cursor(row_factory=class_row(Repository)) as cursor:
        repository = cursor.execute(
            """
            SELECT id, provider, provider_repository_id, owner, name, full_name,
                   created_at, updated_at
            FROM review_agent.repositories
            WHERE provider = %s AND provider_repository_id = %s
            """,
            (provider, provider_repository_id),
        ).fetchone()
    if repository is None:
        raise RegistryError("repository could not be resolved after upsert")
    return repository


def ensure_pull_request(
    connection: psycopg.Connection[TupleRow],
    repository_id: RepositoryId,
    number: int,
) -> PullRequest:
    """Create or return one repository-local pull-request identity."""
    _require_transaction(connection)
    if isinstance(number, bool) or number < 1:
        raise RegistryError("pull request number must be positive")
    with connection.cursor(row_factory=class_row(PullRequest)) as cursor:
        pull_request = cursor.execute(
            """
            INSERT INTO review_agent.pull_requests (
                repository_id, number, created_at
            ) VALUES (%s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT ON CONSTRAINT pull_requests_repository_number_uk
            DO NOTHING
            RETURNING id, repository_id, number, created_at
            """,
            (repository_id, number),
        ).fetchone()
        if pull_request is None:
            pull_request = cursor.execute(
                """
                SELECT id, repository_id, number, created_at
                FROM review_agent.pull_requests
                WHERE repository_id = %s AND number = %s
                """,
                (repository_id, number),
            ).fetchone()
    if pull_request is None:
        raise RegistryError("pull request could not be resolved after insert")
    return pull_request


def create_or_get_subject(
    connection: psycopg.Connection[TupleRow],
    pull_request_id: PullRequestId,
    definition: ReviewSubjectDefinition,
) -> ReviewSubject:
    """Create or re-verify one immutable exact review subject."""
    _require_transaction(connection)
    config = definition.resolved_config
    with connection.cursor(row_factory=class_row(_ReviewSubjectRow)) as cursor:
        row = cursor.execute(
            """
            INSERT INTO review_agent.review_subjects (
                pull_request_id, base_sha, head_sha, policy_revision,
                resolved_config_schema_version, resolved_config,
                resolved_config_hash, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, CURRENT_TIMESTAMP)
            ON CONFLICT ON CONSTRAINT review_subjects_identity_uk DO NOTHING
            RETURNING id, pull_request_id, base_sha, head_sha, policy_revision,
                      resolved_config_schema_version,
                      resolved_config::text AS resolved_config_json,
                      resolved_config_hash, created_at
            """,
            (
                pull_request_id,
                definition.base_sha,
                definition.head_sha,
                definition.policy_revision,
                config.schema_version,
                config.canonical_json,
                config.sha256,
            ),
        ).fetchone()
        if row is None:
            row = cursor.execute(
                """
                SELECT id, pull_request_id, base_sha, head_sha, policy_revision,
                       resolved_config_schema_version,
                       resolved_config::text AS resolved_config_json,
                       resolved_config_hash, created_at
                FROM review_agent.review_subjects
                WHERE pull_request_id = %s
                  AND base_sha = %s
                  AND head_sha = %s
                  AND policy_revision = %s
                  AND resolved_config_schema_version = %s
                  AND resolved_config_hash = %s
                """,
                (
                    pull_request_id,
                    definition.base_sha,
                    definition.head_sha,
                    definition.policy_revision,
                    config.schema_version,
                    config.sha256,
                ),
            ).fetchone()
    if row is None:
        raise RegistryError("review subject could not be resolved after insert")
    try:
        stored_config = decode_resolved_config(
            row.resolved_config_json,
            schema_version=row.resolved_config_schema_version,
        )
    except ReviewDomainError as exc:
        raise SubjectConflict("stored resolved_config is invalid") from exc
    if stored_config != config or row.resolved_config_hash != stored_config.sha256:
        raise SubjectConflict("stored resolved_config does not match its canonical hash")
    stored_definition = ReviewSubjectDefinition(
        base_sha=row.base_sha,
        head_sha=row.head_sha,
        policy_revision=row.policy_revision,
        resolved_config=stored_config,
    )
    if stored_definition != definition:
        raise SubjectConflict("stored review subject does not match the request")
    return ReviewSubject(
        id=row.id,
        pull_request_id=row.pull_request_id,
        definition=stored_definition,
        created_at=row.created_at,
    )
