from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import psycopg


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = ROOT / "bootstrap" / "plugins"
sys.path.insert(0, str(PACKAGE_ROOT))

from review_agent_tools.postgres_migrations import runner  # noqa: E402
from review_agent_tools.postgres.runtime import (  # noqa: E402
    PostgreSQLNotReady,
    PostgreSQLRuntime,
    PostgreSQLRuntimeError,
    PostgreSQLUnavailable,
)
from review_agent_tools.settings import PostgresDatabaseUrl  # noqa: E402


DSN = os.environ.get("REVIEW_AGENT_POSTGRES_DSN", "")


class PostgreSQLRuntimeConstructionTests(unittest.TestCase):
    def test_construction_does_not_open_connections_and_pool_is_bounded(self) -> None:
        runtime = PostgreSQLRuntime(
            PostgresDatabaseUrl("postgresql://invalid@127.0.0.1:1/unreachable")
        )
        self.addCleanup(runtime.close)

        metrics = runtime.pool_metrics()

        self.assertFalse(metrics.open)
        self.assertEqual(metrics.minimum_size, 1)
        self.assertEqual(metrics.maximum_size, 4)
        self.assertEqual(metrics.waiting_requests, 0)

    def test_explicit_open_fails_closed_when_database_is_unavailable(self) -> None:
        password = "very-secret-password"
        runtime = PostgreSQLRuntime(
            PostgresDatabaseUrl(
                f"postgresql://invalid:{password}@127.0.0.1:1/unreachable"
            )
        )
        self.addCleanup(runtime.close)

        with self.assertLogs("psycopg.pool", level="WARNING") as logs:
            with self.assertRaises(PostgreSQLUnavailable) as caught:
                runtime.open(timeout=0.1)

        self.assertEqual(
            str(caught.exception), "PostgreSQL pool could not become ready"
        )
        self.assertNotIn(password, str(caught.exception))
        self.assertNotIn(password, "\n".join(logs.output))
        self.assertFalse(runtime.pool_metrics().open)
        with self.assertRaisesRegex(
            PostgreSQLRuntimeError, "cannot be reopened after close"
        ):
            runtime.open()


@unittest.skipUnless(DSN, "run through scripts/check_postgres_schema.sh")
class PostgreSQLRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        with psycopg.connect(DSN, autocommit=True) as connection:
            connection.execute("DROP SCHEMA IF EXISTS review_agent CASCADE")

    def test_explicit_open_proves_session_migrations_and_metrics(self) -> None:
        with psycopg.connect(DSN) as connection:
            runner.apply_migrations(connection)
        runtime = PostgreSQLRuntime(PostgresDatabaseUrl(DSN))
        self.addCleanup(runtime.close)

        readiness = runtime.open()

        self.assertEqual(readiness.server_version // 10_000, 17)
        self.assertEqual(readiness.applied_migration_version, 1)
        self.assertFalse(readiness.database_ahead)
        metrics = runtime.pool_metrics()
        self.assertTrue(metrics.open)
        self.assertGreaterEqual(metrics.size, 1)
        self.assertGreaterEqual(metrics.available, 1)
        with self.assertRaisesRegex(
            PostgreSQLRuntimeError, "PostgreSQL runtime is already open"
        ):
            runtime.open()

    def test_previous_image_accepts_a_database_with_a_newer_migration(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            current = Path(temp)
            shutil.copy2(
                runner.MIGRATION_DIRECTORY / "001_initial.sql",
                current / "001_initial.sql",
            )
            (current / "002_newer.sql").write_text(
                "CREATE TABLE review_agent.newer_runtime_probe "
                "(id integer PRIMARY KEY);\n",
                encoding="utf-8",
            )
            with psycopg.connect(DSN) as connection:
                runner.apply_migrations(connection, directory=current)
        runtime = PostgreSQLRuntime(PostgresDatabaseUrl(DSN))
        self.addCleanup(runtime.close)

        readiness = runtime.open()

        self.assertEqual(readiness.applied_migration_version, 2)
        self.assertTrue(readiness.database_ahead)

    def test_open_fails_closed_when_migrations_are_pending(self) -> None:
        runtime = PostgreSQLRuntime(PostgresDatabaseUrl(DSN))
        self.addCleanup(runtime.close)

        with self.assertRaisesRegex(
            PostgreSQLNotReady, "pending PostgreSQL migrations: 001"
        ):
            runtime.open()

        self.assertFalse(runtime.pool_metrics().open)
        with self.assertRaisesRegex(
            PostgreSQLRuntimeError, "cannot be reopened after close"
        ):
            runtime.open()

    def test_open_fails_closed_when_a_known_checksum_drifted(self) -> None:
        with psycopg.connect(DSN) as connection:
            runner.apply_migrations(connection)
            connection.execute(
                """
                UPDATE review_agent.schema_migrations
                SET checksum = %s
                WHERE version = 1
                """,
                ("0" * 64,),
            )
        runtime = PostgreSQLRuntime(PostgresDatabaseUrl(DSN))
        self.addCleanup(runtime.close)

        with self.assertRaisesRegex(
            PostgreSQLNotReady, "checksum mismatch for 001_initial.sql"
        ):
            runtime.open()

        self.assertFalse(runtime.pool_metrics().open)


if __name__ == "__main__":
    unittest.main()
