"""Startup configuration for the console's organizational identity provider."""

from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

from pydantic import SecretStr

from .settings import SettingsError


def identity_https_url(value: str, *, allow_query: bool = False) -> str:
    parsed = urlsplit(value)
    if (
        len(value) > 2048
        or parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or (parsed.query and not allow_query)
        or parsed.fragment
        or any(character.isspace() for character in value)
    ):
        raise ValueError(
            "Identity provider URLs must use HTTPS without credentials, query or fragment"
        )
    return value


@dataclass(frozen=True, slots=True)
class OIDCSettings:
    issuer: str
    client_id: str
    client_secret: SecretStr
    name: str


@dataclass(frozen=True, slots=True)
class IdentitySettings:
    oidc: OIDCSettings | None = None
    scim_token: SecretStr | None = None

    @classmethod
    def load(cls, environment: Mapping[str, str]) -> "IdentitySettings":
        issuer = environment.get("REVIEW_AGENT_OIDC_ISSUER", "").strip()
        client_id = environment.get("REVIEW_AGENT_OIDC_CLIENT_ID", "").strip()
        secret = environment.get("REVIEW_AGENT_OIDC_CLIENT_SECRET", "").strip()
        token = environment.get("REVIEW_AGENT_SCIM_TOKEN", "").strip()
        if not any((issuer, client_id, secret, token)):
            return cls()
        if not all((issuer, client_id, secret)):
            raise SettingsError(
                "OIDC requires issuer, client ID and client secret; SCIM requires OIDC configuration"
            )
        name = environment.get("REVIEW_AGENT_OIDC_NAME", "Organization sign-in").strip()
        try:
            identity_https_url(issuer)
            if not 1 <= len(client_id) <= 255 or not 1 <= len(name) <= 80:
                raise ValueError("Invalid identity provider configuration")
            if token and not 32 <= len(token) <= 512:
                raise ValueError("SCIM credentials must contain 32–512 characters")
        except ValueError as exc:
            raise SettingsError("Invalid OIDC or SCIM configuration") from exc
        return cls(
            oidc=OIDCSettings(issuer, client_id, SecretStr(secret), name),
            scim_token=SecretStr(token) if token else None,
        )
