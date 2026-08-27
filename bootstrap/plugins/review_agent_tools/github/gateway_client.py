"""Typed internal client for the closed Review Agent GitHub gateway."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Literal, cast
import urllib.error
import urllib.parse
import urllib.request

from .. import capacity, changed_files
from .gateway import (
    ACKNOWLEDGE_FEEDBACK_PATH,
    AUTHORIZE_FEEDBACK_DELIVERY_PATH,
    AUTHORIZE_REVIEW_DELIVERY_PATH,
    OPERATOR_SMOKE_PATH,
    OPERATOR_STATUS_PATH,
    READ_REVIEW_SOURCE_PATH,
    AuthorizedFeedback,
    AuthorizedReviewSnapshot,
    FeedbackAcknowledgementStatus,
    GitHubGatewayProtocolError,
    GitHubGatewayRejected,
    GitHubGatewayRetryable,
    OperatorAppStatus,
    OperatorSmokeResult,
)
from .source import (
    GitHubSourceError,
    ReviewFilePage,
    ReviewPullSource,
    ReviewSourceBytes,
)
from .publication import (
    GitHubPublicationAuthorityLost,
    GitHubPublicationError,
    InlineReviewComment,
    IssueComment,
    PullRequestReview,
    PullRequestReviewComment,
    PullRequestState,
    PROVIDER_RESPONSE_MAX_BYTES,
    PUBLICATION_DEFAULT_MAX_PAGES,
    PUBLICATION_REQUEST_MAX_PAGES,
)
from .publication_gateway import EXECUTE_REVIEW_PUBLICATION_PATH


_MAX_RESPONSE_BYTES = 65_536
_MAX_CHANGED_FILES_RESPONSE_BYTES = (
    (changed_files.ENUMERATION_MAX_BYTES + 2) // 3
) * 4 + 65_536
_MAX_DIFF_RESPONSE_BYTES = ((1_000_000 + 2) // 3) * 4 + 65_536
_MAX_FILE_RESPONSE_BYTES = capacity.DEFAULT_RESULT_MAX_CHARS * 12 + 65_536
_MAX_PUBLICATION_RESPONSE_BYTES = (
    PUBLICATION_REQUEST_MAX_PAGES * PROVIDER_RESPONSE_MAX_BYTES + 65_536
)
_REQUEST_TIMEOUT_SECONDS = 90.0
_FAILURE_REASON_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_PROVIDER_STATUS_RE = re.compile(r"^github_(?:http_)?(?P<status>[1-5][0-9]{2})(?:_|$)")


def _base_url(value: str) -> str:
    normalized = value.strip()
    try:
        parsed = urllib.parse.urlsplit(normalized)
    except ValueError as exc:
        raise GitHubGatewayProtocolError("gateway URL is invalid") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise GitHubGatewayProtocolError("gateway URL must be one HTTP origin")
    return f"{parsed.scheme}://{parsed.netloc}"


class ReviewGitHubGatewayClient:
    """Call only the gateway's fixed review authorization and source operations."""

    def __init__(
        self,
        base_url: str,
        *,
        opener: urllib.request.OpenerDirector | None = None,
        operator_key: str | None = None,
    ) -> None:
        self._base_url = _base_url(base_url)
        self._opener = opener or urllib.request.build_opener()
        self._operator_key = operator_key.strip() if operator_key is not None else None
        if self._operator_key == "":
            raise GitHubGatewayProtocolError("operator key must not be empty")
        if self._operator_key is not None:
            try:
                self._operator_key.encode("ascii")
            except UnicodeEncodeError as exc:
                raise GitHubGatewayProtocolError(
                    "operator key must contain ASCII characters"
                ) from exc

    def operator_status(self) -> OperatorAppStatus:
        decoded = self._operator_request("GET", OPERATOR_STATUS_PATH)
        return OperatorAppStatus.from_mapping(decoded)

    def operator_smoke(
        self, *, repository: str, pr_number: int
    ) -> OperatorSmokeResult:
        decoded = self._operator_request(
            "POST",
            OPERATOR_SMOKE_PATH,
            {"pr_number": pr_number, "repository": repository},
        )
        return OperatorSmokeResult.from_mapping(decoded)

    def authorize_review_delivery(
        self,
        *,
        delivery_id: int,
        lease_owner: str,
        lease_generation: int,
    ) -> AuthorizedReviewSnapshot:
        decoded = self._post(
            AUTHORIZE_REVIEW_DELIVERY_PATH,
            {
                "delivery_id": delivery_id,
                "lease_owner": lease_owner,
                "lease_generation": lease_generation,
            },
        )
        return AuthorizedReviewSnapshot.from_mapping(decoded)

    def authorize_feedback_delivery(
        self,
        *,
        delivery_id: int,
        lease_owner: str,
        lease_generation: int,
    ) -> AuthorizedFeedback:
        decoded = self._post(
            AUTHORIZE_FEEDBACK_DELIVERY_PATH,
            {
                "delivery_id": delivery_id,
                "lease_owner": lease_owner,
                "lease_generation": lease_generation,
            },
        )
        return AuthorizedFeedback.from_mapping(decoded)

    def acknowledge_feedback(
        self,
        *,
        delivery_id: int,
        lease_owner: str,
        lease_generation: int,
        status: FeedbackAcknowledgementStatus,
    ) -> bool:
        decoded = self._post(
            ACKNOWLEDGE_FEEDBACK_PATH,
            {
                "delivery_id": delivery_id,
                "lease_owner": lease_owner,
                "lease_generation": lease_generation,
                "status": status,
            },
        )
        if set(decoded) != {"acknowledged"} or decoded.get("acknowledged") is not True:
            raise GitHubGatewayProtocolError(
                "gateway response fields do not match the feedback acknowledgement"
            )
        return True

    def for_publication(
        self,
        *,
        publication_id: int,
        lease_owner: str,
        lease_generation: int,
    ) -> "AuthorizedPublicationGateway":
        return AuthorizedPublicationGateway(
            self,
            scope_kind="publication",
            scope_id=publication_id,
            lease_owner=lease_owner,
            lease_generation=lease_generation,
        )

    def for_failure_status(
        self,
        *,
        run_id: int,
        lease_owner: str,
        lease_generation: int,
    ) -> "AuthorizedPublicationGateway":
        return AuthorizedPublicationGateway(
            self,
            scope_kind="failure_status",
            scope_id=run_id,
            lease_owner=lease_owner,
            lease_generation=lease_generation,
        )

    def for_posted_publication(
        self, *, publication_id: int
    ) -> "AuthorizedPublicationGateway":
        return AuthorizedPublicationGateway(
            self,
            scope_kind="posted_publication",
            scope_id=publication_id,
        )

    def execute_publication_operation(
        self, payload: dict[str, object]
    ) -> dict[str, object]:
        """Execute one operation whose repository authority comes from its lease."""
        return self._post(
            EXECUTE_REVIEW_PUBLICATION_PATH,
            payload,
            max_response_bytes=_MAX_PUBLICATION_RESPONSE_BYTES,
        )

    def get_review_pull(
        self,
        *,
        run_id: int,
        job_id: int,
        lease_generation: int,
    ) -> ReviewPullSource:
        decoded = self._post(
            READ_REVIEW_SOURCE_PATH,
            {
                "operation": "pull",
                "run_id": run_id,
                "job_id": job_id,
                "lease_generation": lease_generation,
            },
        )
        try:
            return ReviewPullSource.from_mapping(decoded)
        except GitHubSourceError as exc:
            raise GitHubGatewayProtocolError(str(exc)) from exc

    def get_changed_files_page(
        self,
        *,
        run_id: int,
        job_id: int,
        lease_generation: int,
        per_page: int,
        page: int,
    ) -> ReviewSourceBytes:
        decoded = self._post(
            READ_REVIEW_SOURCE_PATH,
            {
                "operation": "changed_files",
                "run_id": run_id,
                "job_id": job_id,
                "lease_generation": lease_generation,
                "per_page": per_page,
                "page": page,
            },
            max_response_bytes=_MAX_CHANGED_FILES_RESPONSE_BYTES,
        )
        return self._source_bytes(decoded)

    def get_review_diff(
        self,
        *,
        run_id: int,
        job_id: int,
        lease_generation: int,
    ) -> ReviewSourceBytes:
        decoded = self._post(
            READ_REVIEW_SOURCE_PATH,
            {
                "operation": "diff",
                "run_id": run_id,
                "job_id": job_id,
                "lease_generation": lease_generation,
            },
            max_response_bytes=_MAX_DIFF_RESPONSE_BYTES,
        )
        return self._source_bytes(decoded)

    def get_review_file_page(
        self,
        *,
        run_id: int,
        job_id: int,
        lease_generation: int,
        path: str,
        side: str,
        start_line: int,
        max_lines: int,
        max_chars: int,
    ) -> ReviewFilePage:
        decoded = self._post(
            READ_REVIEW_SOURCE_PATH,
            {
                "operation": "file",
                "run_id": run_id,
                "job_id": job_id,
                "lease_generation": lease_generation,
                "path": path,
                "side": side,
                "start_line": start_line,
                "max_lines": max_lines,
                "max_chars": max_chars,
            },
            max_response_bytes=_MAX_FILE_RESPONSE_BYTES,
        )
        try:
            return ReviewFilePage.from_mapping(decoded)
        except GitHubSourceError as exc:
            raise GitHubGatewayProtocolError(str(exc)) from exc

    @staticmethod
    def _source_bytes(decoded: dict[str, object]) -> ReviewSourceBytes:
        try:
            return ReviewSourceBytes.from_mapping(decoded)
        except GitHubSourceError as exc:
            raise GitHubGatewayProtocolError(str(exc)) from exc

    def _post(
        self,
        path: str,
        payload_value: dict[str, object],
        max_response_bytes: int = _MAX_RESPONSE_BYTES,
    ) -> dict[str, object]:
        return self._request(
            "POST",
            path,
            payload_value,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "Review-Agent-GitHub-Events/1.0",
            },
            max_response_bytes=max_response_bytes,
        )

    def _operator_request(
        self,
        method: Literal["GET", "POST"],
        path: str,
        payload_value: dict[str, object] | None = None,
    ) -> dict[str, object]:
        if self._operator_key is None:
            raise GitHubGatewayProtocolError("operator key is required")
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._operator_key}",
            "User-Agent": "Review-Agent-Operator/1.0",
        }
        if payload_value is not None:
            headers["Content-Type"] = "application/json"
        return self._request(
            method,
            path,
            payload_value,
            headers=headers,
            max_response_bytes=_MAX_RESPONSE_BYTES,
        )

    def _request(
        self,
        method: Literal["GET", "POST"],
        path: str,
        payload_value: dict[str, object] | None,
        *,
        headers: dict[str, str],
        max_response_bytes: int,
    ) -> dict[str, object]:
        payload = (
            None
            if payload_value is None
            else json.dumps(payload_value, separators=(",", ":")).encode("utf-8")
        )
        request = urllib.request.Request(
            f"{self._base_url}{path}",
            data=payload,
            method=method,
            headers=headers,
        )
        try:
            with self._opener.open(
                request, timeout=_REQUEST_TIMEOUT_SECONDS
            ) as response:
                raw = response.read(max_response_bytes + 1)
        except urllib.error.HTTPError as exc:
            raw = exc.read(_MAX_RESPONSE_BYTES + 1)
            status = exc.code
            exc.close()
            if status == 409:
                raise GitHubGatewayRejected(_error_reason(raw)) from exc
            if status >= 500:
                raise GitHubGatewayRetryable(_error_reason(raw)) from exc
            raise GitHubGatewayProtocolError(
                f"gateway returned unexpected HTTP status {status}"
            ) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise GitHubGatewayRetryable("github_gateway_unavailable") from exc
        if len(raw) > max_response_bytes:
            raise GitHubGatewayProtocolError("gateway response exceeded its bound")
        try:
            decoded = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise GitHubGatewayProtocolError("gateway returned invalid JSON") from exc
        if not isinstance(decoded, dict):
            raise GitHubGatewayProtocolError("gateway response must be an object")
        return cast(dict[str, object], decoded)


