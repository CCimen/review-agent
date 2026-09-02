from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import psycopg


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = ROOT / "bootstrap" / "plugins"
sys.path.insert(0, str(PACKAGE_ROOT))

from review_agent_tools.postgres_migrations import runner  # noqa: E402


DSN = os.environ.get("REVIEW_AGENT_POSTGRES_DSN", "")
MIGRATIONS = (
    ROOT / "bootstrap" / "plugins" / "review_agent_tools" / "postgres_migrations"
)


@unittest.skipUnless(DSN, "run through scripts/check_postgres_schema.sh")
class PostgreSQLMigrationRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        with psycopg.connect(DSN, autocommit=True) as connection:
            connection.execute("DROP SCHEMA IF EXISTS review_agent CASCADE")

    def test_applies_once_and_records_the_exact_source_checksum(self) -> None:
        with psycopg.connect(DSN) as connection:
            self.assertEqual(
                runner.apply_migrations(connection),
                (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13),
            )
            self.assertEqual(runner.apply_migrations(connection), ())
            rows = connection.execute(
                """
                SELECT version, name, checksum, applied_at IS NOT NULL
                FROM review_agent.schema_migrations
                ORDER BY version
                """
            ).fetchall()
            tables = connection.execute(
                "SELECT to_regclass('review_agent.repositories')::text, "
                "to_regclass('review_agent.review_jobs')::text, "
                "to_regclass('review_agent.github_app_installations')::text, "
                "to_regclass('review_agent.github_app_installation_events')::text, "
                "to_regclass('review_agent.github_app_repository_access')::text, "
                "to_regclass('review_agent.github_app_repository_access_events')::text, "
                "to_regclass('review_agent.github_webhook_deliveries')::text, "
                "to_regclass('review_agent.review_decision_snapshots')::text, "
                "to_regclass('review_agent.intentional_design_evidence')::text, "
                "to_regclass('review_agent.review_quality_feedback_triage')::text, "
                "to_regclass('review_agent.coach_intervention_outcomes')::text, "
                "to_regclass('review_agent.review_guidance_snapshots')::text"
            ).fetchone()

        self.assertEqual(
            rows,
            [
                (
                    version,
                    name,
                    hashlib.sha256((MIGRATIONS / name).read_bytes()).hexdigest(),
                    True,
                )
                for version, name in (
                    (1, "001_initial.sql"),
                    (2, "002_review_jobs.sql"),
                    (3, "003_review_job_lifecycle.sql"),
                    (4, "004_publication_delivery_queue.sql"),
                    (5, "005_failure_status_delivery.sql"),
                    (6, "006_github_app_installations.sql"),
                    (7, "007_github_webhook_deliveries.sql"),
                    (8, "008_feedback_authorization_audit.sql"),
                    (9, "009_repository_decision_context.sql"),
                    (10, "010_intentional_design_evidence.sql"),
                    (11, "011_review_quality_feedback_triage.sql"),
                    (12, "012_coach_intervention_outcomes.sql"),
                    (13, "013_repository_guidance_context.sql"),
                )
            ],
        )
        self.assertEqual(
            tables,
            (
                "review_agent.repositories",
                "review_agent.review_jobs",
                "review_agent.github_app_installations",
                "review_agent.github_app_installation_events",
                "review_agent.github_app_repository_access",
                "review_agent.github_app_repository_access_events",
                "review_agent.github_webhook_deliveries",
                "review_agent.review_decision_snapshots",
                "review_agent.intentional_design_evidence",
                "review_agent.review_quality_feedback_triage",
                "review_agent.coach_intervention_outcomes",
                "review_agent.review_guidance_snapshots",
            ),
        )

    def test_rejects_an_applied_migration_whose_source_changed(self) -> None:
        with psycopg.connect(DSN) as connection:
            runner.apply_migrations(connection)

        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            target = directory / "001_initial.sql"
            shutil.copy2(MIGRATIONS / "001_initial.sql", target)
            target.write_text(
                target.read_text(encoding="utf-8") + "\n-- drift\n",
                encoding="utf-8",
            )
            with psycopg.connect(DSN) as connection:
                with self.assertRaisesRegex(
                    runner.MigrationError, "checksum mismatch for 001_initial.sql"
                ):
                    runner.apply_migrations(connection, directory=directory)

        with psycopg.connect(DSN) as connection:
            count = connection.execute(
                "SELECT count(*) FROM review_agent.schema_migrations"
            ).fetchone()
        self.assertEqual(count, (13,))

    def test_previous_image_accepts_a_database_with_newer_migrations(self) -> None:
        with (
            tempfile.TemporaryDirectory() as current_temp,
            tempfile.TemporaryDirectory() as previous_temp,
        ):
            current = Path(current_temp)
            previous = Path(previous_temp)
            shutil.copy2(MIGRATIONS / "001_initial.sql", current / "001_initial.sql")
            shutil.copy2(MIGRATIONS / "001_initial.sql", previous / "001_initial.sql")
            (current / "002_newer.sql").write_text(
                "CREATE TABLE review_agent.newer_probe (id integer PRIMARY KEY);\n",
                encoding="utf-8",
            )

            with psycopg.connect(DSN) as connection:
                self.assertEqual(
                    runner.apply_migrations(connection, directory=current), (1, 2)
                )
            with psycopg.connect(DSN) as connection:
                self.assertEqual(
                    runner.apply_migrations(connection, directory=previous), ()
                )

    def test_rejects_a_connection_with_an_existing_transaction(self) -> None:
        with psycopg.connect(DSN) as connection:
            connection.execute("SELECT 1")
            with self.assertRaisesRegex(
                runner.MigrationError, "requires an idle PostgreSQL connection"
            ):
                runner.apply_migrations(connection)
            connection.rollback()

    def test_inspection_reports_a_concurrent_migration_holder(self) -> None:
        with psycopg.connect(DSN) as lock_holder:
            with lock_holder.transaction():
                lock_holder.execute(
                    "SELECT pg_advisory_xact_lock(%s)",
                    (runner._MIGRATION_LOCK_KEY,),
                )
                with psycopg.connect(DSN) as connection:
                    with self.assertRaisesRegex(
                        runner.MigrationError,
                        "PostgreSQL migrations are currently running",
                    ):
                        runner.inspect_migrations(connection)

    def test_failed_migration_rolls_back_ddl_and_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            (directory / "001_base.sql").write_text(
                "CREATE TABLE review_agent.rollback_probe (id integer PRIMARY KEY);\n",
                encoding="utf-8",
            )
            (directory / "002_broken.sql").write_text(
                "CREATE TABLE review_agent.never_committed (id integer);\n"
                "SELECT review_agent.missing_function();\n",
                encoding="utf-8",
            )
            with psycopg.connect(DSN) as connection:
                with self.assertRaisesRegex(
                    runner.MigrationError, "failed to apply 002_broken.sql"
                ):
                    runner.apply_migrations(connection, directory=directory)

        with psycopg.connect(DSN) as connection:
            namespace = connection.execute(
                "SELECT to_regnamespace('review_agent')::text"
            ).fetchone()
        self.assertEqual(namespace, (None,))

    def test_concurrent_runners_serialize_and_apply_each_version_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            (directory / "001_concurrent.sql").write_text(
                "SELECT pg_sleep(0.5);\n"
                "CREATE TABLE review_agent.concurrent_probe (id integer PRIMARY KEY);\n",
                encoding="utf-8",
            )

            ready = Barrier(2, timeout=10)

            def apply_once(_: int) -> tuple[int, ...]:
                with psycopg.connect(DSN) as connection:
                    connection.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
                    ready.wait()
                    return runner.apply_migrations(connection, directory=directory)

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(apply_once, range(2)))

        self.assertCountEqual(results, [(1,), ()])
        with psycopg.connect(DSN) as connection:
            ledger = connection.execute(
                "SELECT version, count(*) FROM review_agent.schema_migrations GROUP BY version"
            ).fetchall()
        self.assertEqual(ledger, [(1, 1)])
