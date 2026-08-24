from __future__ import annotations

import os
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from threading import Barrier

import psycopg


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = ROOT / "bootstrap" / "plugins"
sys.path.insert(0, str(PACKAGE_ROOT))

from review_agent_tools.domain.review import resolve_review_subject  # noqa: E402
from review_agent_tools.postgres import jobs, registry, review_runs  # noqa: E402
from review_agent_tools.postgres.runtime import PostgreSQLRuntime  # noqa: E402
from review_agent_tools.postgres_migrations import runner  # noqa: E402
from review_agent_tools.settings import PostgresDatabaseUrl  # noqa: E402


DSN = os.environ.get("REVIEW_AGENT_POSTGRES_DSN", "")


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
            resolved_config_schema_version=1,
            resolved_config={"profile": "team-standard"},
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
    ) -> tuple[review_runs.RunStart, jobs.JobEnqueue]:
        with self.runtime.transaction() as connection:
            run = review_runs.start_run(
                connection,
                pull_request_id=pull_request.id,
                review_subject_id=subject.id,
                request_key=request_key,
            )
            job = jobs.enqueue_run(
                connection,
                review_run_id=run.run.id,
                priority=priority,
                max_attempts=3,
            )
        return run, job

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

    def test_requeue_preserves_a_monotonic_lease_generation(self) -> None:
        pull_request = self.pull_request(provider_id=1011, number=21)
        subject = self.subject(pull_request, head_character="a")
        _, accepted = self.accept_job(
            pull_request,
            subject,
            request_key="github:manual:requeue-fence",
        )
        assert isinstance(accepted, jobs.EnqueuedJob)
        with self.runtime.transaction() as connection:
            first_lease = jobs.claim_next_job(
                connection,
                lease_owner="worker-one",
                lease_duration=timedelta(minutes=2),
            )
        assert first_lease is not None

        with self.runtime.transaction() as connection:
            connection.execute(
                """
                UPDATE review_agent.review_jobs
                SET status = 'queued', lease_owner = NULL,
                    lease_expires_at = NULL, last_heartbeat_at = NULL
                WHERE id = %s
                """,
                (accepted.job.id,),
            )
            second_lease = jobs.claim_next_job(
                connection,
                lease_owner="worker-two",
                lease_duration=timedelta(minutes=2),
            )

        assert second_lease is not None
        self.assertEqual(first_lease.lease_generation, 1)
        self.assertEqual(second_lease.lease_generation, 2)
        self.assertEqual(second_lease.started_at, first_lease.started_at)

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

    def test_new_subject_leaves_leased_job_fence_intact(self) -> None:
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
            leased = jobs.claim_next_job(
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

        self.assertEqual(retained.status, jobs.ReviewJobStatus.LEASED)
        self.assertEqual(retained.lease_owner, "worker-one")
        self.assertEqual(retained.lease_generation, 1)
        self.assertEqual(queued.status, jobs.ReviewJobStatus.QUEUED)

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
            leased = jobs.claim_next_job(
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
            review_runs.fail_run(
                connection,
                started.run.id,
                failure_code="stale_timeout",
            )
        with self.runtime.transaction() as connection:
            claimed = jobs.claim_next_job(
                connection,
                lease_owner="worker-terminal",
                lease_duration=timedelta(minutes=2),
            )
            retained = jobs.get_job(connection, accepted.job.id)

        self.assertIsNone(claimed)
        self.assertEqual(retained.status, jobs.ReviewJobStatus.QUEUED)

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
                    "SELECT id FROM review_agent.review_runs "
                    "WHERE id = %s FOR UPDATE",
                    (started.run.id,),
                )
                with self.assertRaises(jobs.ReviewJobBusy):
                    with self.runtime.transaction() as connection:
                        jobs.enqueue_run(
                            connection,
                            review_run_id=started.run.id,
                            priority=0,
                            max_attempts=3,
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
                return jobs.claim_next_job(
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
                jobs.claim_next_job(
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
            claimed = jobs.claim_next_job(
                connection,
                lease_owner="worker-priority",
                lease_duration=timedelta(minutes=2),
            )

        assert claimed is not None
        self.assertEqual(claimed.id, higher.job.id)


if __name__ == "__main__":
    unittest.main()
