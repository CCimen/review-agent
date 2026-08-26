"""Lease-bound GitHub App operations for deterministic review publication."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import re
from typing import Literal, cast

from ..domain.publication import PublicationId, PublicationStatus
from ..domain.review import ReviewRunId
from ..postgres import github_app, publications, review_runs
from ..postgres.runtime import PostgreSQLRuntime
from .app_auth import (
    GitHubAppTokenPermanent,
    GitHubAppTokenRetryable,
    GitHubAppTokenService,
)
from .gateway import (
    GitHubGatewayProtocolError,
    GitHubGatewayRejected,
    GitHubGatewayRetryable,
)
from .publication import (
    GitHubIssueCommentGateway,
    GitHubPublicationError,
    InlineReviewComment,
    IssueComment,
    PullRequestReview,
    PullRequestReviewComment,
    PullRequestState,
    PUBLICATION_REQUEST_MAX_PAGES,
)


EXECUTE_REVIEW_PUBLICATION_PATH = "/v1/review-publications/execute"
PublicationScopeKind = Literal[
    "publication", "posted_publication", "failure_status"
]
PublicationOperation = Literal[
    "current_user",
    "get_pull",
    "list_issue_comments",
    "update_issue_comment",
    "create_issue_comment",
    "delete_issue_comment",
    "create_pull_request_review",
    "list_pull_request_review_comments",
]
PublicationResult = (
    str
    | PullRequestState
    | list[IssueComment]
    | IssueComment
    | None
    | PullRequestReview
    | list[PullRequestReviewComment]
)

_BASE_FIELDS = {"scope_kind", "scope_id", "operation"}
_LEASE_FIELDS = {"lease_owner", "lease_generation"}
_POSTED_PUBLICATION_OPERATIONS: frozenset[PublicationOperation] = frozenset(
    {
        "list_issue_comments",
        "update_issue_comment",
    }
)
_OPERATION_FIELDS: dict[PublicationOperation, set[str]] = {
    "current_user": set(),
    "get_pull": set(),
    "list_issue_comments": {"max_pages", "newest_first"},
    "update_issue_comment": {"comment_id", "body"},
    "create_issue_comment": {"body"},
    "delete_issue_comment": {"comment_id"},
    "create_pull_request_review": {"commit_id", "body", "comments"},
    "list_pull_request_review_comments": {"max_pages"},
}


def _bounded_publication_gateway(token: str) -> GitHubIssueCommentGateway:
    """Keep one provider operation inside the internal client's deadline."""
    return GitHubIssueCommentGateway(
        token,
        request_timeout_seconds=10.0,
        max_attempts=1,
    )


def _positive(value: object, field: str, *, maximum: int | None = None) -> int:
    if type(value) is not int or value < 1 or (maximum is not None and value > maximum):
        raise GitHubGatewayProtocolError(f"{field} is invalid")
    return value


