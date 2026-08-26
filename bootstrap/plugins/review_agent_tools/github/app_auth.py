"""Repository-scoped GitHub App credentials for Review Agent reads."""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from http.client import HTTPMessage
from collections.abc import Mapping, Sequence
from typing import IO, Final, cast
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
_DEFAULT_PROVIDER_RESPONSE_BYTES: Final = 4 * 1024 * 1024
_MAX_PRIVATE_KEY_BYTES: Final = 64 * 1024
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


class GitHubAppConfigurationError(ValueError):
    """GitHub App credentials are missing or invalid."""


@dataclass(frozen=True, slots=True)
class InstallationToken:
    value: str = field(repr=False)
    expires_at: datetime


ReviewReadToken = InstallationToken


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


def load_private_key_file(
    path: str | Path, *, maximum_bytes: int = _MAX_PRIVATE_KEY_BYTES
) -> str:
    """Read one bounded, non-symlink, regular UTF-8 private-key file."""
    if isinstance(maximum_bytes, bool) or maximum_bytes < 1:
        raise ValueError("maximum_bytes must be positive")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise GitHubAppConfigurationError(
            "GitHub App private key file could not be opened"
        ) from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise GitHubAppConfigurationError(
                "GitHub App private key must be a regular file"
            )
        if metadata.st_size > maximum_bytes:
            raise GitHubAppConfigurationError(
                "GitHub App private key file exceeds the size limit"
            )
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            descriptor = -1
            raw = handle.read(maximum_bytes + 1)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(raw) > maximum_bytes:
        raise GitHubAppConfigurationError(
            "GitHub App private key file exceeds the size limit"
        )
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise GitHubAppConfigurationError(
            "GitHub App private key file must be UTF-8 PEM"
        ) from exc


