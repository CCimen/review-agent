from __future__ import annotations

import os
import json
import shutil
import tempfile
import sys
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier, Event
from types import SimpleNamespace
from unittest.mock import Mock, patch

import psycopg


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = ROOT / "bootstrap" / "plugins"
sys.path.insert(0, str(PACKAGE_ROOT))

from review_agent_tools.domain.review import (  # noqa: E402
    CoverageState,
    DiffPage,
    DiffState,
    FileDomain,
    FileSide,
    ReviewDomainError,
    ReviewMode,
    ReviewRunId,
    ReviewPhase,
    ReviewStatus,
    resolve_file_read,
    resolve_changed_file,
    resolve_diff_observation,
)
from review_agent_tools.postgres import coverage as postgres_coverage  # noqa: E402
from review_agent_tools.postgres import review_runs as postgres_review_runs  # noqa: E402
from review_agent_tools.postgres.runtime import PostgreSQLRuntime  # noqa: E402
from review_agent_tools.postgres_migrations import runner  # noqa: E402
from review_agent_tools import (  # noqa: E402
    review_run_application,
    review_source_tools,
    review_tool_runtime,
)
from review_agent_tools.diff_render import assemble_rendered_diff  # noqa: E402
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

    def test_diff_page_reads_one_snapshot_and_stops_when_the_head_changes(self) -> None:
        run_id = self.start_run()
        review_run_application.register_postgres_changed_files(
            self.runtime,
            run_id=run_id,
            files=(self.changed_file("src/a.py"),),
            changed_files_reported=1,
            registration_complete=True,
        )
        with self.runtime.transaction() as connection:
            postgres_review_runs.advance_phase(
                connection, run_id, ReviewPhase.FETCHING_PR
            )
        client = Mock()
        pull = {
            "base": {"sha": "b" * 40},
            "head": {"sha": "a" * 40},
            "changed_files": 1,
        }
        client.get_review_pull.return_value = SimpleNamespace(
            repository="team/coverage",
            pr_number=21,
            payload=pull,
        )
        diff = "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1 @@\n-old\n+new\n"
        client.get_review_diff.return_value = SimpleNamespace(
            state="ok",
            body=diff.encode(),
            truncated=False,
        )
        source = SimpleNamespace(
            run_id=int(run_id),
            client=client,
            lease=SimpleNamespace(job_id=7, lease_generation=1),
        )
        with (
            patch.object(
                review_source_tools, "gateway_source_session", return_value=source
            ),
            patch.object(
                review_source_tools, "postgres_runtime", return_value=self.runtime
            ),
            patch.object(
                review_tool_runtime, "postgres_runtime", return_value=self.runtime
            ),
        ):
            first = json.loads(
                review_source_tools.pr_diff.__wrapped__(
                    {"run_id": run_id, "path": "src/a.py"}
                )
            )
            self.assertEqual(first["diff"], diff)
            self.assertEqual(client.get_review_pull.call_count, 1)
            coverage = review_run_application.summarize_postgres_coverage(
                self.runtime, run_id
            )
            self.assertEqual(coverage.state, CoverageState.COMPLETE)

            pull["head"] = {"sha": "c" * 40}
            second = json.loads(
                review_source_tools.pr_diff.__wrapped__(
                    {"run_id": run_id, "path": "src/a.py"}
                )
            )
        self.assertEqual(second["status"], "superseded")
        self.assertEqual(client.get_review_pull.call_count, 2)
        self.assertEqual(client.get_review_diff.call_count, 1)
        with self.runtime.transaction() as connection:
            run = postgres_review_runs.get_run(connection, run_id)
        self.assertEqual(run.failure_code, "snapshot_superseded")

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

    def test_all_diff_pages_complete_coverage_after_restart(self) -> None:
        run_id = self.start_run()
        review_run_application.register_postgres_changed_files(
            self.runtime,
            run_id=run_id,
            files=(self.changed_file("src/a.py"),),
            changed_files_reported=1,
            registration_complete=True,
        )
        with self.runtime.transaction() as connection:
            postgres_review_runs.advance_phase(
                connection, run_id, ReviewPhase.FETCHING_PR
            )
            postgres_review_runs.advance_phase(
                connection, run_id, ReviewPhase.COLLECTING_DIFF
            )
        subject = review_run_application.RunSubject("team/coverage", 21, run_id)
        text = (
            "diff --git a/src/a.py b/src/a.py\n"
            "--- a/src/a.py\n+++ b/src/a.py\n@@ -0,0 +1 @@\n+" + "å" * 2500 + "\n"
        )
        returned: list[str] = []
        for start in range(0, len(text), 500):
            assembled = assemble_rendered_diff(
                text,
                only_path="src/a.py" if start else None,
                max_chars=500,
                start_char=start,
            )
            returned.append(assembled.text)
            exposure = review_run_application.DiffExposure(
                exposed_paths=tuple(assembled.exposed_paths),
                page=assembled.page,
            )
            for _ in range(2):
                review_run_application.record_live_diff_result(
                    self.runtime, subject, exposure
                )
            self.runtime.close()
            self.runtime = PostgreSQLRuntime(PostgresDatabaseUrl(DSN))
            self.addCleanup(self.runtime.close)
            self.runtime.open()
        self.assertEqual("".join(returned), text)
        summary = review_run_application.summarize_postgres_coverage(
            self.runtime, run_id
        )
        self.assertEqual(summary.state, CoverageState.COMPLETE)

    def test_diff_page_gaps_retries_and_content_changes_remain_incomplete(self) -> None:
        run_id = self.start_run()
        review_run_application.register_postgres_changed_files(
            self.runtime,
            run_id=run_id,
            files=(self.changed_file("src/a.py"),),
            changed_files_reported=1,
            registration_complete=True,
        )

        def record(start: int, end: int, digest: str = "a" * 64) -> CoverageState:
            with self.runtime.transaction() as connection:
                postgres_coverage.record_diff_page(
                    connection,
                    run_id=run_id,
                    page=DiffPage("src/a.py", digest, start, end, 100),
                )
                return postgres_coverage.summarize(connection, run_id).state

        # Last page alone, retries and overlapping prefixes leave the middle unread.
        for start, end in ((80, 100), (80, 100), (0, 30), (10, 40)):
            self.assertEqual(record(start, end), CoverageState.INCOMPLETE)
        # A different rendering must not fill a gap in the original content.
        self.assertEqual(record(40, 80, "b" * 64), CoverageState.INCOMPLETE)
        self.assertEqual(record(0, 40), CoverageState.INCOMPLETE)
        self.assertEqual(record(40, 80), CoverageState.INCOMPLETE)
        self.assertEqual(record(80, 100), CoverageState.COMPLETE)
        self.assertEqual(record(80, 100), CoverageState.COMPLETE)
        self.assertEqual(record(40, 80, "b" * 64), CoverageState.COMPLETE)

        with self.runtime.transaction() as connection:
            postgres_review_runs.mark_superseded(connection, run_id)
        with self.assertRaises(postgres_coverage.CoverageRunNotActive):
            record(0, 10)
        new_run = review_run_application.start_postgres_review(
            self.runtime,
            replace(
                self.request(),
                head_sha="c" * 40,
                request_key="github:issue-comment:2002",
            ),
        )
        self.assertIsInstance(new_run, postgres_review_runs.StartedRun)
        run_id = new_run.run.id
        review_run_application.register_postgres_changed_files(
            self.runtime,
            run_id=run_id,
            files=(self.changed_file("src/a.py"),),
            changed_files_reported=1,
            registration_complete=True,
        )
        self.assertEqual(record(40, 100), CoverageState.INCOMPLETE)

    def test_concurrent_diff_pages_do_not_lose_exposure(self) -> None:
        run_id = self.start_run()
        review_run_application.register_postgres_changed_files(
            self.runtime,
            run_id=run_id,
            files=(self.changed_file("src/a.py"),),
            changed_files_reported=1,
            registration_complete=True,
        )
        barrier = Barrier(2)

        def record(start: int) -> None:
            with self.runtime.transaction() as connection:
                barrier.wait(timeout=5)
                postgres_coverage.record_diff_page(
                    connection,
                    run_id=run_id,
                    page=DiffPage("src/a.py", "a" * 64, start, start + 50, 100),
                )

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(record, start) for start in (0, 50)]
            for future in futures:
                future.result(timeout=5)
        summary = review_run_application.summarize_postgres_coverage(
            self.runtime, run_id
        )
        self.assertEqual(summary.state, CoverageState.COMPLETE)

    def test_diff_page_migration_preserves_legacy_coverage_and_writes(self) -> None:
        self.runtime.close()
        with psycopg.connect(DSN, autocommit=True) as connection:
            connection.execute("DROP SCHEMA review_agent CASCADE")
        with tempfile.TemporaryDirectory() as temp:
            previous = Path(temp)
            for source in runner.MIGRATION_DIRECTORY.glob("*.sql"):
                if int(source.name[:3]) < 15:
                    shutil.copy2(source, previous / source.name)
            with psycopg.connect(DSN) as connection:
                runner.apply_migrations(connection, directory=previous)
                with connection.transaction():
                    admitted = (
                        review_run_application.admit_postgres_review_in_transaction(
                            connection,
                            self.request(),
                            priority=1,
                            max_attempts=3,
                            active_job_limit=10,
                        )
                    )
                    run_id = admitted.run.run.id
                    postgres_coverage.insert_changed_files(
                        connection,
                        run_id=run_id,
                        files=tuple(
                            resolve_changed_file(path=path, change_status="modified")
                            for path in ("complete.py", "partial.py")
                        ),
                        changed_files_reported=2,
                        registration_complete=True,
                    )
                    for path, state in (
                        ("complete.py", DiffState.COMPLETE),
                        ("partial.py", DiffState.TRUNCATED),
                    ):
                        postgres_coverage.record_diff_observation(
                            connection,
                            run_id=run_id,
                            observation=resolve_diff_observation(
                                paths=(path,), state=state
                            ),
                        )
                    before = postgres_coverage.summarize(connection, run_id)
            with psycopg.connect(DSN) as connection:
                self.assertEqual(
                    runner.apply_migrations(connection),
                (15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28),
                )
                self.assertTrue(
                    runner.inspect_migrations(
                        connection, directory=previous
                    ).database_ahead
                )
                with connection.transaction():
                    self.assertEqual(
                        postgres_coverage.summarize(connection, run_id), before
                    )
                    # Previous code can still write ordinary observations after upgrade.
                    postgres_coverage.record_diff_observation(
                        connection,
                        run_id=run_id,
                        observation=resolve_diff_observation(
                            paths=("partial.py",), state=DiffState.TRUNCATED
                        ),
                    )
                    for path in ("complete.py", "partial.py"):
                        postgres_coverage.record_diff_page(
                            connection,
                            run_id=run_id,
                            page=DiffPage(path, "a" * 64, 50, 100, 100),
                        )
                    self.assertEqual(
                        postgres_coverage.summarize(connection, run_id), before
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
