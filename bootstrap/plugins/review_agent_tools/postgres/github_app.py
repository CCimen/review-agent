"""PostgreSQL ownership for GitHub App installation and repository access state."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import NewType

import psycopg
from psycopg.pq import TransactionStatus
from psycopg.rows import TupleRow, class_row

from ..domain.review import RepositoryId
from . import registry


GitHubAppInstallationId = NewType("GitHubAppInstallationId", int)


class GitHubAppStateError(ValueError):
    """A GitHub App installation or repository transition is invalid."""


class GitHubAppInstallationNotFound(GitHubAppStateError):
    """The requested GitHub App installation does not exist."""


class GitHubAppRepositoryNotFound(GitHubAppStateError):
    """The requested GitHub App repository access record does not exist."""


class GitHubAppReviewReadUnauthorized(GitHubAppStateError):
    """The repository is not currently authorized for App-backed review reads."""


class AccountType(StrEnum):
    USER = "user"
    ORGANIZATION = "organization"


class RepositorySelection(StrEnum):
    SELECTED = "selected"
    ALL = "all"


class PermissionLevel(StrEnum):
    NONE = "none"
    READ = "read"
    WRITE = "write"


class InstallationStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    DELETED = "deleted"


class RepositoryAccess(StrEnum):
    AVAILABLE = "available"
    REMOVED = "removed"
    INSTALLATION_SUSPENDED = "installation_suspended"
    INSTALLATION_DELETED = "installation_deleted"


class TriggerMode(StrEnum):
    MANUAL = "manual"
    AUTOMATIC = "automatic"


class AccessEvent(StrEnum):
    GRANTED = "granted"
    ENABLED = "enabled"
    DISABLED = "disabled"
    REMOVED = "removed"
    INSTALLATION_SUSPENDED = "installation_suspended"
    INSTALLATION_RESTORED = "installation_restored"
    INSTALLATION_DELETED = "installation_deleted"


@dataclass(frozen=True, slots=True)
class InstallationDefinition:
    provider_installation_id: int
    account_id: int
    account_login: str
    account_type: AccountType
    repository_selection: RepositorySelection
    contents_permission: PermissionLevel
    issues_permission: PermissionLevel
    pull_requests_permission: PermissionLevel


@dataclass(frozen=True, slots=True)
class GitHubAppInstallation:
    id: GitHubAppInstallationId
    provider_installation_id: int
    account_id: int
    account_login: str
    account_type: AccountType
    repository_selection: RepositorySelection
    status: InstallationStatus
    contents_permission: PermissionLevel
    issues_permission: PermissionLevel
    pull_requests_permission: PermissionLevel
    created_at: datetime
    updated_at: datetime
    suspended_at: datetime | None
    deleted_at: datetime | None


@dataclass(frozen=True, slots=True)
class InstallationEvent:
    id: int
    installation_id: GitHubAppInstallationId
    previous_status: InstallationStatus
    status: InstallationStatus
    actor: str
    reason: str
    recorded_at: datetime


@dataclass(frozen=True, slots=True)
class RepositoryAccessState:
    repository_id: RepositoryId
    installation_id: GitHubAppInstallationId
    provider_repository_id: int
    full_name: str
    access_state: RepositoryAccess
    enabled: bool
    trigger_mode: TriggerMode
    profile_key: str | None
    enabled_at: datetime | None
    disabled_at: datetime | None
    updated_by: str
    update_reason: str
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class RepositoryAccessEvent:
    id: int
    repository_id: RepositoryId
    installation_id: GitHubAppInstallationId
    event_kind: AccessEvent
    access_state: RepositoryAccess
    enabled: bool
    trigger_mode: TriggerMode
    profile_key: str | None
    actor: str
    reason: str
    recorded_at: datetime


@dataclass(frozen=True, slots=True)
class ReviewReadAuthorization:
    """Current provider IDs authorized for one repository's review reads."""

    repository_id: RepositoryId
    provider_repository_id: int
    provider_installation_id: int


