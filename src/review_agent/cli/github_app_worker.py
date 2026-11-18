"""Run the opt-in direct GitHub App admission worker."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import timedelta
import logging
import os
import signal
import socket
import sys
import threading
import time
import uuid

from ..github.app_processor import WebhookDeliveryProcessor, WebhookDeliveryConfig
from ..github.gateway import GitHubGatewayProtocolError
from ..github.gateway_client import ReviewGitHubGatewayClient
from ..postgres import webhook_deliveries
from ..postgres.deployment_settings import apply_at_startup
from ..postgres.runtime import (
    TRANSIENT_DATABASE_ERRORS,
    PostgreSQLRuntime,
    PostgreSQLRuntimeRole,
)
from ..postgres.telemetry import WorkerTelemetry
from ..review import contract as review_contract
from ..settings import ReviewAgentSettings


logger = logging.getLogger(__name__)
class GitHubAppWorkerConfigurationError(ValueError):
    """The GitHub App worker cannot start from its supplied configuration."""


@dataclass(frozen=True, slots=True)
class GitHubAppWorkerPolicy:
    poll_interval: timedelta = timedelta(seconds=2)
    recovery_interval: timedelta = timedelta(seconds=30)
    recovery_batch_size: int = 100
    database_backoff_initial: timedelta = timedelta(seconds=1)
    database_backoff_maximum: timedelta = timedelta(seconds=30)

    def __post_init__(self) -> None:
        for name, value in (
            ("poll_interval", self.poll_interval),
            ("recovery_interval", self.recovery_interval),
            ("database_backoff_initial", self.database_backoff_initial),
            ("database_backoff_maximum", self.database_backoff_maximum),
        ):
            if value <= timedelta(0):
                raise GitHubAppWorkerConfigurationError(f"{name} must be positive")
        if self.recovery_batch_size < 1:
            raise GitHubAppWorkerConfigurationError(
                "recovery_batch_size must be positive"
            )
        if self.database_backoff_initial > self.database_backoff_maximum:
            raise GitHubAppWorkerConfigurationError(
                "database_backoff_initial must not exceed database_backoff_maximum"
            )


class GitHubAppWorker:
    """Recover leases and process at most one App delivery at a time."""

    def __init__(
        self,
        runtime: PostgreSQLRuntime,
        processor: WebhookDeliveryProcessor,
        policy: GitHubAppWorkerPolicy,
        *,
        lease_owner: str,
        stop_event: threading.Event,
    ) -> None:
        owner = lease_owner.strip()
        if not owner:
            raise GitHubAppWorkerConfigurationError("lease_owner is required")
        self._runtime = runtime
        self._processor = processor
        self._policy = policy
        self._lease_owner = owner
        self._stop = stop_event
        self._next_recovery_at = 0.0

    def run(self, *, once: bool = False) -> None:
        backoff = self._policy.database_backoff_initial
        while not self._stop.is_set():
            try:
                self._recover_if_due()
                result = self._processor.process_next(lease_owner=self._lease_owner)
            except TRANSIENT_DATABASE_ERRORS as exc:
                if once:
                    raise
                logger.warning(
                    "GitHub App delivery deferred after transient database failure: %s",
                    type(exc).__name__,
                )
                if self._stop.wait(backoff.total_seconds()):
                    return
                backoff = min(backoff * 2, self._policy.database_backoff_maximum)
                continue
            backoff = self._policy.database_backoff_initial
            if result is not None:
                logger.info(
                    "GitHub App delivery %d finished with status=%s reason=%s",
                    result.delivery_id,
                    result.status,
                    result.reason or "none",
                )
            if once:
                return
            if result is None and self._stop.wait(
                self._policy.poll_interval.total_seconds()
            ):
                return

    def _recover_if_due(self) -> None:
        now = time.monotonic()
        if now < self._next_recovery_at:
            return
        self._next_recovery_at = now + self._policy.recovery_interval.total_seconds()
        with self._runtime.transaction() as connection:
            webhook_deliveries.recover_expired_deliveries(
                connection,
                limit=self._policy.recovery_batch_size,
                actor=self._lease_owner,
            )


def default_github_app_worker_name() -> str:
    """Return one bounded process-unique lease owner without credentials."""
    return (f"github-app:{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:12]}")[
        :120
    ]


def _positive_integer(name: str, default: str | None = None) -> int:
    raw = os.environ.get(name, default or "").strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise GitHubAppWorkerConfigurationError(f"{name} must be an integer") from exc
    if value < 1:
        raise GitHubAppWorkerConfigurationError(f"{name} must be positive")
    return value


def _nonnegative_integer(name: str, default: str) -> int:
    raw = os.environ.get(name, default).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise GitHubAppWorkerConfigurationError(f"{name} must be an integer") from exc
    if value < 0:
        raise GitHubAppWorkerConfigurationError(f"{name} must be zero or greater")
    return value


def _seconds(name: str, default: str) -> timedelta:
    raw = os.environ.get(name, default).strip()
    try:
        value = float(raw)
        return timedelta(seconds=value)
    except (OverflowError, ValueError) as exc:
        raise GitHubAppWorkerConfigurationError(f"{name} must be a number") from exc


def _gateway_client() -> ReviewGitHubGatewayClient:
    gateway_url = os.environ.get("REVIEW_AGENT_GITHUB_GATEWAY_URL", "").strip()
    if not gateway_url:
        raise GitHubAppWorkerConfigurationError(
            "REVIEW_AGENT_GITHUB_GATEWAY_URL is required"
        )
    try:
        return ReviewGitHubGatewayClient(gateway_url)
    except GitHubGatewayProtocolError as exc:
        raise GitHubAppWorkerConfigurationError(str(exc)) from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--once", action="store_true", help="claim at most one delivery"
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stdout,
        format="%(levelname)s %(name)s %(message)s",
    )
    stop = threading.Event()

    def request_stop(_signal: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    configured = ReviewAgentSettings.from_environment()
    try:
        gateway = _gateway_client()
        policy = GitHubAppWorkerPolicy(
            poll_interval=_seconds("REVIEW_AGENT_GITHUB_APP_POLL_SECONDS", "2"),
            recovery_interval=_seconds(
                "REVIEW_AGENT_GITHUB_APP_RECOVERY_SECONDS", "30"
            ),
            recovery_batch_size=_positive_integer(
                "REVIEW_AGENT_GITHUB_APP_RECOVERY_BATCH_SIZE", "100"
            ),
            database_backoff_initial=_seconds(
                "REVIEW_AGENT_GITHUB_APP_DATABASE_BACKOFF_SECONDS", "1"
            ),
            database_backoff_maximum=_seconds(
                "REVIEW_AGENT_GITHUB_APP_DATABASE_BACKOFF_MAX_SECONDS", "30"
            ),
        )
    except GitHubAppWorkerConfigurationError as exc:
        parser.error(str(exc))
    runtime = PostgreSQLRuntime(
        configured.postgres_database_url,
        role=PostgreSQLRuntimeRole.WORKER,
        worker_concurrency=1,
    )
    runtime.open()
    apply_at_startup(runtime, "admission")
    try:
        processor = WebhookDeliveryProcessor(
            postgres=runtime,
            gateway=gateway,
            config=WebhookDeliveryConfig(
                profile=configured.profile,
                policy_revision=configured.policy_revision(),
                job_priority=_nonnegative_integer("REVIEW_AGENT_JOB_PRIORITY", "0"),
                job_max_attempts=_positive_integer(
                    "REVIEW_AGENT_JOB_MAX_ATTEMPTS", "3"
                ),
                active_job_limit=_positive_integer(
                    "REVIEW_AGENT_ACTIVE_JOB_LIMIT", "100"
                ),
                admission_max_age=timedelta(
                    seconds=_positive_integer(
                        "REVIEW_AGENT_GITHUB_APP_ADMISSION_MAX_AGE_SECONDS", "86400"
                    )
                ),
                capacity_retry_delay=timedelta(
                    seconds=_positive_integer(
                        "REVIEW_AGENT_GITHUB_APP_CAPACITY_RETRY_SECONDS", "300"
                    )
                ),
                contract_environment=review_contract.deployment_environment(
                    os.environ
                ),
            ),
        )
        lease_owner = default_github_app_worker_name()
        with WorkerTelemetry(
            runtime,
            kind="webhook",
            lease_owner=lease_owner,
            capacity=1,
            stop_event=stop,
        ):
            GitHubAppWorker(
                runtime,
                processor,
                policy,
                lease_owner=lease_owner,
                stop_event=stop,
            ).run(once=args.once)
        return 0
    finally:
        runtime.close()


if __name__ == "__main__":
    raise SystemExit(main())
