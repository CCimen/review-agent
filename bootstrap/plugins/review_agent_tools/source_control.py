"""Bounded, read-only GitHub transport for live pull-request reviews."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from http.client import HTTPMessage
import json
import re
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
_ARCHIVE_ENDPOINT = re.compile(
    r"^/repos/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/tarball/([0-9a-f]{40,64})$"
)


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


def github_retry_at(
    exc: urllib.error.HTTPError, *, rate_limited: bool, now: datetime | None = None,
) -> datetime | None:
    """Retain GitHub's latest valid cooldown, or wait a minute without timing."""
    observed_at = now or datetime.now(timezone.utc)
    deadlines: list[datetime] = []
    retry_after = exc.headers.get("retry-after")
    if retry_after is not None:
        try:
            seconds = int(retry_after)
        except ValueError:
            try:
                deadline = parsedate_to_datetime(retry_after)
                if deadline.utcoffset() is not None and deadline > observed_at:
                    deadlines.append(deadline.astimezone(timezone.utc))
            except (TypeError, ValueError, OverflowError):
                pass
        else:
            try:
                if seconds > 0:
                    deadlines.append(observed_at + timedelta(seconds=seconds))
            except OverflowError:
                pass
    if exc.headers.get("x-ratelimit-remaining") == "0":
        reset = exc.headers.get("x-ratelimit-reset")
        if reset is not None:
            try:
                deadline = datetime.fromtimestamp(int(reset), timezone.utc)
                if deadline > observed_at:
                    deadlines.append(deadline)
            except (ValueError, OverflowError, OSError):
                pass
    if deadlines:
        return max(deadlines)
    return observed_at + timedelta(minutes=1) if rate_limited else None


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


class GitHubArchiveRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Download one exact GitHub archive without forwarding the App token."""

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
            source = urllib.parse.urlsplit(req.full_url)
            target = urllib.parse.urlsplit(newurl)
            subject = _ARCHIVE_ENDPOINT.fullmatch(source.path)
            allowed_origin = (
                _credentialed_https_origin(req.full_url) == ("https", "api.github.com", 443)
                and _credentialed_https_origin(newurl) == ("https", "codeload.github.com", 443)
            )
        except ValueError:
            return None
        if subject and allowed_origin and not source.query and not target.fragment:
            owner, repository, sha = subject.groups()
            if target.path not in {
                f"/{owner}/{repository}/legacy.tar.gz/{sha}",
                f"/{owner}/{repository}/tar.gz/{sha}",
            }:
                return None
            # Private archive URLs contain a short-lived download credential.
            # They must never carry the installation token to the download host.
            return urllib.request.Request(
                newurl,
                headers={"User-Agent": "Hermes-PR-Review/2.0"},
                method="GET",
            )
        return None


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
        retry_at: datetime | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.retryable = retryable
        self.retry_at = retry_at


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
        return self._request(
            endpoint, accept=accept, max_bytes=max_bytes, opener=self._opener
        )

    def request_archive(self, repository: str, commit_sha: str, *, max_bytes: int) -> bytes:
        """Read a bounded tar archive for a caller-authorized immutable commit."""
        endpoint = f"/repos/{repository}/tarball/{commit_sha}"
        if (_ARCHIVE_ENDPOINT.fullmatch(endpoint) is None or max_bytes < 1
                or any(part in {".", ".."} for part in repository.split("/"))):
            raise GitHubReadError("invalid_endpoint", "invalid GitHub archive subject")
        raw, truncated, _ = self._request(
            endpoint,
            accept="application/vnd.github+json",
            max_bytes=max_bytes,
            opener=urllib.request.build_opener(GitHubArchiveRedirectHandler()),
        )
        if truncated:
            raise GitHubReadError("response_too_large", "repository archive exceeds the size limit")
        return raw

    def _request(
        self,
        endpoint: str,
        *,
        accept: str,
        max_bytes: int,
        opener: urllib.request.OpenerDirector,
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
                with opener.open(
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
                retry_at = (
                    github_retry_at(exc, rate_limited=rate_limited)
                    if retryable
                    else None
                )
                exc.close()
                if retryable and retry_at is None and attempt + 1 < self._max_attempts:
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
                        retry_at=retry_at,
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
                    retry_at=retry_at,
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