@dataclass(frozen=True, slots=True)
class ReviewAdmissionAuthorization:
    """Locked repository state authorized for final review admission."""

    repository_id: RepositoryId
    provider_repository_id: int
    provider_installation_id: int
    full_name: str
    profile_key: str


@dataclass(frozen=True, slots=True)
class _InstallationRow:
    id: GitHubAppInstallationId
    provider_installation_id: int
    account_id: int
    account_login: str
    account_type: str
    repository_selection: str
    status: str
    contents_permission: str
    issues_permission: str
    pull_requests_permission: str
    created_at: datetime
    updated_at: datetime
    suspended_at: datetime | None
    deleted_at: datetime | None


@dataclass(frozen=True, slots=True)
class _InstallationEventRow:
    id: int
    installation_id: GitHubAppInstallationId
    previous_status: str
    status: str
    actor: str
    reason: str
    recorded_at: datetime


@dataclass(frozen=True, slots=True)
class _RepositoryAccessRow:
    repository_id: RepositoryId
    installation_id: GitHubAppInstallationId
    provider_repository_id: int
    full_name: str
    access_state: str
    enabled: bool
    trigger_mode: str
    profile_key: str | None
    enabled_at: datetime | None
    disabled_at: datetime | None
    updated_by: str
    update_reason: str
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class _RepositoryAccessEventRow:
    id: int
    repository_id: RepositoryId
    installation_id: GitHubAppInstallationId
    event_kind: str
    access_state: str
    enabled: bool
    trigger_mode: str
    profile_key: str | None
    actor: str
    reason: str
    recorded_at: datetime


_INSTALLATION_COLUMNS = """
    id, provider_installation_id, account_id, account_login, account_type,
    repository_selection, status, contents_permission, issues_permission,
    pull_requests_permission, created_at, updated_at, suspended_at, deleted_at
"""

_ACCESS_COLUMNS = """
    access.repository_id, access.installation_id,
    repository.provider_repository_id, repository.full_name,
    access.access_state, access.enabled, access.trigger_mode, access.profile_key,
    access.enabled_at, access.disabled_at, access.updated_by,
    access.update_reason, access.updated_at
"""


def _require_transaction(connection: psycopg.Connection[TupleRow]) -> None:
    if connection.info.transaction_status != TransactionStatus.INTRANS:
        raise GitHubAppStateError(
            "GitHub App state operations require an active transaction"
        )


def _positive(value: int, field: str) -> int:
    if isinstance(value, bool) or value < 1:
        raise GitHubAppStateError(f"{field} must be positive")
    return value


def _text(value: str, field: str, maximum: int) -> str:
    normalized = " ".join(value.strip().split())
    if not normalized:
        raise GitHubAppStateError(f"{field} is required")
    if len(normalized) > maximum:
        raise GitHubAppStateError(f"{field} exceeds {maximum} characters")
    return normalized


def _profile(value: str) -> str:
    normalized = _text(value, "profile_key", 80)
    if not all(
        part and part.isascii() and part.isalnum() and part == part.lower()
        for part in normalized.split("-")
    ):
        raise GitHubAppStateError(
            "profile_key must use lower-case ASCII words and hyphens"
        )
    return normalized


def _installation(row: _InstallationRow) -> GitHubAppInstallation:
    return GitHubAppInstallation(
        id=row.id,
        provider_installation_id=row.provider_installation_id,
        account_id=row.account_id,
        account_login=row.account_login,
        account_type=AccountType(row.account_type),
        repository_selection=RepositorySelection(row.repository_selection),
        status=InstallationStatus(row.status),
        contents_permission=PermissionLevel(row.contents_permission),
        issues_permission=PermissionLevel(row.issues_permission),
        pull_requests_permission=PermissionLevel(row.pull_requests_permission),
        created_at=row.created_at,
        updated_at=row.updated_at,
        suspended_at=row.suspended_at,
        deleted_at=row.deleted_at,
    )


