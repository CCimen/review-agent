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
    job_priority: int = 0
    job_priority_aging_seconds: int = 900
    job_retry_seconds: int = 30
    job_poll_seconds: int = 2
    job_recovery_seconds: int = 30
    job_recovery_batch_size: int = 100
    publication_lease_seconds: int = 120
    publication_heartbeat_seconds: int = 30
    publication_retry_seconds: int = 30
    publication_poll_seconds: int = 2
    admission_max_age_seconds: int = 86400
    github_app_max_body_bytes: int = 2097152
    admission_max_concurrent_requests: int = 8
    admission_request_timeout_seconds: int = 30
    github_gateway_max_concurrent_requests: int = 8
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
        if (
            type(self.job_priority) is not int
            or not 0 <= self.job_priority <= 2_147_483_647
        ):
            raise ValueError("Job priority must be between 0 and 2147483647")
        if self.github_app_max_body_bytes > 2_097_152:
            raise ValueError("Webhook size must not exceed 2097152 bytes")
        if self.publication_heartbeat_seconds * 2 >= self.publication_lease_seconds:
            raise ValueError(
                "Publication heartbeat must be less than half the lease duration"
            )
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
            "REVIEW_AGENT_JOB_PRIORITY": str(self.job_priority),
            "REVIEW_AGENT_JOB_PRIORITY_AGING_SECONDS": str(
                self.job_priority_aging_seconds
            ),
            "REVIEW_AGENT_JOB_RETRY_SECONDS": str(self.job_retry_seconds),
            "REVIEW_AGENT_JOB_POLL_SECONDS": str(self.job_poll_seconds),
            "REVIEW_AGENT_JOB_RECOVERY_SECONDS": str(self.job_recovery_seconds),
            "REVIEW_AGENT_JOB_RECOVERY_BATCH_SIZE": str(self.job_recovery_batch_size),
            "REVIEW_AGENT_PUBLICATION_LEASE_SECONDS": str(
                self.publication_lease_seconds
            ),
            "REVIEW_AGENT_PUBLICATION_HEARTBEAT_SECONDS": str(
                self.publication_heartbeat_seconds
            ),
            "REVIEW_AGENT_PUBLICATION_RETRY_SECONDS": str(
                self.publication_retry_seconds
            ),
            "REVIEW_AGENT_PUBLICATION_POLL_SECONDS": str(self.publication_poll_seconds),
            "REVIEW_AGENT_GITHUB_APP_ADMISSION_MAX_AGE_SECONDS": str(
                self.admission_max_age_seconds
            ),
            "REVIEW_AGENT_GITHUB_APP_MAX_BODY_BYTES": str(
                self.github_app_max_body_bytes
            ),
            "REVIEW_AGENT_ADMISSION_MAX_CONCURRENT_REQUESTS": str(
                self.admission_max_concurrent_requests
            ),
            "REVIEW_AGENT_ADMISSION_REQUEST_TIMEOUT_SECONDS": str(
                self.admission_request_timeout_seconds
            ),
            "REVIEW_AGENT_GITHUB_GATEWAY_MAX_CONCURRENT_REQUESTS": str(
                self.github_gateway_max_concurrent_requests
            ),
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
            job_priority=int(env.get("REVIEW_AGENT_JOB_PRIORITY", "0")),
            job_priority_aging_seconds=int(
                env.get("REVIEW_AGENT_JOB_PRIORITY_AGING_SECONDS", "900")
            ),
            job_retry_seconds=int(env.get("REVIEW_AGENT_JOB_RETRY_SECONDS", "30")),
            job_poll_seconds=int(env.get("REVIEW_AGENT_JOB_POLL_SECONDS", "2")),
            job_recovery_seconds=int(
                env.get("REVIEW_AGENT_JOB_RECOVERY_SECONDS", "30")
            ),
            job_recovery_batch_size=int(
                env.get("REVIEW_AGENT_JOB_RECOVERY_BATCH_SIZE", "100")
            ),
            publication_lease_seconds=int(
                env.get("REVIEW_AGENT_PUBLICATION_LEASE_SECONDS", "120")
            ),
            publication_heartbeat_seconds=int(
                env.get("REVIEW_AGENT_PUBLICATION_HEARTBEAT_SECONDS", "30")
            ),
            publication_retry_seconds=int(
                env.get("REVIEW_AGENT_PUBLICATION_RETRY_SECONDS", "30")
            ),
            publication_poll_seconds=int(
                env.get("REVIEW_AGENT_PUBLICATION_POLL_SECONDS", "2")
            ),
            admission_max_age_seconds=int(
                env.get("REVIEW_AGENT_GITHUB_APP_ADMISSION_MAX_AGE_SECONDS", "86400")
            ),
            github_app_max_body_bytes=int(
                env.get("REVIEW_AGENT_GITHUB_APP_MAX_BODY_BYTES", "2097152")
            ),
            admission_max_concurrent_requests=int(
                env.get("REVIEW_AGENT_ADMISSION_MAX_CONCURRENT_REQUESTS", "8")
            ),
            admission_request_timeout_seconds=int(
                env.get("REVIEW_AGENT_ADMISSION_REQUEST_TIMEOUT_SECONDS", "30")
            ),
            github_gateway_max_concurrent_requests=int(
                env.get("REVIEW_AGENT_GITHUB_GATEWAY_MAX_CONCURRENT_REQUESTS", "8")
            ),
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
        "REVIEW_AGENT_JOB_PRIORITY_AGING_SECONDS",
        "REVIEW_AGENT_JOB_RETRY_SECONDS",
        "REVIEW_AGENT_JOB_POLL_SECONDS",
        "REVIEW_AGENT_JOB_RECOVERY_SECONDS",
        "REVIEW_AGENT_JOB_RECOVERY_BATCH_SIZE",
        "REVIEW_AGENT_PUBLICATION_LEASE_SECONDS",
        "REVIEW_AGENT_PUBLICATION_HEARTBEAT_SECONDS",
        "REVIEW_AGENT_PUBLICATION_RETRY_SECONDS",
        "REVIEW_AGENT_PUBLICATION_POLL_SECONDS",
        "REVIEW_AGENT_GITHUB_APP_ADMISSION_MAX_AGE_SECONDS",
        "REVIEW_AGENT_GITHUB_APP_MAX_BODY_BYTES",
        "REVIEW_AGENT_ADMISSION_MAX_CONCURRENT_REQUESTS",
        "REVIEW_AGENT_ADMISSION_REQUEST_TIMEOUT_SECONDS",
        "REVIEW_AGENT_GITHUB_GATEWAY_MAX_CONCURRENT_REQUESTS",
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
