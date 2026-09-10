from __future__ import annotations

import os
import sys
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import UUID

import psycopg
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bootstrap/plugins"))
from review_agent_tools import review_contract  # noqa: E402
from review_agent_tools.admin_settings_api import create_router  # noqa: E402
from review_agent_tools.deployment_settings import DeploymentSettings  # noqa: E402
from review_agent_tools.postgres import deployment_settings as store  # noqa: E402
from review_agent_tools.postgres_migrations import runner  # noqa: E402
from review_agent_tools.postgres.runtime import PostgreSQLRuntime  # noqa: E402
from review_agent_tools.settings import PostgresDatabaseUrl  # noqa: E402


class DeploymentSettingsTests(unittest.TestCase):
    def test_operator_policy_preserves_environment_defaults_and_validates_delivery(
        self,
    ) -> None:
        policy = DeploymentSettings.from_environment(
            {
                "REVIEW_AGENT_JOB_PRIORITY": "0",
                "REVIEW_AGENT_JOB_RETRY_SECONDS": "45",
                "REVIEW_AGENT_PUBLICATION_LEASE_SECONDS": "180",
                "REVIEW_AGENT_PUBLICATION_HEARTBEAT_SECONDS": "40",
                "REVIEW_AGENT_ADMISSION_MAX_CONCURRENT_REQUESTS": "12",
            }
        )
        self.assertEqual(policy.job_retry_seconds, 45)
        self.assertEqual(policy.admission_max_concurrent_requests, 12)
        self.assertEqual(
            policy, DeploymentSettings.from_environment(policy.environment())
        )
        with self.assertRaisesRegex(ValueError, "Publication heartbeat"):
            replace(policy, publication_heartbeat_seconds=90)
        with self.assertRaises(ValueError):
            replace(policy, github_app_max_body_bytes=2_097_153)

    def test_old_saved_policy_keeps_new_environment_defaults(self) -> None:
        legacy = {
            "REVIEW_AGENT_ACTIVE_JOB_LIMIT": "100",
            "REVIEW_AGENT_GITHUB_APP_CAPACITY_RETRY_SECONDS": "300",
            "REVIEW_AGENT_WORKER_CONCURRENCY": "4",
            "REVIEW_AGENT_JOB_MAX_ATTEMPTS": "3",
            "REVIEW_AGENT_JOB_LEASE_SECONDS": "120",
            "REVIEW_AGENT_JOB_HEARTBEAT_SECONDS": "30",
            "REVIEW_AGENT_HERMES_TIMEOUT_SECONDS": "7200",
            "REVIEW_AGENT_PUBLISH_MAX_BYTES": "60000",
            "REVIEW_AGENT_PUBLICATION_MAX_ATTEMPTS": "3",
            "REVIEW_AGENT_FEEDBACK_ENABLED": "false",
            "REVIEW_AGENT_CODE_GRAPH_ENABLED": "false",
            "REVIEW_AGENT_CODE_GRAPH_EMBEDDINGS": "none",
            "REVIEW_AGENT_MODEL_PROVIDER": "openai-codex",
            "REVIEW_AGENT_MODEL": "gpt-5.6-sol",
            "REVIEW_AGENT_REASONING_EFFORT": "xhigh",
        }
        connection = MagicMock()
        connection.execute.return_value.fetchone.return_value = (
            1,
            legacy,
            "admin:test",
            "initial",
            datetime.now(timezone.utc),
        )
        with patch.dict(os.environ, {"REVIEW_AGENT_JOB_RETRY_SECONDS": "47"}):
            revision = store.latest(connection)
        assert revision is not None
        self.assertEqual(revision.settings.job_retry_seconds, 47)
        self.assertEqual(revision.settings.worker_concurrency, 4)
        legacy["API_SERVER_KEY"] = "must not be stored"
        with self.assertRaisesRegex(ValueError, "Stored deployment"):
            store.latest(connection)

    def test_queue_freezes_route_but_artifact_drift_remains_rejected(self) -> None:
        installed = review_contract.load_packaged_contract(
            "default-standard",
            Path(__file__).resolve().parents[1] / "bootstrap",
            environment={"HERMES_IMAGE": "hermes@sha256:" + "a" * 64},
        )
        queued = review_contract.with_model_route(
            installed, provider="anthropic", model="claude-test", effort="high"
        )
        frozen = review_contract.resolved_config(queued)
        review_contract.require_matching_execution_contract(frozen, installed)
        later_selection = replace(DeploymentSettings(), model="next-model")
        self.assertNotEqual(queued.model, later_selection.model)
        self.assertEqual(review_contract.queued_contract(frozen).model, "claude-test")
        with self.assertRaises(review_contract.ReviewContractError):
            review_contract.require_matching_execution_contract(
                frozen, replace(installed, engine_bundle_sha256="b" * 64)
            )
        with self.assertRaises(review_contract.ReviewContractError):
            review_contract.queued_contract({**frozen, "profile": "other"})

    def test_policy_rejects_unsafe_lease_and_round_trips(self) -> None:
        values = DeploymentSettings(worker_concurrency=8, model="test-model")
        self.assertEqual(
            values, DeploymentSettings.from_environment(values.environment())
        )
        with self.assertRaisesRegex(ValueError, "Heartbeat"):
            replace(values, job_lease_seconds=60)
        with self.assertRaises(ValueError):
            replace(values, worker_concurrency=0)

    def test_admin_transport_rejects_viewers_and_returns_conflicts(self) -> None:
        from review_agent_tools.postgres.team_access import AccessRequest
        from review_agent_tools import admin_application

        class Auth:
            def current_scope(self) -> AccessRequest:
                return AccessRequest(UUID(int=1))

            def current_owner(self) -> object:
                return SimpleNamespace(id=UUID(int=1))

        app = FastAPI()
        auth = Auth()
        app.include_router(create_router(MagicMock(), auth))  # type: ignore[arg-type]
        client = TestClient(app)
        from dataclasses import asdict

        body = {
            "settings": asdict(DeploymentSettings()),
            "expected_revision": 0,
            "reason": "adjust capacity",
        }
        with patch.object(admin_application, "save_deployment_settings", side_effect=store.SettingsConflict("Reload")):
            self.assertEqual(client.put("/api/settings", json=body).status_code, 409)

        def denied() -> None:
            raise HTTPException(403, "Forbidden")

        app.dependency_overrides[auth.current_owner] = denied
        self.assertEqual(client.get("/api/settings").status_code, 403)
        self.assertEqual(client.put("/api/settings", json=body).status_code, 403)


