from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, Mock, patch
from uuid import UUID

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bootstrap/plugins"))

from review_agent_tools.admin_access_api import create_router  # noqa: E402
from review_agent_tools.domain.review import RepositoryId  # noqa: E402
from review_agent_tools.github import app_inventory  # noqa: E402
from review_agent_tools.postgres import github_app  # noqa: E402


NOW = datetime(2026, 9, 8, 10, 30, tzinfo=timezone.utc)
ADMIN_ID = UUID("4a0c9aa4-644f-4ca1-8da6-5cb20511d022")


class FakeAuth:
    def __init__(self, *, admin: bool = True) -> None:
        self.admin = admin

    def current_admin(self) -> object:
        if not self.admin:
            raise HTTPException(403, "Forbidden")
        return SimpleNamespace(id=ADMIN_ID, email="renamed@example.test")


def installation() -> github_app.GitHubAppInstallation:
    return github_app.GitHubAppInstallation(
        id=github_app.GitHubAppInstallationId(1),
        provider_installation_id=48219944,
        account_id=81,
        account_login="sundsvall-labs",
        account_type=github_app.AccountType.ORGANIZATION,
        repository_selection=github_app.RepositorySelection.SELECTED,
        repository_activation_policy=github_app.RepositoryActivationPolicy.EXPLICIT,
        activation_policy_actor=None,
        activation_policy_reason=None,
        activation_policy_changed_at=None,
        status=github_app.InstallationStatus.ACTIVE,
        contents_permission=github_app.PermissionLevel.READ,
        issues_permission=github_app.PermissionLevel.WRITE,
        pull_requests_permission=github_app.PermissionLevel.WRITE,
        created_at=NOW,
        updated_at=NOW,
        suspended_at=None,
        deleted_at=None,
    )


def repository(*, enabled: bool = False) -> github_app.RepositoryAccessState:
    return github_app.RepositoryAccessState(
        repository_id=RepositoryId(3),
        installation_id=github_app.GitHubAppInstallationId(1),
        provider_repository_id=76122,
        full_name="sundsvall-labs/service-api",
        access_state=github_app.RepositoryAccess.AVAILABLE,
        enabled=enabled,
        automatic_activation_blocked=True,
        trigger_mode=github_app.TriggerMode.MANUAL,
        profile_key="default" if enabled else None,
        enabled_at=NOW if enabled else None,
        disabled_at=None,
        updated_by=f"admin:{ADMIN_ID}",
        update_reason="operator decision",
        updated_at=NOW,
    )


