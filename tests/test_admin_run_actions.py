from __future__ import annotations

import os
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from typing import cast
from unittest.mock import Mock

import psycopg
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bootstrap/plugins"))

from review_agent_tools import review_contract, review_run_application  # noqa: E402
from review_agent_tools.admin_run_api import create_router  # noqa: E402
from review_agent_tools.domain.review import JsonObject, resolve_review_subject  # noqa: E402
from review_agent_tools.postgres import admin_run_actions, jobs, registry  # noqa: E402
from review_agent_tools.postgres.runtime import PostgreSQLRuntime  # noqa: E402
from review_agent_tools.postgres_migrations import runner  # noqa: E402
from review_agent_tools.settings import PostgresDatabaseUrl  # noqa: E402


DSN = os.environ.get("REVIEW_AGENT_RUN_ACTIONS_DSN", "")
CONTRACT = review_contract.ReviewContract(
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


class DeniedAuth:
    def current_user(self) -> object:
        raise HTTPException(403, "Forbidden")

    def current_scope(self) -> object:
        raise HTTPException(403, "Forbidden")


class AdminRunAuthorizationTests(unittest.TestCase):
    def test_controls_require_authorized_access(self) -> None:
        app = FastAPI()
        app.include_router(create_router(Mock(), DeniedAuth()))  # type: ignore[arg-type]
        client = TestClient(app)
        self.assertEqual(client.get("/api/history/1/controls").status_code, 403)
        self.assertEqual(
            client.post(
                "/api/history/1/actions",
                json={
                    "action": "cancel",
                    "expected_job_id": 1,
                    "expected_lease_generation": 0,
                    "expected_status": "queued",
                    "expected_available_at": "2026-09-08T10:00:00Z",
                    "reason": "operator request",
                },
            ).status_code,
            403,
        )


@unittest.skipUnless(DSN, "set REVIEW_AGENT_RUN_ACTIONS_DSN to an isolated database")
class AdminRunActionTests(unittest.TestCase):
    def setUp(self) -> None:
        with psycopg.connect(DSN, autocommit=True) as connection:
            connection.execute("DROP SCHEMA IF EXISTS review_agent CASCADE")
        with psycopg.connect(DSN) as connection:
            runner.apply_migrations(connection)
        self.runtime = PostgreSQLRuntime(PostgresDatabaseUrl(DSN))
        self.runtime.open()
        self.addCleanup(self.runtime.close)

    def queued_job(self, *, suffix: int) -> jobs.ReviewJob:
        with self.runtime.transaction() as connection:
            repository = registry.ensure_repository(
                connection,
                registry.RepositoryDefinition(
                    provider="github",
                    provider_repository_id=9000 + suffix,
                    full_name=f"team/run-actions-{suffix}",
                ),
            )
            pull_request = registry.ensure_pull_request(
                connection, repository.id, suffix
            )
            subject = registry.create_or_get_subject(
                connection,
                pull_request.id,
                resolve_review_subject(
                    base_sha="b" * 40,
                    head_sha="c" * 40,
                    policy_revision="profile@1",
                    resolved_config_schema_version=2,
                    resolved_config=cast(
                        JsonObject, review_contract.resolved_config(CONTRACT)
                    ),
                ),
            )
            run = review_run_application.start_run_in_transaction(
                connection,
                pull_request_id=pull_request.id,
                review_subject_id=subject.id,
                request_key=f"github:manual:run-actions-{suffix}",
            )
            enqueued = jobs.enqueue_run(
                connection,
                review_run_id=run.run.id,
                priority=0,
                max_attempts=3,
                active_job_limit=100,
            )
        assert isinstance(enqueued, jobs.EnqueuedJob)
        return enqueued.job

    def request(
        self,
        job: jobs.ReviewJob,
        action: admin_run_actions.RunAction,
        *,
        generation: int | None = None,
    ) -> admin_run_actions.ActionRequest:
        return admin_run_actions.ActionRequest(
            action=action,
            expected_job_id=job.id,
            expected_lease_generation=(
                job.lease_generation if generation is None else generation
            ),
            expected_status=job.status,
            expected_available_at=job.available_at,
            actor="admin:4a0c9aa4-644f-4ca1-8da6-5cb20511d022",
            reason="operator inspected the current run",
        )

    def test_release_retry_is_fenced_and_audited_without_starting_a_new_run(
        self,
    ) -> None:
        original = self.queued_job(suffix=1)
        with self.runtime.transaction() as connection:
            delayed = connection.execute(
                """
                UPDATE review_agent.review_jobs
                SET available_at = statement_timestamp() + INTERVAL '1 hour',
                    failure_code = 'job_retryable_execution'
                WHERE id = %s
                RETURNING available_at
                """,
                (original.id,),
            ).fetchone()
        assert delayed is not None
        current = replace(
            original,
            available_at=delayed[0],
            failure_code="job_retryable_execution",
        )
        with self.runtime.transaction() as connection:
            admin_run_actions.apply_action(
                connection,
                run_id=current.review_run_id,
                request=self.request(
                    current, admin_run_actions.RunAction.RELEASE_RETRY
                ),
            )
            controls = admin_run_actions.controls(
                connection, run_id=current.review_run_id
            )
            run_count = connection.execute(
                "SELECT count(*) FROM review_agent.review_runs"
            ).fetchone()
        assert controls.job is not None
        self.assertLess(controls.job.available_at, current.available_at)
        self.assertEqual(controls.job.lease_generation, current.lease_generation)
        self.assertEqual(run_count, (1,))
        self.assertEqual(
            controls.audit[0].action, admin_run_actions.RunAction.RELEASE_RETRY
        )

    def test_cancel_terminalizes_the_exact_running_run_and_job_with_one_audit(
        self,
    ) -> None:
        current = self.queued_job(suffix=2)
        with self.runtime.transaction() as connection:
            admin_run_actions.apply_action(
                connection,
                run_id=current.review_run_id,
                request=self.request(current, admin_run_actions.RunAction.CANCEL),
            )
            controls = admin_run_actions.controls(
                connection, run_id=current.review_run_id
            )
        assert controls.job is not None
        self.assertEqual(controls.run.status.value, "failed")
        self.assertEqual(controls.job.status, jobs.ReviewJobStatus.FAILED)
        self.assertEqual(len(controls.audit), 1)
        self.assertEqual(
            controls.audit[0].actor,
            self.request(current, admin_run_actions.RunAction.CANCEL).actor,
        )

    def test_stale_generation_rejects_without_mutation_or_audit(self) -> None:
        current = self.queued_job(suffix=3)
        with self.assertRaises(admin_run_actions.AdminRunActionStale):
            with self.runtime.transaction() as connection:
                admin_run_actions.apply_action(
                    connection,
                    run_id=current.review_run_id,
                    request=self.request(
                        current,
                        admin_run_actions.RunAction.CANCEL,
                        generation=current.lease_generation + 1,
                    ),
                )
        with self.runtime.transaction() as connection:
            controls = admin_run_actions.controls(
                connection, run_id=current.review_run_id
            )
        assert controls.job is not None
        self.assertEqual(controls.run.status.value, "running")
        self.assertEqual(controls.job.status, jobs.ReviewJobStatus.QUEUED)
        self.assertEqual(controls.audit, ())

    def test_mark_stalled_requires_old_heartbeat_and_no_live_lease(self) -> None:
        current = self.queued_job(suffix=4)
        with self.runtime.transaction() as connection:
            connection.execute(
                "UPDATE review_agent.review_runs "
                "SET started_at = statement_timestamp() - INTERVAL '31 minutes', "
                "last_heartbeat_at = statement_timestamp() - INTERVAL '30 minutes' "
                "WHERE id = %s",
                (current.review_run_id,),
            )
            admin_run_actions.apply_action(
                connection,
                run_id=current.review_run_id,
                request=replace(
                    self.request(current, admin_run_actions.RunAction.MARK_STALLED),
                    stale_after_minutes=15,
                ),
            )
            controls = admin_run_actions.controls(
                connection, run_id=current.review_run_id
            )
        self.assertEqual(controls.run.status.value, "failed")
        self.assertEqual(controls.audit[0].stale_after_minutes, 15)


if __name__ == "__main__":
    unittest.main()
