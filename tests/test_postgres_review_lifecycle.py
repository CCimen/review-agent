from __future__ import annotations

import os
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from typing import cast


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = ROOT / "bootstrap" / "plugins"
sys.path.insert(0, str(PACKAGE_ROOT))

from review_agent_tools.domain.review import (  # noqa: E402
    JsonObject,
    ReviewDomainError,
    ReviewPhase,
    ReviewStatus,
    resolve_review_subject,
)
from review_agent_tools.postgres import registry  # noqa: E402
from review_agent_tools.postgres import review_runs as postgres_review_runs  # noqa: E402
from review_agent_tools.postgres.runtime import PostgreSQLRuntime  # noqa: E402
from review_agent_tools.postgres_migrations import runner  # noqa: E402
from review_agent_tools import review_run_application  # noqa: E402
from review_agent_tools.settings import PostgresDatabaseUrl  # noqa: E402


DSN = os.environ.get("REVIEW_AGENT_POSTGRES_DSN", "")


class ReviewSubjectContractTests(unittest.TestCase):
    def test_resolved_config_is_canonical_versioned_and_hashed(self) -> None:
        first = resolve_review_subject(
            base_sha="b" * 40,
            head_sha="a" * 40,
            policy_revision="profile@1",
            resolved_config_schema_version=1,
            resolved_config={"z": [3, 2, 1], "enabled": True, "nested": {"b": 2, "a": 1}},
        )
        second = resolve_review_subject(
            base_sha="B" * 40,
            head_sha="A" * 40,
            policy_revision=" profile@1 ",
            resolved_config_schema_version=1,
            resolved_config={"nested": {"a": 1, "b": 2}, "enabled": True, "z": [3, 2, 1]},
        )

        self.assertEqual(first, second)
        self.assertEqual(
            first.resolved_config.canonical_json,
            '{"enabled":true,"nested":{"a":1,"b":2},"z":[3,2,1]}',
        )
        self.assertEqual(len(first.resolved_config.sha256), 64)

    def test_resolved_config_rejects_non_json_and_invalid_subject_values(self) -> None:
        invalid_inputs = (
            ({"limit": float("nan")}, "resolved_config must contain finite JSON values"),
            ({"keys": {1: "not a string"}}, "resolved_config object keys must be strings"),
        )
        for resolved_config, message in invalid_inputs:
            with self.subTest(resolved_config=resolved_config):
                with self.assertRaisesRegex(ReviewDomainError, message):
                    resolve_review_subject(
                        base_sha="b" * 40,
                        head_sha="a" * 40,
                        policy_revision="profile@1",
                        resolved_config_schema_version=1,
                        resolved_config=cast(JsonObject, resolved_config),
                    )

        with self.assertRaisesRegex(ReviewDomainError, "base_sha"):
            resolve_review_subject(
                base_sha="not-a-sha",
                head_sha="a" * 40,
                policy_revision="profile@1",
                resolved_config_schema_version=1,
                resolved_config={},
            )


