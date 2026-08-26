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
from typing import IO, Final, Literal, cast
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
_MAX_INSTALLATION_TOKEN_RESPONSE_BYTES: Final = 65_536
_DEFAULT_GITHUB_API_RESPONSE_BYTES: Final = 4 * 1024 * 1024
_MAX_PRIVATE_KEY_BYTES: Final = 64 * 1024
GitHubAppTokenPurpose = Literal["review_read", "publication"]
_PURPOSE_PERMISSIONS: Final[dict[GitHubAppTokenPurpose, dict[str, str]]] = {
    "review_read": {
        "contents": "read",
        "issues": "read",
        "pull_requests": "read",
    },
    "publication": {
        "issues": "write",
        "pull_requests": "write",
    },
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


@dataclass(frozen=True, slots=True)
class _CachedToken:
    token: InstallationToken
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
        response_byte_limit: int = _DEFAULT_GITHUB_API_RESPONSE_BYTES,
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
            response_byte_limit=_MAX_INSTALLATION_TOKEN_RESPONSE_BYTES,
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


class GitHubAppTokenService:
    """Mint and briefly cache one of the two code-owned repository token scopes."""

    def __init__(
        self,
        *,
        app_id: int,
        private_key_pem: str,
        postgres: PostgreSQLRuntime,
        profile: str,
        api_url: str = "https://api.github.com",
    ) -> None:
        self._authenticator = GitHubAppAuthenticator(
            app_id=app_id,
            private_key_pem=private_key_pem,
            api_url=api_url,
        )
        self._postgres = postgres
        self._profile = profile.strip()
        if not self._profile:
            raise GitHubAppConfigurationError("profile is required")
        self._cache: dict[tuple[int, GitHubAppTokenPurpose], _CachedToken] = {}
        self._bot_login: str | None = None
        self._bot_login_lock = threading.Lock()
        self._locks: WeakValueDictionary[
            tuple[int, GitHubAppTokenPurpose], threading.Lock
        ] = WeakValueDictionary()
        self._locks_guard = threading.Lock()
        # Retain this internal seam for existing focused HTTP tests.
        self._opener = self._authenticator.opener

    def app_bot_login(self) -> str:
        """Return the stable bot login derived with App JWT authentication."""
        with self._bot_login_lock:
            if self._bot_login is not None:
                return self._bot_login
            result = self._authenticator.app_json("/app")
            if not isinstance(result, Mapping):
                raise GitHubAppTokenPermanent("GitHub App metadata was invalid")
            metadata = cast(Mapping[str, object], result)
            slug = metadata.get("slug")
            if not isinstance(slug, str) or not slug or slug != slug.strip():
                raise GitHubAppTokenPermanent("GitHub App metadata was invalid")
            self._bot_login = f"{slug}[bot]"
            return self._bot_login

    def token_for(
        self,
        provider_repository_id: int,
        *,
        purpose: GitHubAppTokenPurpose = "review_read",
        now: datetime | None = None,
    ) -> InstallationToken:
        """Return a token only after reauthorizing current PostgreSQL state."""
        if purpose not in _PURPOSE_PERMISSIONS:
            raise ValueError("unsupported GitHub App token purpose")
        moment = now or datetime.now(timezone.utc)
        if moment.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        cache_key = (provider_repository_id, purpose)
        lock = self._scope_lock(cache_key)
        with lock:
            try:
                authorization = self._authorize(provider_repository_id, purpose)
            except github_app.GitHubAppRepositoryUnauthorized:
                self._cache.pop(cache_key, None)
                raise
            cached = self._cache.get(cache_key)
            if (
                cached is not None
                and cached.provider_installation_id
                == authorization.provider_installation_id
                and cached.token.expires_at - moment > _TOKEN_REFRESH_MARGIN
            ):
                return cached.token
            token = self._exchange(authorization, purpose, moment)
            self._cache[cache_key] = _CachedToken(
                token=token,
                provider_installation_id=authorization.provider_installation_id,
            )
            return token

    def invalidate(
        self,
        provider_repository_id: int,
        *,
        purpose: GitHubAppTokenPurpose = "review_read",
    ) -> None:
        """Forget one cached repository token before a single credential retry."""
        if type(provider_repository_id) is not int or provider_repository_id < 1:
            raise ValueError("provider_repository_id must be positive")
        cache_key = (provider_repository_id, purpose)
        with self._scope_lock(cache_key):
            self._cache.pop(cache_key, None)

    def _scope_lock(
        self, cache_key: tuple[int, GitHubAppTokenPurpose]
    ) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(cache_key, threading.Lock())

    def _authorize(
        self,
        provider_repository_id: int,
        purpose: GitHubAppTokenPurpose,
    ) -> github_app.GitHubAppAuthorization:
        with self._postgres.transaction() as connection:
            if purpose == "publication":
                return github_app.authorize_review_publication(
                    connection,
                    provider_repository_id,
                    profile_key=self._profile,
                )
            return github_app.authorize_review_read(
                connection,
                provider_repository_id,
                profile_key=self._profile,
            )

    def _exchange(
        self,
        authorization: github_app.GitHubAppAuthorization,
        purpose: GitHubAppTokenPurpose,
        now: datetime,
    ) -> InstallationToken:
        return self._authenticator.installation_token(
            authorization.provider_installation_id,
            repository_ids=(authorization.provider_repository_id,),
            permissions=_PURPOSE_PERMISSIONS[purpose],
            now=now,
        )
