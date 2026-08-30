from __future__ import annotations

import os
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier
from unittest.mock import Mock, patch

import psycopg


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = ROOT / "bootstrap" / "plugins"
sys.path.insert(0, str(PACKAGE_ROOT))

from review_agent_tools import (  # noqa: E402
    failure_codes,
    operator_application,
    review_contract,
    review_run_application,
)
from review_agent_tools.domain.review import (  # noqa: E402
    ReviewPhase,
    ReviewStatus,
    resolve_review_subject,
)
from review_agent_tools.postgres import jobs, registry, review_runs  # noqa: E402
from review_agent_tools.postgres.runtime import (  # noqa: E402
    PostgreSQLRuntime,
    PostgreSQLRuntimeRole,
)
from review_agent_tools.postgres_migrations import runner  # noqa: E402
from review_agent_tools.settings import PostgresDatabaseUrl  # noqa: E402
from review_agent_tools.worker import (  # noqa: E402
    HermesChatClient,
    HermesRequestError,
    ReviewWorker,
    WorkerPolicy,
)


DSN = os.environ.get("REVIEW_AGENT_POSTGRES_DSN", "")
ACTIVE_JOB_LIMIT = 100
PRIORITY_AGING_INTERVAL = timedelta(minutes=15)
TEST_REVIEW_CONTRACT = review_contract.ReviewContract(
    profile="team-standard",
    hermes_image="hermes@test",
    model_provider="openai-codex",
    model="gpt-test",
    reasoning_effort="high",
    plugin_result_max_chars=160_000,
    profile_bundle_sha256="1" * 64,
    managed_config_sha256="2" * 64,
    engine_bundle_sha256="3" * 64,
    sha256="4" * 64,
)


