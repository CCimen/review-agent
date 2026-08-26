"""Sequential recoverable worker for direct GitHub App admission deliveries."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import logging
import os
import socket
import threading
import time
import uuid

import psycopg

from ..postgres import webhook_deliveries
from ..postgres.runtime import PostgreSQLRuntime, PostgreSQLUnavailable
from .app_processor import GitHubAppProcessor


logger = logging.getLogger(__name__)
_TRANSIENT_DATABASE_ERRORS = (
    PostgreSQLUnavailable,
    psycopg.errors.DeadlockDetected,
    psycopg.errors.QueryCanceled,
    psycopg.errors.LockNotAvailable,
)


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
        processor: GitHubAppProcessor,
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
            except _TRANSIENT_DATABASE_ERRORS as exc:
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
