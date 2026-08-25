"""One-process durable review worker backed directly by PostgreSQL."""

from __future__ import annotations

from collections.abc import Mapping
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import timedelta
from http import HTTPStatus
import json
import logging
import os
from pathlib import Path
import socket
import threading
import time
import uuid
from urllib import error, parse, request
from typing import cast

import psycopg

from . import failure_codes, review_contract, review_run_application
from .domain.review import JsonObject
from .postgres import jobs, review_runs
from .postgres.runtime import PostgreSQLRuntime, PostgreSQLUnavailable


logger = logging.getLogger(__name__)
_TRANSIENT_DATABASE_ERRORS = (
    PostgreSQLUnavailable,
    psycopg.errors.DeadlockDetected,
    psycopg.errors.QueryCanceled,
    psycopg.errors.LockNotAvailable,
)


class WorkerConfigurationError(ValueError):
    """The worker cannot start from its supplied configuration."""


class HermesRequestError(RuntimeError):
    """Hermes did not accept or finish one review turn."""

    retryable: bool

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class WorkerPolicy:
    lease_duration: timedelta
    heartbeat_interval: timedelta
    retry_delay: timedelta
    poll_interval: timedelta
    request_timeout: timedelta
    recovery_interval: timedelta
    recovery_batch_size: int
    priority_aging_interval: timedelta
    concurrency: int = 1

    def __post_init__(self) -> None:
        positive_durations = {
            "lease_duration": self.lease_duration,
            "heartbeat_interval": self.heartbeat_interval,
            "retry_delay": self.retry_delay,
            "poll_interval": self.poll_interval,
            "request_timeout": self.request_timeout,
            "recovery_interval": self.recovery_interval,
            "priority_aging_interval": self.priority_aging_interval,
        }
        for name, value in positive_durations.items():
            if value <= timedelta(0):
                raise WorkerConfigurationError(f"{name} must be positive")
        if self.heartbeat_interval * 2 >= self.lease_duration:
            raise WorkerConfigurationError(
                "heartbeat_interval must be less than half of lease_duration"
            )
        if isinstance(self.recovery_batch_size, bool) or self.recovery_batch_size < 1:
            raise WorkerConfigurationError("recovery_batch_size must be positive")
        if isinstance(self.concurrency, bool) or self.concurrency < 1:
            raise WorkerConfigurationError("concurrency must be positive")


