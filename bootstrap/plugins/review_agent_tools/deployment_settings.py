"""Validated operator policy, loaded at service startup and review admission."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from .review_contract import REASONING_EFFORTS
from .settings import ReviewAgentSettings


@dataclass(frozen=True, slots=True)
class DeploymentSettings:
    active_job_limit: int = 100
    capacity_retry_seconds: int = 300
    worker_concurrency: int = 4
    job_max_attempts: int = 3
    job_lease_seconds: int = 120
    job_heartbeat_seconds: int = 30
    hermes_timeout_seconds: int = 7200
    publish_max_bytes: int = 60000
    publication_max_attempts: int = 3
    feedback_enabled: bool = False
    code_graph_enabled: bool = False
    code_graph_embeddings: Literal["none", "openai"] = "none"
    model_provider: Literal["openai-codex", "anthropic"] = "openai-codex"
    model: str = "gpt-5.6-sol"
    reasoning_effort: Literal[
        "none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"
    ] = "xhigh"

    def __post_init__(self) -> None:
        for name, value in self.environment().items():
            if name in _INTEGER_ENV and (not value.isdecimal() or int(value) < 1):
                raise ValueError(f"{name} must be a positive integer")
        if self.job_heartbeat_seconds * 2 >= self.job_lease_seconds:
            raise ValueError("Heartbeat must be less than half the lease duration")
        if not 1000 <= self.publish_max_bytes <= 65000:
            raise ValueError("Publication size must be between 1000 and 65000 bytes")
        if self.model_provider not in {"openai-codex", "anthropic"}:
            raise ValueError("Unsupported model provider")
        if self.reasoning_effort not in REASONING_EFFORTS:
            raise ValueError("Unsupported reasoning effort")
        if (
            not self.model.strip()
            or len(self.model) > 200
            or not self.model.isprintable()
        ):
            raise ValueError("Model must be printable and at most 200 characters")
        if self.code_graph_embeddings not in {"none", "openai"}:
            raise ValueError("Unsupported embedding mode")

    def environment(self) -> dict[str, str]:
        return {
            "REVIEW_AGENT_ACTIVE_JOB_LIMIT": str(self.active_job_limit),
            "REVIEW_AGENT_GITHUB_APP_CAPACITY_RETRY_SECONDS": str(
                self.capacity_retry_seconds
            ),
            "REVIEW_AGENT_WORKER_CONCURRENCY": str(self.worker_concurrency),
            "REVIEW_AGENT_JOB_MAX_ATTEMPTS": str(self.job_max_attempts),
            "REVIEW_AGENT_JOB_LEASE_SECONDS": str(self.job_lease_seconds),
            "REVIEW_AGENT_JOB_HEARTBEAT_SECONDS": str(self.job_heartbeat_seconds),
            "REVIEW_AGENT_HERMES_TIMEOUT_SECONDS": str(self.hermes_timeout_seconds),
            "REVIEW_AGENT_PUBLISH_MAX_BYTES": str(self.publish_max_bytes),
            "REVIEW_AGENT_PUBLICATION_MAX_ATTEMPTS": str(self.publication_max_attempts),
            "REVIEW_AGENT_FEEDBACK_ENABLED": str(self.feedback_enabled).lower(),
            "REVIEW_AGENT_CODE_GRAPH_ENABLED": str(self.code_graph_enabled).lower(),
            "REVIEW_AGENT_CODE_GRAPH_EMBEDDINGS": self.code_graph_embeddings,
            "REVIEW_AGENT_MODEL_PROVIDER": self.model_provider,
            "REVIEW_AGENT_MODEL": self.model,
            "REVIEW_AGENT_REASONING_EFFORT": self.reasoning_effort,
        }

    @classmethod
    def from_environment(
        cls, environment: Mapping[str, str] | None = None
    ) -> DeploymentSettings:
        env = os.environ if environment is None else environment
        core = ReviewAgentSettings(env)
        provider = env.get("REVIEW_AGENT_MODEL_PROVIDER", "openai-codex")
        effort = env.get("REVIEW_AGENT_REASONING_EFFORT", "xhigh")
        embeddings = env.get("REVIEW_AGENT_CODE_GRAPH_EMBEDDINGS", "none")
        if (
            provider not in {"openai-codex", "anthropic"}
            or effort not in REASONING_EFFORTS
            or embeddings not in {"none", "openai"}
        ):
            raise ValueError("Unsupported model or embedding selection")
        from typing import cast

        return cls(
            active_job_limit=core.active_job_limit,
            capacity_retry_seconds=int(
                env.get("REVIEW_AGENT_GITHUB_APP_CAPACITY_RETRY_SECONDS", "300")
            ),
            worker_concurrency=int(env.get("REVIEW_AGENT_WORKER_CONCURRENCY", "4")),
            job_max_attempts=int(env.get("REVIEW_AGENT_JOB_MAX_ATTEMPTS", "3")),
            job_lease_seconds=int(env.get("REVIEW_AGENT_JOB_LEASE_SECONDS", "120")),
            job_heartbeat_seconds=int(
                env.get("REVIEW_AGENT_JOB_HEARTBEAT_SECONDS", "30")
            ),
            hermes_timeout_seconds=int(
                env.get("REVIEW_AGENT_HERMES_TIMEOUT_SECONDS", "7200")
            ),
            publish_max_bytes=core.publish_max_bytes,
            publication_max_attempts=core.publication_max_attempts,
            feedback_enabled=core.feedback_enabled,
            code_graph_enabled=env.get(
                "REVIEW_AGENT_CODE_GRAPH_ENABLED", "false"
            ).lower()
            == "true",
            code_graph_embeddings=cast(Literal["none", "openai"], embeddings),
            model_provider=cast(Literal["openai-codex", "anthropic"], provider),
            model=env.get("REVIEW_AGENT_MODEL", "gpt-5.6-sol"),
            reasoning_effort=cast(
                Literal[
                    "none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"
                ],
                effort,
            ),
        )


_INTEGER_ENV = frozenset(
    {
        "REVIEW_AGENT_ACTIVE_JOB_LIMIT",
        "REVIEW_AGENT_GITHUB_APP_CAPACITY_RETRY_SECONDS",
        "REVIEW_AGENT_WORKER_CONCURRENCY",
        "REVIEW_AGENT_JOB_MAX_ATTEMPTS",
        "REVIEW_AGENT_JOB_LEASE_SECONDS",
        "REVIEW_AGENT_JOB_HEARTBEAT_SECONDS",
        "REVIEW_AGENT_HERMES_TIMEOUT_SECONDS",
        "REVIEW_AGENT_PUBLISH_MAX_BYTES",
        "REVIEW_AGENT_PUBLICATION_MAX_ATTEMPTS",
    }
)
