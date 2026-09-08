from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import os
from threading import Barrier
import unittest
from uuid import uuid4

from tests import test_postgres_jobs
from review_agent_tools import failure_codes, review_contract, review_run_application
from review_agent_tools.domain.review import resolve_review_subject
from review_agent_tools.model_quota import QuotaCheck
from review_agent_tools.postgres import jobs, registry


@unittest.skipUnless(
    os.environ.get("REVIEW_AGENT_POSTGRES_DSN"), "requires isolated PostgreSQL"
)
class ModelCapacityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = test_postgres_jobs.PostgreSQLJobTests("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.runtime = self.fixture.runtime
        self.sequence = 0
        with self.runtime.transaction() as connection:
            self.teams = [
                row[0]
                for row in connection.execute(
                    "INSERT INTO review_agent.teams (name) VALUES ('Payments'), ('Search') RETURNING id"
                ).fetchall()
            ]

    def enqueue(
        self, team_id: int | None, *, connection_id: int = 1, priority: int = 0
    ) -> jobs.ReviewJob:
        self.sequence += 1
        pr = self.fixture.pull_request(provider_id=8000 + self.sequence, number=1)
        definition = resolve_review_subject(
            base_sha="b" * 40,
            head_sha="a" * 40,
            policy_revision="profile@1",
            resolved_config_schema_version=2,
            resolved_config=review_contract.resolved_config(
                test_postgres_jobs.TEST_REVIEW_CONTRACT,
                model_route=review_contract.ModelRoute(team_id, connection_id, 1, 1),
            ),
        )
        with self.runtime.transaction() as connection:
            subject = registry.create_or_get_subject(connection, pr.id, definition)
        _, result = self.fixture.accept_job(
            pr, subject, request_key=f"capacity-{self.sequence}", priority=priority
        )
        return result.job

    def claim(self, runtime_key: str = "shared") -> jobs.ReviewJob | None:
        with self.runtime.transaction() as connection:
            return jobs.claim_next_job(
                connection,
                lease_owner="capacity-test",
                lease_duration=timedelta(minutes=2),
                priority_aging_interval=test_postgres_jobs.PRIORITY_AGING_INTERVAL,
                runtime_key=runtime_key,
            )

    def test_eligible_teams_take_turns_with_priority_preserved_within_each_team(
        self,
    ) -> None:
        low = self.enqueue(self.teams[0], priority=1)
        high = self.enqueue(self.teams[0], priority=2)
        other = self.enqueue(self.teams[1])
        retained = self.enqueue(None)
        more_retained = self.enqueue(None)
        with self.runtime.transaction() as connection:
            connection.execute(
                "UPDATE review_agent.model_connections SET max_concurrency = 5 WHERE id = 1"
            )
        self.assertEqual(self.claim().id, retained.id)
        self.assertEqual(self.claim().id, high.id)
        self.assertEqual(self.claim().id, other.id)
        self.assertEqual(self.claim().id, more_retained.id)
        self.assertEqual(self.claim().id, low.id)

    def test_replicas_share_team_capacity_across_connections(self) -> None:
        with self.runtime.transaction() as connection:
            connection.execute(
                "INSERT INTO review_agent.team_model_policies (team_id, max_concurrency) VALUES (%s, 1), (%s, 1)",
                tuple(self.teams),
            )
            other = connection.execute(
                "INSERT INTO review_agent.model_connections (runtime_key, name, state, max_concurrency) VALUES ('other', 'Other', 'enabled', 2) RETURNING id"
            ).fetchone()[0]
            connection.execute(
                "UPDATE review_agent.model_connections SET max_concurrency = 2 WHERE id = 1"
            )
            connection.execute(
                "INSERT INTO review_agent.model_accounts (connection_id, provider) VALUES (%s, 'openai-codex')",
                (other,),
            )
        self.enqueue(self.teams[0])
        self.enqueue(self.teams[0], connection_id=other)
        self.enqueue(self.teams[1])
        self.enqueue(self.teams[1], connection_id=other)
        barrier = Barrier(4)

        def claim(runtime_key: str) -> jobs.ReviewJob | None:
            barrier.wait(5)
            return self.claim(runtime_key)

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = tuple(pool.map(claim, ("shared", "other", "shared", "other")))
        self.assertEqual(sum(item is not None for item in results), 2)
        with self.runtime.transaction() as connection:
            counts = connection.execute("""SELECT subject.admission_team_id, count(*)
                FROM review_agent.review_jobs job JOIN review_agent.review_runs run ON run.id = job.review_run_id
                JOIN review_agent.review_subjects subject ON subject.id = run.review_subject_id
                WHERE job.status = 'leased' GROUP BY subject.admission_team_id""").fetchall()
            self.assertEqual(dict(counts), dict.fromkeys(self.teams, 1))

    def test_unfinished_remote_execution_holds_capacity_after_lease_recovery_and_terminalization(
        self,
    ) -> None:
        first = self.enqueue(self.teams[0])
        second = self.enqueue(self.teams[1])
        with self.runtime.transaction() as connection:
            connection.execute(
                "UPDATE review_agent.model_connections SET max_concurrency = 1 WHERE id = 1"
            )
        claimed = self.claim()
        self.assertEqual(claimed.id, first.id)
        session = jobs.WorkerLeaseSession(claimed.id, claimed.lease_generation)
        instance = uuid4()
        with self.runtime.transaction() as connection:
            jobs.begin_model_execution(
                connection,
                session=session,
                runtime_key="shared",
                runtime_instance=instance,
                provider="openai-codex",
                model="gpt-test",
                reasoning_effort="high",
                identity_sha256="a" * 64,
                quota=None,
            )
            connection.execute(
                "UPDATE review_agent.review_jobs SET lease_expires_at = statement_timestamp() WHERE id = %s",
                (claimed.id,),
            )
        with self.runtime.transaction() as connection:
            jobs.recover_expired_leases(connection, limit=10)
            review_run_application.fail_run_in_transaction(
                connection,
                first.review_run_id,
                failure_code=failure_codes.REVIEW_FAILED,
            )
        self.assertIsNone(self.claim())
        with self.runtime.transaction() as connection:
            jobs.finish_model_execution(
                connection, session=session, runtime_instance=instance
            )
        self.assertEqual(self.claim().id, second.id)

    def test_already_claimed_work_cannot_clear_a_newer_account_cooldown(self) -> None:
        self.enqueue(self.teams[0])
        self.enqueue(self.teams[1])
        first, second = self.claim(), self.claim()
        assert first is not None and second is not None
        now = datetime.now(timezone.utc)
        for claimed, quota in (
            (first, QuotaCheck(now, False, now + timedelta(minutes=5))),
            (second, QuotaCheck(now - timedelta(seconds=1), True, now)),
        ):
            with self.runtime.transaction() as connection:
                self.assertIsNone(
                    jobs.begin_model_execution(
                        connection,
                        session=jobs.WorkerLeaseSession(
                            claimed.id, claimed.lease_generation
                        ),
                        runtime_key="shared",
                        runtime_instance=uuid4(),
                        provider="openai-codex",
                        model="gpt-test",
                        reasoning_effort="high",
                        identity_sha256="a" * 64,
                        quota=quota,
                    )
                )
                self.assertEqual(jobs.get_job(connection, claimed.id).attempt_count, 0)
        self.assertIsNone(self.claim())
