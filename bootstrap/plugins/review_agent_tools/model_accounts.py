"""Redacted account identity projected inside the owning Hermes process."""

from dataclasses import dataclass
from enum import StrEnum
import hashlib
from importlib import import_module
import json
from pathlib import Path
from typing import Protocol, cast
from uuid import UUID

import jwt

from .review_contract import ReviewContract


class ModelProvider(StrEnum):
    CODEX = "openai-codex"
    ANTHROPIC = "anthropic"


class AccountAvailability(StrEnum):
    AVAILABLE = "available"
    DISCONNECTED = "disconnected"
    MULTIPLE_ACCOUNTS = "multiple_accounts"
    IDENTITY_UNAVAILABLE = "identity_unavailable"
    ISOLATION_REQUIRED = "isolation_required"


@dataclass(frozen=True, slots=True)
class RuntimeAccount:
    provider: ModelProvider
    availability: AccountAvailability
    account_count: int
    identity_sha256: str | None


@dataclass(frozen=True, slots=True)
class ManagedRuntimeStatus:
    runtime_key: str
    instance_id: UUID
    contract: ReviewContract
    accounts: tuple[RuntimeAccount, ...]


class _Credential(Protocol):
    access_token: str
    auth_type: str


class _Pool(Protocol):
    def entries(self) -> list[_Credential]: ...


class _PoolModule(Protocol):
    def load_pool(self, provider: str) -> _Pool: ...


class _HermesHomes(Protocol):
    def get_hermes_home(self) -> Path: ...
    def get_default_hermes_root(self) -> Path: ...


def account_identity(
    provider: ModelProvider, *, token: str, auth_type: str
) -> str | None:
    """Identify a locally owned credential without persisting or returning it.

    OAuth claims are an identity hint, never authentication or authorization.
    Hermes still verifies and refreshes the actual credential with its provider.
    """
    if not token or len(token) > 65536:
        return None
    if provider is ModelProvider.CODEX:
        try:
            claims = cast(
                dict[str, object],
                jwt.decode(token, options={"verify_signature": False}),
            )
        except jwt.PyJWTError:
            return None
        auth = claims.get("https://api.openai.com/auth")
        if not isinstance(auth, dict):
            return None
        account = cast(dict[str, object], auth).get("chatgpt_account_id")
        subject = cast(dict[str, object], auth).get("chatgpt_user_id") or claims.get(
            "sub"
        )
        if (
            not isinstance(account, str)
            or not account
            or not isinstance(subject, str)
            or not subject
        ):
            return None
        identity = [provider.value, account, subject]
    elif auth_type == "api_key":
        identity = [provider.value, token]
    else:
        # The managed console does not infer an Anthropic consumer account from
        # an opaque OAuth token. Authorized API-key integrations remain supported.
        return None
    return hashlib.sha256(
        json.dumps(identity, separators=(",", ":")).encode()
    ).hexdigest()


def read_accounts() -> tuple[RuntimeAccount, ...]:
    """Use Hermes' credential owner; the companion never reads auth files."""
    homes = cast(_HermesHomes, import_module("hermes_constants"))
    if homes.get_hermes_home().resolve() != homes.get_default_hermes_root().resolve():
        return tuple(
            RuntimeAccount(provider, AccountAvailability.ISOLATION_REQUIRED, 0, None)
            for provider in ModelProvider
        )
    pools = cast(_PoolModule, import_module("agent.credential_pool"))
    accounts: list[RuntimeAccount] = []
    for provider in ModelProvider:
        entries = pools.load_pool(provider.value).entries()
        count = len(entries)
        fingerprint = None
        if count == 0:
            availability = AccountAvailability.DISCONNECTED
        elif count != 1:
            availability = AccountAvailability.MULTIPLE_ACCOUNTS
        else:
            entry = entries[0]
            fingerprint = account_identity(
                provider, token=entry.access_token, auth_type=entry.auth_type
            )
            availability = (
                AccountAvailability.AVAILABLE
                if fingerprint is not None
                else AccountAvailability.IDENTITY_UNAVAILABLE
            )
        accounts.append(
            RuntimeAccount(provider, availability, min(count, 1000), fingerprint)
        )
    return tuple(accounts)
