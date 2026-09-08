from __future__ import annotations

import os
import sys
import unittest
from dataclasses import replace
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


class DeploymentSettingsTests(unittest.TestCase):
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
        class Auth:
            def current_admin(self) -> object:
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
        with patch.object(store, "save", side_effect=store.SettingsConflict("Reload")):
            self.assertEqual(client.put("/api/settings", json=body).status_code, 409)

        def denied() -> None:
            raise HTTPException(403, "Forbidden")

        app.dependency_overrides[auth.current_admin] = denied
        self.assertEqual(client.get("/api/settings").status_code, 403)
        self.assertEqual(client.put("/api/settings", json=body).status_code, 403)


@unittest.skipUnless(
    os.environ.get("REVIEW_AGENT_SETTINGS_DSN"), "isolated settings database required"
)
class SettingsPersistenceTests(unittest.TestCase):
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
