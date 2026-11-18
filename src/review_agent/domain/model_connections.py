"""What the service records about a model provider account, without its credential."""

from dataclasses import dataclass
from enum import StrEnum


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
