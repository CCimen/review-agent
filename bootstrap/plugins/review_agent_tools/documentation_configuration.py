"""Bounded, read-only default-branch configuration inspection for operators."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import re
from typing import Literal, cast
from urllib.parse import quote

from .domain.documentation_policy import (
    CONFIG_PATH, DocumentationPolicy, DocumentationPolicyError, parse_policy,
)
from .github.source import read_documentation_policy_at_revision
from .source_control import GitHubReadClient, GitHubReadError


@dataclass(frozen=True, slots=True)
class DocumentationConfiguration:
    state: Literal["valid", "not_configured", "invalid", "unavailable"]
    read_at: datetime
    default_branch: str | None = None
    revision: str | None = None
    policy_hash: str | None = None
    source_url: str | None = None
    policy: DocumentationPolicy | None = None
    problem: str | None = None


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("GitHub returned invalid repository metadata")
    return cast(dict[str, object], value)


def read_configuration(
    github: GitHubReadClient, *, repository: str, provider_repository_id: int,
) -> DocumentationConfiguration:
    """Inspect syntax and relationships; execution always reloads the PR's own base policy."""
    result = DocumentationConfiguration("unavailable", datetime.now(timezone.utc))
    try:
        endpoint = f"/repos/{quote(repository, safe='/')}"
        metadata = _object(github.request_json(endpoint, max_bytes=256_000))
        if (metadata.get("id") != provider_repository_id
                or str(metadata.get("full_name", "")).casefold() != repository.casefold()):
            raise ValueError("GitHub repository identity does not match the registered repository")
        branch = metadata.get("default_branch")
        if not isinstance(branch, str) or not 1 <= len(branch) <= 255:
            raise ValueError("GitHub returned no usable default branch")
        branch_data = _object(github.request_json(
            f"{endpoint}/branches/{quote(branch, safe='')}", max_bytes=256_000,
        ))
        revision = _object(branch_data.get("commit")).get("sha")
        if (branch_data.get("name") != branch or not isinstance(revision, str)
                or re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", revision) is None):
            raise ValueError("GitHub returned an invalid default-branch revision")
        result = replace(result, default_branch=branch, revision=revision,
                         source_url=f"https://github.com/{quote(repository, safe='/')}/blob/{revision}/{CONFIG_PATH}")
        source = read_documentation_policy_at_revision(github, repository=repository, revision=revision)
        if source.state == "not_found_at_revision":
            return replace(result, state="not_configured")
        if source.state != "ok" or source.content is None:
            return replace(result, problem=f"Configuration could not be read: {source.state}")
        result = replace(result, policy_hash=source.content_sha256)
        try:
            policy = parse_policy(source.content)
        except DocumentationPolicyError as exc:
            return replace(result, state="invalid", problem=str(exc))
        return replace(result, state="valid", policy=policy)
    except GitHubReadError:
        return replace(result, problem="GitHub could not read the configuration. Check App access and retry.")
    except ValueError as exc:
        return replace(result, problem=str(exc))