def _access(row: _RepositoryAccessRow) -> RepositoryAccessState:
    return RepositoryAccessState(
        repository_id=row.repository_id,
        installation_id=row.installation_id,
        provider_repository_id=row.provider_repository_id,
        full_name=row.full_name,
        access_state=RepositoryAccess(row.access_state),
        enabled=row.enabled,
        trigger_mode=TriggerMode(row.trigger_mode),
        profile_key=row.profile_key,
        enabled_at=row.enabled_at,
        disabled_at=row.disabled_at,
        updated_by=row.updated_by,
        update_reason=row.update_reason,
        updated_at=row.updated_at,
    )


def _installation_event(row: _InstallationEventRow) -> InstallationEvent:
    return InstallationEvent(
        id=row.id,
        installation_id=row.installation_id,
        previous_status=InstallationStatus(row.previous_status),
        status=InstallationStatus(row.status),
        actor=row.actor,
        reason=row.reason,
        recorded_at=row.recorded_at,
    )


def _event(row: _RepositoryAccessEventRow) -> RepositoryAccessEvent:
    return RepositoryAccessEvent(
        id=row.id,
        repository_id=row.repository_id,
        installation_id=row.installation_id,
        event_kind=AccessEvent(row.event_kind),
        access_state=RepositoryAccess(row.access_state),
        enabled=row.enabled,
        trigger_mode=TriggerMode(row.trigger_mode),
        profile_key=row.profile_key,
        actor=row.actor,
        reason=row.reason,
        recorded_at=row.recorded_at,
    )


def sync_installation(
    connection: psycopg.Connection[TupleRow], definition: InstallationDefinition
) -> GitHubAppInstallation:
    """Create or refresh one active installation by GitHub installation ID."""
    _require_transaction(connection)
    provider_installation_id = _positive(
        definition.provider_installation_id, "provider_installation_id"
    )
    account_id = _positive(definition.account_id, "account_id")
    account_login = _text(definition.account_login, "account_login", 100)
    with connection.cursor(row_factory=class_row(_InstallationRow)) as cursor:
        row = cursor.execute(
            f"""
            INSERT INTO review_agent.github_app_installations (
                provider_installation_id, account_id, account_login, account_type,
                repository_selection, status, contents_permission,
                issues_permission, pull_requests_permission, created_at, updated_at
            ) VALUES (
                %s, %s, %s, %s, %s, 'active', %s, %s, %s,
                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            )
            ON CONFLICT ON CONSTRAINT github_app_installations_provider_id_uk
            DO UPDATE SET
                account_id = EXCLUDED.account_id,
                account_login = EXCLUDED.account_login,
                account_type = EXCLUDED.account_type,
                repository_selection = EXCLUDED.repository_selection,
                contents_permission = EXCLUDED.contents_permission,
                issues_permission = EXCLUDED.issues_permission,
                pull_requests_permission = EXCLUDED.pull_requests_permission,
                updated_at = CURRENT_TIMESTAMP
            WHERE review_agent.github_app_installations.status <> 'deleted'
            RETURNING {_INSTALLATION_COLUMNS}
            """,
            (
                provider_installation_id,
                account_id,
                account_login,
                definition.account_type.value,
                definition.repository_selection.value,
                definition.contents_permission.value,
                definition.issues_permission.value,
                definition.pull_requests_permission.value,
            ),
        ).fetchone()
    if row is None:
        raise GitHubAppStateError("a deleted installation cannot be synchronized")
    return _installation(row)


def get_installation(
    connection: psycopg.Connection[TupleRow], installation_id: GitHubAppInstallationId
) -> GitHubAppInstallation:
    _require_transaction(connection)
    with connection.cursor(row_factory=class_row(_InstallationRow)) as cursor:
        row = cursor.execute(
            f"""
            SELECT {_INSTALLATION_COLUMNS}
            FROM review_agent.github_app_installations
            WHERE id = %s
            """,
            (_positive(installation_id, "installation_id"),),
        ).fetchone()
    if row is None:
        raise GitHubAppInstallationNotFound("GitHub App installation was not found")
    return _installation(row)


