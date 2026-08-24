"""Environment composition for deterministic GitHub review publication."""

from __future__ import annotations

import sqlite3

try:
    from . import review_publication_application
    from .github.publication import (
        GitHubIssueCommentGateway,
        GitHubPublicationGateway,
    )
    from .memory_validation import ReviewMemoryError
    from .settings import ReviewAgentSettings, SettingsError
except ImportError:  # pragma: no cover - supports direct module imports in tests.
    import review_publication_application  # type: ignore[no-redef]
    from github.publication import (
        GitHubIssueCommentGateway,
        GitHubPublicationGateway,
    )
    from memory_validation import ReviewMemoryError
    from settings import ReviewAgentSettings, SettingsError


def _max_comment_bytes() -> int:
    try:
        return ReviewAgentSettings.from_environment().publish_max_bytes
    except SettingsError as exc:
        raise ReviewMemoryError(str(exc)) from exc


def _default_gateway() -> GitHubIssueCommentGateway:
    settings = ReviewAgentSettings.from_environment()
    return GitHubIssueCommentGateway(
        settings.github_publish_token,
        read_token=settings.github_read_token,
    )


def publish_review(
    connection: sqlite3.Connection,
    *,
    publication_id: int,
    review_run_id: int,
    github: GitHubPublicationGateway | None = None,
    max_comment_bytes: int | None = None,
) -> dict[str, object]:
    """Publish through explicit application and GitHub owners."""
    return review_publication_application.publish_review(
        connection,
        publication_id=publication_id,
        review_run_id=review_run_id,
        github=github or _default_gateway(),
        max_comment_bytes=(
            max_comment_bytes
            if max_comment_bytes is not None
            else _max_comment_bytes()
        ),
    )


def publish_run_failure_status(
    connection: sqlite3.Connection,
    *,
    run_id: int,
    failure_code: str,
    github: GitHubPublicationGateway | None = None,
) -> dict[str, object]:
    """Publish deterministic failure status through the application owner."""
    return review_publication_application.publish_run_failure_status(
        connection,
        run_id=run_id,
        failure_code=failure_code,
        github=github or _default_gateway(),
    )
