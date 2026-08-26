"""Typed internal client for the closed Review Agent GitHub gateway."""

from __future__ import annotations

import json
import re
from typing import cast
import urllib.error
import urllib.parse
import urllib.request

from .gateway import (
    AUTHORIZE_REVIEW_DELIVERY_PATH,
    AuthorizedReviewSnapshot,
    GitHubGatewayProtocolError,
    GitHubGatewayRejected,
    GitHubGatewayRetryable,
)


_MAX_RESPONSE_BYTES = 65_536
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
    """Call only the gateway's fixed review-delivery authorization operation."""

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
        payload = json.dumps(
            {
                "delivery_id": delivery_id,
                "lease_owner": lease_owner,
                "lease_generation": lease_generation,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self._base_url}{AUTHORIZE_REVIEW_DELIVERY_PATH}",
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
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
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
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise GitHubGatewayProtocolError("gateway response exceeded its bound")
        try:
            decoded = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise GitHubGatewayProtocolError("gateway returned invalid JSON") from exc
        if not isinstance(decoded, dict):
            raise GitHubGatewayProtocolError("gateway response must be an object")
        return AuthorizedReviewSnapshot.from_mapping(
            cast(dict[str, object], decoded)
        )


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