def get_installation_by_provider_id(
    connection: psycopg.Connection[TupleRow],
    provider_installation_id: int,
    *,
    for_update: bool = False,
) -> GitHubAppInstallation:
    """Resolve one installation by its stable GitHub identity."""
    _require_transaction(connection)
    lock = "FOR UPDATE" if for_update else ""
    with connection.cursor(row_factory=class_row(_InstallationRow)) as cursor:
        row = cursor.execute(
            f"""
            SELECT {_INSTALLATION_COLUMNS}
            FROM review_agent.github_app_installations
            WHERE provider_installation_id = %s
            {lock}
            """,
            (_positive(provider_installation_id, "provider_installation_id"),),
        ).fetchone()
    if row is None:
        raise GitHubAppInstallationNotFound("GitHub App installation was not found")
    return _installation(row)


def _lock_installation(
    connection: psycopg.Connection[TupleRow],
    installation_id: GitHubAppInstallationId,
    *,
    exclusive: bool,
) -> GitHubAppInstallation:
    _require_transaction(connection)
    lock = "UPDATE" if exclusive else "SHARE"
    with connection.cursor(row_factory=class_row(_InstallationRow)) as cursor:
        row = cursor.execute(
            f"""
            SELECT {_INSTALLATION_COLUMNS}
            FROM review_agent.github_app_installations
            WHERE id = %s
            FOR {lock}
            """,
            (_positive(installation_id, "installation_id"),),
        ).fetchone()
    if row is None:
        raise GitHubAppInstallationNotFound("GitHub App installation was not found")
    return _installation(row)


def get_repository_access(
    connection: psycopg.Connection[TupleRow], repository_id: RepositoryId
) -> RepositoryAccessState:
    _require_transaction(connection)
    with connection.cursor(row_factory=class_row(_RepositoryAccessRow)) as cursor:
        row = cursor.execute(
            f"""
            SELECT {_ACCESS_COLUMNS}
            FROM review_agent.github_app_repository_access AS access
            JOIN review_agent.repositories AS repository
              ON repository.id = access.repository_id
            WHERE access.repository_id = %s
            """,
            (_positive(repository_id, "repository_id"),),
        ).fetchone()
    if row is None:
        raise GitHubAppRepositoryNotFound(
            "GitHub App repository access was not found"
        )
    return _access(row)


def get_repository_access_by_provider_id(
    connection: psycopg.Connection[TupleRow], provider_repository_id: int
) -> RepositoryAccessState:
    """Resolve App access by GitHub's stable repository identity."""
    _require_transaction(connection)
    with connection.cursor(row_factory=class_row(_RepositoryAccessRow)) as cursor:
        row = cursor.execute(
            f"""
            SELECT {_ACCESS_COLUMNS}
            FROM review_agent.github_app_repository_access AS access
            JOIN review_agent.repositories AS repository
              ON repository.id = access.repository_id
            WHERE repository.provider = 'github'
              AND repository.provider_repository_id = %s
            """,
            (_positive(provider_repository_id, "provider_repository_id"),),
        ).fetchone()
    if row is None:
        raise GitHubAppRepositoryNotFound(
            "GitHub App repository access was not found"
        )
    return _access(row)


def authorize_review_read(
    connection: psycopg.Connection[TupleRow], provider_repository_id: int
) -> ReviewReadAuthorization:
    """Authorize one stable GitHub repository ID against current installation state."""
    _require_transaction(connection)
    row = connection.execute(
        """
        SELECT access.repository_id, repository.provider_repository_id,
               installation.provider_installation_id
        FROM review_agent.github_app_repository_access AS access
        JOIN review_agent.repositories AS repository
          ON repository.id = access.repository_id
        JOIN review_agent.github_app_installations AS installation
          ON installation.id = access.installation_id
        WHERE repository.provider = 'github'
          AND repository.provider_repository_id = %s
          AND access.access_state = 'available'
          AND access.enabled
          AND installation.status = 'active'
          AND installation.contents_permission IN ('read', 'write')
          AND installation.issues_permission IN ('read', 'write')
          AND installation.pull_requests_permission IN ('read', 'write')
        """,
        (_positive(provider_repository_id, "provider_repository_id"),),
    ).fetchone()
    if row is None:
        raise GitHubAppReviewReadUnauthorized(
            "repository is not authorized for GitHub App review reads"
        )
    authorized_repository_id, authorized_provider_id, provider_installation_id = row
    return ReviewReadAuthorization(
        repository_id=RepositoryId(authorized_repository_id),
        provider_repository_id=authorized_provider_id,
        provider_installation_id=provider_installation_id,
    )


