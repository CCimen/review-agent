"""Recoverable PostgreSQL-backed publication worker."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import logging
import os
import socket
import threading
import uuid
from typing import Protocol

import psycopg

from . import failure_codes, review_run_application
from .github.publication import GitHubPublicationGateway
from .postgres import publications, review_runs
from .postgres.runtime import PostgreSQLRuntime, PostgreSQLUnavailable
from .review_publication_application import (
    publish_postgres_publication,
    publish_postgres_run_failure_status,
)


logger = logging.getLogger(__name__)
_TRANSIENT_DATABASE_ERRORS = (
    PostgreSQLUnavailable,
    psycopg.errors.DeadlockDetected,
    psycopg.errors.QueryCanceled,
    psycopg.errors.LockNotAvailable,
)


class PublisherConfigurationError(ValueError):
    """The publisher cannot start from its supplied configuration."""


class PublicationGatewayFactory(Protocol):
    def for_publication(
        self,
        *,
        publication_id: int,
        lease_owner: str,
        lease_generation: int,
    ) -> GitHubPublicationGateway: ...

    def for_failure_status(
        self,
        *,
        run_id: int,
        lease_owner: str,
        lease_generation: int,
    ) -> GitHubPublicationGateway: ...

    def for_posted_publication(
        self, *, publication_id: int
    ) -> GitHubPublicationGateway: ...


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


@dataclass(frozen=True, slots=True)
class _ClaimedWork:
    publication: publications.StoredPublication | None = None
    failure_status: review_runs.FailureStatusTarget | None = None


class PublicationWorker:
    """Claim and deliver one exact stored publication at a time."""

    def __init__(
        self,
        runtime: PostgreSQLRuntime,
        github: PublicationGatewayFactory,
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
        self._prefer_failure_status = False

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
                if claim.publication is not None:
                    self._deliver(claim.publication)
                elif claim.failure_status is not None:
                    self._deliver_failure_status(claim.failure_status)
            except _TRANSIENT_DATABASE_ERRORS as exc:
                if once:
                    raise
                work_id = (
                    int(claim.publication.id)
                    if claim.publication is not None
                    else int(claim.failure_status.run_id)
                    if claim.failure_status is not None
                    else 0
                )
                logger.warning(
                    "Publication %s state update deferred: %s",
                    work_id,
                    exc,
                )
                if self._stop.wait(self._policy.poll_interval.total_seconds()):
                    return
            if once:
                return

    def _claim(self) -> _ClaimedWork | None:
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
            if self._prefer_failure_status:
                failure_claim = review_runs.claim_next_failure_status(
                    connection, lease_owner=self._lease_owner,
                    lease_duration=self._policy.lease_duration,
                )
                if failure_claim is not None:
                    self._prefer_failure_status = False
                    return _ClaimedWork(failure_status=failure_claim.target)
            publication_claim = publications.claim_next_publication(
                connection, lease_owner=self._lease_owner,
                lease_duration=self._policy.lease_duration,
            )
            if publication_claim is not None:
                self._prefer_failure_status = True
                if not publication_claim.acquired:
                    return None
                return _ClaimedWork(publication=publication_claim.publication)
            failure_claim = review_runs.claim_next_failure_status(
                connection, lease_owner=self._lease_owner,
                lease_duration=self._policy.lease_duration,
            )
            if failure_claim is None:
                return None
            self._prefer_failure_status = False
            return _ClaimedWork(failure_status=failure_claim.target)

    def _deliver_failure_status(self, target: review_runs.FailureStatusTarget) -> None:
        stop = threading.Event()
        lost = threading.Event()
        heartbeat = threading.Thread(
            target=self._heartbeat_failure_status, args=(target, stop, lost),
            name=f"failure-status-{int(target.run_id)}-heartbeat",
        )
        heartbeat.start()
        try:
            github = self._github.for_failure_status(
                run_id=int(target.run_id),
                lease_owner=self._lease_owner,
                lease_generation=target.delivery_lease_generation,
            )
            publish_postgres_run_failure_status(
                self._runtime, run_id=int(target.run_id), github=github,
                lease_owner=self._lease_owner,
                lease_generation=target.delivery_lease_generation,
                retry_delay=self._policy.retry_delay, lease_lost=lost,
            )
        except review_runs.FailureStatusLeaseLost:
            logger.info("Failure status %s lost its lease", int(target.run_id))
        finally:
            stop.set()
            heartbeat.join()

    def _heartbeat_failure_status(
        self, target: review_runs.FailureStatusTarget,
        stop: threading.Event, lost: threading.Event,
    ) -> None:
        while not stop.wait(self._policy.heartbeat_interval.total_seconds()):
            try:
                with self._runtime.transaction() as connection:
                    review_runs.heartbeat_failure_status(
                        connection, run_id=target.run_id,
                        lease_owner=self._lease_owner,
                        lease_generation=target.delivery_lease_generation,
                        lease_duration=self._policy.lease_duration,
                    )
            except review_runs.FailureStatusLeaseLost:
                lost.set()
                return
            except _TRANSIENT_DATABASE_ERRORS as exc:
                logger.warning("Failure status %s heartbeat deferred: %s", int(target.run_id), exc)

    def _deliver(self, publication: publications.StoredPublication) -> None:
        heartbeat_stop = threading.Event()
        lease_lost = threading.Event()
        heartbeat = threading.Thread(
            target=self._heartbeat,
            args=(publication, heartbeat_stop, lease_lost),
            name=f"publication-{int(publication.id)}-heartbeat",
        )
        heartbeat.start()
        try:
            github = self._github.for_publication(
                publication_id=int(publication.id),
                lease_owner=self._lease_owner,
                lease_generation=publication.delivery_lease_generation,
            )
            posted_github = self._github.for_posted_publication(
                publication_id=int(publication.id)
            )
            publish_postgres_publication(
                self._runtime,
                publication_id=int(publication.id),
                github=github,
                max_comment_bytes=self._policy.max_comment_bytes,
                lease_owner=self._lease_owner,
                lease_generation=publication.delivery_lease_generation,
                retry_delay=self._policy.retry_delay,
                lease_lost=lease_lost,
                posted_github=posted_github,
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
        lease_lost: threading.Event,
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
                lease_lost.set()
                return
            except _TRANSIENT_DATABASE_ERRORS as exc:
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
