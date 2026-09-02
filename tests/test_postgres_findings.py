from __future__ import annotations

import os
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier

import psycopg


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = ROOT / "bootstrap" / "plugins"
sys.path.insert(0, str(PACKAGE_ROOT))

from review_agent_tools.domain.finding import (  # noqa: E402
    FindingDomainError,
    FindingInput,
    compute_fingerprint,
)
from review_agent_tools.domain.review import RepositoryId  # noqa: E402
from review_agent_tools import (  # noqa: E402
    review_finding_application,
    review_run_application,
)
from review_agent_tools.postgres import (  # noqa: E402
    findings as postgres_findings,
    review_runs as postgres_review_runs,
)
from review_agent_tools.postgres.runtime import PostgreSQLRuntime  # noqa: E402
from review_agent_tools.postgres_migrations import runner  # noqa: E402
from review_agent_tools.settings import PostgresDatabaseUrl  # noqa: E402


DSN = os.environ.get("REVIEW_AGENT_POSTGRES_DSN", "")


class PostgreSQLFindingDomainTests(unittest.TestCase):
    def test_fingerprint_is_independent_of_repository_name(self) -> None:
        before_rename = compute_fingerprint(
            rule_id="correctness.boolean-default",
            path="backend/changed.py",
            symbol="Handler",
            anchor="Feature   default",
        )
        after_rename = compute_fingerprint(
            rule_id="correctness.boolean-default",
            path="backend/changed.py",
            symbol=" handler ",
            anchor="feature default",
        )

        self.assertEqual(before_rename, after_rename)
        self.assertEqual(len(before_rename), 64)

    def test_application_admission_rejects_before_pool_checkout(self) -> None:
        runtime = PostgreSQLRuntime(
            PostgresDatabaseUrl("postgresql://invalid@127.0.0.1:1/unreachable")
        )
        self.addCleanup(runtime.close)
        finding = PostgreSQLFindingTests.finding(confidence=0.84)

        with self.assertRaisesRegex(FindingDomainError, "confidence"):
            review_finding_application.record_postgres_findings(
                runtime,
                run_id=postgres_review_runs.ReviewRunId(1),
                head_sha="a" * 40,
                findings=(finding,),
                changed_files=(
                    review_finding_application.ChangedFile(
                        path="backend/changed.py",
                        context_hash="c" * 40,
                        context_hash_source="blob",
                    ),
                ),
            )

        self.assertFalse(runtime.pool_metrics().open)

    def test_duplicate_identity_rejects_before_pool_checkout(self) -> None:
        runtime = PostgreSQLRuntime(
            PostgresDatabaseUrl("postgresql://invalid@127.0.0.1:1/unreachable")
        )
        self.addCleanup(runtime.close)
        finding = PostgreSQLFindingTests.finding()

        with self.assertRaisesRegex(FindingDomainError, "duplicate stable identities"):
            review_finding_application.record_postgres_findings(
                runtime,
                run_id=postgres_review_runs.ReviewRunId(1),
                head_sha="a" * 40,
                findings=(finding, finding),
                changed_files=(
                    review_finding_application.ChangedFile(
                        path="backend/changed.py",
                        context_hash="c" * 40,
                        context_hash_source="blob",
                    ),
                ),
            )

        self.assertFalse(runtime.pool_metrics().open)