def authorize_review_admission(
    connection: psycopg.Connection[TupleRow],
    *,
    provider_repository_id: int,
    provider_installation_id: int,
    profile_key: str,
) -> ReviewAdmissionAuthorization:
    """Lock and reauthorize one exact App review immediately before admission."""
    _require_transaction(connection)
    row = connection.execute(
        """
        SELECT access.repository_id, repository.provider_repository_id,
               installation.provider_installation_id, repository.full_name,
               access.profile_key
        FROM review_agent.github_app_repository_access AS access
        JOIN review_agent.repositories AS repository
          ON repository.id = access.repository_id
        JOIN review_agent.github_app_installations AS installation
          ON installation.id = access.installation_id
        WHERE repository.provider = 'github'
          AND repository.provider_repository_id = %s
          AND installation.provider_installation_id = %s
          AND access.access_state = 'available'
          AND access.enabled
          AND access.profile_key = %s
          AND installation.status = 'active'
          AND installation.repository_selection = 'selected'
          AND installation.contents_permission IN ('read', 'write')
          AND installation.issues_permission IN ('read', 'write')
          AND installation.pull_requests_permission IN ('read', 'write')
        FOR UPDATE OF access, installation, repository
        """,
        (
            _positive(provider_repository_id, "provider_repository_id"),
            _positive(provider_installation_id, "provider_installation_id"),
            _profile(profile_key),
        ),
    ).fetchone()
    if row is None:
        raise GitHubAppReviewReadUnauthorized(
            "repository is not authorized for GitHub App review admission"
        )
    repository_id, repository_provider_id, installation_provider_id, name, profile = row
    return ReviewAdmissionAuthorization(
        repository_id=RepositoryId(repository_id),
        provider_repository_id=repository_provider_id,
        provider_installation_id=installation_provider_id,
        full_name=name,
        profile_key=profile,
    )


def _lock_repository_access(
    connection: psycopg.Connection[TupleRow], repository_id: RepositoryId
) -> RepositoryAccessState:
    _require_transaction(connection)
    with connection.cursor(row_factory=class_row(_RepositoryAccessRow)) as cursor:
        row = cursor.execute(
            f"""
            SELECT {_ACCESS_COLUMNS}
            FROM review_agent.github_app_repository_access AS access
            JOIN review_agent.repositories AS repository
              ON repository.id = access.repository_id
            WHERE access.repository_id = %s
            FOR UPDATE OF access
            """,
            (_positive(repository_id, "repository_id"),),
        ).fetchone()
    if row is None:
        raise GitHubAppRepositoryNotFound(
            "GitHub App repository access was not found"
        )
    return _access(row)


def _record_event(
    connection: psycopg.Connection[TupleRow],
    state: RepositoryAccessState,
    event_kind: AccessEvent,
) -> None:
    connection.execute(
        """
        INSERT INTO review_agent.github_app_repository_access_events (
            repository_id, installation_id, event_kind, access_state, enabled,
            trigger_mode, profile_key, actor, reason, recorded_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
        """,
        (
            state.repository_id,
            state.installation_id,
            event_kind.value,
            state.access_state.value,
            state.enabled,
            state.trigger_mode.value,
            state.profile_key,
            state.updated_by,
            state.update_reason,
        ),
    )


