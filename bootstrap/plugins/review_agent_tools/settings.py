"""Typed, stateless interpretation of core review-agent environment settings."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import NewType
from urllib.parse import urlsplit

DEFAULT_PROFILE = "sundsvall-standard"
DEFAULT_POLICY_REVISION = "policy-v1"
DEFAULT_PUBLISH_MAX_BYTES = 60_000
MIN_PUBLISH_MAX_BYTES = 1_000
MAX_PUBLISH_MAX_BYTES = 65_000
PostgresDatabaseUrl = NewType("PostgresDatabaseUrl", str)
_PROFILE_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class SettingsError(ValueError):
    """A configured value cannot be interpreted safely."""


@dataclass(frozen=True, slots=True)
class ReviewAgentSettings:
    """Read one environment mapping without caching or validating unused values."""

    environment: Mapping[str, str]

    @classmethod
    def from_environment(cls) -> ReviewAgentSettings:
        return cls(os.environ)

    @property
    def allowed_repositories(self) -> frozenset[str]:
        raw = self.environment.get("REVIEW_AGENT_ALLOWED_REPOSITORIES", "")
        return frozenset(
            item.strip().lower() for item in raw.split(",") if item.strip()
        )

    @property
    def github_read_token(self) -> str:
        return self.environment.get("GITHUB_READ_TOKEN", "").strip()

    @property
    def github_publish_token(self) -> str:
        return self.environment.get("REVIEW_AGENT_PUBLISH_GH_TOKEN", "").strip()

    @property
    def postgres_database_url(self) -> PostgresDatabaseUrl:
        value = self.environment.get("REVIEW_AGENT_DATABASE_URL", "").strip()
        if not value:
            raise SettingsError("REVIEW_AGENT_DATABASE_URL is required")
        try:
            parsed = urlsplit(value)
        except ValueError as exc:
            raise SettingsError(
                "REVIEW_AGENT_DATABASE_URL must be a PostgreSQL URL"
            ) from exc
        if parsed.scheme not in {"postgresql", "postgres"} or any(
            character.isspace() for character in value
        ):
            raise SettingsError(
                "REVIEW_AGENT_DATABASE_URL must be a PostgreSQL URL"
            )
        if not parsed.hostname or not parsed.path.strip("/"):
            raise SettingsError(
                "REVIEW_AGENT_DATABASE_URL must include a host and database name"
            )
        return PostgresDatabaseUrl(value)

    @property
    def publish_max_bytes(self) -> int:
        raw = self.environment.get("REVIEW_AGENT_PUBLISH_MAX_BYTES", "").strip()
        if not raw:
            return DEFAULT_PUBLISH_MAX_BYTES
        try:
            value = int(raw)
        except ValueError as exc:
            raise SettingsError(
                "REVIEW_AGENT_PUBLISH_MAX_BYTES must be an integer"
            ) from exc
        return max(MIN_PUBLISH_MAX_BYTES, min(value, MAX_PUBLISH_MAX_BYTES))

    def policy_revision(self, explicit: str | None = None) -> str:
        raw = explicit or self.environment.get(
            "REVIEW_AGENT_POLICY_REVISION", DEFAULT_POLICY_REVISION
        )
        value = " ".join(str(raw or "").strip().split())
        if not value:
            raise SettingsError("policy_revision is required")
        if len(value) > 120:
            raise SettingsError("policy_revision exceeds 120 characters")
        return value

    @property
    def profile(self) -> str:
        value = self.environment.get("REVIEW_AGENT_PROFILE", DEFAULT_PROFILE).strip()
        if len(value) > 80 or _PROFILE_RE.fullmatch(value) is None:
            raise SettingsError(
                "REVIEW_AGENT_PROFILE must use lower-case words and hyphens"
            )
        return value

    @property
    def feedback_enabled(self) -> bool:
        return self.environment.get(
            "REVIEW_AGENT_FEEDBACK_ENABLED", ""
        ).strip().lower() in {"1", "true", "yes", "on"}