@unittest.skipUnless(DSN, "run through scripts/check_postgres_schema.sh")
class PostgreSQLFindingTests(unittest.TestCase):
    def setUp(self) -> None:
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
        repository: str,
        head_sha: str,
        request_key: str,
        provider_repository_id: int = 930,
        pr_number: int = 31,
    ) -> review_run_application.PostgresRunRequest:
        return review_run_application.PostgresRunRequest(
            provider="github",
            provider_repository_id=provider_repository_id,
            repository=repository,
            pr_number=pr_number,
            base_sha="b" * 40,
            head_sha=head_sha,
            policy_revision="profile@1",
            resolved_config_schema_version=1,
            resolved_config={"profile": "default-standard"},
            request_key=request_key,
        )

    @staticmethod
    def finding(**overrides: object) -> FindingInput:
        finding = FindingInput(
            rule_id="correctness.boolean-default",
            category="correctness",
            path="backend/changed.py",
            line=7,
            symbol="handler",
            anchor="feature default",
            title="Boolean default remains disabled",
            severity="High",
            publication_score=9,
            confidence=0.9,
            evidence="Concrete evidence.",
            disproof_checks="Checked the guard.",
            impact="The feature remains unavailable.",
            smallest_fix="Restore the enabled default.",
            introduced_by_diff=True,
        )
        return replace(finding, **overrides)

    def start(
        self,
        *,
        repository: str,
        head_sha: str,
        request_key: str,
        provider_repository_id: int = 930,
        pr_number: int = 31,
    ) -> postgres_review_runs.ReviewRunId:
        result = review_run_application.start_postgres_review(
            self.runtime,
            self.request(
                repository=repository,
                head_sha=head_sha,
                request_key=request_key,
                provider_repository_id=provider_repository_id,
                pr_number=pr_number,
            ),
        )
        assert isinstance(result, postgres_review_runs.StartedRun)
        review_run_application.register_postgres_changed_files(
            self.runtime,
            run_id=result.run.id,
            files=(
                review_run_application.PostgresChangedFile(
                    path="backend/changed.py",
                    change_status="modified",
                ),
            ),
            changed_files_reported=1,
            registration_complete=True,
        )
        return result.run.id

    def record(
        self,
        run_id: postgres_review_runs.ReviewRunId,
        head_sha: str,
        *,
        findings: tuple[FindingInput, ...] | None = None,
    ) -> review_finding_application.PostgresFindingBatch:
        return review_finding_application.record_postgres_findings(
            self.runtime,
            run_id=run_id,
            head_sha=head_sha,
            findings=findings if findings is not None else (self.finding(),),
            changed_files=(
                review_finding_application.ChangedFile(
                    path="backend/changed.py",
                    context_hash="c" * 40,
                    context_hash_source="blob",
                ),
            ),
        )

    def test_repository_rename_preserves_identity_and_adds_an_occurrence(self) -> None:
        first_run = self.start(
            repository="team/before-rename",
            head_sha="a" * 40,
            request_key="github:issue-comment:3001",
        )
        first = self.record(
            first_run,
            "a" * 40,
            findings=(self.finding(symbol="Handler", anchor="Feature Default"),),
        )

        second_run = self.start(
            repository="team/after-rename",
            head_sha="d" * 40,
            request_key="github:issue-comment:3002",
        )
        prior = review_finding_application.load_postgres_repeat_history(
            self.runtime, run_id=second_run
        )
        self.assertEqual(len(prior), 1)
        self.assertEqual(prior[0].previous_head, "a" * 40)
        self.assertEqual(prior[0].local_reference, "F1")
        second = self.record(second_run, "d" * 40)

        self.assertEqual(first.items[0].fingerprint, second.items[0].fingerprint)
        self.assertEqual(first.items[0].finding_id, second.items[0].finding_id)
        with self.runtime.transaction() as connection:
            identity_count = connection.execute(
                "SELECT count(*) FROM review_agent.finding_identities"
            ).fetchone()
            occurrence_count = connection.execute(
                "SELECT count(*) FROM review_agent.finding_occurrences"
            ).fetchone()
        self.assertEqual(identity_count, (1,))
        self.assertEqual(occurrence_count, (2,))

        third_run = self.start(
            repository="team/after-rename",
            head_sha="e" * 40,
            request_key="github:issue-comment:3003",
        )
        latest = review_finding_application.load_postgres_repeat_history(
            self.runtime, run_id=third_run
        )
        self.assertEqual(len(latest), 1)
        self.assertEqual(latest[0].previous_run_id, second_run)
        self.assertEqual(latest[0].previous_head, "d" * 40)
        self.assertEqual(latest[0].prior_claim, "Concrete evidence.")

    def test_last_seen_does_not_regress_when_an_older_run_retries(self) -> None:
        older_run = self.start(
            repository="team/monotonic-history",
            head_sha="a" * 40,
            request_key="github:issue-comment:3051",
            pr_number=31,
        )
        older = self.record(older_run, "a" * 40)
        newer_run = self.start(
            repository="team/monotonic-history",
            head_sha="d" * 40,
            request_key="github:issue-comment:3052",
            pr_number=32,
        )
        self.record(newer_run, "d" * 40)

        with self.runtime.transaction() as connection:
            before_retry = connection.execute(
                "SELECT last_seen_at FROM review_agent.finding_identities "
                "WHERE id = %s",
                (older.items[0].finding_id,),
            ).fetchone()
        self.record(older_run, "a" * 40)
        with self.runtime.transaction() as connection:
            after_retry = connection.execute(
                "SELECT last_seen_at FROM review_agent.finding_identities "
                "WHERE id = %s",
                (older.items[0].finding_id,),
            ).fetchone()

        self.assertIsNotNone(before_retry)
        self.assertEqual(before_retry, after_retry)

    def test_scope_and_repository_lookup_failures_are_typed(self) -> None:
        run_id = self.start(
            repository="team/failure-contracts",
            head_sha="a" * 40,
            request_key="github:issue-comment:3071",
        )
        with self.assertRaises(postgres_findings.FindingConflict):
            self.record(run_id, "d" * 40)

        with self.assertRaises(postgres_findings.FindingPathNotChanged):
            review_finding_application.record_postgres_findings(
                self.runtime,
                run_id=run_id,
                head_sha="a" * 40,
                findings=(self.finding(path="backend/not-changed.py"),),
                changed_files=(
                    review_finding_application.ChangedFile(
                        path="backend/not-changed.py",
                        context_hash="c" * 40,
                        context_hash_source="blob",
                    ),
                ),
            )

        recorded = self.record(run_id, "a" * 40)
        with self.assertRaises(postgres_findings.FingerprintNotFound):
            review_finding_application.resolve_postgres_fingerprint(
                self.runtime,
                repository_id=recorded.repository_id,
                value="f" * 64,
            )

        with self.runtime.transaction() as connection:
            postgres_review_runs.fail_run(
                connection, run_id, failure_code="tests_terminal_run"
            )
        with self.assertRaises(postgres_findings.FindingRunNotActive):
            self.record(run_id, "a" * 40)

    def test_lock_contention_has_a_typed_busy_failure(self) -> None:
        run_id = self.start(
            repository="team/finding-lock",
            head_sha="a" * 40,
            request_key="github:issue-comment:3091",
        )
        with self.runtime.transaction() as connection:
            row = connection.execute(
                "SELECT pull_request_id FROM review_agent.review_runs WHERE id = %s",
                (run_id,),
            ).fetchone()
        assert row is not None

        with psycopg.connect(DSN) as blocker:
            blocker.execute(
                "SELECT id FROM review_agent.pull_requests WHERE id = %s FOR UPDATE",
                (row[0],),
            ).fetchone()
            with self.assertRaises(postgres_findings.FindingRunBusy):
                self.record(run_id, "a" * 40)

    def test_repository_scoped_prefix_resolution_ignores_other_repositories(self) -> None:
        first_run = self.start(
            repository="team/one",
            head_sha="a" * 40,
            request_key="github:issue-comment:3101",
        )
        first = self.record(first_run, "a" * 40)
        second_run = self.start(
            repository="team/two",
            head_sha="a" * 40,
            request_key="github:issue-comment:3102",
            provider_repository_id=931,
        )
        second = self.record(second_run, "a" * 40)
        self.assertNotEqual(first.repository_id, second.repository_id)

        with self.runtime.transaction() as connection:
            for repository_id, fingerprint in (
                (first.repository_id, "deadbeef" + "0" * 56),
                (second.repository_id, "deadbeef" + "1" * 56),
            ):
                connection.execute(
                    """
                    INSERT INTO review_agent.finding_identities (
                        repository_id, fingerprint, rule_id, path, anchor,
                        first_seen_at, last_seen_at
                    ) VALUES (
                        %s, %s, 'tests.prefix-scope', 'src/prefix.py', 'prefix',
                        statement_timestamp(), statement_timestamp()
                    )
                    """,
                    (repository_id, fingerprint),
                )

        self.assertEqual(
            review_finding_application.resolve_postgres_fingerprint(
                self.runtime,
                repository_id=first.repository_id,
                value="deadbeef",
            ),
            "deadbeef" + "0" * 56,
        )
        self.assertEqual(
            review_finding_application.resolve_postgres_fingerprint(
                self.runtime,
                repository_id=second.repository_id,
                value="deadbeef",
            ),
            "deadbeef" + "1" * 56,
        )

        with self.runtime.transaction() as connection:
            connection.execute(
                """
                INSERT INTO review_agent.finding_identities (
                    repository_id, fingerprint, rule_id, path, anchor,
                    first_seen_at, last_seen_at
                ) VALUES (
                    %s, %s, 'tests.prefix-ambiguity', 'src/other.py', 'other',
                    statement_timestamp(), statement_timestamp()
                )
                """,
                (first.repository_id, "deadbeef" + "2" * 56),
            )
        with self.assertRaises(postgres_findings.AmbiguousFingerprint):
            review_finding_application.resolve_postgres_fingerprint(
                self.runtime,
                repository_id=first.repository_id,
                value="deadbeef",
            )
        with self.assertRaises(FindingDomainError):
            review_finding_application.resolve_postgres_fingerprint(
                self.runtime,
                repository_id=RepositoryId(1),
                value="deadbee",
            )

    def test_two_hundred_findings_are_atomic_idempotent_and_deterministic(self) -> None:
        run_id = self.start(
            repository="team/batch",
            head_sha="a" * 40,
            request_key="github:issue-comment:3201",
        )
        findings = tuple(
            self.finding(
                rule_id=f"correctness.batch-{index:03d}",
                symbol=f"handler_{index}",
                anchor=f"feature default {index}",
                line=index + 1,
            )
            for index in reversed(range(200))
        )
        first = self.record(run_id, "a" * 40, findings=findings)
        retry = self.record(run_id, "a" * 40, findings=findings)

        self.assertEqual(first.items, retry.items)
        expected_references = {
            item.fingerprint: f"F{index}"
            for index, item in enumerate(
                sorted(first.items, key=lambda item: item.fingerprint), start=1
            )
        }
        self.assertEqual(
            {item.fingerprint: item.local_reference for item in first.items},
            expected_references,
        )

        conflicting = (replace(findings[0], title="Conflicting retry"), *findings[1:])
        with self.assertRaises(postgres_findings.FindingConflict):
            self.record(run_id, "a" * 40, findings=conflicting)
        with self.runtime.transaction() as connection:
            occurrence_count = connection.execute(
                "SELECT count(*) FROM review_agent.finding_occurrences"
            ).fetchone()
            conflicting_title_count = connection.execute(
                "SELECT count(*) FROM review_agent.finding_occurrences "
                "WHERE title = 'Conflicting retry'"
            ).fetchone()
        self.assertEqual(occurrence_count, (200,))
        self.assertEqual(conflicting_title_count, (0,))

    def test_concurrent_batches_allocate_unique_pull_request_references(self) -> None:
        run_id = self.start(
            repository="team/concurrent",
            head_sha="a" * 40,
            request_key="github:issue-comment:3301",
        )
        barrier = Barrier(4)

        def record_one(index: int) -> postgres_findings.RecordedFinding:
            barrier.wait(timeout=5)
            batch = self.record(
                run_id,
                "a" * 40,
                findings=(
                    self.finding(
                        rule_id=f"correctness.concurrent-{index}",
                        symbol=f"handler_{index}",
                        anchor=f"concurrent finding {index}",
                    ),
                ),
            )
            return batch.items[0]

        with ThreadPoolExecutor(max_workers=4) as executor:
            recorded = list(executor.map(record_one, range(4)))

        self.assertEqual(
            {item.local_reference for item in recorded},
            {"F1", "F2", "F3", "F4"},
        )
        self.assertEqual(len({item.finding_id for item in recorded}), 4)


if __name__ == "__main__":
    unittest.main()