class AdminAccessAPITests(unittest.TestCase):
    def app(self, *, admin: bool = True) -> FastAPI:
        app = FastAPI()
        app.include_router(create_router(Mock(), FakeAuth(admin=admin)))  # type: ignore[arg-type]
        return app

    def test_inventory_is_bounded_admin_only_and_reports_missing_credentials(
        self,
    ) -> None:
        with patch.dict(os.environ, {}, clear=True), patch(
            "review_agent_tools.admin_access_api.operator_application.list_github_app_installations",
            return_value=(installation(),),
        ) as list_installations, patch(
            "review_agent_tools.admin_access_api.operator_application.list_github_app_repositories",
            return_value=(repository(),),
        ) as list_repositories:
            client = TestClient(self.app())
            installations = client.get(
                "/api/access/installations?limit=1&after_id=20"
            )
            repositories = client.get("/api/access/repositories")

        self.assertEqual(installations.status_code, 200, installations.text)
        self.assertFalse(installations.json()["capability"]["configured"])
        self.assertEqual(installations.json()["next_after_id"], 48219944)
        self.assertNotIn("private_key", installations.text)
        self.assertEqual(repositories.status_code, 200, repositories.text)
        self.assertEqual(repositories.json()["items"][0]["repository_id"], 76122)
        list_installations.assert_called_once_with(
            ANY,
            limit=1,
            after_provider_installation_id=20,
        )
        list_repositories.assert_called_once_with(
            ANY,
            limit=50,
            after_provider_repository_id=0,
        )
        self.assertEqual(
            TestClient(self.app(admin=False)).get("/api/access/installations").status_code,
            403,
        )

    def test_writes_derive_stable_actor_and_delegate_to_existing_owners(self) -> None:
        enabled = repository(enabled=True)
        reconciliation = github_app.InstallationReconciliationResult(
            installation=installation(),
            repositories_seen=3,
            repositories_removed=1,
            repositories_enabled=0,
        )
        environment = {
            "REVIEW_AGENT_GITHUB_APP_ID": "123",
            "REVIEW_AGENT_GITHUB_APP_PRIVATE_KEY_FILE": "/private/key.pem",
        }
        with patch.dict(os.environ, environment, clear=True), patch(
            "review_agent_tools.admin_access_api.operator_setup.github_app_authenticator",
            return_value=Mock(),
        ), patch(
            "review_agent_tools.admin_access_api.operator_application.approve_github_app_installation",
            return_value=installation(),
        ) as approve, patch(
            "review_agent_tools.admin_access_api.operator_application.sync_github_app_installation",
            return_value=reconciliation,
        ) as sync, patch(
            "review_agent_tools.admin_access_api.operator_application.enable_github_app_repository",
            return_value=enabled,
        ) as enable, patch(
            "review_agent_tools.admin_access_api.operator_application.disable_github_app_repository",
            return_value=repository(),
        ) as disable:
            client = TestClient(self.app())
            responses = (
                client.post(
                    "/api/access/installations/48219944/approve",
                    json={"policy": "explicit", "reason": "  approved   by owner "},
                ),
                client.post(
                    "/api/access/installations/48219944/sync",
                    json={"reason": "refresh inventory"},
                ),
                client.post(
                    "/api/access/repositories/76122/enable",
                    json={"profile": " default ", "reason": "enable reviews"},
                ),
                client.post(
                    "/api/access/repositories/76122/disable",
                    json={"reason": "disable reviews"},
                ),
            )

        self.assertEqual([response.status_code for response in responses], [200] * 4)
        actor = f"admin:{ADMIN_ID}"
        self.assertEqual(approve.call_args.kwargs["actor"], actor)
        self.assertEqual(approve.call_args.kwargs["reason"], "approved by owner")
        self.assertEqual(sync.call_args.kwargs["actor"], actor)
        self.assertEqual(enable.call_args.kwargs["actor"], actor)
        self.assertEqual(enable.call_args.kwargs["profile"], "default")
        self.assertEqual(disable.call_args.kwargs["actor"], actor)

    def test_invalid_input_and_provider_failure_are_safe(self) -> None:
        environment = {
            "REVIEW_AGENT_GITHUB_APP_ID": "123",
            "REVIEW_AGENT_GITHUB_APP_PRIVATE_KEY_FILE": "/private/key.pem",
        }
        with patch.dict(os.environ, environment, clear=True), patch(
            "review_agent_tools.admin_access_api.operator_setup.github_app_authenticator",
            return_value=Mock(),
        ), patch(
            "review_agent_tools.admin_access_api.operator_application.sync_github_app_installation",
            side_effect=app_inventory.GitHubAppInventoryRetryable("private provider detail"),
        ):
            client = TestClient(self.app())
            invalid = client.post(
                "/api/access/installations/0/sync", json={"reason": "sync"}
            )
            missing_reason = client.post(
                "/api/access/repositories/76122/disable", json={"reason": ""}
            )
            failed = client.post(
                "/api/access/installations/48219944/sync",
                json={"reason": "sync"},
            )

        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(missing_reason.status_code, 422)
        self.assertEqual(failed.status_code, 503)
        self.assertNotIn("private provider detail", failed.text)


if __name__ == "__main__":
    unittest.main()