def _text(value: object, field: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise GitHubGatewayProtocolError(f"{field} is invalid")
    return value


def _comments(value: object) -> tuple[InlineReviewComment, ...]:
    if not isinstance(value, list):
        raise GitHubGatewayProtocolError("comments is invalid")
    raw_comments = cast(list[object], value)
    if not 1 <= len(raw_comments) <= 12:
        raise GitHubGatewayProtocolError("comments is invalid")
    comments: list[InlineReviewComment] = []
    for raw in raw_comments:
        if not isinstance(raw, Mapping):
            raise GitHubGatewayProtocolError("comments is invalid")
        item = cast(Mapping[str, object], raw)
        expected = {"path", "body", "line", "side", "start_line", "start_side"}
        if set(item) != expected:
            raise GitHubGatewayProtocolError("comments is invalid")
        side = item.get("side")
        start_side = item.get("start_side")
        if side not in {"LEFT", "RIGHT"} or start_side not in {None, "LEFT", "RIGHT"}:
            raise GitHubGatewayProtocolError("comments is invalid")
        start_line = item.get("start_line")
        comments.append(
            InlineReviewComment(
                path=_text(item.get("path"), "comments.path", maximum=500),
                body=_text(item.get("body"), "comments.body", maximum=100_000),
                line=_positive(item.get("line"), "comments.line"),
                side=cast(Literal["LEFT", "RIGHT"], side),
                start_line=(
                    None
                    if start_line is None
                    else _positive(start_line, "comments.start_line")
                ),
                start_side=cast(Literal["LEFT", "RIGHT"] | None, start_side),
            )
        )
    return tuple(comments)


@dataclass(frozen=True, slots=True)
class PublicationGatewayRequest:
    scope_kind: PublicationScopeKind
    scope_id: int
    lease_owner: str | None
    lease_generation: int | None
    operation: PublicationOperation
    max_pages: int | None = None
    newest_first: bool | None = None
    comment_id: int | None = None
    body: str | None = None
    commit_id: str | None = None
    comments: tuple[InlineReviewComment, ...] = ()

    @classmethod
    def from_mapping(
        cls, value: Mapping[str, object]
    ) -> "PublicationGatewayRequest":
        scope_kind = value.get("scope_kind")
        operation = value.get("operation")
        if scope_kind not in {
            "publication",
            "posted_publication",
            "failure_status",
        }:
            raise GitHubGatewayProtocolError("scope_kind is invalid")
        typed_scope_kind = cast(PublicationScopeKind, scope_kind)
        if operation not in _OPERATION_FIELDS:
            raise GitHubGatewayProtocolError("operation is invalid")
        typed_operation = operation
        authority_fields: set[str] = (
            set() if typed_scope_kind == "posted_publication" else _LEASE_FIELDS
        )
        if set(value) != (
            _BASE_FIELDS | authority_fields | _OPERATION_FIELDS[typed_operation]
        ):
            raise GitHubGatewayProtocolError("publication request has unexpected fields")
        max_pages: int | None = None
        newest_first: bool | None = None
        comment_id: int | None = None
        body: str | None = None
        commit_id: str | None = None
        comments: tuple[InlineReviewComment, ...] = ()
        if "max_pages" in value:
            max_pages = _positive(
                value.get("max_pages"),
                "max_pages",
                maximum=PUBLICATION_REQUEST_MAX_PAGES,
            )
        if "newest_first" in value:
            raw_newest_first = value.get("newest_first")
            if not isinstance(raw_newest_first, bool):
                raise GitHubGatewayProtocolError("newest_first is invalid")
            newest_first = raw_newest_first
        if "comment_id" in value:
            comment_id = _positive(value.get("comment_id"), "comment_id")
        if "body" in value:
            body = _text(value.get("body"), "body", maximum=100_000)
        if "commit_id" in value:
            commit_id = _text(
                value.get("commit_id"), "commit_id", maximum=128
            ).strip()
        if "comments" in value:
            comments = _comments(value.get("comments"))
        return cls(
            scope_kind=typed_scope_kind,
            scope_id=_positive(value.get("scope_id"), "scope_id"),
            lease_owner=(
                None
                if typed_scope_kind == "posted_publication"
                else _text(
                    value.get("lease_owner"), "lease_owner", maximum=200
                ).strip()
            ),
            lease_generation=(
                None
                if typed_scope_kind == "posted_publication"
                else _positive(
                    value.get("lease_generation"), "lease_generation"
                )
            ),
            operation=typed_operation,
            max_pages=max_pages,
            newest_first=newest_first,
            comment_id=comment_id,
            body=body,
            commit_id=commit_id,
            comments=comments,
        )


@dataclass(frozen=True, slots=True)
class _PublicationScope:
    provider_repository_id: int
    repository: str
    pr_number: int


class ReviewPublicationGateway:
    """Execute one fixed publication operation under current durable authority."""

    def __init__(
        self,
        *,
        postgres: PostgreSQLRuntime,
        tokens: GitHubAppTokenService,
        profile: str,
        github_factory: Callable[[str], GitHubIssueCommentGateway] | None = None,
    ) -> None:
        self._postgres = postgres
        self._tokens = tokens
        self._profile = profile.strip()
        if not self._profile:
            raise GitHubGatewayProtocolError("profile is required")
        self._github_factory = github_factory or _bounded_publication_gateway

    def execute(self, request: PublicationGatewayRequest) -> PublicationResult:
        scope = self._require_authority(request)

        if request.operation == "current_user":
            try:
                result: PublicationResult = self._tokens.app_bot_login()
            except GitHubAppTokenRetryable as exc:
                raise GitHubGatewayRetryable("app_identity_unavailable") from exc
            except GitHubAppTokenPermanent as exc:
                raise GitHubGatewayRejected("provider_authorization_denied") from exc
            self._require_authority(request)
            return result

        def operation(token: str) -> PublicationResult:
            github = self._github_factory(token)
            return _execute_provider(github, scope, request)

        result = self._provider(scope.provider_repository_id, operation)
        self._require_authority(request)
        return result

    def _require_authority(
        self, request: PublicationGatewayRequest
    ) -> _PublicationScope:
        try:
            with self._postgres.transaction() as connection:
                if request.scope_kind == "publication":
                    assert request.lease_owner is not None
                    assert request.lease_generation is not None
                    publication_id = PublicationId(request.scope_id)
                    publications.require_live_publication_lease(
                        connection,
                        publication_id=publication_id,
                        lease_owner=request.lease_owner,
                        lease_generation=request.lease_generation,
                    )
                    publication = publications.get_publication(connection, publication_id)
                    run_scope = review_runs.get_run_scope(
                        connection, publication.review_run_id
                    )
                    if (
                        publication.repository != run_scope.repository
                        or publication.pr_number != run_scope.pr_number
                    ):
                        raise GitHubGatewayRejected("publication_subject_mismatch")
                    scope = _PublicationScope(
                        provider_repository_id=run_scope.provider_repository_id,
                        repository=run_scope.repository,
                        pr_number=run_scope.pr_number,
                    )
                elif request.scope_kind == "posted_publication":
                    if request.operation not in _POSTED_PUBLICATION_OPERATIONS:
                        raise GitHubGatewayRejected(
                            "publication_operation_not_allowed"
                        )
                    publication = publications.get_publication(
                        connection, PublicationId(request.scope_id)
                    )
                    if publication.status is not PublicationStatus.POSTED:
                        raise GitHubGatewayRejected("publication_not_posted")
                    finalization = publications.publication_for_supersession(
                        connection,
                        superseding_publication_id=publication.id,
                    )
                    if finalization is None:
                        raise GitHubGatewayRejected(
                            "publication_finalization_complete"
                        )
                    if (
                        request.operation == "update_issue_comment"
                        and request.comment_id not in finalization.comment_ids
                    ):
                        raise GitHubGatewayRejected(
                            "publication_comment_not_authorized"
                        )
                    run_scope = review_runs.get_run_scope(
                        connection, publication.review_run_id
                    )
                    if (
                        publication.repository != run_scope.repository
                        or publication.pr_number != run_scope.pr_number
                    ):
                        raise GitHubGatewayRejected("publication_subject_mismatch")
                    scope = _PublicationScope(
                        provider_repository_id=run_scope.provider_repository_id,
                        repository=run_scope.repository,
                        pr_number=run_scope.pr_number,
                    )
                else:
                    assert request.lease_owner is not None
                    assert request.lease_generation is not None
                    run_id = ReviewRunId(request.scope_id)
                    target = review_runs.require_live_failure_status_lease(
                        connection,
                        run_id=run_id,
                        lease_owner=request.lease_owner,
                        lease_generation=request.lease_generation,
                    )
                    run_scope = review_runs.get_run_scope(connection, run_id)
                    if (
                        target.repository != run_scope.repository
                        or target.pr_number != run_scope.pr_number
                    ):
                        raise GitHubGatewayRejected("publication_subject_mismatch")
                    scope = _PublicationScope(
                        provider_repository_id=run_scope.provider_repository_id,
                        repository=run_scope.repository,
                        pr_number=run_scope.pr_number,
                    )
                github_app.authorize_review_publication(
                    connection,
                    scope.provider_repository_id,
                    profile_key=self._profile,
                )
                return scope
        except (publications.PublicationLeaseLost, review_runs.FailureStatusLeaseLost) as exc:
            raise GitHubGatewayRejected("publication_lease_lost") from exc
        except (publications.PublicationStoreError, review_runs.ReviewRunError) as exc:
            raise GitHubGatewayRejected("publication_authority_invalid") from exc
        except github_app.GitHubAppRepositoryUnauthorized as exc:
            raise GitHubGatewayRejected("repository_not_authorized") from exc

    def _provider(
        self,
        provider_repository_id: int,
        operation: Callable[[str], PublicationResult],
    ) -> PublicationResult:
        attempt = 0
        while True:
            try:
                token = self._tokens.token_for(
                    provider_repository_id, purpose="publication"
                )
                return operation(token.value)
            except GitHubAppTokenRetryable as exc:
                raise GitHubGatewayRetryable("token_exchange_unavailable") from exc
            except GitHubAppTokenPermanent as exc:
                raise GitHubGatewayRejected("provider_authorization_denied") from exc
            except github_app.GitHubAppRepositoryUnauthorized as exc:
                raise GitHubGatewayRejected("repository_not_authorized") from exc
            except GitHubPublicationError as exc:
                if exc.status == 401 and attempt == 0:
                    self._tokens.invalidate(
                        provider_repository_id, purpose="publication"
                    )
                    attempt += 1
                    continue
                reason = (
                    exc.code
                    if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", exc.code)
                    else "github_publication_failed"
                )
                if exc.status is not None and (
                    exc.status == 429 or exc.status >= 500
                ):
                    raise GitHubGatewayRetryable(reason) from exc
                if exc.code in {
                    "github_unreachable",
                    "github_response_too_large",
                    "github_invalid_json",
                }:
                    raise GitHubGatewayRetryable(reason) from exc
                raise GitHubGatewayRejected(reason) from exc


def _execute_provider(
    github: GitHubIssueCommentGateway,
    scope: _PublicationScope,
    request: PublicationGatewayRequest,
) -> PublicationResult:
    if request.operation == "current_user":
        raise GitHubGatewayProtocolError(
            "App identity must not use an installation token"
        )
    if request.operation == "get_pull":
        return github.get_pull_request(scope.repository, scope.pr_number)
    if request.operation == "list_issue_comments":
        assert request.max_pages is not None and request.newest_first is not None
        return github.list_issue_comments(
            scope.repository,
            scope.pr_number,
            max_pages=request.max_pages,
            newest_first=request.newest_first,
        )
    if request.operation == "update_issue_comment":
        assert request.comment_id is not None and request.body is not None
        return github.update_issue_comment(
            scope.repository, request.comment_id, request.body
        )
    if request.operation == "create_issue_comment":
        assert request.body is not None
        return github.create_issue_comment(
            scope.repository, scope.pr_number, request.body
        )
    if request.operation == "delete_issue_comment":
        assert request.comment_id is not None
        github.delete_issue_comment(scope.repository, request.comment_id)
        return None
    if request.operation == "create_pull_request_review":
        assert request.commit_id is not None and request.body is not None
        return github.create_pull_request_review(
            scope.repository,
            scope.pr_number,
            commit_id=request.commit_id,
            body=request.body,
            comments=request.comments,
        )
    assert request.max_pages is not None
    return github.list_pull_request_review_comments(
        scope.repository, scope.pr_number, max_pages=request.max_pages
    )


def result_mapping(result: PublicationResult) -> dict[str, object]:
    if isinstance(result, str):
        return {"kind": "current_user", "login": result}
    if isinstance(result, PullRequestState):
        return {
            "kind": "pull",
            "state": result.state,
            "draft": result.draft,
            "base_sha": result.base_sha,
            "head_sha": result.head_sha,
        }
    if isinstance(result, IssueComment):
        return {"kind": "issue_comment", **_issue_comment_mapping(result)}
    if isinstance(result, PullRequestReview):
        return {
            "kind": "pull_request_review",
            "review_id": result.review_id,
            "body": result.body,
            "author_login": result.author_login,
            "commit_id": result.commit_id,
            "state": result.state,
        }
    if result is None:
        return {"kind": "deleted"}
    if not result:
        return {"kind": "list", "items": []}
    first = result[0]
    if isinstance(first, IssueComment):
        return {
            "kind": "list",
            "items": [_issue_comment_mapping(item) for item in cast(Sequence[IssueComment], result)],
        }
    return {
        "kind": "list",
        "items": [
            {
                "comment_id": item.comment_id,
                "review_id": item.review_id,
                "body": item.body,
                "author_login": item.author_login,
                "path": item.path,
                "commit_id": item.commit_id,
                "line": item.line,
                "side": item.side,
                "start_line": item.start_line,
                "start_side": item.start_side,
            }
            for item in cast(Sequence[PullRequestReviewComment], result)
        ],
    }


def _issue_comment_mapping(comment: IssueComment) -> dict[str, object]:
    return {
        "comment_id": comment.comment_id,
        "body": comment.body,
        "author_login": comment.author_login,
    }