@unittest.skipUnless(
    os.environ.get("REVIEW_AGENT_SETTINGS_DSN"), "isolated settings database required"
)
class SettingsPersistenceTests(unittest.TestCase):
    def test_publisher_and_webhook_load_extended_policy_and_record_the_revision(
        self,
    ) -> None:
        dsn = os.environ["REVIEW_AGENT_SETTINGS_DSN"]
        with psycopg.connect(dsn) as connection:
            runner.apply_migrations(connection)
            current = store.latest(connection)
            saved = store.save(
                connection,
                settings=DeploymentSettings(
                    publication_retry_seconds=53, admission_max_concurrent_requests=12
                ),
                expected_revision=current.id if current else 0,
                actor="admin:test",
                reason="adjust delivery and admission",
            )
        runtime = PostgreSQLRuntime(PostgresDatabaseUrl(dsn))
        runtime.open()
        self.addCleanup(runtime.close)
        with patch.dict(os.environ, {"REVIEW_AGENT_MODEL": "installed-model"}):
            store.apply_at_startup(runtime, "publisher")
            store.apply_at_startup(runtime, "webhook")
            self.assertEqual(os.environ["REVIEW_AGENT_PUBLICATION_RETRY_SECONDS"], "53")
            self.assertEqual(
                os.environ["REVIEW_AGENT_ADMISSION_MAX_CONCURRENT_REQUESTS"], "12"
            )
            self.assertEqual(os.environ["REVIEW_AGENT_MODEL"], "installed-model")
        with runtime.transaction() as connection:
            loads = store.startup_loads(connection)
        self.assertEqual({row.service for row in loads}, {"publisher", "webhook"})
        self.assertTrue(all(row.revision == saved.id for row in loads))

    def test_revisions_cas_rollback_and_startup_policy(self) -> None:
        with psycopg.connect(os.environ["REVIEW_AGENT_SETTINGS_DSN"]) as connection:
            runner.apply_migrations(connection)
            initial = store.latest(connection)
            revision = initial.id if initial else 0
            first = store.save(
                connection,
                settings=DeploymentSettings(worker_concurrency=8),
                expected_revision=revision,
                actor="admin:test",
                reason="raise capacity",
            )
            with connection.transaction():
                with self.assertRaises(store.SettingsConflict):
                    store.save(
                        connection,
                        settings=DeploymentSettings(),
                        expected_revision=revision,
                        actor="admin:test",
                        reason="stale",
                    )
            self.assertEqual(store.latest(connection), first)
            restored = store.save(
                connection,
                settings=DeploymentSettings(),
                expected_revision=first.id,
                actor="admin:test",
                reason="restore defaults",
            )
            self.assertEqual(
                [r.id for r in store.history(connection)][:2], [restored.id, first.id]
            )
            self.assertEqual(store.history(connection, before_id=restored.id)[0], first)
