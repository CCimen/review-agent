"""Bounded, read-only GitHub transport for live pull-request reviews."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Literal


GitHubReadErrorKind = Literal[
    "invalid_endpoint",
    "unauthorized",
    "forbidden",
    "rate_limited",
    "not_found",
    "diff_unavailable",
    "http_error",
    "unreachable",
    "response_too_large",
    "invalid_json",
]

_API_ROOT = "https://api.github.com"
_RETRYABLE_STATUS = frozenset({502, 503, 504})
_MAX_ATTEMPTS = 3
_MAX_ERROR_RESPONSE_BYTES = 4_096


def is_github_rate_limit_error(exc: urllib.error.HTTPError) -> bool:
    """Recognize GitHub's bounded primary and secondary limit signals."""
    if exc.code == 429:
        return True
    if exc.code != 403:
        return False
    if (
        exc.headers.get("retry-after") is not None
        or exc.headers.get("x-ratelimit-remaining") == "0"
    ):
        return True
    try:
        raw = exc.read(_MAX_ERROR_RESPONSE_BYTES + 1)
    except (AttributeError, OSError, ValueError):
        return False
    if len(raw) > _MAX_ERROR_RESPONSE_BYTES:
        return False
    return b"secondary rate limit" in raw.lower()


class GitHubReadError(Exception):
    """A transport failure that the tool boundary translates into its public error."""

    kind: GitHubReadErrorKind

    def __init__(self, kind: GitHubReadErrorKind, message: str) -> None:
        super().__init__(message)
        self.kind = kind


class GitHubReadClient:
    """Perform authenticated, bounded GET requests against the GitHub API."""

    def __init__(self, read_token: str) -> None:
        self._read_token = read_token

    def request(
        self,
        endpoint: str,
        *,
        accept: str = "application/vnd.github+json",
        max_bytes: int = 2_000_000,
    ) -> tuple[bytes, bool, dict[str, str]]:
        if not endpoint.startswith("/") or "//" in endpoint:
            raise GitHubReadError("invalid_endpoint", "invalid GitHub API endpoint")
        headers = {
            "Accept": accept,
            "User-Agent": "Hermes-PR-Review/2.0",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self._read_token:
            headers["Authorization"] = f"Bearer {self._read_token}"
        request = urllib.request.Request(
            f"{_API_ROOT}{endpoint}", headers=headers, method="GET"
        )
        for attempt in range(_MAX_ATTEMPTS):
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    data = response.read(max_bytes + 1)
                    truncated = len(data) > max_bytes
                    if truncated:
                        data = data[:max_bytes]
                    response_headers = {
                        "etag": response.headers.get("ETag", ""),
                        "content_type": response.headers.get("Content-Type", ""),
                    }
                    return data, truncated, response_headers
            except urllib.error.HTTPError as exc:
                rate_limited = is_github_rate_limit_error(exc)
                exc.close()
                if exc.code in _RETRYABLE_STATUS and attempt + 1 < _MAX_ATTEMPTS:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                if exc.code == 401:
                    raise GitHubReadError(
                        "unauthorized", "GitHub rejected the read token"
                    ) from exc
                if rate_limited:
                    raise GitHubReadError(
                        "rate_limited", "GitHub rate-limited the read request"
                    ) from exc
                if exc.code == 403:
                    raise GitHubReadError(
                        "forbidden", "GitHub denied the read request"
                    ) from exc
                if exc.code == 404:
                    raise GitHubReadError("not_found", "not found") from exc
                if exc.code == 406:
                    raise GitHubReadError(
                        "diff_unavailable",
                        "GitHub could not render this diff; inspect smaller files instead",
                    ) from exc
                raise GitHubReadError(
                    "http_error", f"GitHub read failed with HTTP {exc.code}"
                ) from exc
            except urllib.error.URLError as exc:
                raise GitHubReadError(
                    "unreachable", "GitHub could not be reached"
                ) from exc
        raise GitHubReadError("unreachable", "GitHub could not be reached")

    def request_json(self, endpoint: str, *, max_bytes: int = 2_000_000) -> object:
        raw, truncated, _ = self.request(endpoint, max_bytes=max_bytes)
        if truncated:
            raise GitHubReadError(
                "response_too_large",
                "GitHub JSON response exceeded the safe size limit",
            )
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GitHubReadError(
                "invalid_json", "GitHub returned invalid JSON"
            ) from exc