class GitHubAppAuthenticator:
    """Own GitHub App JWTs, installation tokens, and bounded provider reads."""

    def __init__(
        self,
        *,
        app_id: int,
        private_key_pem: str,
        api_url: str = "https://api.github.com",
        response_byte_limit: int = _DEFAULT_PROVIDER_RESPONSE_BYTES,
    ) -> None:
        if isinstance(app_id, bool) or app_id < 1:
            raise ValueError("app_id must be positive")
        if not private_key_pem.strip():
            raise ValueError("private_key_pem is required")
        try:
            private_key = serialization.load_pem_private_key(
                private_key_pem.encode("utf-8"), password=None
            )
        except (TypeError, ValueError) as exc:
            raise GitHubAppConfigurationError(
                "private_key_pem must contain a valid RSA private key"
            ) from exc
        if not isinstance(private_key, RSAPrivateKey):
            raise GitHubAppConfigurationError(
                "private_key_pem must contain a valid RSA private key"
            )
        if isinstance(response_byte_limit, bool) or response_byte_limit < 1:
            raise ValueError("response_byte_limit must be positive")
        _https_origin(api_url)
        self._app_id = app_id
        self._private_key = private_key
        self._api_url = api_url.rstrip("/")
        self._response_byte_limit = response_byte_limit
        self._opener = urllib.request.build_opener(_SameOriginRedirectHandler())

    def app_json(self, path: str, *, now: datetime | None = None) -> object:
        """Read one App-authenticated GitHub JSON resource."""
        return self._request_json(path, credential=self._app_jwt(now))

    def installation_json(self, path: str, token: InstallationToken) -> object:
        """Read one installation-authenticated GitHub JSON resource."""
        return self._request_json(path, credential=token.value)

    def installation_token(
        self,
        provider_installation_id: int,
        *,
        repository_ids: Sequence[int] | None = None,
        permissions: Mapping[str, str] | None = None,
        now: datetime | None = None,
    ) -> InstallationToken:
        """Mint one in-memory installation token, optionally reduced in scope."""
        moment = now or datetime.now(timezone.utc)
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        if isinstance(provider_installation_id, bool) or provider_installation_id < 1:
            raise ValueError("provider_installation_id must be positive")
        payload: dict[str, object] = {}
        if repository_ids is not None:
            normalized_ids = tuple(repository_ids)
            if not normalized_ids or any(
                isinstance(value, bool) or value < 1 for value in normalized_ids
            ):
                raise ValueError("repository_ids must contain positive IDs")
            payload["repository_ids"] = normalized_ids
        if permissions is not None:
            payload["permissions"] = dict(permissions)
        result = self._request_json(
            f"/app/installations/{provider_installation_id}/access_tokens",
            credential=self._app_jwt(moment),
            method="POST",
            payload=payload,
            response_byte_limit=_MAX_TOKEN_RESPONSE_BYTES,
        )
        if not isinstance(result, Mapping):
            raise GitHubAppTokenPermanent(
                "GitHub installation token response was invalid"
            )
        typed_result = cast(Mapping[str, object], result)
        try:
            value = typed_result["token"]
            raw_expiry = typed_result["expires_at"]
            if not isinstance(raw_expiry, str):
                raise TypeError
            expires_at = datetime.fromisoformat(raw_expiry.replace("Z", "+00:00"))
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise GitHubAppTokenPermanent(
                "GitHub installation token response was invalid"
            ) from exc
        if not isinstance(value, str) or not value or expires_at.tzinfo is None:
            raise GitHubAppTokenPermanent(
                "GitHub installation token response was invalid"
            )
        if permissions is not None:
            raw_permissions = typed_result.get("permissions")
            if not isinstance(raw_permissions, Mapping):
                raise GitHubAppTokenPermanent(
                    "GitHub installation token permissions were invalid"
                )
            granted_permissions = cast(Mapping[object, object], raw_permissions)
            if any(
                not isinstance(name, str) or not isinstance(level, str)
                for name, level in granted_permissions.items()
            ):
                raise GitHubAppTokenPermanent(
                    "GitHub installation token permissions were invalid"
                )
            expected_permissions = dict(permissions)
            normalized_permissions = cast(Mapping[str, str], granted_permissions)
            if any(
                normalized_permissions.get(name) != level
                for name, level in expected_permissions.items()
            ):
                raise GitHubAppTokenPermanent(
                    "GitHub installation token permissions did not match the requested scope"
                )
            unexpected_permissions = set(normalized_permissions).difference(
                expected_permissions, {"metadata"}
            )
            if unexpected_permissions or normalized_permissions.get(
                "metadata", "read"
            ) != "read":
                raise GitHubAppTokenPermanent(
                    "GitHub installation token permissions exceeded the requested scope"
                )
        if expires_at - moment <= _TOKEN_REFRESH_MARGIN:
            raise GitHubAppTokenPermanent("GitHub installation token expires too soon")
        return InstallationToken(value=value, expires_at=expires_at)

    @property
    def opener(self) -> urllib.request.OpenerDirector:
        """Expose the HTTP seam for the repository token owner's focused tests."""
        return self._opener

    def _app_jwt(self, now: datetime | None) -> str:
        moment = now or datetime.now(timezone.utc)
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        issued_at = moment - _JWT_CLOCK_SKEW
        return jwt.encode(
            {
                "iat": int(issued_at.timestamp()),
                "exp": int((issued_at + _JWT_LIFETIME).timestamp()),
                "iss": str(self._app_id),
            },
            self._private_key,
            algorithm="RS256",
        )

    def _request_json(
        self,
        path: str,
        *,
        credential: str,
        method: str = "GET",
        payload: Mapping[str, object] | None = None,
        response_byte_limit: int | None = None,
    ) -> object:
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("GitHub API path must be absolute")
        raw_payload = (
            json.dumps(payload, separators=(",", ":")).encode("utf-8")
            if payload is not None
            else None
        )
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {credential}",
            "User-Agent": "Review-Agent/1.0",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if raw_payload is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self._api_url}{path}",
            data=raw_payload,
            method=method,
            headers=headers,
        )
        byte_limit = response_byte_limit or self._response_byte_limit
        try:
            with self._opener.open(
                request, timeout=_REQUEST_TIMEOUT_SECONDS
            ) as response:
                raw_result = response.read(byte_limit + 1)
                if len(raw_result) > byte_limit:
                    raise GitHubAppTokenPermanent(
                        "GitHub App response exceeded the configured size limit"
                    )
                return json.loads(raw_result)
        except urllib.error.HTTPError as exc:
            retryable = exc.code >= 500 or is_github_rate_limit_error(exc)
            exc.close()
            if retryable:
                raise GitHubAppTokenRetryable(
                    "GitHub App request is temporarily unavailable"
                ) from exc
            raise GitHubAppTokenPermanent("GitHub rejected the App request") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise GitHubAppTokenRetryable(
                "GitHub App request is temporarily unavailable"
            ) from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise GitHubAppTokenPermanent("GitHub App response was invalid") from exc


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
        self._authenticator = GitHubAppAuthenticator(
            app_id=app_id,
            private_key_pem=private_key_pem,
            api_url=api_url,
        )
        self._postgres = postgres
        self._cache: dict[int, _CachedToken] = {}
        self._locks: WeakValueDictionary[int, threading.Lock] = WeakValueDictionary()
        self._locks_guard = threading.Lock()
        # Retain this internal seam for existing focused HTTP tests.
        self._opener = self._authenticator.opener

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

    def invalidate(self, provider_repository_id: int) -> None:
        """Forget one cached repository token before a single credential retry."""
        if type(provider_repository_id) is not int or provider_repository_id < 1:
            raise ValueError("provider_repository_id must be positive")
        with self._scope_lock(provider_repository_id):
            self._cache.pop(provider_repository_id, None)

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
        return self._authenticator.installation_token(
            authorization.provider_installation_id,
            repository_ids=(authorization.provider_repository_id,),
            permissions=_READ_PERMISSIONS,
            now=now,
        )