class AuthorizedPublicationGateway:
    """Adapt publication operations to one gateway-owned durable authority."""

    def __init__(
        self,
        client: ReviewGitHubGatewayClient,
        *,
        scope_kind: Literal[
            "publication", "posted_publication", "failure_status"
        ],
        scope_id: int,
        lease_owner: str | None = None,
        lease_generation: int | None = None,
    ) -> None:
        self._client = client
        self._identity: dict[str, object] = {
            "scope_kind": scope_kind,
            "scope_id": scope_id,
        }
        if scope_kind == "posted_publication":
            if lease_owner is not None or lease_generation is not None:
                raise ValueError("posted publication authority has no lease")
        elif lease_owner is None or lease_generation is None:
            raise ValueError("active publication authority requires a lease")
        else:
            self._identity["lease_owner"] = lease_owner
            self._identity["lease_generation"] = lease_generation
        self._repository: str | None = None
        self._pr_number: int | None = None

    def _execute(
        self, operation: str, values: dict[str, object] | None = None
    ) -> dict[str, object]:
        try:
            return self._client.execute_publication_operation(
                {
                    **self._identity,
                    "operation": operation,
                    **(values or {}),
                },
            )
        except GitHubGatewayRejected as exc:
            if exc.reason == "publication_lease_lost":
                raise GitHubPublicationAuthorityLost(exc.reason) from exc
            matched = _PROVIDER_STATUS_RE.match(exc.reason)
            status = int(matched.group("status")) if matched is not None else None
            raise GitHubPublicationError(
                exc.reason,
                status=status,
                operation=_provider_operation_name(operation),
                retryable=False,
            ) from exc
        except GitHubGatewayRetryable as exc:
            matched = _PROVIDER_STATUS_RE.match(exc.reason)
            status = int(matched.group("status")) if matched is not None else None
            raise GitHubPublicationError(
                exc.reason,
                status=status,
                operation=_provider_operation_name(operation),
                retryable=True,
            ) from exc
        except GitHubGatewayProtocolError as exc:
            raise GitHubPublicationError(
                "github_gateway_invalid_response",
                operation=_provider_operation_name(operation),
            ) from exc

    def current_user_login(self) -> str:
        value = self._execute("current_user")
        if set(value) != {"kind", "login"} or value.get("kind") != "current_user":
            raise GitHubPublicationError("github_gateway_invalid_response")
        login = value.get("login")
        if not isinstance(login, str) or not login:
            raise GitHubPublicationError("github_gateway_invalid_response")
        return login

    def get_pull_request(self, repository: str, pr_number: int) -> PullRequestState:
        self._check_subject(repository, pr_number)
        value = self._execute("get_pull")
        if set(value) != {"kind", "state", "draft", "base_sha", "head_sha"}:
            raise GitHubPublicationError("github_gateway_invalid_response")
        if value.get("kind") != "pull" or not isinstance(value.get("draft"), bool):
            raise GitHubPublicationError("github_gateway_invalid_response")
        fields = [value.get(name) for name in ("state", "base_sha", "head_sha")]
        if not all(isinstance(item, str) for item in fields):
            raise GitHubPublicationError("github_gateway_invalid_response")
        return PullRequestState(
            state=cast(str, fields[0]),
            draft=cast(bool, value["draft"]),
            base_sha=cast(str, fields[1]),
            head_sha=cast(str, fields[2]),
        )

    def list_issue_comments(
        self,
        repository: str,
        issue_number: int,
        *,
        max_pages: int = PUBLICATION_DEFAULT_MAX_PAGES,
        newest_first: bool = False,
    ) -> list[IssueComment]:
        self._check_subject(repository, issue_number)
        return [
            _issue_comment(item)
            for item in _list_items(
                self._execute(
                    "list_issue_comments",
                    {"max_pages": max_pages, "newest_first": newest_first},
                )
            )
        ]

    def update_issue_comment(
        self, repository: str, comment_id: int, body: str
    ) -> IssueComment:
        self._check_subject(repository)
        return _issue_comment(
            self._execute(
                "update_issue_comment", {"comment_id": comment_id, "body": body}
            ),
            envelope=True,
        )

    def create_issue_comment(
        self, repository: str, issue_number: int, body: str
    ) -> IssueComment:
        self._check_subject(repository, issue_number)
        return _issue_comment(
            self._execute("create_issue_comment", {"body": body}), envelope=True
        )

    def delete_issue_comment(self, repository: str, comment_id: int) -> None:
        self._check_subject(repository)
        value = self._execute("delete_issue_comment", {"comment_id": comment_id})
        if value != {"kind": "deleted"}:
            raise GitHubPublicationError("github_gateway_invalid_response")

    def create_pull_request_review(
        self,
        repository: str,
        pr_number: int,
        *,
        commit_id: str,
        body: str,
        comments: Sequence[InlineReviewComment],
    ) -> PullRequestReview:
        self._check_subject(repository, pr_number)
        value = self._execute(
            "create_pull_request_review",
            {
                "commit_id": commit_id,
                "body": body,
                "comments": [_inline_comment_mapping(item) for item in comments],
            },
        )
        expected = {"kind", "review_id", "body", "author_login", "commit_id", "state"}
        if set(value) != expected or value.get("kind") != "pull_request_review":
            raise GitHubPublicationError("github_gateway_invalid_response")
        return PullRequestReview(
            review_id=_response_positive(value.get("review_id")),
            body=_response_text(value.get("body")),
            author_login=_response_text(value.get("author_login")),
            commit_id=_response_text(value.get("commit_id")),
            state=_response_text(value.get("state")),
        )

    def list_pull_request_review_comments(
        self,
        repository: str,
        pr_number: int,
        *,
        max_pages: int = PUBLICATION_DEFAULT_MAX_PAGES,
    ) -> list[PullRequestReviewComment]:
        self._check_subject(repository, pr_number)
        result: list[PullRequestReviewComment] = []
        for item in _list_items(
            self._execute(
                "list_pull_request_review_comments", {"max_pages": max_pages}
            )
        ):
            expected = {
                "comment_id", "review_id", "body", "author_login", "path",
                "commit_id", "line", "side", "start_line", "start_side",
            }
            if set(item) != expected:
                raise GitHubPublicationError("github_gateway_invalid_response")
            side = item.get("side")
            start_side = item.get("start_side")
            if side not in {None, "LEFT", "RIGHT"} or start_side not in {
                None, "LEFT", "RIGHT"
            }:
                raise GitHubPublicationError("github_gateway_invalid_response")
            result.append(
                PullRequestReviewComment(
                    comment_id=_response_positive(item.get("comment_id")),
                    review_id=_response_positive(item.get("review_id")),
                    body=_response_text(item.get("body")),
                    author_login=_response_text(item.get("author_login")),
                    path=_response_text(item.get("path")),
                    commit_id=_response_text(item.get("commit_id")),
                    line=_response_optional_positive(item.get("line")),
                    side=cast(Literal["LEFT", "RIGHT"] | None, side),
                    start_line=_response_optional_positive(item.get("start_line")),
                    start_side=cast(Literal["LEFT", "RIGHT"] | None, start_side),
                )
            )
        return result

    def _check_subject(
        self, repository: str, pr_number: int | None = None
    ) -> None:
        normalized = repository.strip()
        if not normalized:
            raise GitHubPublicationError(
                "publication_subject_mismatch", retryable=False
            )
        if self._repository is None:
            self._repository = normalized
        elif self._repository.casefold() != normalized.casefold():
            raise GitHubPublicationError(
                "publication_subject_mismatch", retryable=False
            )
        if pr_number is None:
            return
        if isinstance(pr_number, bool) or pr_number < 1:
            raise GitHubPublicationError(
                "publication_subject_mismatch", retryable=False
            )
        if self._pr_number is None:
            self._pr_number = pr_number
        elif self._pr_number != pr_number:
            raise GitHubPublicationError(
                "publication_subject_mismatch", retryable=False
            )


