"""Lease-bound GitHub App operations for deterministic review publication."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
import hmac
import re
from typing import Literal, cast

import psycopg
from psycopg.rows import TupleRow

from ..domain import failure_codes
from ..domain.publication import (
    CheckRunDelivery, IssueCommentDelivery, PublicationDomainError, PublicationId,
    PublicationPartStatus, PublicationPartType, PublicationStatus, SuggestionReviewDelivery,
)
from ..domain.review import ReviewPurpose, ReviewRunId
from ..postgres import findings, github_app, publications, review_runs
from ..publication.publish import (
    existing_part_comment, failure_status_body, failure_status_marker, failure_status_marker_run,
    issue_comments_by_part, reviews_with_exact_suggestions, suggestion_comments, supersession_bodies,
    target_failure,
)
from ..postgres.runtime import PostgreSQLRuntime
from .app_auth import (
    GitHubAppTokenPermanent,
    GitHubAppTokenRetryable,
    GitHubAppTokenService,
    GitHubAppTokenPurpose,
)
from .gateway import (
    GitHubGatewayProtocolError,
    GitHubGatewayRejected,
    GitHubGatewayRetryable,
)
from .publication import (
    CheckRun,
    CheckRunScan,
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
    "list_check_runs",
    "create_check_run",
    "update_check_run",
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
    | CheckRun
    | CheckRunScan
    | PullRequestState
    | list[IssueComment]
    | IssueComment
    | None
    | PullRequestReview
    | list[PullRequestReviewComment]
)

_BASE_FIELDS = {"scope_kind", "scope_id", "operation"}
_LEASE_FIELDS = {"lease_owner", "lease_generation"}
_FAILURE_STATUS_OPERATIONS = frozenset({
    "current_user", "get_pull", "list_issue_comments", "create_issue_comment", "update_issue_comment", "delete_issue_comment",
})
_WRITES = frozenset({
    "create_check_run", "update_check_run", "create_issue_comment", "update_issue_comment",
    "delete_issue_comment", "create_pull_request_review",
})
_POSTED_PUBLICATION_OPERATIONS: frozenset[PublicationOperation] = frozenset(
    {
        "list_issue_comments",
        "update_issue_comment",
    }
)
_OPERATION_FIELDS: dict[PublicationOperation, set[str]] = {
    "list_check_runs": {"max_pages"},
    "create_check_run": set(),
    "update_check_run": {"check_run_id", "cancelled"},
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
    check_run_id: int | None = None
    cancelled: bool = False
    finalization_token: str | None = field(default=None, repr=False)

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
            {"finalization_token"} if typed_scope_kind == "posted_publication" else _LEASE_FIELDS
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
        check_run_id = _positive(value.get("check_run_id"), "check_run_id") if "check_run_id" in value else None
        cancelled = value.get("cancelled", False)
        if type(cancelled) is not bool:
            raise GitHubGatewayProtocolError("cancelled is invalid")
        if "check_run" in typed_operation and typed_scope_kind != "publication":
            raise GitHubGatewayProtocolError("check operations require a publication lease")
        token = value.get("finalization_token")
        if typed_scope_kind == "posted_publication" and (
            not isinstance(token, str) or not re.fullmatch(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", token)
        ):
            raise GitHubGatewayProtocolError("finalization_token is invalid")
        return cls(
            check_run_id=check_run_id, cancelled=cancelled,
            finalization_token=token if isinstance(token, str) else None,
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
    check: CheckRunDelivery | None = None
    check_summary: str = ""
    purpose: ReviewPurpose = ReviewPurpose.CODE
    publication: publications.StoredPublication | None = None
    historical: publications.HistoricalPublication | None = None
    failure: review_runs.FailureStatusTarget | None = None
    stale_reason: str | None = None
    stored_cleanup_target: bool = False

    @property
    def next_part(self) -> publications.StoredPublicationPart | None:
        if self.publication is None:
            return None
        return next((part for part in self.publication.parts if part.status is not PublicationPartStatus.POSTED), None)


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
        authorized_body = _authorize_write(scope, request) if request.operation in _WRITES else None

        if request.operation == "current_user":
            try:
                result: PublicationResult = self._tokens.app_bot_login()
            except GitHubAppTokenRetryable as exc:
                raise GitHubGatewayRetryable("app_identity_unavailable", retry_at=exc.retry_at) from exc
            except GitHubAppTokenPermanent as exc:
                raise GitHubGatewayRejected("provider_authorization_denied") from exc
            self._require_authority(request)
            return result

        def operation(token: str) -> PublicationResult:
            github = self._github_factory(token)

            def before_write() -> None:
                pull = (
                    github.get_pull_request(scope.repository, scope.pr_number)
                    if request.scope_kind != "posted_publication" and request.operation != "delete_issue_comment"
                    else None
                )
                fresh = self._require_authority(request)
                if fresh.next_part != scope.next_part or fresh.stored_cleanup_target != scope.stored_cleanup_target:
                    raise GitHubGatewayRejected("publication_operation_not_allowed")
                if pull is not None and fresh.publication is not None:
                    publication = fresh.publication
                    stale = fresh.stale_reason or target_failure(
                        base_sha=publication.base_sha, head_sha=publication.head_sha, pull=pull,
                    )
                    if request.cancelled and stale is None:
                        raise GitHubGatewayRejected("publication_check_not_stale")
                    if not request.cancelled and stale is not None:
                        raise GitHubGatewayRejected(stale)
                if pull is not None and fresh.failure is not None and pull.state != "open":
                    raise GitHubGatewayRejected(failure_codes.PR_NOT_OPEN)

            if "check_run" in request.operation:
                check = scope.check
                if check is None:
                    raise GitHubGatewayRejected("documentation_check_not_prepared")
                app_id = self._tokens.app_identity().provider_app_id
                scan = github.list_check_runs(
                    scope.repository, head_sha=check.head_sha, name=check.name,
                    app_id=app_id, max_pages=request.max_pages or PUBLICATION_REQUEST_MAX_PAGES,
                )
                if request.operation == "list_check_runs":
                    return scan
                matches = [item for item in scan.check_runs if (
                    item.app_id == app_id and item.head_sha == check.head_sha
                    and item.name == check.name and item.external_id == check.external_id
                )]
                if not scan.complete or len(matches) > 1:
                    raise GitHubPublicationError("check_recovery_unresolved", retryable=True)
                if request.operation == "create_check_run":
                    if matches:
                        return matches[0]
                    before_write()
                    result = github.create_check_run(scope.repository, delivery=check, summary=scope.check_summary)
                else:
                    if not matches or matches[0].check_run_id != request.check_run_id:
                        raise GitHubGatewayRejected("publication_check_not_authorized")
                    before_write()
                    result = github.update_check_run(scope.repository, matches[0].check_run_id,
                        delivery=check, summary=scope.check_summary, cancelled=request.cancelled)
                if (result.app_id, result.head_sha, result.name, result.external_id) != (app_id, check.head_sha, check.name, check.external_id):
                    raise GitHubPublicationError("github_check_subject_mismatch")
                return result
            if request.operation in _WRITES:
                return _execute_write(github, scope, request, authorized_body, self._tokens.app_bot_login(), before_write)
            if request.operation == "list_issue_comments" and scope.historical is not None:
                assert request.max_pages is not None and request.newest_first is not None
                listed = github.list_issue_comments(scope.repository, scope.pr_number,
                    max_pages=request.max_pages, newest_first=request.newest_first)
                historical = scope.historical
                by_part = issue_comments_by_part(listed, author_login=self._tokens.app_bot_login(),
                    publication_key=historical.publication_key)
                return [item for number, comment_id in enumerate(historical.comment_ids, 1)
                        for item in by_part.get(number, ()) if item.comment_id == comment_id]
            return _execute_read(github, scope, request)

        result = self._provider(scope.provider_repository_id, operation,
            purpose="documentation_publication" if scope.purpose is ReviewPurpose.DOCUMENTATION or "check_run" in request.operation else "publication")
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
                    check: CheckRunDelivery | None = None
                    summary = ""
                    if "check_run" in request.operation:
                        if publication.purpose is not ReviewPurpose.DOCUMENTATION:
                            raise GitHubGatewayRejected("documentation_check_requires_docs_run")
                        primary = next((part for part in publication.parts if part.part_type is PublicationPartType.CHECK_RUN), None)
                        if primary is None or not isinstance(primary.delivery, CheckRunDelivery):
                            raise GitHubGatewayRejected("documentation_check_not_prepared")
                        check = primary.delivery
                        summary = check.summary
                        if request.operation != "list_check_runs":
                            for number in check.report_part_numbers:
                                report = next((part for part in publication.parts if part.part_number == number and part.part_type in {PublicationPartType.SUMMARY, PublicationPartType.CONTINUATION}), None)
                                if report is None or report.external_id is None:
                                    raise GitHubGatewayRejected("documentation_report_not_acknowledged")
                                summary += f"\n\n[Report part {number}](https://github.com/{publication.repository}/pull/{publication.pr_number}#issuecomment-{report.external_id})"
                    scope = _PublicationScope(
                        provider_repository_id=run_scope.provider_repository_id,
                        repository=run_scope.repository,
                        pr_number=run_scope.pr_number,
                        check=check, check_summary=summary, purpose=publication.purpose,
                        publication=publication,
                        stale_reason=(
                            _stale_finding_reason(connection, publication.review_run_id)
                            if request.operation in _WRITES and request.operation != "delete_issue_comment" else None
                        ),
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
                    if not finalization.finalization_token or not request.finalization_token or not hmac.compare_digest(
                        finalization.finalization_token, request.finalization_token
                    ):
                        raise GitHubGatewayRejected("publication_finalization_not_authorized")
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
                        historical=finalization, purpose=publication.purpose,
                    )
                else:
                    if request.operation not in _FAILURE_STATUS_OPERATIONS:
                        raise GitHubGatewayRejected("publication_operation_not_allowed")
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
                        purpose=run_scope.run.purpose,
                        failure=target,
                    )
                authorize = (github_app.authorize_documentation_review
                    if scope.purpose is ReviewPurpose.DOCUMENTATION
                    else github_app.authorize_review_publication)
                authorize(connection, scope.provider_repository_id, profile_key=self._profile)
                if request.operation == "delete_issue_comment":
                    targets = review_runs.failure_status_comments_for_pull_request(
                        connection, repository=scope.repository, pr_number=scope.pr_number, purpose=scope.purpose,
                        exclude_run_id=scope.failure.run_id if scope.failure else None,
                    )
                    scope = replace(scope, stored_cleanup_target=any(target.comment_id == request.comment_id for target in targets))
                return scope
        except (publications.PublicationLeaseLost, review_runs.FailureStatusLeaseLost) as exc:
            raise GitHubGatewayRejected("publication_lease_lost") from exc
        except (publications.PublicationStoreError, review_runs.ReviewRunError) as exc:
            raise GitHubGatewayRejected("publication_authority_invalid") from exc
        except github_app.GitHubAppRepositoryUnauthorized as exc:
            raise GitHubGatewayRejected(exc.reason) from exc

    def _provider(
        self,
        provider_repository_id: int,
        operation: Callable[[str], PublicationResult],
        *, purpose: GitHubAppTokenPurpose = "publication",
    ) -> PublicationResult:
        attempt = 0
        while True:
            try:
                token = self._tokens.token_for(
                    provider_repository_id, purpose=purpose
                )
                return operation(token.value)
            except GitHubAppTokenRetryable as exc:
                raise GitHubGatewayRetryable("token_exchange_unavailable", retry_at=exc.retry_at) from exc
            except GitHubAppTokenPermanent as exc:
                raise GitHubGatewayRejected("provider_authorization_denied") from exc
            except github_app.GitHubAppRepositoryUnauthorized as exc:
                raise GitHubGatewayRejected(exc.reason) from exc
            except GitHubPublicationError as exc:
                if exc.status == 401 and attempt == 0:
                    self._tokens.invalidate(
                        provider_repository_id, purpose=purpose
                    )
                    attempt += 1
                    continue
                reason = (
                    exc.code
                    if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", exc.code)
                    else "github_publication_failed"
                )
                if exc.retryable:
                    raise GitHubGatewayRetryable(reason, retry_at=exc.retry_at) from exc
                raise GitHubGatewayRejected(reason) from exc


def _stale_finding_reason(connection: psycopg.Connection[TupleRow], run_id: ReviewRunId) -> str | None:
    if not findings.finding_relationships_are_current(connection, run_id=run_id):
        return failure_codes.FINDING_DECISIONS_CHANGED
    if not findings.finding_groups_are_current(connection, run_id=run_id):
        return failure_codes.FINDING_GROUPS_CHANGED
    return None


def _authorize_write(scope: _PublicationScope, request: PublicationGatewayRequest) -> str | None:
    """Bind the operation and every supplied payload field to its durable delivery."""
    part = scope.next_part
    if request.operation == "delete_issue_comment":
        if scope.failure is None and (scope.publication is None or part is not None):
            raise GitHubGatewayRejected("publication_operation_not_allowed")
        return None
    if "check_run" in request.operation:
        if scope.check is None or (not request.cancelled and (part is None or part.delivery != scope.check)):
            raise GitHubGatewayRejected("publication_operation_not_allowed")
        return None
    if request.operation == "create_pull_request_review":
        if (scope.publication is None or part is None or not isinstance(part.delivery, SuggestionReviewDelivery)
                or request.commit_id != scope.publication.head_sha or request.comments != suggestion_comments(part.delivery)
                or request.body != part.delivery.body):
            raise GitHubGatewayRejected("publication_review_not_authorized")
        return part.delivery.body
    if request.operation not in {"create_issue_comment", "update_issue_comment"}:
        raise GitHubGatewayRejected("publication_operation_not_allowed")
    if scope.historical is not None:
        if request.operation != "update_issue_comment" or request.comment_id not in scope.historical.comment_ids:
            raise GitHubGatewayRejected("publication_comment_not_authorized")
        try:
            body = supersession_bodies(scope.historical)[scope.historical.comment_ids.index(request.comment_id)]
        except PublicationDomainError as exc:
            raise GitHubGatewayRejected(exc.code) from exc
    elif scope.failure is not None:
        failure = scope.failure
        body = failure_status_body(failure.run_id, failure.head_sha, failure.failure_code, purpose=scope.purpose)
    elif part is not None and isinstance(part.delivery, IssueCommentDelivery):
        body = part.delivery.body
    else:
        raise GitHubGatewayRejected("publication_operation_not_allowed")
    if request.body != body:
        raise GitHubGatewayRejected("publication_body_not_authorized")
    return body


def _execute_write(
    github: GitHubIssueCommentGateway, scope: _PublicationScope, request: PublicationGatewayRequest,
    body: str | None, bot_login: str, before_write: Callable[[], None],
) -> PublicationResult:
    """Reconcile only App-owned objects on this PR, using the frozen write payload."""
    repository, pr = scope.repository, scope.pr_number
    if request.operation == "delete_issue_comment":
        assert request.comment_id is not None
        target = github.issue_comment_on_pull(repository, pr, request.comment_id)
        marker_run = failure_status_marker_run(target.body, scope.purpose)
        if (target.author_login.casefold() != bot_login.casefold()
                or (marker_run is None and not scope.stored_cleanup_target)
                or (scope.failure is not None and marker_run == scope.failure.run_id)):
            raise GitHubGatewayRejected("publication_comment_not_authorized")
        before_write()
        github.delete_issue_comment(repository, target.comment_id)
        return None

    assert body is not None
    if scope.historical is not None:
        assert request.comment_id is not None
        historical = scope.historical
        target = github.issue_comment_on_pull(repository, pr, request.comment_id)
        number = historical.comment_ids.index(request.comment_id) + 1
        if number not in issue_comments_by_part([target], author_login=bot_login, publication_key=historical.publication_key):
            raise GitHubGatewayRejected("publication_comment_not_authorized")
        if target.body == body:
            return target
        before_write()
        return github.update_issue_comment(repository, target.comment_id, body)

    publication = scope.publication
    part = scope.next_part
    if request.operation == "create_pull_request_review":
        assert publication is not None and part is not None and isinstance(part.delivery, SuggestionReviewDelivery)
        comments = suggestion_comments(part.delivery)
        reviews = github.list_pull_request_review_comments(repository, pr, max_pages=PUBLICATION_REQUEST_MAX_PAGES)
        matches = reviews_with_exact_suggestions(reviews, comments, author_login=bot_login, head_sha=publication.head_sha,
                                                 publication_key=publication.plan.publication_key)
        if matches:
            return PullRequestReview(matches[-1], body, bot_login, publication.head_sha, "COMMENTED")
        before_write()
        return github.create_pull_request_review(repository, pr, commit_id=publication.head_sha, body=body, comments=comments)

    listed = github.list_issue_comments(repository, pr, max_pages=PUBLICATION_REQUEST_MAX_PAGES, newest_first=True)
    if scope.failure is not None:
        marker = failure_status_marker(scope.failure.run_id, scope.failure.head_sha, purpose=scope.purpose)
        existing = next((item for item in listed if item.author_login.casefold() == bot_login.casefold()
                         and item.body.rstrip().endswith(marker)), None)
    else:
        assert publication is not None and part is not None
        by_part = issue_comments_by_part(listed, author_login=bot_login, publication_key=publication.plan.publication_key)
        existing = existing_part_comment(by_part.get(part.part_number, ()), body)
    if request.operation == "update_issue_comment" and (existing is None or existing.comment_id != request.comment_id):
        raise GitHubGatewayRejected("publication_comment_not_authorized")
    if existing is not None:
        if existing.body == body:
            return existing
        if request.operation != "update_issue_comment":
            raise GitHubGatewayRejected("publication_comment_not_authorized")
        try:
            before_write()
        except GitHubGatewayRejected as exc:
            # Failure notices written just before a close are recorded without editing.
            if scope.failure is not None and exc.reason == failure_codes.PR_NOT_OPEN:
                return existing
            raise
        return github.update_issue_comment(repository, existing.comment_id, body)
    before_write()
    return github.create_issue_comment(repository, pr, body)


def _execute_read(
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
    assert request.operation == "list_pull_request_review_comments" and request.max_pages is not None
    return github.list_pull_request_review_comments(
        scope.repository, scope.pr_number, max_pages=request.max_pages
    )


def result_mapping(result: PublicationResult) -> dict[str, object]:
    if isinstance(result, CheckRun):
        return {"kind": "check_run", **_check_mapping(result)}
    if isinstance(result, CheckRunScan):
        return {"kind": "check_runs", "check_runs": [_check_mapping(item) for item in result.check_runs], "complete": result.complete, "app_id": result.app_id}
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


def _check_mapping(check: CheckRun) -> dict[str, object]:
    return {"check_run_id": check.check_run_id, "name": check.name, "head_sha": check.head_sha,
            "external_id": check.external_id, "app_id": check.app_id,
            "status": check.status, "conclusion": check.conclusion}