@unittest.skipUnless(DSN, "run through scripts/check_postgres_schema.sh")
class PostgreSQLRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        import psycopg

        with psycopg.connect(DSN, autocommit=True) as connection:
            connection.execute("DROP SCHEMA IF EXISTS review_agent CASCADE")
        with psycopg.connect(DSN) as connection:
            runner.apply_migrations(connection)
        self.runtime = PostgreSQLRuntime(PostgresDatabaseUrl(DSN))
        self.runtime.open()
        self.addCleanup(self.runtime.close)

    def test_provider_identity_survives_rename_and_pr_numbers_are_repository_scoped(
        self,
    ) -> None:
        with self.runtime.transaction() as connection:
            first = registry.ensure_repository(
                connection,
                registry.RepositoryDefinition(
                    provider="github",
                    provider_repository_id=901,
                    full_name="team/old-name",
                ),
            )
            first_pr = registry.ensure_pull_request(connection, first.id, 17)
            second = registry.ensure_repository(
                connection,
                registry.RepositoryDefinition(
                    provider="github",
                    provider_repository_id=902,
                    full_name="other/repository",
                ),
            )
            second_pr = registry.ensure_pull_request(connection, second.id, 17)

        with self.runtime.transaction() as connection:
            renamed = registry.ensure_repository(
                connection,
                registry.RepositoryDefinition(
                    provider="github",
                    provider_repository_id=901,
                    full_name="platform/new-name",
                ),
            )

        self.assertEqual(renamed.id, first.id)
        self.assertEqual(renamed.full_name, "platform/new-name")
        self.assertNotEqual(first_pr.id, second_pr.id)
        self.assertNotEqual(first_pr.repository_id, second_pr.repository_id)

    def test_subject_create_or_get_reverifies_canonical_immutable_config(self) -> None:
        expected = resolve_review_subject(
            base_sha="b" * 40,
            head_sha="a" * 40,
            policy_revision="profile@1",
            resolved_config_schema_version=1,
            resolved_config={"mode": "strict", "limits": {"files": 300}},
        )
        changed = resolve_review_subject(
            base_sha="b" * 40,
            head_sha="a" * 40,
            policy_revision="profile@1",
            resolved_config_schema_version=1,
            resolved_config={"mode": "strict", "limits": {"files": 200}},
        )
        with self.runtime.transaction() as connection:
            repository = registry.ensure_repository(
                connection,
                registry.RepositoryDefinition("github", 903, "team/subject"),
            )
            pull_request = registry.ensure_pull_request(connection, repository.id, 8)
            first = registry.create_or_get_subject(
                connection, pull_request.id, expected
            )
            repeated = registry.create_or_get_subject(
                connection, pull_request.id, expected
            )
            different = registry.create_or_get_subject(
                connection, pull_request.id, changed
            )

        self.assertEqual(repeated.id, first.id)
        self.assertNotEqual(different.id, first.id)

        with self.runtime.transaction() as connection:
            connection.execute(
                "UPDATE review_agent.review_subjects "
                "SET resolved_config = '{\"tampered\":true}'::jsonb WHERE id = %s",
                (first.id,),
            )
        with self.assertRaisesRegex(registry.SubjectConflict, "stored resolved_config"):
            with self.runtime.transaction() as connection:
                registry.create_or_get_subject(connection, pull_request.id, expected)

    def test_rename_to_another_repository_name_fails_without_losing_identity(
        self,
    ) -> None:
        with self.runtime.transaction() as connection:
            registry.ensure_repository(
                connection, registry.RepositoryDefinition("github", 904, "team/one")
            )
            second = registry.ensure_repository(
                connection, registry.RepositoryDefinition("github", 905, "team/two")
            )

        with self.assertRaises(registry.RepositoryNameConflict):
            with self.runtime.transaction() as connection:
                registry.ensure_repository(
                    connection,
                    registry.RepositoryDefinition("github", 905, "TEAM/ONE"),
                )

        with self.runtime.transaction() as connection:
            unchanged = registry.ensure_repository(
                connection, registry.RepositoryDefinition("github", 905, "team/two")
            )
        self.assertEqual(unchanged.id, second.id)
        self.assertEqual(unchanged.full_name, "team/two")