def grant_repository_access(
    connection: psycopg.Connection[TupleRow],
    *,
    installation_id: GitHubAppInstallationId,
    provider_repository_id: int,
    full_name: str,
    actor: str,
    reason: str,
) -> RepositoryAccessState:
    """Make one selected repository available without enabling reviews."""
    _require_transaction(connection)
    installation = _lock_installation(
        connection, installation_id, exclusive=False
    )
    if installation.status is not InstallationStatus.ACTIVE:
        raise GitHubAppStateError("installation is not active")
    updated_by = _text(actor, "actor", 120)
    update_reason = _text(reason, "reason", 500)
    repository = registry.ensure_repository(
        connection,
        registry.RepositoryDefinition(
            provider="github",
            provider_repository_id=_positive(
                provider_repository_id, "provider_repository_id"
            ),
            full_name=full_name,
        ),
    )
    changed = connection.execute(
        """
        INSERT INTO review_agent.github_app_repository_access (
            repository_id, installation_id, access_state, enabled, trigger_mode,
            profile_key, enabled_at, disabled_at, updated_by, update_reason,
            updated_at
        ) VALUES (
            %s, %s, 'available', false, 'manual', NULL, NULL, NULL,
            %s, %s, CURRENT_TIMESTAMP
        )
        ON CONFLICT (repository_id) DO UPDATE SET
            installation_id = EXCLUDED.installation_id,
            access_state = 'available',
            enabled = CASE
                WHEN review_agent.github_app_repository_access.installation_id
                     = EXCLUDED.installation_id
                 AND review_agent.github_app_repository_access.access_state
                     = 'available'
                THEN review_agent.github_app_repository_access.enabled
                ELSE false
            END,
            enabled_at = CASE
                WHEN review_agent.github_app_repository_access.installation_id
                     = EXCLUDED.installation_id
                 AND review_agent.github_app_repository_access.access_state
                     = 'available'
                THEN review_agent.github_app_repository_access.enabled_at
                ELSE NULL
            END,
            disabled_at = CASE
                WHEN review_agent.github_app_repository_access.installation_id
                     = EXCLUDED.installation_id
                 AND review_agent.github_app_repository_access.access_state
                     = 'available'
                THEN review_agent.github_app_repository_access.disabled_at
                ELSE CURRENT_TIMESTAMP
            END,
            updated_by = EXCLUDED.updated_by,
            update_reason = EXCLUDED.update_reason,
            updated_at = CURRENT_TIMESTAMP
        WHERE (
            review_agent.github_app_repository_access.installation_id,
            review_agent.github_app_repository_access.access_state
        ) IS DISTINCT FROM (
            EXCLUDED.installation_id,
            EXCLUDED.access_state
        )
          AND review_agent.github_app_repository_access.installation_id
              <= EXCLUDED.installation_id
        RETURNING repository_id
        """,
        (repository.id, installation.id, updated_by, update_reason),
    ).fetchone()
    state = get_repository_access(connection, repository.id)
    if changed is not None:
        _record_event(connection, state, AccessEvent.GRANTED)
    return state


def enable_repository(
    connection: psycopg.Connection[TupleRow],
    *,
    repository_id: RepositoryId,
    profile_key: str,
    trigger_mode: TriggerMode,
    actor: str,
    reason: str,
) -> RepositoryAccessState:
    """Enable reviews only while the App has active access to the repository."""
    _require_transaction(connection)
    current = get_repository_access(connection, repository_id)
    installation = _lock_installation(
        connection, current.installation_id, exclusive=False
    )
    current = _lock_repository_access(connection, repository_id)
    if (
        current.installation_id != installation.id
        or current.access_state is not RepositoryAccess.AVAILABLE
        or installation.status is not InstallationStatus.ACTIVE
    ):
        raise GitHubAppStateError("repository access is not available")
    updated_by = _text(actor, "actor", 120)
    update_reason = _text(reason, "reason", 500)
    connection.execute(
        """
        UPDATE review_agent.github_app_repository_access
        SET enabled = true,
            trigger_mode = %s,
            profile_key = %s,
            enabled_at = CURRENT_TIMESTAMP,
            disabled_at = NULL,
            updated_by = %s,
            update_reason = %s,
            updated_at = CURRENT_TIMESTAMP
        WHERE repository_id = %s
        """,
        (
            trigger_mode.value,
            _profile(profile_key),
            updated_by,
            update_reason,
            repository_id,
        ),
    )
    state = get_repository_access(connection, repository_id)
    _record_event(connection, state, AccessEvent.ENABLED)
    return state


