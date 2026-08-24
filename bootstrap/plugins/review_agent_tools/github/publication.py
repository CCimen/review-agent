"""Concrete GitHub delivery adapter for review publication."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol, cast

_API_ROOT = "https://api.github.com"
_MAX_ATTEMPTS = 3
_RETRYABLE_STATUS = frozenset({502, 503, 504})
_RETRYABLE_METHODS = frozenset({"GET", "PATCH"})
_READ_TOKEN_FALLBACK_STATUS = frozenset({401, 403, 404})
_AMBIGUOUS_REVIEW_CREATE_CODES = frozenset(
    {
        "github_unreachable",
        "github_response_too_large",
        "github_invalid_json",
        "github_bad_review_response",
    }
)
GitHubAuthPurpose = Literal["read", "write"]
ReviewCommentSide = Literal["LEFT", "RIGHT"]


@dataclass(frozen=True)
class PullRequestState:
    state: str
    draft: bool
    base_sha: str
    head_sha: str


@dataclass(frozen=True)
class IssueComment:
    comment_id: int
    body: str
    author_login: str = ""


@dataclass(frozen=True)
class InlineReviewComment:
    """One line or contiguous line range in a pull-request review."""

    path: str
    body: str
    line: int
    side: ReviewCommentSide
    start_line: int | None = None
    start_side: ReviewCommentSide | None = None


@dataclass(frozen=True)
class PullRequestReview:
    review_id: int
    body: str
    author_login: str
    commit_id: str
    state: str


@dataclass(frozen=True)
class PullRequestReviewComment:
    comment_id: int
    review_id: int
    body: str
    author_login: str
    path: str
    commit_id: str
    line: int | None
    side: ReviewCommentSide | None
    start_line: int | None
    start_side: ReviewCommentSide | None


class GitHubPublicationGateway(Protocol):
    def current_user_login(self) -> str: ...

    def get_pull_request(self, repository: str, pr_number: int) -> PullRequestState: ...

    def list_issue_comments(
        self, repository: str, issue_number: int, *, max_pages: int = 3
    ) -> list[IssueComment]: ...

    def update_issue_comment(
        self, repository: str, comment_id: int, body: str
    ) -> IssueComment: ...

    def create_issue_comment(
        self, repository: str, issue_number: int, body: str
    ) -> IssueComment: ...

    def delete_issue_comment(self, repository: str, comment_id: int) -> None: ...

    def create_pull_request_review(
        self,
        repository: str,
        pr_number: int,
        *,
        commit_id: str,
        body: str,
        comments: Sequence[InlineReviewComment],
    ) -> PullRequestReview: ...

    def list_pull_request_review_comments(
        self, repository: str, pr_number: int, *, max_pages: int = 3
    ) -> list[PullRequestReviewComment]: ...


class GitHubPublicationError(RuntimeError):
    def __init__(
        self, code: str, *, status: int | None = None, operation: str = ""
    ) -> None:
        super().__init__(code)
        self.code = code
        self.status = status
        self.operation = operation


def _owner_repo(repository: str) -> str:
    return urllib.parse.quote(repository, safe="/")


def _json_object(value: Any, code: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GitHubPublicationError(code)
    return cast(dict[str, Any], value)


def _json_list(value: Any, code: str) -> list[Any]:
    if not isinstance(value, list):
        raise GitHubPublicationError(code)
    return cast(list[Any], value)


def _positive_int(value: object, code: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise GitHubPublicationError(code)
    return value


def _nonempty_string(value: object, code: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GitHubPublicationError(code)
    return value


def _optional_positive_int(value: object, code: str) -> int | None:
    if value is None:
        return None
    return _positive_int(value, code)


def _optional_review_side(value: object, code: str) -> ReviewCommentSide | None:
    if value is None:
        return None
    if value not in {"LEFT", "RIGHT"}:
        raise GitHubPublicationError(code)
    return cast(ReviewCommentSide, value)


def _github_failure_code(status: int, operation: str) -> str:
    suffix = f"_{operation}" if operation else ""
    if status in {401, 403, 404}:
        return f"github_{status}{suffix}"
    return f"github_http_{status}{suffix}"


def ambiguous_review_create_failure(error: GitHubPublicationError) -> bool:
    return error.operation == "create_pull_request_review" and (
        error.status in _RETRYABLE_STATUS
        or error.code in _AMBIGUOUS_REVIEW_CREATE_CODES
    )


def _inline_review_comment_payload(
    comment: InlineReviewComment,
) -> dict[str, object]:
    if not comment.path.strip() or comment.path != comment.path.strip():
        raise GitHubPublicationError("invalid_review_comment_path")
    if not comment.body.strip():
        raise GitHubPublicationError("invalid_review_comment_body")
    line = _positive_int(comment.line, "invalid_review_comment_line")
    if comment.side not in {"LEFT", "RIGHT"}:
        raise GitHubPublicationError("invalid_review_comment_side")

    payload: dict[str, object] = {
        "path": comment.path,
        "body": comment.body,
        "line": line,
        "side": comment.side,
    }
    if comment.start_line is None:
        if comment.start_side is not None:
            raise GitHubPublicationError("invalid_review_comment_range")
        return payload

    start_line = _positive_int(comment.start_line, "invalid_review_comment_start_line")
    if start_line >= line or comment.start_side not in {"LEFT", "RIGHT"}:
        raise GitHubPublicationError("invalid_review_comment_range")
    payload["start_line"] = start_line
    payload["start_side"] = comment.start_side
    return payload


def _review_comment_from_json(value: object) -> PullRequestReviewComment:
    code = "github_bad_review_comments_response"
    root = _json_object(value, code)
    user = _json_object(root.get("user"), code)
    return PullRequestReviewComment(
        comment_id=_positive_int(root.get("id"), code),
        review_id=_positive_int(root.get("pull_request_review_id"), code),
        body=_nonempty_string(root.get("body"), code),
        author_login=_nonempty_string(user.get("login"), code),
        path=_nonempty_string(root.get("path"), code),
        commit_id=_nonempty_string(root.get("commit_id"), code),
        line=_optional_positive_int(root.get("line"), code),
        side=_optional_review_side(root.get("side"), code),
        start_line=_optional_positive_int(root.get("start_line"), code),
        start_side=_optional_review_side(root.get("start_side"), code),
    )


class GitHubIssueCommentGateway:
    def __init__(self, token: str, *, read_token: str = "") -> None:
        token = token.strip()
        if not token:
            raise GitHubPublicationError("missing_publish_token")
        self._token = token
        self._read_token = read_token.strip()
        self._current_user_login: str | None = None

    def _tokens_for(self, auth: GitHubAuthPurpose) -> tuple[str, ...]:
        if auth == "read" and self._read_token and self._read_token != self._token:
            return (self._read_token, self._token)
        return (self._token,)

    def _request_json(
        self,
        method: str,
        endpoint: str,
        *,
        payload: dict[str, object] | None = None,
        max_bytes: int = 2_000_000,
        auth: GitHubAuthPurpose = "write",
        operation: str = "",
    ) -> Any:
        if not endpoint.startswith("/") or "//" in endpoint:
            raise GitHubPublicationError("invalid_github_endpoint")
        tokens = self._tokens_for(auth)
        last_error: GitHubPublicationError | None = None
        for index, token in enumerate(tokens):
            try:
                return self._request_json_with_token(
                    method,
                    endpoint,
                    token=token,
                    payload=payload,
                    max_bytes=max_bytes,
                    operation=operation,
                )
            except GitHubPublicationError as exc:
                last_error = exc
                has_fallback = index + 1 < len(tokens)
                if has_fallback and exc.status in _READ_TOKEN_FALLBACK_STATUS:
                    continue
                raise
        if last_error is not None:
            raise last_error
        raise GitHubPublicationError("github_unreachable", operation=operation)

    def _request_json_with_token(
        self,
        method: str,
        endpoint: str,
        *,
        token: str,
        payload: dict[str, object] | None,
        max_bytes: int,
        operation: str,
    ) -> Any:
        body = None
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "Hermes-PR-Review-Publisher/1.0",
            "X-GitHub-Api-Version": "2022-11-28",
            "Authorization": f"Bearer {token}",
        }
        if payload is not None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{_API_ROOT}{endpoint}", data=body, headers=headers, method=method
        )
        for attempt in range(_MAX_ATTEMPTS):
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    data = response.read(max_bytes + 1)
            except urllib.error.HTTPError as exc:
                exc.close()
                if (
                    method in _RETRYABLE_METHODS
                    and exc.code in _RETRYABLE_STATUS
                    and attempt + 1 < _MAX_ATTEMPTS
                ):
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise GitHubPublicationError(
                    _github_failure_code(exc.code, operation),
                    status=exc.code,
                    operation=operation,
                ) from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                raise GitHubPublicationError(
                    "github_unreachable", operation=operation
                ) from exc
            if len(data) > max_bytes:
                raise GitHubPublicationError(
                    "github_response_too_large", operation=operation
                )
            if method == "DELETE" and not data:
                return {}
            try:
                return json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise GitHubPublicationError(
                    "github_invalid_json", operation=operation
                ) from exc
        raise GitHubPublicationError("github_unreachable", operation=operation)

    def get_pull_request(self, repository: str, pr_number: int) -> PullRequestState:
        root = _json_object(
            self._request_json(
                "GET",
                f"/repos/{_owner_repo(repository)}/pulls/{pr_number}",
                auth="read",
                operation="get_pull_request",
            ),
            "github_bad_pr_response",
        )
        base = _json_object(root.get("base"), "github_bad_pr_response")
        head = _json_object(root.get("head"), "github_bad_pr_response")
        return PullRequestState(
            state=str(root.get("state", "")),
            draft=bool(root.get("draft")),
            base_sha=str(base.get("sha", "")).lower(),
            head_sha=str(head.get("sha", "")).lower(),
        )

    def current_user_login(self) -> str:
        if self._current_user_login is None:
            root = _json_object(
                self._request_json(
                    "GET",
                    "/user",
                    auth="write",
                    operation="get_authenticated_user",
                ),
                "github_bad_user_response",
            )
            login = str(root.get("login", "")).strip()
            if not login:
                raise GitHubPublicationError("github_bad_user_response")
            self._current_user_login = login
        return self._current_user_login

    def list_issue_comments(
        self, repository: str, issue_number: int, *, max_pages: int = 3
    ) -> list[IssueComment]:
        comments: list[IssueComment] = []
        for page in range(1, max_pages + 1):
            page_items = _json_list(
                self._request_json(
                    "GET",
                    f"/repos/{_owner_repo(repository)}/issues/{issue_number}/comments"
                    f"?per_page=100&page={page}",
                    auth="read",
                    operation="list_issue_comments",
                ),
                "github_bad_comments_response",
            )
            for item in page_items:
                if isinstance(item, dict):
                    comment = cast(Mapping[str, object], item)
                    raw_id = comment.get("id")
                    comment_id = (
                        raw_id
                        if isinstance(raw_id, int) and not isinstance(raw_id, bool)
                        else 0
                    )
                    comments.append(
                        IssueComment(
                            comment_id=comment_id,
                            body=str(comment.get("body", "")),
                            author_login=str(
                                _json_object(
                                    comment.get("user"), "github_bad_comments_response"
                                ).get("login", "")
                            ),
                        )
                    )
            if len(page_items) < 100:
                break
        return comments

    def update_issue_comment(
        self, repository: str, comment_id: int, body: str
    ) -> IssueComment:
        root = _json_object(
            self._request_json(
                "PATCH",
                f"/repos/{_owner_repo(repository)}/issues/comments/{comment_id}",
                payload={"body": body},
                auth="write",
                operation="update_issue_comment",
            ),
            "github_bad_comment_response",
        )
        user = _json_object(root.get("user"), "github_bad_comment_response")
        return IssueComment(
            comment_id=int(root.get("id", 0)),
            body=str(root.get("body", "")),
            author_login=str(user.get("login", "")),
        )

    def create_issue_comment(
        self, repository: str, issue_number: int, body: str
    ) -> IssueComment:
        root = _json_object(
            self._request_json(
                "POST",
                f"/repos/{_owner_repo(repository)}/issues/{issue_number}/comments",
                payload={"body": body},
                auth="write",
                operation="create_issue_comment",
            ),
            "github_bad_comment_response",
        )
        user = _json_object(root.get("user"), "github_bad_comment_response")
        return IssueComment(
            comment_id=int(root.get("id", 0)),
            body=str(root.get("body", "")),
            author_login=str(user.get("login", "")),
        )

    def delete_issue_comment(self, repository: str, comment_id: int) -> None:
        self._request_json(
            "DELETE",
            f"/repos/{_owner_repo(repository)}/issues/comments/{comment_id}",
            max_bytes=0,
            auth="write",
            operation="delete_issue_comment",
        )

    def create_pull_request_review(
        self,
        repository: str,
        pr_number: int,
        *,
        commit_id: str,
        body: str,
        comments: Sequence[InlineReviewComment],
    ) -> PullRequestReview:
        if not commit_id.strip() or commit_id != commit_id.strip():
            raise GitHubPublicationError("invalid_review_commit_id")
        if not body.strip():
            raise GitHubPublicationError("invalid_review_body")
        if not comments:
            raise GitHubPublicationError("review_comments_required")
        comment_payloads = [
            _inline_review_comment_payload(comment) for comment in comments
        ]
        operation = "create_pull_request_review"
        try:
            root = _json_object(
                self._request_json(
                    "POST",
                    f"/repos/{_owner_repo(repository)}/pulls/{pr_number}/reviews",
                    payload={
                        "commit_id": commit_id,
                        "body": body,
                        "event": "COMMENT",
                        "comments": comment_payloads,
                    },
                    auth="write",
                    operation=operation,
                ),
                "github_bad_review_response",
            )
            user = _json_object(root.get("user"), "github_bad_review_response")
            response_body = _nonempty_string(
                root.get("body"), "github_bad_review_response"
            )
            response_commit_id = _nonempty_string(
                root.get("commit_id"), "github_bad_review_response"
            )
            if (
                response_body != body
                or response_commit_id.casefold() != commit_id.casefold()
            ):
                raise GitHubPublicationError("github_bad_review_response")
            return PullRequestReview(
                review_id=_positive_int(root.get("id"), "github_bad_review_response"),
                body=response_body,
                author_login=_nonempty_string(
                    user.get("login"), "github_bad_review_response"
                ),
                commit_id=response_commit_id,
                state=_nonempty_string(root.get("state"), "github_bad_review_response"),
            )
        except GitHubPublicationError as exc:
            if exc.operation:
                raise
            raise GitHubPublicationError(
                exc.code, status=exc.status, operation=operation
            ) from exc

    def list_pull_request_review_comments(
        self, repository: str, pr_number: int, *, max_pages: int = 3
    ) -> list[PullRequestReviewComment]:
        comments: list[PullRequestReviewComment] = []
        for page in range(1, max_pages + 1):
            page_items = _json_list(
                self._request_json(
                    "GET",
                    f"/repos/{_owner_repo(repository)}/pulls/{pr_number}/comments"
                    f"?per_page=100&page={page}&sort=created&direction=desc",
                    auth="read",
                    operation="list_pull_request_review_comments",
                ),
                "github_bad_review_comments_response",
            )
            comments.extend(_review_comment_from_json(item) for item in page_items)
            if len(page_items) < 100:
                break
        return comments
