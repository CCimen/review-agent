from __future__ import annotations

import os
import sys
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event

import psycopg


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = ROOT / "bootstrap" / "plugins"
sys.path.insert(0, str(PACKAGE_ROOT))

from review_agent_tools.domain.review import (  # noqa: E402
    CoverageState,
    DiffState,
    FileDomain,
    FileSide,
    ReviewDomainError,
    ReviewMode,
    ReviewRunId,
    ReviewStatus,
    resolve_file_read,
)
from review_agent_tools.postgres import coverage as postgres_coverage  # noqa: E402
from review_agent_tools.postgres import review_runs as postgres_review_runs  # noqa: E402
from review_agent_tools.postgres.runtime import PostgreSQLRuntime  # noqa: E402
from review_agent_tools.postgres_migrations import runner  # noqa: E402
from review_agent_tools import review_run_application  # noqa: E402
from review_agent_tools.settings import PostgresDatabaseUrl  # noqa: E402


DSN = os.environ.get("REVIEW_AGENT_POSTGRES_DSN", "")


class PostgreSQLCoverageInputTests(unittest.TestCase):
    def test_invalid_inventory_is_rejected_before_pool_checkout(self) -> None:
        runtime = PostgreSQLRuntime(
            PostgresDatabaseUrl("postgresql://invalid@127.0.0.1:1/unreachable")
        )
        self.addCleanup(runtime.close)

        with self.assertRaisesRegex(ReviewDomainError, "path"):
            review_run_application.register_postgres_changed_files(
                runtime,
                run_id=ReviewRunId(1),
                files=(
                    review_run_application.PostgresChangedFile(
                        path="../outside.py",
                        change_status="modified",
                    ),
                ),
                changed_files_reported=1,
                registration_complete=True,
            )

        self.assertFalse(runtime.pool_metrics().open)


