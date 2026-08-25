from __future__ import annotations

import os
import sys
import unittest
from datetime import timedelta
from pathlib import Path

import psycopg


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = ROOT / "bootstrap" / "plugins"
sys.path.insert(0, str(PACKAGE_ROOT))

from review_agent_tools.postgres import webhook_deliveries  # noqa: E402
from review_agent_tools.postgres_migrations import runner  # noqa: E402


DSN = os.environ.get("REVIEW_AGENT_POSTGRES_DSN", "")


@unittest.skipUnless(DSN, "run through scripts/check_postgres_schema.sh")
class PostgreSQLWebhookDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        with psycopg.connect(DSN, autocommit=True) as connection:
            connection.execute("DROP SCHEMA IF EXISTS review_agent CASCADE")
        with psycopg.connect(DSN) as connection:
            runner.apply_migrations(connection)

    def definition(
        self,
        *,
        delivery_guid: str = "203d1e32-5d99-4a3a-8a70-2f72f175c394",
        payload_sha256: str = "a" * 64,
    ) -> webhook_deliveries.DeliveryDefinition:
        return webhook_deliveries.DeliveryDefinition(
            delivery_guid=delivery_guid,
            event="issue_comment",
            action="created",
            payload_sha256=payload_sha256,
            provider_installation_id=7001,
            provider_repository_id=9001,
            repository_full_name="CCimen/review-agent",
            command_category=webhook_deliveries.CommandCategory.REVIEW,
            normalized_schema_version=1,
            normalized_payload={
                "pull_request_number": 42,
                "comment_id": 123456,
                "sender_id": 8001,
                "sender_login": "ccimen",
                "author_association": "OWNER",
                "command": "review",
            },
        )

    def test_redelivery_is_idempotent_and_conflicting_guid_is_rejected(self) -> None:
        with psycopg.connect(DSN) as connection:
            with connection.transaction():
                registered = webhook_deliveries.register_delivery(
                    connection, definition=self.definition(), max_attempts=3
                )
                repeated = webhook_deliveries.register_delivery(
                    connection, definition=self.definition(), max_attempts=9
                )
                with self.assertRaises(webhook_deliveries.DeliveryConflict):
                    webhook_deliveries.register_delivery(
                        connection,
                        definition=self.definition(payload_sha256="b" * 64),
                        max_attempts=3,
                    )
                stored = webhook_deliveries.get_delivery(
                    connection, registered.delivery.id
                )

        self.assertIsInstance(registered, webhook_deliveries.RegisteredDelivery)
        self.assertIsInstance(repeated, webhook_deliveries.DuplicateDelivery)
        self.assertEqual(repeated.delivery, registered.delivery)
        self.assertEqual(stored, registered.delivery)
        self.assertEqual(stored.max_attempts, 3)
        self.assertEqual(stored.status, webhook_deliveries.DeliveryStatus.RECEIVED)
        self.assertIsNotNone(stored.normalized_payload)

    def test_exact_live_lease_is_required_to_finish_and_payload_is_cleared(
        self,
    ) -> None:
        with psycopg.connect(DSN) as connection:
            with connection.transaction():
                webhook_deliveries.register_delivery(
                    connection, definition=self.definition(), max_attempts=3
                )
                claimed = webhook_deliveries.claim_next_delivery(
                    connection,
                    lease_owner="webhook-worker-1",
                    lease_duration=timedelta(minutes=1),
                )
                self.assertIsNotNone(claimed)
                assert claimed is not None
                heartbeat = webhook_deliveries.heartbeat_delivery(
                    connection,
                    delivery_id=claimed.id,
                    lease_owner="webhook-worker-1",
                    lease_generation=claimed.lease_generation,
                    lease_duration=timedelta(minutes=2),
                )
                with self.assertRaises(webhook_deliveries.DeliveryLeaseLost):
                    webhook_deliveries.finish_delivery(
                        connection,
                        delivery_id=claimed.id,
                        lease_owner="another-worker",
                        lease_generation=claimed.lease_generation,
                        status=webhook_deliveries.TerminalStatus.ACCEPTED,
                        actor="processor:another-worker",
                    )
                finished = webhook_deliveries.finish_delivery(
                    connection,
                    delivery_id=claimed.id,
                    lease_owner="webhook-worker-1",
                    lease_generation=claimed.lease_generation,
                    status=webhook_deliveries.TerminalStatus.ACCEPTED,
                    actor="processor:webhook-worker-1",
                )
                with self.assertRaises(webhook_deliveries.DeliveryLeaseLost):
                    webhook_deliveries.heartbeat_delivery(
                        connection,
                        delivery_id=claimed.id,
                        lease_owner="webhook-worker-1",
                        lease_generation=claimed.lease_generation,
                        lease_duration=timedelta(minutes=1),
                    )

        self.assertIsNotNone(heartbeat.lease_expires_at)
        self.assertIsNotNone(claimed.lease_expires_at)
        assert heartbeat.lease_expires_at is not None
        assert claimed.lease_expires_at is not None
        self.assertGreater(heartbeat.lease_expires_at, claimed.lease_expires_at)
        self.assertEqual(finished.status, webhook_deliveries.DeliveryStatus.ACCEPTED)
        self.assertIsNone(finished.normalized_payload)
        self.assertEqual(finished.completed_by, "processor:webhook-worker-1")
        self.assertIsNotNone(finished.processed_at)

    def test_processing_failure_retries_until_attempt_budget_is_exhausted(self) -> None:
        with psycopg.connect(DSN) as connection:
            with connection.transaction():
                webhook_deliveries.register_delivery(
                    connection, definition=self.definition(), max_attempts=2
                )
                first = webhook_deliveries.claim_next_delivery(
                    connection,
                    lease_owner="webhook-worker-1",
                    lease_duration=timedelta(minutes=1),
                )
                assert first is not None
                retry = webhook_deliveries.retry_or_fail_delivery(
                    connection,
                    delivery_id=first.id,
                    lease_owner="webhook-worker-1",
                    lease_generation=first.lease_generation,
                    actor="processor:webhook-worker-1",
                    failure_code="admission_busy",
                    retry_delay=timedelta(0),
                )
                second = webhook_deliveries.claim_next_delivery(
                    connection,
                    lease_owner="webhook-worker-2",
                    lease_duration=timedelta(minutes=1),
                )
                assert second is not None
                failed = webhook_deliveries.retry_or_fail_delivery(
                    connection,
                    delivery_id=second.id,
                    lease_owner="webhook-worker-2",
                    lease_generation=second.lease_generation,
                    actor="processor:webhook-worker-2",
                    failure_code="admission_busy",
                    retry_delay=timedelta(0),
                )

        self.assertEqual(retry.status, webhook_deliveries.DeliveryStatus.RECEIVED)
        self.assertIsNotNone(retry.normalized_payload)
        self.assertEqual(retry.failure_code, "admission_busy")
        self.assertEqual(second.attempt_count, 2)
        self.assertGreater(second.lease_generation, first.lease_generation)
        self.assertEqual(failed.status, webhook_deliveries.DeliveryStatus.FAILED)
        self.assertIsNone(failed.normalized_payload)
        self.assertEqual(failed.failure_actor, "processor:webhook-worker-2")

    def test_expired_lease_recovery_requeues_then_fails_at_attempt_limit(self) -> None:
        with psycopg.connect(DSN) as connection:
            with connection.transaction():
                webhook_deliveries.register_delivery(
                    connection, definition=self.definition(), max_attempts=2
                )
                first = webhook_deliveries.claim_next_delivery(
                    connection,
                    lease_owner="webhook-worker-1",
                    lease_duration=timedelta(minutes=1),
                )
                assert first is not None
                connection.execute(
                    "UPDATE review_agent.github_webhook_deliveries "
                    "SET lease_expires_at = statement_timestamp() "
                    "WHERE id = %s",
                    (first.id,),
                )
                recovered = webhook_deliveries.recover_expired_deliveries(
                    connection, limit=10, actor="recovery:webhook"
                )
                second = webhook_deliveries.claim_next_delivery(
                    connection,
                    lease_owner="webhook-worker-2",
                    lease_duration=timedelta(minutes=1),
                )
                assert second is not None
                connection.execute(
                    "UPDATE review_agent.github_webhook_deliveries "
                    "SET lease_expires_at = statement_timestamp() "
                    "WHERE id = %s",
                    (second.id,),
                )
                exhausted = webhook_deliveries.recover_expired_deliveries(
                    connection, limit=10, actor="recovery:webhook"
                )

        self.assertEqual(len(recovered), 1)
        self.assertEqual(
            recovered[0].status, webhook_deliveries.DeliveryStatus.RECEIVED
        )
        self.assertIsNotNone(recovered[0].normalized_payload)
        self.assertEqual(len(exhausted), 1)
        self.assertEqual(exhausted[0].status, webhook_deliveries.DeliveryStatus.FAILED)
        self.assertIsNone(exhausted[0].normalized_payload)
        self.assertEqual(exhausted[0].failure_code, "processing_lease_expired")

    def test_claim_skips_a_locked_delivery(self) -> None:
        with psycopg.connect(DSN) as setup:
            with setup.transaction():
                first = webhook_deliveries.register_delivery(
                    setup, definition=self.definition(), max_attempts=2
                ).delivery
                second = webhook_deliveries.register_delivery(
                    setup,
                    definition=self.definition(
                        delivery_guid="819eb306-03f8-4f28-ad73-57725e6426d8",
                        payload_sha256="b" * 64,
                    ),
                    max_attempts=2,
                ).delivery

        with psycopg.connect(DSN) as lock_holder:
            with lock_holder.transaction():
                lock_holder.execute(
                    "SELECT id FROM review_agent.github_webhook_deliveries "
                    "WHERE id = %s FOR UPDATE",
                    (first.id,),
                )
                with psycopg.connect(DSN) as worker:
                    with worker.transaction():
                        claimed = webhook_deliveries.claim_next_delivery(
                            worker,
                            lease_owner="webhook-worker-2",
                            lease_duration=timedelta(minutes=1),
                        )

        self.assertIsNotNone(claimed)
        assert claimed is not None
        self.assertEqual(claimed.id, second.id)
