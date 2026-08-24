"""Recoverable PostgreSQL-backed publication worker."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import logging
import os
import socket
import threading
import uuid

import psycopg

from . import failure_codes, review_run_application
from .github.publication import GitHubPublicationGateway
from .postgres import publications
from .postgres.runtime import PostgreSQLRuntime, PostgreSQLUnavailable
from .review_publication_application import publish_postgres_publication


logger = logging.getLogger(__name__)
_TRANSIENT_DATABASE_ERRORS = (
    PostgreSQLUnavailable,
    psycopg.errors.DeadlockDetected,
    psycopg.errors.QueryCanceled,
    psycopg.errors.LockNotAvailable,
)


class PublisherConfigurationError(ValueError):
    """The publisher cannot start from its supplied configuration."""


@dataclass(frozen=True, slots=True)
class PublisherPolicy:
    lease_duration: timedelta
    heartbeat_interval: timedelta
    retry_delay: timedelta
    poll_interval: timedelta
    max_comment_bytes: int

    def __post_init__(self) -> None:
        for name, value in (
            ("lease_duration", self.lease_duration),
            ("heartbeat_interval", self.heartbeat_interval),
            ("retry_delay", self.retry_delay),
            ("poll_interval", self.poll_interval),
        ):
            if value <= timedelta(0):
                raise PublisherConfigurationError(f"{name} must be positive")
        if self.heartbeat_interval * 2 >= self.lease_duration:
            raise PublisherConfigurationError(
                "heartbeat_interval must be less than half of lease_duration"
            )
        if self.max_comment_bytes < 1:
            raise PublisherConfigurationError("max_comment_bytes must be positive")


class PublicationWorker:
    """Claim and deliver one exact stored publication at a time."""

    def __init__(
        self,
        runtime: PostgreSQLRuntime,
        github: GitHubPublicationGateway,
        policy: PublisherPolicy,
        *,
        lease_owner: str,
        stop_event: threading.Event,
    ) -> None:
        owner = lease_owner.strip()
        if not owner:
            raise PublisherConfigurationError("lease_owner is required")
        self._runtime = runtime
        self._github = github
        self._policy = policy
        self._lease_owner = owner
        self._stop = stop_event

    def run(self, *, once: bool = False) -> None:
        while not self._stop.is_set():
            try:
                claim = self._claim()
            except _TRANSIENT_DATABASE_ERRORS as exc:
                if once:
                    raise
                logger.warning("Publication claim deferred: %s", exc)
                if self._stop.wait(self._policy.poll_interval.total_seconds()):
                    return
                continue
            if claim is None:
                if once or self._stop.wait(self._policy.poll_interval.total_seconds()):
                    return
                continue
            try:
                self._deliver(claim.publication)
            except _TRANSIENT_DATABASE_ERRORS as exc:
                if once:
                    raise
                logger.warning(
                    "Publication %s state update deferred: %s",
                    int(claim.publication.id),
                    exc,
                )
                if self._stop.wait(self._policy.poll_interval.total_seconds()):
                    return
            if once:
                return

    def _claim(self) -> publications.PublicationClaim | None:
        with self._runtime.transaction() as connection:
            exhausted = publications.fail_one_expired_exhausted_publication(
                connection
            )
            if exhausted is not None:
                review_run_application.fail_run_after_publication_in_transaction(
                    connection,
                    exhausted.review_run_id,
                    failure_code=failure_codes.PUBLICATION_ATTEMPTS_EXHAUSTED,
                )
            return publications.claim_next_publication(
                connection,
                lease_owner=self._lease_owner,
                lease_duration=self._policy.lease_duration,
            )

    def _deliver(self, publication: publications.StoredPublication) -> None:
        heartbeat_stop = threading.Event()
        heartbeat = threading.Thread(
            target=self._heartbeat,
            args=(publication, heartbeat_stop),
            name=f"publication-{int(publication.id)}-heartbeat",
        )
        heartbeat.start()
        try:
            publish_postgres_publication(
                self._runtime,
                publication_id=int(publication.id),
                github=self._github,
                max_comment_bytes=self._policy.max_comment_bytes,
                lease_owner=self._lease_owner,
                lease_generation=publication.delivery_lease_generation,
                retry_delay=self._policy.retry_delay,
            )
        except publications.PublicationLeaseLost:
            logger.info("Publication %s lost its lease", int(publication.id))
        finally:
            heartbeat_stop.set()
            heartbeat.join()

    def _heartbeat(
        self,
        publication: publications.StoredPublication,
        stop: threading.Event,
    ) -> None:
        while not stop.wait(self._policy.heartbeat_interval.total_seconds()):
            try:
                with self._runtime.transaction() as connection:
                    publications.heartbeat_publication(
                        connection,
                        publication_id=publication.id,
                        lease_owner=self._lease_owner,
                        lease_generation=publication.delivery_lease_generation,
                        lease_duration=self._policy.lease_duration,
                    )
            except publications.PublicationLeaseLost:
                return
            except Exception as exc:
                logger.warning(
                    "Publication %s heartbeat deferred: %s",
                    int(publication.id),
                    exc,
                )


def default_publisher_name() -> str:
    configured = os.environ.get("REVIEW_AGENT_PUBLISHER_NAME", "").strip()
    return configured or (
        f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:12]}"
    )