@unittest.skipUnless(DSN, "run through scripts/check_postgres_schema.sh")
class PostgreSQLReviewStartTests(unittest.TestCase):
    def setUp(self) -> None:
        import psycopg

        with psycopg.connect(DSN, autocommit=True) as connection:
            connection.execute("DROP SCHEMA IF EXISTS review_agent CASCADE")
        with psycopg.connect(DSN) as connection:
            runner.apply_migrations(connection)
        self.runtime = PostgreSQLRuntime(PostgresDatabaseUrl(DSN))
        self.runtime.open()
        self.addCleanup(self.runtime.close)

    @staticmethod
    def request(
        *,
        provider_repository_id: int = 910,
        repository: str = "team/reviewer",
        request_key: str = "github:issue-comment:1001",
        head_sha: str = "a" * 40,
    ) -> review_run_application.PostgresRunRequest:
        return review_run_application.PostgresRunRequest(
            provider="github",
            provider_repository_id=provider_repository_id,
            repository=repository,
            pr_number=14,
            base_sha="b" * 40,
            head_sha=head_sha,
            policy_revision="profile@1",
            resolved_config_schema_version=1,
            resolved_config={"profile": "sundsvall-standard"},
            request_key=request_key,
            trigger_comment_id=1001,
            trigger_user="reviewer",
        )

    def test_start_is_idempotent_and_cross_scope_key_conflict_rolls_back(self) -> None:
        first = review_run_application.start_postgres_review(
            self.runtime, self.request()
        )
        repeated = review_run_application.start_postgres_review(
            self.runtime, self.request()
        )

        self.assertIsInstance(first, postgres_review_runs.StartedRun)
        self.assertIsInstance(repeated, postgres_review_runs.DuplicateRun)
        assert isinstance(first, postgres_review_runs.StartedRun)
        assert isinstance(repeated, postgres_review_runs.DuplicateRun)
        self.assertEqual(repeated.run.id, first.run.id)
        self.assertEqual(repeated.reason, "request_key")

        with self.assertRaises(postgres_review_runs.DuplicateReviewRequest):
            review_run_application.start_postgres_review(
                self.runtime,
                self.request(
                    provider_repository_id=911,
                    repository="other/reviewer",
                ),
            )

        with self.runtime.transaction() as connection:
            rolled_back = connection.execute(
                "SELECT count(*) FROM review_agent.repositories "
                "WHERE provider_repository_id = 911"
            ).fetchone()
        self.assertEqual(rolled_back, (0,))

    def test_run_transitions_are_explicit_and_monotonic(self) -> None:
        started = review_run_application.start_postgres_review(
            self.runtime, self.request()
        )
        assert isinstance(started, postgres_review_runs.StartedRun)
        run_id = started.run.id

        with self.runtime.transaction() as connection:
            postgres_review_runs.advance_phase(
                connection, run_id, ReviewPhase.FETCHING_PR
            )
            collecting = postgres_review_runs.advance_phase(
                connection, run_id, ReviewPhase.COLLECTING_DIFF
            )
        self.assertEqual(collecting.phase, ReviewPhase.COLLECTING_DIFF)

        with self.assertRaises(postgres_review_runs.InvalidReviewTransition):
            with self.runtime.transaction() as connection:
                postgres_review_runs.advance_phase(
                    connection, run_id, ReviewPhase.FETCHING_PR
                )

        with self.assertRaises(postgres_review_runs.InvalidReviewTransition):
            with self.runtime.transaction() as connection:
                postgres_review_runs.complete_run(connection, run_id, findings_count=0)

        with self.runtime.transaction() as connection:
            for phase in (
                ReviewPhase.REVIEWING,
                ReviewPhase.RENDERING,
                ReviewPhase.PUBLISHING,
            ):
                postgres_review_runs.advance_phase(connection, run_id, phase)
            completed = postgres_review_runs.complete_run(
                connection, run_id, findings_count=2
            )

        self.assertEqual(completed.status, ReviewStatus.COMPLETED)
        self.assertEqual(completed.phase, ReviewPhase.POSTED)
        self.assertEqual(completed.findings_count, 2)

        next_run = review_run_application.start_postgres_review(
            self.runtime,
            self.request(request_key="github:issue-comment:1002"),
        )
        assert isinstance(next_run, postgres_review_runs.StartedRun)
        with self.runtime.transaction() as connection:
            failed = postgres_review_runs.fail_run(
                connection,
                next_run.run.id,
                failure_code="review_failed",
            )
        self.assertEqual(failed.status, ReviewStatus.FAILED)
        self.assertEqual(failed.phase, ReviewPhase.FAILED)
        self.assertEqual(failed.failure_code, "review_failed")

        with self.runtime.transaction() as connection:
            recorded = postgres_review_runs.record_failure_status_comment(
                connection,
                run_id=failed.id,
                comment_id=8801,
            )
            comments = postgres_review_runs.failure_status_comments_for_pull_request(
                connection,
                repository="team/reviewer",
                pr_number=14,
            )
        self.assertEqual(recorded.comment_id, 8801)
        self.assertEqual(len(comments), 1)
        self.assertEqual(comments[0].run_id, recorded.run_id)
        self.assertEqual(comments[0].comment_id, recorded.comment_id)

        with self.runtime.transaction() as connection:
            cleared = postgres_review_runs.clear_failure_status_comment(
                connection,
                run_id=failed.id,
            )
            comments = postgres_review_runs.failure_status_comments_for_pull_request(
                connection,
                repository="team/reviewer",
                pr_number=14,
            )
        self.assertIsNone(cleared.comment_id)
        self.assertEqual(comments, ())

    def test_render_validation_can_reopen_finding_collection_only(self) -> None:
        started = review_run_application.start_postgres_review(
            self.runtime,
            self.request(request_key="github:issue-comment:render-correction"),
        )
        assert isinstance(started, postgres_review_runs.StartedRun)
        run_id = started.run.id

        with self.runtime.transaction() as connection:
            for phase in (
                ReviewPhase.FETCHING_PR,
                ReviewPhase.COLLECTING_DIFF,
                ReviewPhase.REVIEWING,
            ):
                postgres_review_runs.advance_phase(connection, run_id, phase)
            unchanged = postgres_review_runs.reopen_finding_collection(
                connection, run_id
            )
            postgres_review_runs.advance_phase(
                connection, run_id, ReviewPhase.RENDERING
            )

        review_run_application.reopen_live_finding_collection(
            self.runtime,
            review_run_application.RunSubject(
                repository="team/reviewer",
                pr_number=14,
                run_id=int(run_id),
            ),
            expected_head_sha="a" * 40,
        )
        with self.runtime.transaction() as connection:
            reopened = postgres_review_runs.get_run(connection, run_id)

        self.assertEqual(unchanged.phase, ReviewPhase.REVIEWING)
        self.assertEqual(reopened.phase, ReviewPhase.REVIEWING)

        with self.runtime.transaction() as connection:
            postgres_review_runs.advance_phase(
                connection, run_id, ReviewPhase.RENDERING
            )
            postgres_review_runs.advance_phase(
                connection, run_id, ReviewPhase.PUBLISHING
            )
            with self.assertRaises(postgres_review_runs.InvalidReviewTransition):
                postgres_review_runs.reopen_finding_collection(connection, run_id)

    def test_new_exact_subject_supersedes_the_active_run(self) -> None:
        first = review_run_application.start_postgres_review(
            self.runtime, self.request()
        )
        second = review_run_application.start_postgres_review(
            self.runtime,
            self.request(
                request_key="github:issue-comment:1002",
                head_sha="c" * 40,
            ),
        )
        assert isinstance(first, postgres_review_runs.StartedRun)
        assert isinstance(second, postgres_review_runs.StartedRun)

        with self.runtime.transaction() as connection:
            superseded = postgres_review_runs.get_run(connection, first.run.id)
            active = postgres_review_runs.get_run(connection, second.run.id)

        self.assertEqual(superseded.status, ReviewStatus.SUPERSEDED)
        self.assertEqual(superseded.phase, ReviewPhase.SUPERSEDED)
        self.assertEqual(superseded.failure_code, "snapshot_superseded")
        self.assertEqual(active.status, ReviewStatus.RUNNING)

    def test_overlapping_older_transaction_can_supersede_a_newer_run(self) -> None:
        first_definition = resolve_review_subject(
            base_sha="b" * 40,
            head_sha="a" * 40,
            policy_revision="profile@1",
            resolved_config_schema_version=1,
            resolved_config={"profile": "sundsvall-standard"},
        )
        second_definition = resolve_review_subject(
            base_sha="b" * 40,
            head_sha="c" * 40,
            policy_revision="profile@1",
            resolved_config_schema_version=1,
            resolved_config={"profile": "sundsvall-standard"},
        )
        with self.runtime.transaction() as connection:
            repository = registry.ensure_repository(
                connection,
                registry.RepositoryDefinition(
                    provider="github",
                    provider_repository_id=910,
                    full_name="team/reviewer",
                ),
            )
            pull_request = registry.ensure_pull_request(connection, repository.id, 14)
            first_subject = registry.create_or_get_subject(
                connection, pull_request.id, first_definition
            )
            second_subject = registry.create_or_get_subject(
                connection, pull_request.id, second_definition
            )

        with self.runtime.transaction() as older_connection:
            # Pin this transaction's timestamp before the newer run commits.
            older_connection.execute("SELECT CURRENT_TIMESTAMP").fetchone()
            with self.runtime.transaction() as newer_connection:
                first = postgres_review_runs.start_run(
                    newer_connection,
                    pull_request_id=pull_request.id,
                    review_subject_id=first_subject.id,
                    request_key="github:issue-comment:1201",
                )
            second = postgres_review_runs.start_run(
                older_connection,
                pull_request_id=pull_request.id,
                review_subject_id=second_subject.id,
                request_key="github:issue-comment:1202",
            )

        assert isinstance(first, postgres_review_runs.StartedRun)
        assert isinstance(second, postgres_review_runs.StartedRun)
        with self.runtime.transaction() as connection:
            superseded = postgres_review_runs.get_run(connection, first.run.id)
        assert superseded.completed_at is not None
        self.assertGreaterEqual(superseded.completed_at, superseded.started_at)
        self.assertEqual(superseded.status, ReviewStatus.SUPERSEDED)

    def test_four_concurrent_starts_create_one_active_run(self) -> None:
        ready = Barrier(4, timeout=10)

        def start(index: int) -> postgres_review_runs.RunStart:
            ready.wait()
            return review_run_application.start_postgres_review(
                self.runtime,
                self.request(request_key=f"github:issue-comment:{1100 + index}"),
            )

        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(start, range(4)))

        self.assertEqual(
            sum(isinstance(result, postgres_review_runs.StartedRun) for result in results),
            1,
        )
        self.assertEqual(
            sum(
                isinstance(result, postgres_review_runs.DuplicateRun)
                and result.reason == "active_run"
                for result in results
            ),
            3,
        )
        with self.runtime.transaction() as connection:
            counts = connection.execute(
                "SELECT count(*), count(*) FILTER (WHERE status = 'running') "
                "FROM review_agent.review_runs"
            ).fetchone()
        self.assertEqual(counts, (1, 1))


if __name__ == "__main__":
    unittest.main()
