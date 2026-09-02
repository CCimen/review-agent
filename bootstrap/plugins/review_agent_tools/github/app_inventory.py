"""Complete provider inventory for one selected GitHub App installation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import cast
import urllib.parse

from ..domain.feedback import resolve_repository
from ..postgres import github_app, registry
from .app_auth import GitHubAppAuthenticator


_REPOSITORIES_PER_PAGE = 100


class GitHubAppInventoryError(ValueError):
    """GitHub returned an installation inventory that cannot be reconciled."""


class GitHubAppInventoryPermanent(GitHubAppInventoryError):
    """The provider inventory needs configuration or provider-state repair."""


class GitHubAppInventoryRetryable(GitHubAppInventoryError):
    """A complete provider snapshot may be available on a later attempt."""


class GitHubAppInventoryUnsupported(GitHubAppInventoryPermanent):
    """The installation uses a mode this pilot does not activate."""


@dataclass(frozen=True, slots=True)
class InstallationInventory:
    definition: github_app.InstallationDefinition
    status: github_app.InstallationStatus
    repositories: tuple[github_app.InstallationRepositoryDefinition, ...]


@dataclass(frozen=True, slots=True)
class InstallationMetadata:
    definition: github_app.InstallationDefinition
    status: github_app.InstallationStatus


def installation_id_for_repository(
    authenticator: GitHubAppAuthenticator,
    *,
    repository: str,
    now: datetime | None = None,
) -> int:
    """Resolve the App installation that currently grants access to a repository."""
    normalized = resolve_repository(repository)
    encoded = urllib.parse.quote(normalized, safe="/")
    metadata = authenticator.app_json(
        f"/repos/{encoded}/installation",
        now=now,
    )
    root = _object(metadata, "repository installation")
    return _positive(root.get("id"), "installation id")


def _object(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise GitHubAppInventoryPermanent(f"GitHub returned invalid {field}")
    return cast(Mapping[str, object], value)


def _positive(value: object, field: str) -> int:
    if type(value) is not int or value < 1:
        raise GitHubAppInventoryPermanent(f"GitHub returned invalid {field}")
    return value


def _nonnegative(value: object, field: str) -> int:
    if type(value) is not int or value < 0:
        raise GitHubAppInventoryPermanent(f"GitHub returned invalid {field}")
    return value


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GitHubAppInventoryPermanent(f"GitHub returned invalid {field}")
    return value.strip()


def _permission(value: object, field: str) -> github_app.PermissionLevel:
    if value is None:
        return github_app.PermissionLevel.NONE
    try:
        return github_app.PermissionLevel(_text(value, field).lower())
    except ValueError as exc:
        raise GitHubAppInventoryPermanent(
            f"GitHub returned unsupported {field}"
        ) from exc


def _definition(
    metadata: object, provider_installation_id: int
) -> tuple[github_app.InstallationDefinition, github_app.InstallationStatus]:
    root = _object(metadata, "installation metadata")
    if _positive(root.get("id"), "installation id") != provider_installation_id:
        raise GitHubAppInventoryPermanent(
            "GitHub returned a different installation identity"
        )
    raw_selection = _text(
        root.get("repository_selection"), "repository selection"
    )
    try:
        selection = github_app.RepositorySelection(raw_selection)
    except ValueError as exc:
        raise GitHubAppInventoryPermanent(
            "GitHub returned unsupported repository selection"
        ) from exc
    account = _object(root.get("account"), "installation account")
    raw_account_type = _text(account.get("type"), "account type").lower()
    try:
        account_type = github_app.AccountType(raw_account_type)
    except ValueError as exc:
        raise GitHubAppInventoryPermanent(
            "GitHub returned unsupported account type"
        ) from exc
    permissions = _object(root.get("permissions"), "installation permissions")
    suspended_at = root.get("suspended_at")
    if suspended_at is not None and not isinstance(suspended_at, str):
        raise GitHubAppInventoryPermanent(
            "GitHub returned invalid installation suspension state"
        )
    return (
        github_app.InstallationDefinition(
            provider_installation_id=provider_installation_id,
            account_id=_positive(account.get("id"), "account id"),
            account_login=_text(account.get("login"), "account login"),
            account_type=account_type,
            repository_selection=selection,
            contents_permission=_permission(
                permissions.get("contents"), "contents permission"
            ),
            issues_permission=_permission(
                permissions.get("issues"), "issues permission"
            ),
            pull_requests_permission=_permission(
                permissions.get("pull_requests"), "pull request permission"
            ),
        ),
        (
            github_app.InstallationStatus.SUSPENDED
            if suspended_at is not None
            else github_app.InstallationStatus.ACTIVE
        ),
    )


def _repository(value: object) -> github_app.InstallationRepositoryDefinition:
    item = _object(value, "installation repository")
    provider_repository_id = _positive(item.get("id"), "repository id")
    full_name = _text(item.get("full_name"), "repository name")
    try:
        normalized = registry.resolve_repository(
            registry.RepositoryDefinition(
                provider="github",
                provider_repository_id=provider_repository_id,
                full_name=full_name,
            )
        )
    except registry.RegistryError as exc:
        raise GitHubAppInventoryPermanent(
            "GitHub returned invalid repository identity"
        ) from exc
    return github_app.InstallationRepositoryDefinition(
        provider_repository_id=normalized.provider_repository_id,
        full_name=normalized.full_name,
    )


def read_installation_inventory(
    authenticator: GitHubAppAuthenticator,
    *,
    provider_installation_id: int,
    now: datetime | None = None,
) -> InstallationInventory:
    """Fetch and validate one complete selected-installation snapshot."""
    if isinstance(provider_installation_id, bool) or provider_installation_id < 1:
        raise ValueError("provider_installation_id must be positive")
    metadata = read_installation_metadata(
        authenticator,
        provider_installation_id=provider_installation_id,
        now=now,
    )
    definition = metadata.definition
    status = metadata.status
    if definition.repository_selection is not github_app.RepositorySelection.SELECTED:
        raise GitHubAppInventoryUnsupported(
            "complete inventory reconciliation requires a selected-repository installation"
        )
    token = authenticator.installation_token(
        provider_installation_id,
        permissions={"metadata": "read"},
        now=now,
    )

    repositories: list[github_app.InstallationRepositoryDefinition] = []
    repository_ids: set[int] = set()
    expected_total: int | None = None
    page = 1
    while expected_total is None or len(repositories) < expected_total:
        payload = authenticator.installation_json(
            "/installation/repositories"
            f"?per_page={_REPOSITORIES_PER_PAGE}&page={page}",
            token,
        )
        root = _object(payload, "installation repository page")
        total = _nonnegative(root.get("total_count"), "repository total")
        if expected_total is None:
            expected_total = total
        elif total != expected_total:
            raise GitHubAppInventoryRetryable(
                "GitHub repository inventory changed during pagination"
            )
        raw_repositories = root.get("repositories")
        if not isinstance(raw_repositories, list):
            raise GitHubAppInventoryPermanent(
                "GitHub returned invalid installation repositories"
            )
        repository_values = cast(list[object], raw_repositories)
        if len(repository_values) > _REPOSITORIES_PER_PAGE:
            raise GitHubAppInventoryPermanent(
                "GitHub returned an invalid repository page size"
            )
        if not repository_values and len(repositories) < expected_total:
            raise GitHubAppInventoryRetryable(
                "GitHub returned an incomplete repository inventory"
            )
        for raw_repository in repository_values:
            repository = _repository(raw_repository)
            if repository.provider_repository_id in repository_ids:
                raise GitHubAppInventoryPermanent(
                    "GitHub returned a duplicate repository identity"
                )
            repository_ids.add(repository.provider_repository_id)
            repositories.append(repository)
        if len(repositories) > expected_total:
            raise GitHubAppInventoryRetryable(
                "GitHub returned an inconsistent repository total"
            )
        page += 1
    if len(repositories) != expected_total:
        raise GitHubAppInventoryRetryable(
            "GitHub returned an incomplete repository inventory"
        )
    return InstallationInventory(
        definition=definition,
        status=status,
        repositories=tuple(repositories),
    )


def read_installation_metadata(
    authenticator: GitHubAppAuthenticator,
    *,
    provider_installation_id: int,
    now: datetime | None = None,
) -> InstallationMetadata:
    """Fetch one installation without enumerating its repository inventory."""
    if isinstance(provider_installation_id, bool) or provider_installation_id < 1:
        raise ValueError("provider_installation_id must be positive")
    payload = authenticator.app_json(
        f"/app/installations/{provider_installation_id}", now=now
    )
    definition, status = _definition(payload, provider_installation_id)
    return InstallationMetadata(definition=definition, status=status)
