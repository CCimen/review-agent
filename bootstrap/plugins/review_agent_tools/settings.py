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
    def github_gateway_url(self) -> str:
        value = self.environment.get("REVIEW_AGENT_GITHUB_GATEWAY_URL", "").strip()
        if not value:
            raise SettingsError("REVIEW_AGENT_GITHUB_GATEWAY_URL is required")
        try:
            parsed = urlsplit(value)
        except ValueError as exc:
            raise SettingsError(
                "REVIEW_AGENT_GITHUB_GATEWAY_URL must be one HTTP origin"
            ) from exc
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise SettingsError(
                "REVIEW_AGENT_GITHUB_GATEWAY_URL must be one HTTP origin"
            )
        return value.rstrip("/")

    @property
    def hermes_health_url(self) -> str:
        value = self.environment.get(
            "REVIEW_AGENT_HERMES_CHAT_URL",
            "http://127.0.0.1:8642/v1/chat/completions",
        ).strip()
        try:
            parsed = urlsplit(value)
        except ValueError as exc:
            raise SettingsError(
                "REVIEW_AGENT_HERMES_CHAT_URL must be one HTTP endpoint"
            ) from exc
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise SettingsError(
                "REVIEW_AGENT_HERMES_CHAT_URL must be one HTTP endpoint"
            )
        return f"{parsed.scheme}://{parsed.netloc}/health"

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

    @property
    def publication_max_attempts(self) -> int:
        raw = self.environment.get(
            "REVIEW_AGENT_PUBLICATION_MAX_ATTEMPTS", "3"
        ).strip()
        try:
            value = int(raw)
        except ValueError as exc:
            raise SettingsError(
                "REVIEW_AGENT_PUBLICATION_MAX_ATTEMPTS must be an integer"
            ) from exc
        if value < 1:
            raise SettingsError(
                "REVIEW_AGENT_PUBLICATION_MAX_ATTEMPTS must be positive"
            )
        return value

    @property
    def active_job_limit(self) -> int:
        raw = self.environment.get("REVIEW_AGENT_ACTIVE_JOB_LIMIT", "100").strip()
        try:
            value = int(raw)
        except ValueError as exc:
            raise SettingsError(
                "REVIEW_AGENT_ACTIVE_JOB_LIMIT must be an integer"
            ) from exc
        if value < 1:
            raise SettingsError(
                "REVIEW_AGENT_ACTIVE_JOB_LIMIT must be positive"
            )
        return value

    @property
    def operator_page_max_items(self) -> int:
        raw = self.environment.get(
            "REVIEW_AGENT_OPERATOR_PAGE_MAX_ITEMS", "100"
        ).strip()
        try:
            value = int(raw)
        except ValueError as exc:
            raise SettingsError(
                "REVIEW_AGENT_OPERATOR_PAGE_MAX_ITEMS must be an integer"
            ) from exc
        if value < 1:
            raise SettingsError(
                "REVIEW_AGENT_OPERATOR_PAGE_MAX_ITEMS must be positive"
            )
        return value

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