def disable_repository(
    connection: psycopg.Connection[TupleRow],
    *,
    repository_id: RepositoryId,
    actor: str,
    reason: str,
) -> RepositoryAccessState:
    """Disable review admission while retaining the selected-repository grant."""
    _require_transaction(connection)
    current = _lock_repository_access(connection, repository_id)
    if current.access_state is not RepositoryAccess.AVAILABLE:
        raise GitHubAppStateError("repository access is not available")
    changed = connection.execute(
        """
        UPDATE review_agent.github_app_repository_access
        SET enabled = false,
            disabled_at = CURRENT_TIMESTAMP,
            updated_by = %s,
            update_reason = %s,
            updated_at = CURRENT_TIMESTAMP
        WHERE repository_id = %s
          AND enabled
        RETURNING repository_id
        """,
        (
            _text(actor, "actor", 120),
            _text(reason, "reason", 500),
            repository_id,
        ),
    ).fetchone()
    state = get_repository_access(connection, repository_id)
    if changed is not None:
        _record_event(connection, state, AccessEvent.DISABLED)
    return state


def remove_repository_access(
    connection: psycopg.Connection[TupleRow],
    *,
    repository_id: RepositoryId,
    actor: str,
    reason: str,
) -> RepositoryAccessState:
    """Fence a repository removed from the App installation."""
    _require_transaction(connection)
    _lock_repository_access(connection, repository_id)
    changed = connection.execute(
        """
        UPDATE review_agent.github_app_repository_access
        SET access_state = 'removed',
            enabled = false,
            disabled_at = CURRENT_TIMESTAMP,
            updated_by = %s,
            update_reason = %s,
            updated_at = CURRENT_TIMESTAMP
        WHERE repository_id = %s
          AND access_state IN ('available', 'installation_suspended')
        RETURNING repository_id
        """,
        (
            _text(actor, "actor", 120),
            _text(reason, "reason", 500),
            repository_id,
        ),
    ).fetchone()
    state = get_repository_access(connection, repository_id)
    if changed is not None:
        _record_event(connection, state, AccessEvent.REMOVED)
    return state


def remove_repository_access_for_installation(
    connection: psycopg.Connection[TupleRow],
    *,
    provider_repository_id: int,
    expected_provider_installation_id: int,
    actor: str,
    reason: str,
) -> RepositoryAccessState | None:
    """Remove access only if the named installation still owns the repository."""
    _require_transaction(connection)
    row = connection.execute(
        """
        SELECT access.repository_id
        FROM review_agent.github_app_repository_access AS access
        JOIN review_agent.repositories AS repository
          ON repository.id = access.repository_id
        JOIN review_agent.github_app_installations AS installation
          ON installation.id = access.installation_id
        WHERE repository.provider = 'github'
          AND repository.provider_repository_id = %s
          AND installation.provider_installation_id = %s
        FOR UPDATE OF access
        """,
        (
            _positive(provider_repository_id, "provider_repository_id"),
            _positive(
                expected_provider_installation_id,
                "expected_provider_installation_id",
            ),
        ),
    ).fetchone()
    if row is None:
        return None
    return remove_repository_access(
        connection,
        repository_id=RepositoryId(row[0]),
        actor=actor,
        reason=reason,
    )


