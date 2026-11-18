"""One-process durable review worker backed directly by PostgreSQL."""

from __future__ import annotations

from collections.abc import Mapping
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import timedelta
import json
import logging
import os
import re
import socket
import threading
import time
import uuid
from typing import cast


from .documentation import preflight as documentation_preflight
from .domain import failure_codes
from . import settings
from .review import (
    contract as review_contract,
    runs as review_runs,
)
from .domain.review import JsonObject, ReviewPurpose
from .hermes.chat import HermesChatClient
from .github.gateway import GitHubGatewayRejected, GitHubGatewayRetryable
from .github.gateway_client import ReviewGitHubGatewayClient
from .review.tool_runtime import GatewaySourceSession, ToolInputError
from .review.turn import ClaimedReview, ReviewTurnError
from .postgres import jobs, parts as postgres_parts, review_runs as postgres_review_runs
from .postgres.runtime import TRANSIENT_DATABASE_ERRORS, PostgreSQLRuntime
from .postgres.telemetry import (
    TokenUsage,
    WorkerTelemetry,
    record_usage,
)

logger = logging.getLogger(__name__)
_ERROR_CODE = re.compile(r"[a-z][a-z0-9_]{0,59}")
class WorkerConfigurationError(ValueError):
    """The worker cannot start from its supplied configuration."""


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
    runtime_key: str = "shared"

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
        if re.fullmatch(r"[a-z][a-z0-9-]{0,62}", self.runtime_key) is None:
            raise WorkerConfigurationError("model connection must be a valid runtime key")


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
        telemetry: WorkerTelemetry | None = None,
        source_client: ReviewGitHubGatewayClient | None = None,
    ) -> None:
        owner = lease_owner.strip()
        if not owner:
            raise WorkerConfigurationError("lease_owner is required")
        self._runtime = runtime
        self._telemetry = telemetry
        self._source_client = source_client
        self._client = client
        self._policy = policy
        self._lease_owner = owner
        self._stop = stop_event
        self._next_recovery_at = 0.0

    def run(self, *, once: bool = False) -> None:
        """Recover expired work, then claim only while an execution slot is free."""
        if once:
            if self._stop.is_set():
                return
            self._recover_if_due()
            if self._stop.is_set():
                return
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
                try:
                    self._recover_if_due()
                except TRANSIENT_DATABASE_ERRORS as exc:
                    logger.warning("Review worker recovery deferred: %s", exc)
                    self._wait_for_work_or_completion(active)
                    continue
                if self._stop.is_set():
                    break
                fatal_error = self._reap_completed(active)
                if fatal_error is not None:
                    break
                if len(active) >= self._policy.concurrency:
                    self._wait_for_work_or_completion(active)
                    continue
                try:
                    claimed = self._claim()
                except TRANSIENT_DATABASE_ERRORS as exc:
                    logger.warning("Review worker claim deferred: %s", exc)
                    self._wait_for_work_or_completion(active)
                    continue
                if claimed is None:
                    self._wait_for_work_or_completion(active)
                    continue
                self._log_claim(claimed)
                active[executor.submit(self._execute, claimed)] = claimed

            if self._stop.is_set():
                stopped_error = self._reap_completed(active)
                if fatal_error is None:
                    fatal_error = stopped_error
                if active:
                    logger.info(
                        "Review worker stop requested; draining %s active review(s). "
                        "Lease heartbeats continue until each request returns or the "
                        "process is terminated.",
                        len(active),
                    )

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
            except TRANSIENT_DATABASE_ERRORS as exc:
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
            "Claimed review job %s for review run %s at lease generation %s",
            claimed.job.id,
            claimed.job.review_run_id,
            claimed.job.lease_generation,
        )

    def _claim(self) -> ClaimedReview | None:
        with self._runtime.transaction() as connection:
            job = jobs.claim_next_job(
                connection,
                lease_owner=self._lease_owner,
                lease_duration=self._policy.lease_duration,
                priority_aging_interval=self._policy.priority_aging_interval,
                runtime_key=self._policy.runtime_key,
            )
            if job is None:
                return None
            scope = postgres_review_runs.get_run_scope(connection, job.review_run_id)
            return ClaimedReview(
                job=job,
                repository=scope.repository,
                pr_number=scope.pr_number,
                resolved_config=cast(
                    JsonObject, json.loads(scope.resolved_config.canonical_json)
                ),
                purpose=scope.run.purpose,
                part=job.part,
                parts=postgres_parts.planned_parts(
                    connection, run_id=job.review_run_id
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
            review_runs.recover_expired_jobs_in_transaction(
                connection, limit=self._policy.recovery_batch_size
            )

    def _execute(self, claimed: ClaimedReview) -> None:
        if self._stop.is_set():
            try:
                with self._runtime.transaction() as connection:
                    jobs.requeue_unstarted_job(
                        connection,
                        job_id=claimed.job.id,
                        lease_owner=self._lease_owner,
                        lease_generation=claimed.job.lease_generation,
                    )
            except jobs.ReviewJobLeaseLost:
                logger.info(
                    "Review job %s lost its lease before Hermes activation",
                    claimed.job.id,
                )
            except TRANSIENT_DATABASE_ERRORS as exc:
                logger.warning(
                    "Review job %s could not be returned before shutdown; "
                    "its lease will expire into recovery: %s",
                    claimed.job.id,
                    exc,
                )
            else:
                logger.info(
                    "Worker returned review job %s to the queue without consuming "
                    "an attempt",
                    claimed.job.id,
                )
            return
        try:
            installed_contract = review_contract.load_installed_contract()
        except review_contract.ReviewContractError as exc:
            logger.critical(
                "Worker cannot verify its installed review contract: %s", exc
            )
            self._stop.set()
            return
        try:
            review_contract.require_matching_execution_contract(
                claimed.resolved_config, installed_contract
            )
        except review_contract.ReviewContractError as exc:
            try:
                with self._runtime.transaction() as connection:
                    review_runs.fail_claimed_job_in_transaction(
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
        failure: ReviewTurnError | None = None
        usage: TokenUsage | None = None
        model_invoked = False
        if self._telemetry is not None:
            self._telemetry.event(
                "review_started",
                run_id=claimed.job.review_run_id,
                job_id=claimed.job.id,
            )
        try:
            terminal_documentation = False
            if claimed.purpose is ReviewPurpose.DOCUMENTATION:
                if self._source_client is None:
                    raise ReviewTurnError(
                        "Documentation source client is not configured",
                        retryable=False, code="worker_misconfigured",
                    )
                result = documentation_preflight.run_preflight(
                    self._runtime,
                    GatewaySourceSession(
                        run_id=int(claimed.job.review_run_id),
                        lease=jobs.WorkerLeaseSession(
                            job_id=claimed.job.id, lease_generation=claimed.job.lease_generation,
                        ),
                        client=self._source_client,
                    ),
                    installed_contract,
                )
                terminal_documentation = result.frozen
                if terminal_documentation:
                    from .publication.publish import (
                        prepare_postgres_documentation_publication,
                    )

                    configured = settings.ReviewAgentSettings.from_environment()
                    prepare_postgres_documentation_publication(
                        self._runtime, run_id=int(claimed.job.review_run_id),
                        max_comment_bytes=configured.publish_max_bytes,
                        delivery_max_attempts=configured.publication_max_attempts,
                        review_job_id=claimed.job.id,
                        review_lease_generation=claimed.job.lease_generation,
                    )
            if not terminal_documentation and not lease_lost.is_set():
                model_invoked = True
                usage = self._client.review(claimed, timeout=self._policy.request_timeout)
        except ReviewTurnError as exc:
            failure = exc
            usage = exc.usage
        except GitHubGatewayRetryable as exc:
            failure = ReviewTurnError(
                f"Review source unavailable: {exc.reason}",
                retryable=True, code="source_unavailable", detail=exc.reason,
            )
        except (GitHubGatewayRejected, ToolInputError, review_runs.ReviewRunError) as exc:
            failure = ReviewTurnError(
                f"Review preflight rejected: {exc}",
                retryable=False, code="preflight_rejected",
                detail=type(exc).__name__,
            )
        except (jobs.ReviewJobLeaseLost, review_runs.ReviewRunTerminal):
            lease_lost.set()
        finally:
            heartbeat_stop.set()
            heartbeat.join()
            if usage is not None:
                record_usage(
                    self._runtime,
                    job_id=claimed.job.id,
                    generation=claimed.job.lease_generation,
                    usage=usage,
                )
            if self._telemetry is not None:
                self._telemetry.event(
                    "usage_unavailable" if model_invoked and usage is None else "review_returned",
                    run_id=claimed.job.review_run_id,
                    job_id=claimed.job.id,
                )

        if lease_lost.is_set():
            logger.info("Review job %s lost its lease", claimed.job.id)
            return
        with self._runtime.transaction() as connection:
            current = jobs.get_job(connection, claimed.job.id)
        if current.status is not jobs.ReviewJobStatus.LEASED:
            logger.info(
                "Review job %s for review run %s finished with status %s",
                claimed.job.id,
                claimed.job.review_run_id,
                current.status,
            )
            return
        if failure is None:
            failure = ReviewTurnError(
                "Hermes finished before the review reached a terminal state",
                retryable=True,
                code="review_not_terminal",
            )
        try:
            with self._runtime.transaction() as connection:
                outcome = review_runs.fail_claimed_job_in_transaction(
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
                    attempt_failure_code=failure.code,
                    attempt_failure_detail=failure.detail,
                )
            logger.warning(
                "Review job %s for review run %s ended in status %s after "
                "attempt %s failed: %s (%s)",
                claimed.job.id,
                claimed.job.review_run_id,
                outcome.job.status,
                claimed.job.lease_generation,
                failure,
                failure.code,
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
            except TRANSIENT_DATABASE_ERRORS as exc:
                # A single missed heartbeat is safe because policy requires
                # multiple heartbeat opportunities within one lease.
                logger.warning("Review job %s heartbeat deferred: %s", job.id, exc)


def default_lease_owner(environment: Mapping[str, str]) -> str:
    configured = environment.get("REVIEW_AGENT_WORKER_NAME", "").strip()
    return configured or (
        f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:12]}"
    )