@unittest.skipUnless(DSN, "run through scripts/check_postgres_schema.sh")
class PostgreSQLJobTests(unittest.TestCase):
    def setUp(self) -> None:
        with psycopg.connect(DSN, autocommit=True) as connection:
            connection.execute("DROP SCHEMA IF EXISTS review_agent CASCADE")
        with psycopg.connect(DSN) as connection:
            runner.apply_migrations(connection)
        self.runtime = PostgreSQLRuntime(PostgresDatabaseUrl(DSN))
        self.runtime.open()
        self.addCleanup(self.runtime.close)

    def pull_request(self, *, provider_id: int, number: int) -> registry.PullRequest:
        with self.runtime.transaction() as connection:
            repository = registry.ensure_repository(
                connection,
                registry.RepositoryDefinition(
                    provider="github",
                    provider_repository_id=provider_id,
                    full_name=f"team/service-{provider_id}",
                ),
            )
            return registry.ensure_pull_request(connection, repository.id, number)

    def subject(
        self,
        pull_request: registry.PullRequest,
        *,
        head_character: str,
    ) -> registry.ReviewSubject:
        definition = resolve_review_subject(
            base_sha="b" * 40,
            head_sha=head_character * 40,
            policy_revision="profile@1",
            resolved_config_schema_version=2,
            resolved_config=review_contract.resolved_config(TEST_REVIEW_CONTRACT),
        )
        with self.runtime.transaction() as connection:
            return registry.create_or_get_subject(
                connection, pull_request.id, definition
            )

    def start_run(
        self,
        pull_request: registry.PullRequest,
        subject: registry.ReviewSubject,
        *,
        request_key: str,
    ) -> review_runs.RunStart:
        with self.runtime.transaction() as connection:
            return review_runs.start_run(
                connection,
                pull_request_id=pull_request.id,
                review_subject_id=subject.id,
                request_key=request_key,
            )

    def accept_job(
        self,
        pull_request: registry.PullRequest,
        subject: registry.ReviewSubject,
        *,
        request_key: str,
        priority: int = 0,
        max_attempts: int = 3,
    ) -> tuple[review_runs.RunStart, jobs.JobEnqueue]:
        with self.runtime.transaction() as connection:
            run = review_run_application.start_run_in_transaction(
                connection,
                pull_request_id=pull_request.id,
                review_subject_id=subject.id,
                request_key=request_key,
            )
            job = jobs.enqueue_run(
                connection,
                review_run_id=run.run.id,
                priority=priority,
                max_attempts=max_attempts,
                active_job_limit=ACTIVE_JOB_LIMIT,
            )
        return run, job

    def claim_job(
        self,
        connection: psycopg.Connection[tuple[object, ...]],
        *,
        lease_owner: str,
        lease_duration: timedelta,
    ) -> jobs.ReviewJob | None:
        return jobs.claim_next_job(
            connection,
            lease_owner=lease_owner,
            lease_duration=lease_duration,
            priority_aging_interval=PRIORITY_AGING_INTERVAL,
        )

    @staticmethod
    def admission_request(
        *, provider_id: int, pr_number: int, request_key: str, head: str
    ) -> review_run_application.PostgresRunRequest:
        return review_run_application.PostgresRunRequest(
            provider="github",
            provider_repository_id=provider_id,
            repository=f"team/service-{provider_id}",
            pr_number=pr_number,
            base_sha="b" * 40,
            head_sha=head * 40,
            policy_revision="profile@1",
            resolved_config_schema_version=1,
            resolved_config={"profile": "team-standard"},
            request_key=request_key,
        )

    def test_run_identity_makes_concurrent_enqueue_idempotent(self) -> None:
        pull_request = self.pull_request(provider_id=1001, number=11)
        subject = self.subject(pull_request, head_character="a")
        start = Barrier(2)

        def accept_once(_: int) -> tuple[review_runs.RunStart, jobs.JobEnqueue]:
            start.wait(timeout=5)
            return self.accept_job(
                pull_request,
                subject,
                request_key="github:issue-comment:7001",
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            concurrent = tuple(executor.map(accept_once, range(2)))
        enqueued = next(
            job for _, job in concurrent if isinstance(job, jobs.EnqueuedJob)
        )
        duplicate = next(
            job for _, job in concurrent if isinstance(job, jobs.DuplicateJob)
        )
        _, same_subject = self.accept_job(
            pull_request,
            subject,
            request_key="github:issue-comment:7002",
        )

        assert isinstance(enqueued, jobs.EnqueuedJob)
        self.assertEqual(enqueued.job.status, jobs.ReviewJobStatus.QUEUED)
        self.assertEqual(duplicate, jobs.DuplicateJob(enqueued.job))
        self.assertEqual(same_subject, jobs.DuplicateJob(enqueued.job))
        with self.runtime.transaction() as connection:
            counts = connection.execute(
                "SELECT "
                "(SELECT count(*) FROM review_agent.review_runs), "
                "(SELECT count(*) FROM review_agent.review_jobs)"
            ).fetchone()
        self.assertEqual(counts, (1, 1))

    def test_database_rejects_an_unfenced_lease(self) -> None:
        pull_request = self.pull_request(provider_id=1006, number=16)
        subject = self.subject(pull_request, head_character="a")
        started = self.start_run(
            pull_request,
            subject,
            request_key="github:issue-comment:invalid-lease",
        )

        with self.assertRaises(psycopg.errors.CheckViolation):
            with self.runtime.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO review_agent.review_jobs (
                        review_run_id, status, priority, available_at,
                        attempt_count, max_attempts, lease_owner,
                        lease_generation, lease_expires_at, last_heartbeat_at,
                        created_at, started_at
                    ) VALUES (
                        %s, 'leased', 0, statement_timestamp(), 1, 3,
                        'worker', 0, statement_timestamp() + INTERVAL '2 minutes',
                        statement_timestamp(), statement_timestamp(),
                        statement_timestamp()
                    )
                    """,
                    (started.run.id,),
                )

    def test_unstarted_requeue_is_attempt_neutral_and_keeps_lease_fencing(
        self,
    ) -> None:
        pull_request = self.pull_request(provider_id=1011, number=21)
        subject = self.subject(pull_request, head_character="a")
        _, accepted = self.accept_job(
            pull_request,
            subject,
            request_key="github:manual:requeue-fence",
            max_attempts=1,
        )
        assert isinstance(accepted, jobs.EnqueuedJob)
        with self.runtime.transaction() as connection:
            first_lease = self.claim_job(
                connection,
                lease_owner="worker-one",
                lease_duration=timedelta(minutes=2),
            )
        assert first_lease is not None

        with self.runtime.transaction() as connection:
            requeued = jobs.requeue_unstarted_job(
                connection,
                job_id=first_lease.id,
                lease_owner="worker-one",
                lease_generation=first_lease.lease_generation,
            )
            second_lease = self.claim_job(
                connection,
                lease_owner="worker-two",
                lease_duration=timedelta(minutes=2),
            )

        assert second_lease is not None
        self.assertEqual(requeued.status, jobs.ReviewJobStatus.QUEUED)
        self.assertEqual(requeued.attempt_count, 0)
        self.assertIsNone(requeued.started_at)
        self.assertEqual(first_lease.lease_generation, 1)
        self.assertEqual(second_lease.lease_generation, 2)
        self.assertEqual(second_lease.attempt_count, 1)

    def test_new_subject_supersedes_queued_run_and_job(self) -> None:
        pull_request = self.pull_request(provider_id=1002, number=12)
        old_subject = self.subject(pull_request, head_character="a")
        new_subject = self.subject(pull_request, head_character="c")
        old_run, first = self.accept_job(
            pull_request,
            old_subject,
            request_key="github:issue-comment:7101",
        )
        assert isinstance(first, jobs.EnqueuedJob)

        new_run, second = self.accept_job(
            pull_request,
            new_subject,
            request_key="github:issue-comment:7102",
        )
        assert isinstance(new_run, review_runs.StartedRun)
        assert isinstance(second, jobs.EnqueuedJob)

        with self.runtime.transaction() as connection:
            prior_run = review_runs.get_run(connection, old_run.run.id)
            prior_job = jobs.get_job(connection, first.job.id)
            current_job = jobs.get_job(connection, second.job.id)
        self.assertEqual(prior_run.status.value, "superseded")
        self.assertEqual(prior_job.status, jobs.ReviewJobStatus.SUPERSEDED)
        self.assertIsNotNone(prior_job.completed_at)
        self.assertEqual(current_job.status, jobs.ReviewJobStatus.QUEUED)

    def test_new_subject_terminalizes_a_leased_old_head_job(self) -> None:
        pull_request = self.pull_request(provider_id=1003, number=13)
        old_subject = self.subject(pull_request, head_character="a")
        new_subject = self.subject(pull_request, head_character="c")
        _, first = self.accept_job(
            pull_request,
            old_subject,
            request_key="github:issue-comment:7201",
        )
        assert isinstance(first, jobs.EnqueuedJob)
        with self.runtime.transaction() as connection:
            leased = self.claim_job(
                connection,
                lease_owner="worker-one",
                lease_duration=timedelta(minutes=2),
            )
        assert leased is not None

        _, second = self.accept_job(
            pull_request,
            new_subject,
            request_key="github:issue-comment:7202",
        )
        assert isinstance(second, jobs.EnqueuedJob)
        with self.runtime.transaction() as connection:
            retained = jobs.get_job(connection, first.job.id)
            queued = jobs.get_job(connection, second.job.id)

        self.assertEqual(retained.status, jobs.ReviewJobStatus.SUPERSEDED)
        self.assertIsNone(retained.lease_owner)
        self.assertIsNone(retained.lease_expires_at)
        self.assertEqual(retained.lease_generation, 1)
        self.assertIsNotNone(retained.completed_at)
        self.assertEqual(queued.status, jobs.ReviewJobStatus.QUEUED)

        with self.runtime.transaction() as connection:
            with self.assertRaises(jobs.ReviewJobLeaseLost) as caught:
                jobs.heartbeat_job(
                    connection,
                    job_id=retained.id,
                    lease_owner="worker-one",
                    lease_generation=1,
                    lease_duration=timedelta(minutes=2),
                )
        self.assertEqual(
            caught.exception.current_job.status, jobs.ReviewJobStatus.SUPERSEDED
        )

    def test_new_subject_supersedes_requeued_job_without_resetting_fence(
        self,
    ) -> None:
        pull_request = self.pull_request(provider_id=1014, number=24)
        old_subject = self.subject(pull_request, head_character="a")
        new_subject = self.subject(pull_request, head_character="c")
        _, first = self.accept_job(
            pull_request,
            old_subject,
            request_key="github:manual:requeued-old-head",
        )
        assert isinstance(first, jobs.EnqueuedJob)
        with self.runtime.transaction() as connection:
            leased = self.claim_job(
                connection,
                lease_owner="worker-requeue",
                lease_duration=timedelta(minutes=2),
            )
            connection.execute(
                """
                UPDATE review_agent.review_jobs
                SET status = 'queued', lease_owner = NULL,
                    lease_expires_at = NULL, last_heartbeat_at = NULL
                WHERE id = %s
                """,
                (first.job.id,),
            )
        assert leased is not None

        _, second = self.accept_job(
            pull_request,
            new_subject,
            request_key="github:manual:new-head-after-requeue",
        )
        assert isinstance(second, jobs.EnqueuedJob)
        with self.runtime.transaction() as connection:
            superseded = jobs.get_job(connection, first.job.id)

        self.assertEqual(superseded.status, jobs.ReviewJobStatus.SUPERSEDED)
        self.assertEqual(superseded.lease_generation, 1)
        self.assertEqual(superseded.started_at, leased.started_at)
        self.assertIsNone(superseded.lease_owner)

    def test_request_key_cannot_be_reused_for_another_subject(self) -> None:
        pull_request = self.pull_request(provider_id=1004, number=14)
        first_subject = self.subject(pull_request, head_character="a")
        second_subject = self.subject(pull_request, head_character="c")
        self.accept_job(
            pull_request,
            first_subject,
            request_key="github:issue-comment:7301",
        )

        with self.assertRaises(review_runs.DuplicateReviewRequest):
            self.accept_job(
                pull_request,
                second_subject,
                request_key="github:issue-comment:7301",
            )

    def test_terminal_run_is_not_claimed(self) -> None:
        pull_request = self.pull_request(provider_id=1012, number=22)
        subject = self.subject(pull_request, head_character="a")
        started, accepted = self.accept_job(
            pull_request,
            subject,
            request_key="github:manual:terminal-run",
        )
        assert isinstance(accepted, jobs.EnqueuedJob)

        with self.runtime.transaction() as connection:
            review_run_application.fail_run_in_transaction(
                connection,
                started.run.id,
                failure_code="stale_timeout",
            )
        with self.runtime.transaction() as connection:
            claimed = self.claim_job(
                connection,
                lease_owner="worker-terminal",
                lease_duration=timedelta(minutes=2),
            )
            retained = jobs.get_job(connection, accepted.job.id)
            repeated = jobs.enqueue_run(
                connection,
                review_run_id=started.run.id,
                priority=0,
                max_attempts=3,
                active_job_limit=ACTIVE_JOB_LIMIT,
            )

        self.assertIsNone(claimed)
        self.assertEqual(retained.status, jobs.ReviewJobStatus.FAILED)
        self.assertEqual(retained.failure_code, failure_codes.JOB_RUN_FAILED)
        self.assertIsInstance(repeated, jobs.DuplicateJob)
        self.assertEqual(repeated.job.id, retained.id)

    def test_heartbeat_extends_only_a_live_exact_lease(self) -> None:
        pull_request = self.pull_request(provider_id=1015, number=25)
        subject = self.subject(pull_request, head_character="a")
        _, accepted = self.accept_job(
            pull_request,
            subject,
            request_key="github:manual:heartbeat",
        )
        assert isinstance(accepted, jobs.EnqueuedJob)
        with self.runtime.transaction() as connection:
            leased = self.claim_job(
                connection,
                lease_owner="worker-heartbeat",
                lease_duration=timedelta(minutes=2),
            )
        assert leased is not None

        with self.runtime.transaction() as connection:
            heartbeated = jobs.heartbeat_job(
                connection,
                job_id=leased.id,
                lease_owner="worker-heartbeat",
                lease_generation=leased.lease_generation,
                lease_duration=timedelta(minutes=3),
            )
        assert heartbeated.lease_expires_at is not None
        assert leased.lease_expires_at is not None
        self.assertGreater(heartbeated.lease_expires_at, leased.lease_expires_at)

        for owner, generation in (
            ("another-worker", leased.lease_generation),
            ("worker-heartbeat", leased.lease_generation + 1),
        ):
            with self.runtime.transaction() as connection:
                with self.assertRaises(jobs.ReviewJobLeaseLost) as caught:
                    jobs.heartbeat_job(
                        connection,
                        job_id=leased.id,
                        lease_owner=owner,
                        lease_generation=generation,
                        lease_duration=timedelta(minutes=3),
                    )
            self.assertEqual(
                caught.exception.current_job.lease_generation,
                leased.lease_generation,
            )

        with self.runtime.transaction() as connection:
            connection.execute(
                """
                UPDATE review_agent.review_jobs
                SET last_heartbeat_at = started_at,
                    lease_expires_at = statement_timestamp()
                WHERE id = %s
                """,
                (leased.id,),
            )
            with self.assertRaises(jobs.ReviewJobLeaseLost) as caught:
                jobs.heartbeat_job(
                    connection,
                    job_id=leased.id,
                    lease_owner="worker-heartbeat",
                    lease_generation=leased.lease_generation,
                    lease_duration=timedelta(minutes=3),
                )
            usable = connection.execute("SELECT 1").fetchone()
        self.assertEqual(usable, (1,))
        self.assertEqual(
            caught.exception.current_job.status, jobs.ReviewJobStatus.LEASED
        )

    def test_retry_and_terminal_failure_release_the_run_deterministically(self) -> None:
        pull_request = self.pull_request(provider_id=1016, number=26)
        subject = self.subject(pull_request, head_character="a")
        started, accepted = self.accept_job(
            pull_request,
            subject,
            request_key="github:manual:retry-then-terminal",
        )
        assert isinstance(accepted, jobs.EnqueuedJob)
        with self.runtime.transaction() as connection:
            first = self.claim_job(
                connection,
                lease_owner="worker-retry",
                lease_duration=timedelta(minutes=2),
            )
        assert first is not None

        with self.runtime.transaction() as connection:
            retried = review_run_application.fail_claimed_job_in_transaction(
                connection,
                job_id=first.id,
                lease_owner="worker-retry",
                lease_generation=first.lease_generation,
                failure_code=failure_codes.JOB_RETRYABLE_EXECUTION,
                retryable=True,
                retry_delay=timedelta(seconds=1),
            )
            connection.execute(
                "UPDATE review_agent.review_jobs SET available_at = statement_timestamp() "
                "WHERE id = %s",
                (first.id,),
            )
            second = self.claim_job(
                connection,
                lease_owner="worker-terminal",
                lease_duration=timedelta(minutes=2),
            )
        self.assertEqual(retried.job.status, jobs.ReviewJobStatus.QUEUED)
        assert second is not None
        self.assertEqual(second.attempt_count, 2)

        with self.runtime.transaction() as connection:
            terminal = review_run_application.fail_claimed_job_in_transaction(
                connection,
                job_id=second.id,
                lease_owner="worker-terminal",
                lease_generation=second.lease_generation,
                failure_code=failure_codes.JOB_TERMINAL_EXECUTION,
                retryable=False,
                retry_delay=None,
            )
            run = review_runs.get_run(connection, started.run.id)
        self.assertEqual(terminal.job.status, jobs.ReviewJobStatus.FAILED)
        self.assertEqual(run.status, ReviewStatus.FAILED)
        self.assertEqual(run.failure_code, failure_codes.JOB_EXECUTION_FAILED)

    def test_worker_tool_fence_requires_the_current_live_generation(self) -> None:
        pull_request = self.pull_request(provider_id=1024, number=34)
        subject = self.subject(pull_request, head_character="a")
        started, accepted = self.accept_job(
            pull_request,
            subject,
            request_key="github:manual:worker-tool-fence",
        )
        assert isinstance(accepted, jobs.EnqueuedJob)
        with self.runtime.transaction() as connection:
            leased = self.claim_job(
                connection,
                lease_owner="worker-tool-fence",
                lease_duration=timedelta(minutes=2),
            )
            assert leased is not None
            current = jobs.require_live_lease(
                connection,
                job_id=leased.id,
                review_run_id=started.run.id,
                lease_generation=leased.lease_generation,
            )
        self.assertEqual(current.id, leased.id)

        with self.runtime.transaction() as connection:
            with self.assertRaises(jobs.ReviewJobLeaseLost):
                jobs.require_live_lease(
                    connection,
                    job_id=leased.id,
                    review_run_id=started.run.id,
                    lease_generation=leased.lease_generation + 1,
                )

        session = jobs.WorkerLeaseSession(
            job_id=leased.id,
            lease_generation=leased.lease_generation,
        )
        self.assertEqual(
            jobs.WorkerLeaseSession.parse(session.encode()),
            session,
        )
        self.assertIsNone(jobs.WorkerLeaseSession.parse("manual-session"))
        with self.assertRaisesRegex(jobs.ReviewJobError, "malformed"):
            jobs.WorkerLeaseSession.parse("review-agent-job-invalid")

    def test_worker_stop_does_not_claim_another_job(self) -> None:
        first_pull = self.pull_request(provider_id=1025, number=35)
        first_subject = self.subject(first_pull, head_character="a")
        first_run, _ = self.accept_job(
            first_pull,
            first_subject,
            request_key="github:manual:stop-first",
            priority=10,
        )
        second_pull = self.pull_request(provider_id=1026, number=36)
        second_subject = self.subject(second_pull, head_character="c")
        second_run, second_job = self.accept_job(
            second_pull,
            second_subject,
            request_key="github:manual:stop-second",
        )
        stop = threading.Event()
        client = Mock(spec=HermesChatClient)

        def stop_during_first(*_args: object, **_kwargs: object) -> None:
            stop.set()
            raise HermesRequestError("stop probe", retryable=False)

        client.review.side_effect = stop_during_first
        worker = ReviewWorker(
            self.runtime,
            client,
            WorkerPolicy(
                lease_duration=timedelta(seconds=5),
                heartbeat_interval=timedelta(seconds=1),
                retry_delay=timedelta(seconds=1),
                poll_interval=timedelta(milliseconds=10),
                request_timeout=timedelta(seconds=2),
                recovery_interval=timedelta(seconds=30),
                recovery_batch_size=10,
                priority_aging_interval=PRIORITY_AGING_INTERVAL,
            ),
            lease_owner="worker-stop-probe",
            stop_event=stop,
        )

        with patch.object(
            review_contract,
            "load_installed_contract",
            return_value=TEST_REVIEW_CONTRACT,
        ):
            worker.run()

        with self.runtime.transaction() as connection:
            first = review_runs.get_run(connection, first_run.run.id)
            second = jobs.get_job(connection, second_job.job.id)
        self.assertEqual(first.status, ReviewStatus.FAILED)
        self.assertEqual(second.review_run_id, second_run.run.id)
        self.assertEqual(second.status, jobs.ReviewJobStatus.QUEUED)
        client.review.assert_called_once()

    def test_worker_executes_only_its_bounded_cross_repository_slots(self) -> None:
        accepted = []
        for offset in range(4):
            pull_request = self.pull_request(
                provider_id=1100 + offset, number=40 + offset
            )
            subject = self.subject(pull_request, head_character=chr(ord("c") + offset))
            accepted.append(
                self.accept_job(
                    pull_request,
                    subject,
                    request_key=f"github:manual:concurrent-worker-{offset}",
                )
            )

        worker_runtime = PostgreSQLRuntime(
            PostgresDatabaseUrl(DSN),
            role=PostgreSQLRuntimeRole.WORKER,
            worker_concurrency=4,
        )
        worker_runtime.open()
        self.addCleanup(worker_runtime.close)
        stop = threading.Event()
        all_started = threading.Event()
        release = threading.Event()
        lock = threading.Lock()
        active = 0
        maximum_active = 0
        errors: list[BaseException] = []
        client = Mock(spec=HermesChatClient)

        def review(*_args: object, **_kwargs: object) -> None:
            nonlocal active, maximum_active
            with lock:
                active += 1
                maximum_active = max(maximum_active, active)
                if active == 4:
                    all_started.set()
            release.wait(timeout=5)
            with lock:
                active -= 1
            raise HermesRequestError("concurrency probe", retryable=False)

        client.review.side_effect = review
        worker = ReviewWorker(
            worker_runtime,
            client,
            WorkerPolicy(
                lease_duration=timedelta(seconds=5),
                heartbeat_interval=timedelta(seconds=1),
                retry_delay=timedelta(seconds=1),
                poll_interval=timedelta(milliseconds=10),
                request_timeout=timedelta(seconds=5),
                recovery_interval=timedelta(seconds=30),
                recovery_batch_size=10,
                priority_aging_interval=PRIORITY_AGING_INTERVAL,
                concurrency=4,
            ),
            lease_owner="worker-concurrency-probe",
            stop_event=stop,
        )

        def run_worker() -> None:
            try:
                worker.run()
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        with patch.object(
            review_contract,
            "load_installed_contract",
            return_value=TEST_REVIEW_CONTRACT,
        ):
            runner = threading.Thread(target=run_worker)
            runner.start()
            self.assertTrue(all_started.wait(timeout=5))
            with self.runtime.transaction() as connection:
                leased = connection.execute(
                    "SELECT count(*) FROM review_agent.review_jobs "
                    "WHERE status = 'leased'"
                ).fetchone()
            self.assertEqual(leased, (4,))
            metrics = worker_runtime.pool_metrics()
            self.assertEqual(metrics.waiting_requests, 0)
            self.assertLessEqual(metrics.size, 5)
            stop.set()
            release.set()
            runner.join(timeout=5)

        self.assertFalse(runner.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(maximum_active, 4)
        self.assertEqual(client.review.call_count, 4)
        with self.runtime.transaction() as connection:
            statuses = tuple(
                jobs.get_job(connection, enqueued.job.id).status
                for _, enqueued in accepted
            )
        self.assertEqual(statuses, (jobs.ReviewJobStatus.FAILED,) * 4)

    def test_exhausted_retry_dead_letters_and_releases_the_active_run(self) -> None:
        pull_request = self.pull_request(provider_id=1017, number=27)
        subject = self.subject(pull_request, head_character="a")
        started, _ = self.accept_job(
            pull_request,
            subject,
            request_key="github:manual:dead-letter",
            max_attempts=1,
        )
        with self.runtime.transaction() as connection:
            leased = self.claim_job(
                connection,
                lease_owner="worker-last-attempt",
                lease_duration=timedelta(minutes=2),
            )
        assert leased is not None

        with self.runtime.transaction() as connection:
            outcome = review_run_application.fail_claimed_job_in_transaction(
                connection,
                job_id=leased.id,
                lease_owner="worker-last-attempt",
                lease_generation=leased.lease_generation,
                failure_code=failure_codes.JOB_RETRYABLE_EXECUTION,
                retryable=True,
                retry_delay=timedelta(seconds=1),
            )
            run = review_runs.get_run(connection, started.run.id)
            failure_status = review_runs.failure_status_target(
                connection, started.run.id
            )
        self.assertEqual(outcome.job.status, jobs.ReviewJobStatus.DEAD_LETTER)
        self.assertEqual(run.status, ReviewStatus.FAILED)
        self.assertEqual(run.failure_code, failure_codes.JOB_RETRY_EXHAUSTED)
        self.assertEqual(failure_status.delivery_status, "pending")

    def test_claimed_failure_reports_lease_loss_after_run_terminalization(self) -> None:
        pull_request = self.pull_request(provider_id=1023, number=33)
        subject = self.subject(pull_request, head_character="a")
        started, _ = self.accept_job(
            pull_request,
            subject,
            request_key="github:manual:terminal-before-job-failure",
        )
        with self.runtime.transaction() as connection:
            leased = self.claim_job(
                connection,
                lease_owner="worker-terminal-run",
                lease_duration=timedelta(minutes=2),
            )
            assert leased is not None
            review_runs.mark_superseded(connection, started.run.id)
            with self.assertRaises(jobs.ReviewJobLeaseLost) as caught:
                review_run_application.fail_claimed_job_in_transaction(
                    connection,
                    job_id=leased.id,
                    lease_owner="worker-terminal-run",
                    lease_generation=leased.lease_generation,
                    failure_code=failure_codes.JOB_TERMINAL_EXECUTION,
                    retryable=False,
                    retry_delay=None,
                )

        self.assertEqual(
            caught.exception.current_job.status,
            jobs.ReviewJobStatus.SUPERSEDED,
        )
        self.assertIsNone(caught.exception.current_job.lease_owner)

    def test_expiry_recovery_consumes_attempts_and_reconciles_terminal_runs(
        self,
    ) -> None:
        retry_pull = self.pull_request(provider_id=1018, number=28)
        retry_subject = self.subject(retry_pull, head_character="a")
        retry_run, _ = self.accept_job(
            retry_pull,
            retry_subject,
            request_key="github:manual:expiry-retry",
            max_attempts=2,
        )
        terminal_pull = self.pull_request(provider_id=1019, number=29)
        terminal_subject = self.subject(terminal_pull, head_character="c")
        terminal_run, _ = self.accept_job(
            terminal_pull,
            terminal_subject,
            request_key="github:manual:expiry-terminal-run",
        )
        with self.runtime.transaction() as connection:
            first = self.claim_job(
                connection,
                lease_owner="worker-expiry-one",
                lease_duration=timedelta(minutes=2),
            )
            second = self.claim_job(
                connection,
                lease_owner="worker-expiry-two",
                lease_duration=timedelta(minutes=2),
            )
            connection.execute(
                """
                UPDATE review_agent.review_jobs
                SET last_heartbeat_at = started_at,
                    lease_expires_at = statement_timestamp()
                WHERE id IN (%s, %s)
                """,
                (first.id if first else 0, second.id if second else 0),
            )
            review_run_application.fail_run_in_transaction(
                connection,
                terminal_run.run.id,
                failure_code=failure_codes.REVIEW_FAILED,
            )
            recovered = review_run_application.recover_expired_jobs_in_transaction(
                connection,
                limit=10,
            )
        self.assertEqual(len(recovered.jobs), 1)
        requeued = recovered.jobs[0]
        self.assertEqual(requeued.review_run_id, retry_run.run.id)
        self.assertEqual(requeued.status, jobs.ReviewJobStatus.QUEUED)
        self.assertEqual(requeued.failure_code, failure_codes.JOB_LEASE_EXPIRED)

        with self.runtime.transaction() as connection:
            terminal_job = connection.execute(
                "SELECT status, failure_code FROM review_agent.review_jobs "
                "WHERE review_run_id = %s",
                (terminal_run.run.id,),
            ).fetchone()
            connection.execute(
                "UPDATE review_agent.review_jobs SET available_at = statement_timestamp() "
                "WHERE id = %s",
                (requeued.id,),
            )
            reclaimed = self.claim_job(
                connection,
                lease_owner="worker-expiry-three",
                lease_duration=timedelta(minutes=2),
            )
        self.assertEqual(terminal_job, ("failed", failure_codes.JOB_RUN_FAILED))
        assert reclaimed is not None
        self.assertEqual(reclaimed.attempt_count, 2)

        with self.runtime.transaction() as connection:
            connection.execute(
                """
                UPDATE review_agent.review_jobs
                SET last_heartbeat_at = started_at,
                    lease_expires_at = statement_timestamp()
                WHERE id = %s
                """,
                (reclaimed.id,),
            )
            exhausted = review_run_application.recover_expired_jobs_in_transaction(
                connection,
                limit=1,
            )
            run = review_runs.get_run(connection, retry_run.run.id)
        self.assertEqual(exhausted.jobs[0].status, jobs.ReviewJobStatus.DEAD_LETTER)
        self.assertEqual(run.status, ReviewStatus.FAILED)
        self.assertEqual(run.failure_code, failure_codes.JOB_RETRY_EXHAUSTED)

    def test_completed_run_atomically_succeeds_its_leased_job(self) -> None:
        pull_request = self.pull_request(provider_id=1020, number=30)
        subject = self.subject(pull_request, head_character="a")
        started, _ = self.accept_job(
            pull_request,
            subject,
            request_key="github:manual:run-completes-job",
        )
        with self.runtime.transaction() as connection:
            leased = self.claim_job(
                connection,
                lease_owner="worker-complete",
                lease_duration=timedelta(minutes=2),
            )
            for phase in (
                ReviewPhase.FETCHING_PR,
                ReviewPhase.COLLECTING_DIFF,
                ReviewPhase.REVIEWING,
                ReviewPhase.RENDERING,
                ReviewPhase.PUBLISHING,
            ):
                review_runs.advance_phase(connection, started.run.id, phase)
            review_run_application.complete_run_in_transaction(
                connection,
                started.run.id,
                findings_count=0,
            )
            job = jobs.get_job(connection, leased.id if leased else 0)
        self.assertEqual(job.status, jobs.ReviewJobStatus.SUCCEEDED)
        self.assertIsNone(job.lease_owner)
        self.assertIsNone(job.failure_code)

    def test_stale_sweep_leaves_job_recovery_to_the_job_owner(self) -> None:
        queued_pull = self.pull_request(provider_id=1021, number=31)
        queued_subject = self.subject(queued_pull, head_character="a")
        queued_run, queued_accepted = self.accept_job(
            queued_pull,
            queued_subject,
            request_key="github:manual:queued-stale",
        )
        leased_pull = self.pull_request(provider_id=1022, number=32)
        leased_subject = self.subject(leased_pull, head_character="c")
        leased_run, _ = self.accept_job(
            leased_pull,
            leased_subject,
            request_key="github:manual:leased-live",
            priority=10,
        )
        with self.runtime.transaction() as connection:
            leased = self.claim_job(
                connection,
                lease_owner="worker-live",
                lease_duration=timedelta(minutes=5),
            )
            assert leased is not None
            stale_at = datetime(2026, 8, 20, tzinfo=timezone.utc)
            connection.execute(
                "UPDATE review_agent.review_runs SET started_at = %s, "
                "last_heartbeat_at = %s WHERE id IN (%s, %s)",
                (stale_at, stale_at, queued_run.run.id, leased_run.run.id),
            )
            connection.execute(
                "UPDATE review_agent.review_jobs "
                "SET started_at = created_at, last_heartbeat_at = created_at, "
                "lease_expires_at = created_at + INTERVAL '1 microsecond' "
                "WHERE id = %s",
                (leased.id,),
            )
            failed = review_run_application.mark_stale_runs_failed_in_transaction(
                connection,
                cutoff=datetime(2026, 8, 24, tzinfo=timezone.utc),
                repository=None,
                pr_number=None,
            )
            queued_job = jobs.get_job(connection, queued_accepted.job.id)
            expired_job = jobs.get_job(connection, leased.id)
            recovered = review_run_application.recover_expired_jobs_in_transaction(
                connection,
                limit=1,
            )
            recovered_job = jobs.get_job(connection, leased.id)
        self.assertEqual(failed, ())
        self.assertEqual(queued_job.status, jobs.ReviewJobStatus.QUEUED)
        self.assertEqual(expired_job.status, jobs.ReviewJobStatus.LEASED)
        self.assertEqual(recovered.jobs[0].id, leased.id)
        self.assertEqual(recovered_job.status, jobs.ReviewJobStatus.QUEUED)

    def test_enqueue_translates_pull_request_lock_timeout(self) -> None:
        pull_request = self.pull_request(provider_id=1005, number=15)
        subject = self.subject(pull_request, head_character="a")
        started = self.start_run(
            pull_request,
            subject,
            request_key="github:issue-comment:7401",
        )

        with psycopg.connect(DSN) as lock_holder:
            with lock_holder.transaction():
                lock_holder.execute(
                    "SELECT id FROM review_agent.pull_requests "
                    "WHERE id = %s FOR NO KEY UPDATE",
                    (pull_request.id,),
                )
                with self.assertRaises(jobs.ReviewJobBusy):
                    with self.runtime.transaction() as connection:
                        jobs.enqueue_run(
                            connection,
                            review_run_id=started.run.id,
                            priority=0,
                            max_attempts=3,
                            active_job_limit=ACTIVE_JOB_LIMIT,
                        )

    def test_enqueue_translates_review_run_lock_timeout(self) -> None:
        pull_request = self.pull_request(provider_id=1013, number=23)
        subject = self.subject(pull_request, head_character="a")
        started = self.start_run(
            pull_request,
            subject,
            request_key="github:issue-comment:locked-run",
        )

        with psycopg.connect(DSN) as lock_holder:
            with lock_holder.transaction():
                lock_holder.execute(
                    "SELECT id FROM review_agent.review_runs WHERE id = %s FOR UPDATE",
                    (started.run.id,),
                )
                with self.assertRaises(jobs.ReviewJobBusy):
                    with self.runtime.transaction() as connection:
                        jobs.enqueue_run(
                            connection,
                            review_run_id=started.run.id,
                            priority=0,
                            max_attempts=3,
                            active_job_limit=ACTIVE_JOB_LIMIT,
                        )

    def test_concurrent_claimers_receive_distinct_fenced_leases(self) -> None:
        first_pull = self.pull_request(provider_id=1007, number=17)
        second_pull = self.pull_request(provider_id=1008, number=18)
        first_subject = self.subject(first_pull, head_character="a")
        second_subject = self.subject(second_pull, head_character="c")
        self.accept_job(first_pull, first_subject, request_key="github:manual:7501")
        self.accept_job(second_pull, second_subject, request_key="github:manual:7502")
        start = Barrier(2)

        def claim(worker: str) -> jobs.ReviewJob | None:
            with self.runtime.transaction() as connection:
                start.wait(timeout=5)
                return self.claim_job(
                    connection,
                    lease_owner=worker,
                    lease_duration=timedelta(minutes=2),
                )

        with ThreadPoolExecutor(max_workers=2) as executor:
            claimed = tuple(executor.map(claim, ("worker-one", "worker-two")))

        self.assertNotIn(None, claimed)
        concrete = tuple(item for item in claimed if item is not None)
        self.assertEqual(len({item.id for item in concrete}), 2)
        self.assertEqual(
            {item.status for item in concrete},
            {jobs.ReviewJobStatus.LEASED},
        )
        self.assertEqual({item.lease_generation for item in concrete}, {1})
        self.assertEqual({item.attempt_count for item in concrete}, {1})
        self.assertEqual(
            {item.lease_owner for item in concrete},
            {"worker-one", "worker-two"},
        )

        with self.runtime.transaction() as connection:
            self.assertIsNone(
                self.claim_job(
                    connection,
                    lease_owner="worker-three",
                    lease_duration=timedelta(minutes=2),
                )
            )

    def test_higher_priority_ready_job_is_claimed_first(self) -> None:
        first_pull = self.pull_request(provider_id=1009, number=19)
        second_pull = self.pull_request(provider_id=1010, number=20)
        first_subject = self.subject(first_pull, head_character="a")
        second_subject = self.subject(second_pull, head_character="c")
        _, lower = self.accept_job(
            first_pull,
            first_subject,
            request_key="github:manual:7601",
            priority=1,
        )
        _, higher = self.accept_job(
            second_pull,
            second_subject,
            request_key="github:manual:7602",
            priority=5,
        )
        assert isinstance(lower, jobs.EnqueuedJob)
        assert isinstance(higher, jobs.EnqueuedJob)

        with self.runtime.transaction() as connection:
            claimed = self.claim_job(
                connection,
                lease_owner="worker-priority",
                lease_duration=timedelta(minutes=2),
            )

        assert claimed is not None
        self.assertEqual(claimed.id, higher.job.id)

    def test_capacity_admission_is_exact_across_concurrent_requests(self) -> None:
        start = Barrier(2)

        def admit(index: int) -> review_run_application.AdmittedReview | str:
            start.wait(timeout=5)
            try:
                return review_run_application.admit_postgres_review(
                    self.runtime,
                    self.admission_request(
                        provider_id=1100 + index,
                        pr_number=50 + index,
                        request_key=f"github:manual:capacity-{index}",
                        head="a" if index == 0 else "c",
                    ),
                    priority=0,
                    max_attempts=3,
                    active_job_limit=1,
                )
            except jobs.ReviewQueueFull:
                return "full"

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = tuple(executor.map(admit, range(2)))

        self.assertEqual(sum(item == "full" for item in outcomes), 1)
        with self.runtime.transaction() as connection:
            counts = connection.execute(
                "SELECT "
                "(SELECT count(*) FROM review_agent.review_runs), "
                "(SELECT count(*) FROM review_agent.review_jobs)"
            ).fetchone()
        self.assertEqual(counts, (1, 1))

    def test_one_repository_cannot_hold_two_live_leases(self) -> None:
        first_pull = self.pull_request(provider_id=1200, number=61)
        second_pull = self.pull_request(provider_id=1200, number=62)
        self.accept_job(
            first_pull,
            self.subject(first_pull, head_character="a"),
            request_key="github:manual:repo-fairness-one",
        )
        self.accept_job(
            second_pull,
            self.subject(second_pull, head_character="c"),
            request_key="github:manual:repo-fairness-two",
        )
        start = Barrier(2)

        def claim(worker: str) -> jobs.ReviewJob | None:
            with self.runtime.transaction() as connection:
                start.wait(timeout=5)
                return self.claim_job(
                    connection,
                    lease_owner=worker,
                    lease_duration=timedelta(minutes=2),
                )

        with ThreadPoolExecutor(max_workers=2) as executor:
            claimed = tuple(executor.map(claim, ("worker-fair-one", "worker-fair-two")))

        self.assertEqual(sum(item is not None for item in claimed), 1)

    def test_priority_aging_prevents_an_old_ready_job_from_starving(self) -> None:
        old_pull = self.pull_request(provider_id=1300, number=71)
        new_pull = self.pull_request(provider_id=1301, number=72)
        _, old_job = self.accept_job(
            old_pull,
            self.subject(old_pull, head_character="a"),
            request_key="github:manual:aging-old",
            priority=0,
        )
        self.accept_job(
            new_pull,
            self.subject(new_pull, head_character="c"),
            request_key="github:manual:aging-new",
            priority=5,
        )
        assert isinstance(old_job, jobs.EnqueuedJob)
        with self.runtime.transaction() as connection:
            connection.execute(
                "UPDATE review_agent.review_jobs "
                "SET created_at = statement_timestamp() - INTERVAL '2 hours', "
                "available_at = statement_timestamp() - INTERVAL '2 hours' "
                "WHERE id = %s",
                (old_job.job.id,),
            )
            claimed = self.claim_job(
                connection,
                lease_owner="worker-aging",
                lease_duration=timedelta(minutes=2),
            )

        assert claimed is not None
        self.assertEqual(claimed.id, old_job.job.id)

    def test_operator_can_inspect_release_and_cancel_active_work(self) -> None:
        pull_request = self.pull_request(provider_id=1400, number=81)
        started, accepted = self.accept_job(
            pull_request,
            self.subject(pull_request, head_character="a"),
            request_key="github:manual:operator-control",
        )
        assert isinstance(accepted, jobs.EnqueuedJob)
        with self.runtime.transaction() as connection:
            connection.execute(
                "UPDATE review_agent.review_jobs "
                "SET available_at = statement_timestamp() + INTERVAL '1 hour' "
                "WHERE id = %s",
                (accepted.job.id,),
            )

        reports = operator_application.list_review_jobs(self.runtime, limit=10)
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0].repository, "team/service-1400")
        released = operator_application.retry_review_job(
            self.runtime, job_id=accepted.job.id
        )
        self.assertLess(
            released.available_at, accepted.job.available_at + timedelta(hours=1)
        )

        cancelled = operator_application.cancel_review_job(
            self.runtime, job_id=accepted.job.id
        )
        with self.runtime.transaction() as connection:
            run = review_runs.get_run(connection, started.run.id)
        self.assertEqual(cancelled.status, jobs.ReviewJobStatus.FAILED)
        self.assertEqual(run.status, ReviewStatus.FAILED)
        self.assertEqual(run.failure_code, failure_codes.OPERATOR_CANCELLED)


if __name__ == "__main__":
    unittest.main()