@unittest.skipUnless(DSN, "run through scripts/check_postgres_schema.sh")
class PostgreSQLCoverageTests(unittest.TestCase):
    def setUp(self) -> None:
        with psycopg.connect(DSN, autocommit=True) as connection:
            connection.execute("DROP SCHEMA IF EXISTS review_agent CASCADE")
        with psycopg.connect(DSN) as connection:
            runner.apply_migrations(connection)
        self.runtime = PostgreSQLRuntime(PostgresDatabaseUrl(DSN))
        self.runtime.open()
        self.addCleanup(self.runtime.close)

    @staticmethod
    def request() -> review_run_application.PostgresRunRequest:
        return review_run_application.PostgresRunRequest(
            provider="github",
            provider_repository_id=920,
            repository="team/coverage",
            pr_number=21,
            base_sha="b" * 40,
            head_sha="a" * 40,
            policy_revision="profile@1",
            resolved_config_schema_version=1,
            resolved_config={"profile": "default-standard"},
            request_key="github:issue-comment:2001",
            trigger_comment_id=2001,
            trigger_user="reviewer",
        )

    def start_run(self) -> postgres_review_runs.ReviewRunId:
        result = review_run_application.start_postgres_review(
            self.runtime, self.request()
        )
        assert isinstance(result, postgres_review_runs.StartedRun)
        return result.run.id

    @staticmethod
    def changed_file(
        path: str,
        *,
        change_status: str = "modified",
    ) -> review_run_application.PostgresChangedFile:
        return review_run_application.PostgresChangedFile(
            path=path,
            change_status=change_status,
            domain=FileDomain.BACKEND,
            review_mode=ReviewMode.NORMAL,
        )

    def test_inventory_registration_is_atomic_incremental_and_honest(self) -> None:
        run_id = self.start_run()
        first = review_run_application.register_postgres_changed_files(
            self.runtime,
            run_id=run_id,
            files=(self.changed_file("src/a.py"),),
            changed_files_reported=2,
            registration_complete=False,
        )
        self.assertEqual(first.changed_files_registered, 1)
        self.assertFalse(first.registration_complete)

        with self.assertRaises(postgres_coverage.CoverageConflict):
            review_run_application.register_postgres_changed_files(
                self.runtime,
                run_id=run_id,
                files=(
                    self.changed_file("src/b.py"),
                    self.changed_file("src/c.py"),
                ),
                changed_files_reported=2,
                registration_complete=False,
            )
        with self.assertRaises(postgres_coverage.CoverageConflict):
            review_run_application.register_postgres_changed_files(
                self.runtime,
                run_id=run_id,
                files=(
                    self.changed_file("src/b.py"),
                    self.changed_file("src/a.py", change_status="removed"),
                ),
                changed_files_reported=2,
                registration_complete=False,
            )
        after_conflict = review_run_application.summarize_postgres_coverage(
            self.runtime, run_id
        )
        self.assertEqual(after_conflict.changed_files_registered, 1)
        self.assertFalse(after_conflict.registration_complete)

        complete = review_run_application.register_postgres_changed_files(
            self.runtime,
            run_id=run_id,
            files=(self.changed_file("src/b.py"),),
            changed_files_reported=2,
            registration_complete=True,
        )
        self.assertEqual(complete.changed_files_reported, 2)
        self.assertEqual(complete.changed_files_registered, 2)
        self.assertTrue(complete.registration_complete)

    def test_live_inventory_persists_classifications_and_returns_summary(self) -> None:
        run_id = self.start_run()

        summary = review_run_application.register_live_changed_files(
            self.runtime,
            review_run_application.RunSubject(
                repository="team/coverage",
                pr_number=21,
                run_id=run_id,
            ),
            files=(
                {"path": "backend/app.py", "status": "modified"},
                {"path": ".github/workflows/ci.yml", "status": "added"},
            ),
            changed_files_reported=2,
        )

        self.assertEqual(summary.changed_files_registered, 2)
        self.assertTrue(summary.registration_complete)
        self.assertEqual(
            summary.by_domain,
            (("backend", 1), ("infrastructure", 1)),
        )
        self.assertEqual(
            summary.by_review_mode,
            (("configuration", 1), ("normal", 1)),
        )

    def test_empty_inventory_and_fail_closed_guards_are_explicit(self) -> None:
        run_id = self.start_run()
        unknown = review_run_application.summarize_postgres_coverage(
            self.runtime, run_id
        )
        self.assertEqual(unknown.state, CoverageState.UNKNOWN)

        with self.assertRaises(postgres_coverage.CoverageConflict):
            review_run_application.register_postgres_changed_files(
                self.runtime,
                run_id=run_id,
                files=(self.changed_file("src/a.py"),),
                changed_files_reported=2,
                registration_complete=True,
            )
        rolled_back = review_run_application.summarize_postgres_coverage(
            self.runtime, run_id
        )
        self.assertEqual(rolled_back.state, CoverageState.UNKNOWN)
        self.assertEqual(rolled_back.changed_files_registered, 0)

        empty = review_run_application.register_postgres_changed_files(
            self.runtime,
            run_id=run_id,
            files=(),
            changed_files_reported=0,
            registration_complete=True,
        )
        self.assertTrue(empty.registration_complete)
        complete = review_run_application.summarize_postgres_coverage(
            self.runtime, run_id
        )
        self.assertEqual(complete.state, CoverageState.COMPLETE)

    def test_source_reads_never_claim_complete_diff_coverage(self) -> None:
        run_id = self.start_run()
        review_run_application.register_postgres_changed_files(
            self.runtime,
            run_id=run_id,
            files=(self.changed_file("src/a.py"),),
            changed_files_reported=1,
            registration_complete=True,
        )
        review_run_application.record_postgres_file_reads(
            self.runtime,
            run_id=run_id,
            reads=(
                review_run_application.PostgresFileRead(
                    path="src/a.py", side=FileSide.HEAD, start_line=3, end_line=8
                ),
                review_run_application.PostgresFileRead(
                    path="docs/context.md", side=FileSide.BASE, start_line=1, end_line=2
                ),
            ),
        )

        source_only = review_run_application.summarize_postgres_coverage(
            self.runtime, run_id
        )
        self.assertEqual(source_only.state, CoverageState.INCOMPLETE)
        self.assertEqual(source_only.changed_paths_with_complete_diff, 0)
        self.assertEqual(source_only.changed_paths_with_source_reads, 1)
        self.assertEqual(source_only.supporting_context_paths_read, 1)

        review_run_application.record_postgres_diff_observation(
            self.runtime,
            run_id=run_id,
            paths=("src/a.py",),
            state=DiffState.TRUNCATED,
        )
        truncated = review_run_application.summarize_postgres_coverage(
            self.runtime, run_id
        )
        self.assertEqual(truncated.state, CoverageState.INCOMPLETE)
        self.assertEqual(truncated.truncated_paths, 1)

        review_run_application.record_postgres_diff_observation(
            self.runtime,
            run_id=run_id,
            paths=("src/a.py",),
            state=DiffState.COMPLETE,
        )
        complete = review_run_application.summarize_postgres_coverage(
            self.runtime, run_id
        )
        self.assertEqual(complete.state, CoverageState.COMPLETE)
        self.assertEqual(complete.changed_paths_with_complete_diff, 1)
        self.assertEqual(complete.context_ranges_read, 2)

        with self.assertRaises(postgres_coverage.InvalidCoverageTransition):
            review_run_application.record_postgres_diff_observation(
                self.runtime,
                run_id=run_id,
                paths=("src/a.py",),
                state=DiffState.TRUNCATED,
            )
        with self.assertRaises(postgres_coverage.CoverageFileNotFound):
            review_run_application.record_postgres_diff_observation(
                self.runtime,
                run_id=run_id,
                paths=("docs/context.md",),
                state=DiffState.COMPLETE,
            )

        with self.runtime.transaction() as connection:
            postgres_review_runs.mark_superseded(connection, run_id)
        with self.assertRaises(postgres_coverage.CoverageRunNotActive):
            review_run_application.record_postgres_file_reads(
                self.runtime,
                run_id=run_id,
                reads=(
                    review_run_application.PostgresFileRead(
                        path="src/a.py",
                        side=FileSide.HEAD,
                        start_line=20,
                        end_line=21,
                    ),
                ),
            )

    def test_coverage_write_lock_orders_before_supersession(self) -> None:
        run_id = self.start_run()
        review_run_application.register_postgres_changed_files(
            self.runtime,
            run_id=run_id,
            files=(self.changed_file("src/a.py"),),
            changed_files_reported=1,
            registration_complete=True,
        )
        locked = Event()
        release = Event()
        supersede_started = Event()
        read = resolve_file_read(
            path="src/a.py",
            side=FileSide.HEAD,
            start_line=30,
            end_line=31,
        )

        def hold_coverage_lock() -> postgres_coverage.FileReadBatch:
            with self.runtime.transaction() as connection:
                batch = postgres_coverage.insert_file_reads(
                    connection, run_id=run_id, reads=(read,)
                )
                locked.set()
                if not release.wait(timeout=5):
                    raise AssertionError("coverage lock was not released")
                return batch

        def supersede() -> postgres_review_runs.ReviewRun:
            with self.runtime.transaction() as connection:
                supersede_started.set()
                return postgres_review_runs.mark_superseded(connection, run_id)

        with ThreadPoolExecutor(max_workers=2) as executor:
            coverage_future = executor.submit(hold_coverage_lock)
            self.assertTrue(locked.wait(timeout=5))
            supersede_future = executor.submit(supersede)
            self.assertTrue(supersede_started.wait(timeout=5))
            try:
                time.sleep(0.1)
                self.assertFalse(supersede_future.done())
            finally:
                release.set()
            batch = coverage_future.result(timeout=5)
            superseded = supersede_future.result(timeout=5)

        self.assertEqual(batch.inserted, 1)
        self.assertEqual(superseded.status, ReviewStatus.SUPERSEDED)

    def test_concurrent_duplicate_ranges_dedupe_without_lost_coverage(self) -> None:
        run_id = self.start_run()
        review_run_application.register_postgres_changed_files(
            self.runtime,
            run_id=run_id,
            files=(self.changed_file("src/a.py"),),
            changed_files_reported=1,
            registration_complete=True,
        )
        ready = Barrier(4, timeout=10)

        def record(_: int) -> postgres_coverage.FileReadBatch:
            ready.wait()
            return review_run_application.record_postgres_file_reads(
                self.runtime,
                run_id=run_id,
                reads=(
                    review_run_application.PostgresFileRead(
                        path="src/a.py",
                        side=FileSide.HEAD,
                        start_line=10,
                        end_line=15,
                    ),
                ),
            )

        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(record, range(4)))

        self.assertEqual(sum(result.inserted for result in results), 1)
        summary = review_run_application.summarize_postgres_coverage(
            self.runtime, run_id
        )
        self.assertEqual(summary.context_ranges_read, 1)
        self.assertEqual(summary.changed_paths_with_source_reads, 1)


if __name__ == "__main__":
    unittest.main()
