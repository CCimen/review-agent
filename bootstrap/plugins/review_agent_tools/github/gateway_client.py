"""Typed internal client for the closed Review Agent GitHub gateway."""

from __future__ import annotations

import json
import re
from typing import cast
import urllib.error
import urllib.parse
import urllib.request

from .. import capacity, changed_files
from .gateway import (
    AUTHORIZE_REVIEW_DELIVERY_PATH,
    READ_REVIEW_SOURCE_PATH,
    AuthorizedReviewSnapshot,
    GitHubGatewayProtocolError,
    GitHubGatewayRejected,
    GitHubGatewayRetryable,
)
from .source import (
    GitHubSourceError,
    ReviewFilePage,
    ReviewPullSource,
    ReviewSourceBytes,
)


_MAX_RESPONSE_BYTES = 65_536
_MAX_CHANGED_FILES_RESPONSE_BYTES = (
    (changed_files.ENUMERATION_MAX_BYTES + 2) // 3
) * 4 + 65_536
_MAX_DIFF_RESPONSE_BYTES = ((1_000_000 + 2) // 3) * 4 + 65_536
_MAX_FILE_RESPONSE_BYTES = capacity.DEFAULT_RESULT_MAX_CHARS * 12 + 65_536
_REQUEST_TIMEOUT_SECONDS = 90.0
_FAILURE_REASON_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


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
    ) -> None:
        self._base_url = _base_url(base_url)
        self._opener = opener or urllib.request.build_opener()

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
        payload = json.dumps(payload_value, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            f"{self._base_url}{path}",
            data=payload,
            method="POST",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "Review-Agent-GitHub-Events/1.0",
            },
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