def _provider_operation_name(operation: str) -> str:
    return {
        "current_user": "get_authenticated_user",
        "get_pull": "get_pull_request",
        "list_issue_comments": "list_issue_comments",
        "update_issue_comment": "update_issue_comment",
        "create_issue_comment": "create_issue_comment",
        "delete_issue_comment": "delete_issue_comment",
        "create_pull_request_review": "create_pull_request_review",
        "list_pull_request_review_comments": "list_pull_request_review_comments",
    }.get(operation, operation)


def _list_items(value: dict[str, object]) -> list[dict[str, object]]:
    if set(value) != {"kind", "items"} or value.get("kind") != "list":
        raise GitHubPublicationError("github_gateway_invalid_response")
    items = value.get("items")
    if not isinstance(items, list):
        raise GitHubPublicationError("github_gateway_invalid_response")
    raw_items = cast(list[object], items)
    if not all(isinstance(item, dict) for item in raw_items):
        raise GitHubPublicationError("github_gateway_invalid_response")
    return cast(list[dict[str, object]], raw_items)


def _issue_comment(
    value: dict[str, object], *, envelope: bool = False
) -> IssueComment:
    expected = {"comment_id", "body", "author_login"}
    if envelope:
        expected.add("kind")
        if value.get("kind") != "issue_comment":
            raise GitHubPublicationError("github_gateway_invalid_response")
    if set(value) != expected:
        raise GitHubPublicationError("github_gateway_invalid_response")
    return IssueComment(
        comment_id=_response_positive(value.get("comment_id")),
        body=_response_text(value.get("body"), allow_empty=True),
        author_login=_response_text(value.get("author_login"), allow_empty=True),
    )


def _inline_comment_mapping(comment: InlineReviewComment) -> dict[str, object]:
    return {
        "path": comment.path,
        "body": comment.body,
        "line": comment.line,
        "side": comment.side,
        "start_line": comment.start_line,
        "start_side": comment.start_side,
    }


def _response_positive(value: object) -> int:
    if type(value) is not int or value < 1:
        raise GitHubPublicationError("github_gateway_invalid_response")
    return value


def _response_optional_positive(value: object) -> int | None:
    return None if value is None else _response_positive(value)


def _response_text(value: object, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise GitHubPublicationError("github_gateway_invalid_response")
    return value


def _error_reason(raw: bytes) -> str:
    if len(raw) > _MAX_RESPONSE_BYTES:
        return "github_gateway_invalid_response"
    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return "github_gateway_invalid_response"
    if not isinstance(decoded, dict):
        return "github_gateway_invalid_response"
    reason = cast(dict[str, object], decoded).get("reason")
    if not isinstance(reason, str) or not _FAILURE_REASON_RE.fullmatch(reason):
        return "github_gateway_invalid_response"
    return reason