def set_installation_status(
    connection: psycopg.Connection[TupleRow],
    *,
    installation_id: GitHubAppInstallationId,
    status: InstallationStatus,
    actor: str,
    reason: str,
) -> GitHubAppInstallation:
    """Transition an installation and fence or restore its repositories atomically."""
    _require_transaction(connection)
    current = _lock_installation(connection, installation_id, exclusive=True)
    if current.status is InstallationStatus.DELETED:
        raise GitHubAppStateError("a deleted installation cannot transition")
    if current.status is status:
        return current
    if (
        status is InstallationStatus.ACTIVE
        and current.status is not InstallationStatus.SUSPENDED
    ):
        raise GitHubAppStateError("only a suspended installation can become active")

    updated_by = _text(actor, "actor", 120)
    update_reason = _text(reason, "reason", 500)
    connection.execute(
        """
        UPDATE review_agent.github_app_installations
        SET status = %s,
            suspended_at = CASE
                WHEN %s = 'suspended' THEN CURRENT_TIMESTAMP
                WHEN %s = 'active' THEN NULL
                ELSE suspended_at
            END,
            deleted_at = CASE
                WHEN %s = 'deleted' THEN CURRENT_TIMESTAMP
                ELSE NULL
            END,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = %s
        """,
        (
            status.value,
            status.value,
            status.value,
            status.value,
            installation_id,
        ),
    )
    connection.execute(
        """
        INSERT INTO review_agent.github_app_installation_events (
            installation_id, previous_status, status, actor, reason, recorded_at
        ) VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
        """,
        (
            installation_id,
            current.status.value,
            status.value,
            updated_by,
            update_reason,
        ),
    )

    if status is InstallationStatus.ACTIVE:
        access_state = RepositoryAccess.AVAILABLE
        event_kind = AccessEvent.INSTALLATION_RESTORED
        allowed_previous = [RepositoryAccess.INSTALLATION_SUSPENDED.value]
    elif status is InstallationStatus.SUSPENDED:
        access_state = RepositoryAccess.INSTALLATION_SUSPENDED
        event_kind = AccessEvent.INSTALLATION_SUSPENDED
        allowed_previous = [RepositoryAccess.AVAILABLE.value]
    else:
        access_state = RepositoryAccess.INSTALLATION_DELETED
        event_kind = AccessEvent.INSTALLATION_DELETED
        allowed_previous = [
            RepositoryAccess.AVAILABLE.value,
            RepositoryAccess.INSTALLATION_SUSPENDED.value,
        ]

    connection.execute(
        """
        WITH updated AS (
            UPDATE review_agent.github_app_repository_access
            SET access_state = %s,
                enabled = false,
                disabled_at = CURRENT_TIMESTAMP,
                updated_by = %s,
                update_reason = %s,
                updated_at = CURRENT_TIMESTAMP
            WHERE installation_id = %s
              AND access_state = ANY(%s)
            RETURNING repository_id, installation_id, access_state, enabled,
                      trigger_mode, profile_key, updated_by, update_reason
        )
        INSERT INTO review_agent.github_app_repository_access_events (
            repository_id, installation_id, event_kind, access_state, enabled,
            trigger_mode, profile_key, actor, reason, recorded_at
        )
        SELECT repository_id, installation_id, %s, access_state, enabled,
               trigger_mode, profile_key, updated_by, update_reason,
               CURRENT_TIMESTAMP
        FROM updated
        """,
        (
            access_state.value,
            updated_by,
            update_reason,
            installation_id,
            allowed_previous,
            event_kind.value,
        ),
    )
    return get_installation(connection, installation_id)


def list_installation_events(
    connection: psycopg.Connection[TupleRow],
    installation_id: GitHubAppInstallationId,
) -> tuple[InstallationEvent, ...]:
    _require_transaction(connection)
    with connection.cursor(row_factory=class_row(_InstallationEventRow)) as cursor:
        rows = cursor.execute(
            """
            SELECT id, installation_id, previous_status, status, actor, reason,
                   recorded_at
            FROM review_agent.github_app_installation_events
            WHERE installation_id = %s
            ORDER BY recorded_at, id
            """,
            (_positive(installation_id, "installation_id"),),
        ).fetchall()
    return tuple(_installation_event(row) for row in rows)


def list_repository_access_events(
    connection: psycopg.Connection[TupleRow], repository_id: RepositoryId
) -> tuple[RepositoryAccessEvent, ...]:
    _require_transaction(connection)
    with connection.cursor(row_factory=class_row(_RepositoryAccessEventRow)) as cursor:
        rows = cursor.execute(
            """
            SELECT id, repository_id, installation_id, event_kind, access_state,
                   enabled, trigger_mode, profile_key, actor, reason, recorded_at
            FROM review_agent.github_app_repository_access_events
            WHERE repository_id = %s
            ORDER BY recorded_at, id
            """,
            (_positive(repository_id, "repository_id"),),
        ).fetchall()
    return tuple(_event(row) for row in rows)