@dataclass(frozen=True, slots=True)
class HermesChatSettings:
    endpoint: str
    bearer_token: str = field(repr=False)
    skill_path: Path

    def __post_init__(self) -> None:
        parsed = parse.urlsplit(self.endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise WorkerConfigurationError("Hermes endpoint must be an HTTP URL")
        if not self.bearer_token.strip():
            raise WorkerConfigurationError("Hermes bearer token is required")
        if not self.skill_path.is_file():
            raise WorkerConfigurationError(
                f"review skill does not exist: {self.skill_path}"
            )


@dataclass(frozen=True, slots=True)
class ClaimedReview:
    job: jobs.ReviewJob
    repository: str
    pr_number: int
    resolved_config: JsonObject


class HermesChatClient:
    """Small OpenAI-compatible client for the pinned Hermes chat boundary.

    Hermes loads SOUL.md and AGENTS.md natively for API turns. The chat
    endpoint has no route-skill selector, so this client layers only the
    installed review skill body as the ephemeral system message.
    """

    def __init__(self, settings: HermesChatSettings) -> None:
        self._settings = settings
        self._system_instructions = _load_skill_instructions(settings.skill_path)

    def review(self, claimed: ClaimedReview, *, timeout: timedelta) -> None:
        session = jobs.WorkerLeaseSession(
            job_id=claimed.job.id,
            lease_generation=claimed.job.lease_generation,
        )
        payload = json.dumps(
            {
                "messages": [
                    {"role": "system", "content": self._system_instructions},
                    {
                        "role": "user",
                        "content": (
                            "Continue the assigned durable pull-request review. "
                            "Call review_agent_begin first with repository "
                            f"{claimed.repository!r}, pr_number {claimed.pr_number}, "
                            f"and existing_run_id {int(claimed.job.review_run_id)}. "
                            "Follow the review skill and finish through deterministic "
                            "delivery."
                        ),
                    },
                ],
                "stream": False,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        call = request.Request(
            self._settings.endpoint,
            data=payload,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._settings.bearer_token}",
                "Content-Type": "application/json",
                # Hermes idempotency deduplicates transport retries only within
                # this lease. PostgreSQL generation fences own reclaim safety.
                "Idempotency-Key": session.encode(),
                "X-Hermes-Session-Id": session.encode(),
            },
        )
        try:
            # Durable PostgreSQL state, not optional assistant prose, proves
            # whether the review completed. Close the response without
            # buffering an otherwise unbounded body.
            with request.urlopen(call, timeout=timeout.total_seconds()):
                pass
        except error.HTTPError as exc:
            retryable = (
                exc.code
                in {
                    HTTPStatus.REQUEST_TIMEOUT,
                    HTTPStatus.TOO_EARLY,
                    HTTPStatus.TOO_MANY_REQUESTS,
                }
                or exc.code >= 500
            )
            exc.close()
            raise HermesRequestError(
                f"Hermes returned HTTP {exc.code}", retryable=retryable
            ) from exc
        except (error.URLError, TimeoutError, OSError) as exc:
            raise HermesRequestError(
                "Hermes request did not complete", retryable=True
            ) from exc


class ReviewWorker:
    """Claim only enough reviews to fill this process's bounded execution slots."""

    def __init__(
        self,
        runtime: PostgreSQLRuntime,
        client: HermesChatClient,
        policy: WorkerPolicy,
        *,
        lease_owner: str,
        stop_event: threading.Event,
    ) -> None:
        owner = lease_owner.strip()
        if not owner:
            raise WorkerConfigurationError("lease_owner is required")
        self._runtime = runtime
        self._client = client
        self._policy = policy
        self._lease_owner = owner
        self._stop = stop_event
        self._next_recovery_at = 0.0

    def run(self, *, once: bool = False) -> None:
        """Recover expired work, then claim only while an execution slot is free."""
        if once:
            claimed = self._claim()
            if claimed is not None:
                self._log_claim(claimed)
                self._execute(claimed)
            return

        active: dict[Future[None], ClaimedReview] = {}
        fatal_error: Exception | None = None
        with ThreadPoolExecutor(
            max_workers=self._policy.concurrency,
            thread_name_prefix="review-agent-job",
        ) as executor:
            while not self._stop.is_set():
                fatal_error = self._reap_completed(active)
                if fatal_error is not None:
                    break
                if len(active) >= self._policy.concurrency:
                    self._wait_for_work_or_completion(active)
                    continue
                try:
                    claimed = self._claim()
                except _TRANSIENT_DATABASE_ERRORS as exc:
                    logger.warning("Review worker claim deferred: %s", exc)
                    self._wait_for_work_or_completion(active)
                    continue
                if claimed is None:
                    self._wait_for_work_or_completion(active)
                    continue
                self._log_claim(claimed)
                active[executor.submit(self._execute, claimed)] = claimed

        # Executor shutdown has joined every active review. Observe each result
        # and then propagate the first unexpected defect with its job logged.
        drained_error = self._reap_completed(active)
        if fatal_error is None:
            fatal_error = drained_error
        if fatal_error is not None:
            raise fatal_error

    def _reap_completed(
        self, active: dict[Future[None], ClaimedReview]
    ) -> Exception | None:
        fatal_error: Exception | None = None
        for completed in tuple(future for future in active if future.done()):
            claimed = active.pop(completed)
            try:
                completed.result()
            except _TRANSIENT_DATABASE_ERRORS as exc:
                # The lease will either remain live through its heartbeat or
                # expire into the bounded recovery path. Never guess a state
                # transition while PostgreSQL is unavailable.
                logger.warning(
                    "Review job %s state update deferred: %s",
                    claimed.job.id,
                    exc,
                )
            except Exception as exc:
                logger.exception(
                    "Review job %s failed unexpectedly", claimed.job.id
                )
                self._stop.set()
                if fatal_error is None:
                    fatal_error = exc
        return fatal_error

    def _wait_for_work_or_completion(
        self, active: dict[Future[None], ClaimedReview]
    ) -> None:
        timeout = self._policy.poll_interval.total_seconds()
        if active:
            wait(active, timeout=timeout, return_when=FIRST_COMPLETED)
        else:
            self._stop.wait(timeout)

    @staticmethod
    def _log_claim(claimed: ClaimedReview) -> None:
        logger.info(
            "Claimed review job %s at lease generation %s",
            claimed.job.id,
            claimed.job.lease_generation,
        )

    def _claim(self) -> ClaimedReview | None:
        self._recover_if_due()
        with self._runtime.transaction() as connection:
            job = jobs.claim_next_job(
                connection,
                lease_owner=self._lease_owner,
                lease_duration=self._policy.lease_duration,
                priority_aging_interval=self._policy.priority_aging_interval,
            )
            if job is None:
                return None
            scope = review_runs.get_run_scope(connection, job.review_run_id)
            return ClaimedReview(
                job=job,
                repository=scope.repository,
                pr_number=scope.pr_number,
                resolved_config=cast(
                    JsonObject, json.loads(scope.resolved_config.canonical_json)
                ),
            )

    def _recover_if_due(self) -> None:
        now = time.monotonic()
        if now < self._next_recovery_at:
            return
        # Move the local deadline before the query so a transient database
        # failure cannot create a tight recovery loop in this process.
        self._next_recovery_at = now + self._policy.recovery_interval.total_seconds()
        with self._runtime.transaction() as connection:
            review_run_application.recover_expired_jobs_in_transaction(
                connection, limit=self._policy.recovery_batch_size
            )

    def _execute(self, claimed: ClaimedReview) -> None:
        try:
            installed_contract = review_contract.load_installed_contract()
        except review_contract.ReviewContractError as exc:
            logger.critical(
                "Worker cannot verify its installed review contract: %s", exc
            )
            self._stop.set()
            return
        try:
            review_contract.require_matching_resolved_config(
                claimed.resolved_config, installed_contract
            )
        except review_contract.ReviewContractError as exc:
            try:
                with self._runtime.transaction() as connection:
                    review_run_application.fail_claimed_job_in_transaction(
                        connection,
                        job_id=claimed.job.id,
                        lease_owner=self._lease_owner,
                        lease_generation=claimed.job.lease_generation,
                        failure_code=failure_codes.JOB_TERMINAL_EXECUTION,
                        retryable=False,
                        retry_delay=None,
                        run_failure_code=failure_codes.REVIEW_CONTRACT_CHANGED,
                    )
            except jobs.ReviewJobLeaseLost:
                logger.info("Review job %s lost its lease", claimed.job.id)
                return
            logger.error("Review job %s rejected before Hermes: %s", claimed.job.id, exc)
            return
        heartbeat_stop = threading.Event()
        lease_lost = threading.Event()
        heartbeat = threading.Thread(
            target=self._heartbeat,
            args=(claimed.job, heartbeat_stop, lease_lost),
            name=f"review-job-{claimed.job.id}-heartbeat",
        )
        heartbeat.start()
        failure: HermesRequestError | None = None
        try:
            self._client.review(claimed, timeout=self._policy.request_timeout)
        except HermesRequestError as exc:
            failure = exc
        finally:
            heartbeat_stop.set()
            heartbeat.join()

        if lease_lost.is_set():
            logger.info("Review job %s lost its lease", claimed.job.id)
            return
        with self._runtime.transaction() as connection:
            current = jobs.get_job(connection, claimed.job.id)
        if current.status is not jobs.ReviewJobStatus.LEASED:
            logger.info(
                "Review job %s finished with status %s",
                claimed.job.id,
                current.status,
            )
            return
        if failure is None:
            failure = HermesRequestError(
                "Hermes finished before the review reached a terminal state",
                retryable=True,
            )
        try:
            with self._runtime.transaction() as connection:
                outcome = review_run_application.fail_claimed_job_in_transaction(
                    connection,
                    job_id=claimed.job.id,
                    lease_owner=self._lease_owner,
                    lease_generation=claimed.job.lease_generation,
                    failure_code=(
                        failure_codes.JOB_RETRYABLE_EXECUTION
                        if failure.retryable
                        else failure_codes.JOB_TERMINAL_EXECUTION
                    ),
                    retryable=failure.retryable,
                    retry_delay=(
                        self._policy.retry_delay if failure.retryable else None
                    ),
                )
            logger.warning(
                "Review job %s ended in status %s after Hermes failure: %s",
                claimed.job.id,
                outcome.job.status,
                failure,
            )
        except jobs.ReviewJobLeaseLost:
            logger.info("Review job %s lost its lease", claimed.job.id)
            return

    def _heartbeat(
        self,
        job: jobs.ReviewJob,
        stop: threading.Event,
        lease_lost: threading.Event,
    ) -> None:
        while not stop.wait(self._policy.heartbeat_interval.total_seconds()):
            try:
                with self._runtime.transaction() as connection:
                    jobs.heartbeat_job(
                        connection,
                        job_id=job.id,
                        lease_owner=self._lease_owner,
                        lease_generation=job.lease_generation,
                        lease_duration=self._policy.lease_duration,
                    )
            except jobs.ReviewJobLeaseLost:
                lease_lost.set()
                logger.info("Review job %s lost its lease", job.id)
                return
            except _TRANSIENT_DATABASE_ERRORS as exc:
                # A single missed heartbeat is safe because policy requires
                # multiple heartbeat opportunities within one lease.
                logger.warning("Review job %s heartbeat deferred: %s", job.id, exc)


def _load_skill_instructions(path: Path) -> str:
    """Load the skill body used as the worker's ephemeral system message."""
    content = path.read_text(encoding="utf-8")
    if not content.startswith("---\n"):
        return content
    marker = content.find("\n---\n", 4)
    if marker < 0:
        raise WorkerConfigurationError(
            f"review skill has unterminated YAML frontmatter: {path}"
        )
    return content[marker + len("\n---\n") :].lstrip()


def default_lease_owner(environment: Mapping[str, str]) -> str:
    configured = environment.get("REVIEW_AGENT_WORKER_NAME", "").strip()
    return configured or (
        f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:12]}"
    )
