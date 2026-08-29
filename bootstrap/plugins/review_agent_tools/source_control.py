"""Bounded, read-only GitHub transport for live pull-request reviews."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from http.client import HTTPMessage
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import IO, Literal, cast


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
_DEFAULT_MAX_ATTEMPTS = 3
_DEFAULT_REQUEST_TIMEOUT_SECONDS = 30.0
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


def _credentialed_https_origin(url: str) -> tuple[str, str, int]:
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port or 443
    except ValueError as exc:
        raise ValueError("redirect URL must use one HTTPS origin") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("redirect URL must use one HTTPS origin")
    return parsed.scheme, parsed.hostname.lower(), port


def https_api_origin(url: str) -> tuple[str, str, int]:
    try:
        origin = _credentialed_https_origin(url)
        parsed = urllib.parse.urlsplit(url)
    except ValueError as exc:
        raise ValueError("api_url must be an HTTPS API root") from exc
    if parsed.query or parsed.fragment:
        raise ValueError("api_url must be an HTTPS API root")
    return origin


class SameOriginHttpsRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow credentialed redirects only within one HTTPS origin."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        try:
            same_origin = _credentialed_https_origin(
                req.full_url
            ) == _credentialed_https_origin(newurl)
        except ValueError:
            same_origin = False
        if not same_origin:
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class GitHubReadError(Exception):
    """A transport failure that the tool boundary translates into its public error."""

    kind: GitHubReadErrorKind

    def __init__(
        self,
        kind: GitHubReadErrorKind,
        message: str,
        *,
        status: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.retryable = retryable


class GitHubReadClient:
    """Perform authenticated, bounded GET requests against the GitHub API."""

    def __init__(
        self,
        token: str,
        *,
        request_timeout_seconds: float = _DEFAULT_REQUEST_TIMEOUT_SECONDS,
        max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
        opener: urllib.request.OpenerDirector | None = None,
    ) -> None:
        if request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be positive")
        if type(max_attempts) is not int or max_attempts < 1:
            raise ValueError("max_attempts must be a positive integer")
        self._token = token
        self._request_timeout_seconds = request_timeout_seconds
        self._max_attempts = max_attempts
        self._opener = opener or urllib.request.build_opener(
            SameOriginHttpsRedirectHandler()
        )

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
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        request = urllib.request.Request(
            f"{_API_ROOT}{endpoint}", headers=headers, method="GET"
        )
        for attempt in range(self._max_attempts):
            try:
                with self._opener.open(
                    request, timeout=self._request_timeout_seconds
                ) as response:
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
                retryable = (
                    rate_limited
                    or exc.code in {408, 425, 429}
                    or 500 <= exc.code <= 599
                )
                exc.close()
                if retryable and not rate_limited and attempt + 1 < self._max_attempts:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                if exc.code == 401:
                    raise GitHubReadError(
                        "unauthorized",
                        "GitHub rejected the installation token",
                        status=exc.code,
                    ) from exc
                if rate_limited:
                    raise GitHubReadError(
                        "rate_limited",
                        "GitHub rate-limited the read request",
                        status=exc.code,
                        retryable=True,
                    ) from exc
                if exc.code == 403:
                    raise GitHubReadError(
                        "forbidden",
                        "GitHub denied the read request",
                        status=exc.code,
                    ) from exc
                if exc.code == 404:
                    raise GitHubReadError(
                        "not_found", "not found", status=exc.code
                    ) from exc
                if exc.code == 406:
                    raise GitHubReadError(
                        "diff_unavailable",
                        "GitHub could not render this diff; inspect smaller files instead",
                        status=exc.code,
                    ) from exc
                raise GitHubReadError(
                    "http_error",
                    f"GitHub read failed with HTTP {exc.code}",
                    status=exc.code,
                    retryable=retryable,
                ) from exc
            except urllib.error.URLError as exc:
                raise GitHubReadError(
                    "unreachable", "GitHub could not be reached", retryable=True
                ) from exc
        raise GitHubReadError(
            "unreachable", "GitHub could not be reached", retryable=True
        )

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


@dataclass(frozen=True, slots=True)
class PullSnapshot:
    """Exact live pull-request and base/head repository identities."""

    repository_id: int
    repository: str
    number: int
    state: str
    base_sha: str
    head_sha: str
    head_repository_id: int | None
    head_repository: str | None


def _github_object(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise GitHubReadError("invalid_json", f"GitHub returned invalid {field}")
    return cast(Mapping[str, object], value)


def _github_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GitHubReadError("invalid_json", f"GitHub returned invalid {field}")
    return value.strip()


def _github_int(value: object, field: str) -> int:
    if type(value) is not int or value < 1:
        raise GitHubReadError("invalid_json", f"GitHub returned invalid {field}")
    return value


def _github_repository_name(value: object) -> str:
    name = _github_text(value, "repository name")
    parts = name.split("/")
    if len(parts) != 2 or not all(parts):
        raise GitHubReadError(
            "invalid_json", "GitHub returned an invalid repository name"
        )
    return name


def read_pull_snapshot(
    github: GitHubReadClient, repository: str, pr_number: int
) -> PullSnapshot:
    """Read the exact live pull request and both repository identities."""
    quoted = urllib.parse.quote(repository, safe="/")
    root = _github_object(
        github.request_json(f"/repos/{quoted}/pulls/{pr_number}"),
        "GitHub pull request",
    )
    base = _github_object(root.get("base"), "pull request base")
    head = _github_object(root.get("head"), "pull request head")
    base_repository = _github_object(base.get("repo"), "base repository")
    raw_head_repository = head.get("repo")
    if raw_head_repository is None:
        head_repository_id = None
        head_repository = None
    else:
        head_repository_object = _github_object(raw_head_repository, "head repository")
        head_repository_id = _github_int(
            head_repository_object.get("id"), "head repository id"
        )
        head_repository = _github_repository_name(
            head_repository_object.get("full_name")
        )
    return PullSnapshot(
        repository_id=_github_int(base_repository.get("id"), "repository id"),
        repository=_github_repository_name(base_repository.get("full_name")),
        number=_github_int(root.get("number"), "pull request number"),
        state=_github_text(root.get("state"), "pull request state").lower(),
        base_sha=_github_text(base.get("sha"), "base sha"),
        head_sha=_github_text(head.get("sha"), "head sha"),
        head_repository_id=head_repository_id,
        head_repository=head_repository,
    )
