from __future__ import annotations

import os
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Barrier
from unittest.mock import Mock

import psycopg


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = ROOT / "bootstrap" / "plugins"
sys.path.insert(0, str(PACKAGE_ROOT))

from review_agent_tools.postgres import retention  # noqa: E402
from review_agent_tools.postgres_migrations import runner  # noqa: E402


DSN = os.environ.get("REVIEW_AGENT_POSTGRES_DSN", "")


@unittest.skipUnless(DSN, "run through scripts/check_postgres_schema.sh")
class PostgreSQLRetentionTests(unittest.TestCase):
    def setUp(self) -> None:
        with psycopg.connect(DSN, autocommit=True) as connection:
            connection.execute("DROP SCHEMA IF EXISTS review_agent CASCADE")
        with psycopg.connect(DSN) as connection:
            runner.apply_migrations(connection)

    @staticmethod
    def _seed(connection: psycopg.Connection[tuple[object, ...]]) -> None:
        connection.execute(
            """
            INSERT INTO review_agent.github_webhook_deliveries (
                delivery_guid, event_name, payload_sha256, command_category,
                normalized_schema_version, normalized_payload, status,
                attempt_count, max_attempts, available_at, lease_generation,
                completed_by, received_at, processed_at
            ) VALUES
                (
                    '00000000-0000-0000-0000-000000000001', 'issue_comment',
                    repeat('a', 64), 'review', 1, NULL, 'accepted', 1, 3,
                    '2026-01-01T00:00:00Z', 1, 'processor:test',
                    '2026-01-01T00:00:00Z', '2026-01-01T00:01:00Z'
                ),
                (
                    '00000000-0000-0000-0000-000000000002', 'issue_comment',
                    repeat('b', 64), 'review', 1, NULL, 'accepted', 1, 3,
                    '2026-02-01T00:00:00Z', 1, 'processor:test',
                    '2026-02-01T00:00:00Z', '2026-02-01T00:01:00Z'
                ),
                (
                    '00000000-0000-0000-0000-000000000003', 'issue_comment',
                    repeat('c', 64), 'review', 1, NULL, 'accepted', 1, 3,
                    '2026-08-01T00:00:00Z', 1, 'processor:test',
                    '2026-08-01T00:00:00Z', '2026-08-01T00:01:00Z'
                ),
                (
                    '00000000-0000-0000-0000-000000000004', 'issue_comment',
                    repeat('d', 64), 'review', 1, '{}'::jsonb, 'received', 0, 3,
                    '2026-01-01T00:00:00Z', 0, NULL,
                    '2026-01-01T00:00:00Z', NULL
                )
            """
        )

    def test_dry_run_is_bounded_and_does_not_delete(self) -> None:
        cutoff = datetime(2026, 3, 1, tzinfo=timezone.utc)
        with psycopg.connect(DSN) as connection:
            with connection.transaction():
                self._seed(connection)
                result = retention.prune_terminal_webhook_deliveries(
                    connection,
                    before=cutoff,
                    limit=1,
                    apply=False,
                )
                remaining = connection.execute(
                    "SELECT count(*) FROM review_agent.github_webhook_deliveries"
                ).fetchone()

        self.assertEqual(result.matched, 1)
        self.assertEqual(result.deleted, 0)
        self.assertTrue(result.more)
        self.assertEqual(
            result.oldest_processed_at,
            datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(remaining, (4,))

    def test_apply_deletes_only_the_oldest_terminal_batch(self) -> None:
        cutoff = datetime(2026, 3, 1, tzinfo=timezone.utc)
        with psycopg.connect(DSN) as connection:
            with connection.transaction():
                self._seed(connection)
                result = retention.prune_terminal_webhook_deliveries(
                    connection,
                    before=cutoff,
                    limit=1,
                    apply=True,
                )
                rows = connection.execute(
                    """
                    SELECT delivery_guid::text, status
                    FROM review_agent.github_webhook_deliveries
                    ORDER BY delivery_guid
                    """
                ).fetchall()

        self.assertEqual(result.matched, 1)
        self.assertEqual(result.deleted, 1)
        self.assertTrue(result.more)
        self.assertEqual(
            rows,
            [
                ("00000000-0000-0000-0000-000000000002", "accepted"),
                ("00000000-0000-0000-0000-000000000003", "accepted"),
                ("00000000-0000-0000-0000-000000000004", "received"),
            ],
        )

    def test_concurrent_pruners_serialize_without_hiding_remaining_work(self) -> None:
        cutoff = datetime(2026, 9, 1, tzinfo=timezone.utc)
        with psycopg.connect(DSN) as connection:
            with connection.transaction():
                self._seed(connection)

        barrier = Barrier(2)

        def prune_one() -> retention.RetentionResult:
            with psycopg.connect(DSN) as connection:
                barrier.wait()
                with connection.transaction():
                    return retention.prune_terminal_webhook_deliveries(
                        connection,
                        before=cutoff,
                        limit=1,
                        apply=True,
                    )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = tuple(executor.map(lambda _: prune_one(), range(2)))

        self.assertEqual([result.deleted for result in results], [1, 1])
        self.assertTrue(all(result.more for result in results))
        with psycopg.connect(DSN) as connection:
            remaining = connection.execute(
                """
                SELECT count(*)
                FROM review_agent.github_webhook_deliveries
                WHERE status IN ('accepted', 'ignored', 'rejected', 'failed')
                  AND processed_at < %s
                """,
                (cutoff,),
            ).fetchone()
        self.assertEqual(remaining, (1,))

    def test_invalid_cutoff_and_limit_are_rejected_before_querying(self) -> None:
        connection = Mock()
        with self.assertRaises(retention.RetentionError):
            retention.prune_terminal_webhook_deliveries(
                connection,
                before=datetime(2026, 3, 1),
                limit=1,
                apply=False,
            )
        with self.assertRaises(retention.RetentionError):
            retention.prune_terminal_webhook_deliveries(
                connection,
                before=datetime(2026, 3, 1, tzinfo=timezone.utc),
                limit=0,
                apply=False,
            )
        connection.execute.assert_not_called()
