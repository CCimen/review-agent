"""Repository-scoped GitHub App credentials for Review Agent reads."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from http.client import HTTPMessage
from typing import IO, Final
from urllib.parse import urlsplit
from weakref import WeakValueDictionary

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

from ..postgres import github_app
from ..postgres.runtime import PostgreSQLRuntime
from ..source_control import is_github_rate_limit_error


_TOKEN_REFRESH_MARGIN: Final = timedelta(minutes=5)
_JWT_LIFETIME: Final = timedelta(minutes=9)
_JWT_CLOCK_SKEW: Final = timedelta(seconds=60)
_REQUEST_TIMEOUT_SECONDS: Final = 15
_MAX_TOKEN_RESPONSE_BYTES: Final = 65_536
_READ_PERMISSIONS: Final = {
    "contents": "read",
    "issues": "read",
    "pull_requests": "read",
}


class GitHubAppTokenError(RuntimeError):
    """A repository-scoped installation token could not be obtained."""


class GitHubAppTokenRetryable(GitHubAppTokenError):
    """Token exchange may succeed when retried later."""


class GitHubAppTokenPermanent(GitHubAppTokenError):
    """Token exchange failed in a way that requires configuration or state repair."""


@dataclass(frozen=True, slots=True)
class ReviewReadToken:
    value: str = field(repr=False)
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class _CachedToken:
    token: ReviewReadToken
    provider_installation_id: int


def _https_origin(url: str) -> tuple[str, str, int]:
    try:
        parsed = urlsplit(url)
        port = parsed.port or 443
    except ValueError as exc:
        raise ValueError("api_url must be an HTTPS API root") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("api_url must be an HTTPS API root")
    return parsed.scheme, parsed.hostname.lower(), port


class _SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow API redirects only when the App JWT stays on the same origin."""

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
            same_origin = _https_origin(req.full_url) == _https_origin(newurl)
        except ValueError:
            same_origin = False
        if not same_origin:
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class ReviewReadTokenService:
    """Mint and briefly cache minimum-permission tokens for one repository."""

    def __init__(
        self,
        *,
        app_id: int,
        private_key_pem: str,
        postgres: PostgreSQLRuntime,
        api_url: str = "https://api.github.com",
    ) -> None:
        if isinstance(app_id, bool) or app_id < 1:
            raise ValueError("app_id must be positive")
        if not private_key_pem.strip():
            raise ValueError("private_key_pem is required")
        self._app_id = app_id
        try:
            private_key = serialization.load_pem_private_key(
                private_key_pem.encode("utf-8"), password=None
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "private_key_pem must contain a valid RSA private key"
            ) from exc
        if not isinstance(private_key, RSAPrivateKey):
            raise ValueError("private_key_pem must contain a valid RSA private key")
        _https_origin(api_url)
        self._private_key = private_key
        self._postgres = postgres
        self._api_url = api_url.rstrip("/")
        self._cache: dict[int, _CachedToken] = {}
        self._locks: WeakValueDictionary[int, threading.Lock] = WeakValueDictionary()
        self._locks_guard = threading.Lock()
        self._opener = urllib.request.build_opener(_SameOriginRedirectHandler())

    def token_for(
        self, provider_repository_id: int, *, now: datetime | None = None
    ) -> ReviewReadToken:
        """Return a token only after reauthorizing current PostgreSQL state."""
        moment = now or datetime.now(timezone.utc)
        if moment.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        lock = self._scope_lock(provider_repository_id)
        with lock:
            try:
                authorization = self._authorize(provider_repository_id)
            except github_app.GitHubAppReviewReadUnauthorized:
                self._cache.pop(provider_repository_id, None)
                raise
            cached = self._cache.get(provider_repository_id)
            if (
                cached is not None
                and cached.provider_installation_id
                == authorization.provider_installation_id
                and cached.token.expires_at - moment > _TOKEN_REFRESH_MARGIN
            ):
                return cached.token
            token = self._exchange(authorization, moment)
            self._cache[provider_repository_id] = _CachedToken(
                token=token,
                provider_installation_id=authorization.provider_installation_id,
            )
            return token

    def _scope_lock(self, provider_repository_id: int) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(provider_repository_id, threading.Lock())

    def _authorize(
        self, provider_repository_id: int
    ) -> github_app.ReviewReadAuthorization:
        with self._postgres.transaction() as connection:
            return github_app.authorize_review_read(connection, provider_repository_id)

    def _exchange(
        self,
        authorization: github_app.ReviewReadAuthorization,
        now: datetime,
    ) -> ReviewReadToken:
        issued_at = now - _JWT_CLOCK_SKEW
        app_jwt = jwt.encode(
            {
                "iat": int(issued_at.timestamp()),
                "exp": int((issued_at + _JWT_LIFETIME).timestamp()),
                "iss": str(self._app_id),
            },
            self._private_key,
            algorithm="RS256",
        )
        payload = json.dumps(
            {
                "repository_ids": [authorization.provider_repository_id],
                "permissions": _READ_PERMISSIONS,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self._api_url}/app/installations/"
            f"{authorization.provider_installation_id}/access_tokens",
            data=payload,
            method="POST",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {app_jwt}",
                "Content-Type": "application/json",
                "User-Agent": "Review-Agent/1.0",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with self._opener.open(
                request, timeout=_REQUEST_TIMEOUT_SECONDS
            ) as response:
                raw_result = response.read(_MAX_TOKEN_RESPONSE_BYTES + 1)
                if len(raw_result) > _MAX_TOKEN_RESPONSE_BYTES:
                    raise GitHubAppTokenPermanent(
                        "GitHub installation token response was invalid"
                    )
                result = json.loads(raw_result)
        except urllib.error.HTTPError as exc:
            retryable = exc.code >= 500 or is_github_rate_limit_error(exc)
            exc.close()
            if retryable:
                raise GitHubAppTokenRetryable(
                    "GitHub installation token exchange is temporarily unavailable"
                ) from exc
            raise GitHubAppTokenPermanent(
                "GitHub rejected the installation token exchange"
            ) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise GitHubAppTokenRetryable(
                "GitHub installation token exchange is temporarily unavailable"
            ) from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise GitHubAppTokenPermanent(
                "GitHub installation token response was invalid"
            ) from exc

        try:
            value = result["token"]
            expires_at = datetime.fromisoformat(
                result["expires_at"].replace("Z", "+00:00")
            )
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise GitHubAppTokenPermanent(
                "GitHub installation token response was invalid"
            ) from exc
        if not isinstance(value, str) or not value or expires_at.tzinfo is None:
            raise GitHubAppTokenPermanent(
                "GitHub installation token response was invalid"
            )
        if expires_at - now <= _TOKEN_REFRESH_MARGIN:
            raise GitHubAppTokenPermanent("GitHub installation token expires too soon")
        return ReviewReadToken(value=value, expires_at=expires_at)
