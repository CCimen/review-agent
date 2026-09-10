from __future__ import annotations

import os
from pathlib import Path
from uuid import UUID
import unittest
from unittest.mock import Mock, patch

import psycopg

from tests import test_admin_api
from review_agent_tools import review_contract
from review_agent_tools.hermes_control import (
    Cancellation,
    HermesControlError,
    LoginSession,
)
from review_agent_tools.model_accounts import (
    AccountAvailability,
    ManagedRuntimeStatus,
    ModelProvider,
    RuntimeAccount,
)
from review_agent_tools.model_connection_config import (
    ConnectionConfigurationError,
    ManagedControl,
    load_controls,
)
from review_agent_tools.model_quota import (
    AccountQuota,
    ManagedQuota,
    ObservedQuota,
    QuotaSnapshot,
)


class ConnectionConfigurationTests(unittest.TestCase):
    def test_invalid_shared_control_names_the_setting(self) -> None:
        with self.assertRaisesRegex(
            ConnectionConfigurationError, "REVIEW_AGENT_HERMES_CONTROL_URL"
        ):
            load_controls(
                {
                    "REVIEW_AGENT_HERMES_CONTROL_URL": "http://hermes.test/api",
                    "REVIEW_AGENT_HERMES_CONTROL_TOKEN": "local-test-control-token",
                }
            )


@unittest.skipUnless(
    os.environ.get("REVIEW_AGENT_POSTGRES_DSN"), "requires isolated PostgreSQL"
)
class ModelConnectionTests(unittest.TestCase):
    def setUp(self) -> None:
        contract = review_contract.load_packaged_contract(
            "default-standard",
            Path(__file__).resolve().parents[1] / "bootstrap",
            environment={"HERMES_IMAGE": "hermes@sha256:" + "a" * 64},
        )
        self.observed = ManagedRuntimeStatus(
            "payments",
            UUID(int=12),
            contract,
            (
                RuntimeAccount(
                    ModelProvider.CODEX, AccountAvailability.AVAILABLE, 1, "a" * 64
                ),
                RuntimeAccount(
                    ModelProvider.ANTHROPIC, AccountAvailability.DISCONNECTED, 0, None
                ),
            ),
        )
        self.control = Mock()
        self.control.managed_status.return_value = self.observed
        self.control.start_codex_login.return_value = LoginSession(
            "s" * 32,
            "pending",
            "TEST-CODE",
            "https://auth.openai.com/codex/device",
            600,
            5,
        )
        self.control.poll_codex_login.return_value = LoginSession(
            "s" * 32, "approved", None, None, None, 5
        )
        self.control.cancel_codex_login.return_value = Cancellation(True, "s" * 32)
        self.enterContext(
            patch(
                "review_agent_tools.model_connection_config.load_controls",
                return_value={
                    "shared": ManagedControl("shared", None),
                    "payments": ManagedControl("payments", self.control),
                },
            )
        )
        self.fixture = test_admin_api.AdminAPITests("runTest")
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.fixture.login()
        self.client = self.fixture.client
        self.team = self.client.post(
            "/api/teams", json={"name": "Payments", "reason": "Onboard"}
        ).json()

    def create_connection(self) -> dict[str, object]:
        response = self.client.post(
            "/api/model-connections",
            json={
                "runtime_key": "payments",
                "name": "Payments account",
                "team_id": self.team["id"],
                "allowed_routes": [
                    {
                        "provider": "openai-codex",
                        "model": "allowed-model",
                        "reasoning_efforts": ["high"],
                    }
                ],
                "reason": "Dedicated account",
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def test_owned_connection_permissions_policy_and_audit(self) -> None:
        managed = self.create_connection()
        connection_id = managed["id"]
        response = self.client.post(
            f"/api/model-connections/{connection_id}/enabled",
            json={
                "enabled": True,
                "expected_revision": managed["revision"],
                "reason": "Account verified",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        policy = self.client.put(
            f"/api/teams/{self.team['id']}/model-policy",
            json={
                "connection_id": connection_id,
                "provider": "openai-codex",
                "model": "allowed-model",
                "reasoning_effort": "high",
                "expected_revision": 0,
                "reason": "Team default",
            },
        )
        self.assertEqual(policy.status_code, 200, policy.text)
        self.assertEqual(policy.json()["effective_model"], "allowed-model")
        for email, role in (
            ("maintainer@example.com", "maintainer"),
            ("reader@example.com", "viewer"),
        ):
            self.client.post(
                "/api/users", json={"email": email, "password": test_admin_api.PASSWORD}
            )
            self.client.put(
                f"/api/teams/{self.team['id']}/members",
                json={"email": email, "role": role, "reason": "Team access"},
            )
        self.fixture.login("reader@example.com")
        self.assertEqual(
            self.client.get(f"/api/model-connections/{connection_id}").status_code, 200
        )
        self.assertEqual(
            self.client.post(
                f"/api/model-connections/{connection_id}/enabled",
                json={"enabled": False, "expected_revision": 2, "reason": "Pause"},
            ).status_code,
            403,
        )
        self.fixture.login("maintainer@example.com")
        response = self.client.post(
            f"/api/model-connections/{connection_id}/enabled",
            json={
                "enabled": False,
                "expected_revision": 2,
                "reason": "Pause for reconnect",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.client.get("/api/model-connections/1").status_code, 404)
        self.assertEqual(self.client.get("/api/audit").status_code, 403)
        self.fixture.login()
        self.fixture.authorize_audit()
        events = self.client.get("/api/audit?action=connection_updated").json()["items"]
        self.assertEqual(events[0]["actor_email"], "maintainer@example.com")
        self.assertEqual(events[0]["team_id"], self.team["id"])

    def test_quota_is_visible_to_members_and_bound_to_the_verified_account(
        self,
    ) -> None:
        managed = self.create_connection()
        connection_id = managed["id"]
        self.client.post(
            f"/api/model-connections/{connection_id}/enabled",
            json={
                "enabled": True,
                "expected_revision": 1,
                "reason": "Verify account",
            },
        ).raise_for_status()
        for email in ("reader@example.com", "outsider@example.com"):
            self.client.post(
                "/api/users",
                json={
                    "email": email,
                    "password": test_admin_api.PASSWORD,
                },
            ).raise_for_status()
        self.client.put(
            f"/api/teams/{self.team['id']}/members",
            json={
                "email": "reader@example.com",
                "role": "viewer",
                "reason": "Read quota",
            },
        ).raise_for_status()
        quota = AccountQuota(
            ModelProvider.CODEX,
            QuotaSnapshot(1788897600, "Pro", (), 0, None, None),
            False,
            False,
            1788897900,
            None,
        )
        observed = ManagedQuota(
            "payments", UUID(int=12), ObservedQuota("a" * 64, quota)
        )
        self.control.quota.return_value = observed
        path = f"/api/model-connections/{connection_id}/quota/openai-codex"
        self.fixture.login("outsider@example.com")
        self.assertEqual(self.client.get(path).status_code, 404)
        self.control.quota.assert_not_called()
        self.fixture.login("reader@example.com")
        result = self.client.get(path + "?refresh=true")
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["snapshot"]["reset_credits_available"], 0)
        self.assertNotIn("a" * 64, result.text)
        self.assertNotIn("identity", result.text)
        self.control.quota.return_value = ManagedQuota(
            "payments", UUID(int=12), ObservedQuota("b" * 64, quota)
        )
        mismatch = self.client.get(path)
        self.assertEqual(mismatch.status_code, 409, mismatch.text)

        def revoke(*_args: object, **_kwargs: object) -> ManagedQuota:
            with psycopg.connect(os.environ["REVIEW_AGENT_POSTGRES_DSN"]) as db:
                db.execute(
                    "DELETE FROM review_agent.team_members WHERE team_id = %s",
                    (self.team["id"],),
                )
            return observed

        self.control.quota.side_effect = revoke
        self.assertEqual(self.client.get(path).status_code, 404)

    def test_capacity_changes_require_platform_access_and_keep_omitted_values(
        self,
    ) -> None:
        managed = self.create_connection()
        path = f"/api/model-connections/{managed['id']}"
        change = {
            "name": managed["name"],
            "allowed_routes": managed["allowed_routes"],
            "expected_revision": 1,
            "max_concurrency": 2,
            "reason": "Limit shared worker load",
        }
        updated = self.client.patch(path, json=change)
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.json()["max_concurrency"], 2)
        self.client.post(
            path + "/enabled",
            json={"enabled": True, "expected_revision": 2, "reason": "Verify"},
        ).raise_for_status()
        policy_path = f"/api/teams/{self.team['id']}/model-policy"
        policy = {
            "connection_id": managed["id"],
            "expected_revision": 0,
            "max_concurrency": 1,
            "reason": "Team capacity",
        }
        saved = self.client.put(policy_path, json=policy)
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["max_concurrency"], 1)
        self.client.post(
            "/api/users",
            json={
                "email": "maintainer@example.com",
                "password": test_admin_api.PASSWORD,
            },
        ).raise_for_status()
        self.client.put(
            f"/api/teams/{self.team['id']}/members",
            json={
                "email": "maintainer@example.com",
                "role": "maintainer",
                "reason": "Maintain",
            },
        ).raise_for_status()
        self.fixture.login("maintainer@example.com")
        policy["expected_revision"] = 1
        policy["max_concurrency"] = 3
        self.assertEqual(self.client.put(policy_path, json=policy).status_code, 403)
        del policy["max_concurrency"]
        unchanged = self.client.put(policy_path, json=policy)
        self.assertEqual(unchanged.status_code, 200, unchanged.text)
        self.assertEqual(unchanged.json()["max_concurrency"], 1)
        policy["max_concurrency"] = 0
        self.assertEqual(self.client.put(policy_path, json=policy).status_code, 422)

    def test_login_is_bound_to_initiator_and_rechecks_permission(self) -> None:
        managed = self.create_connection()
        connection_id = managed["id"]
        user = self.client.post(
            "/api/users",
            json={
                "email": "maintainer@example.com",
                "password": test_admin_api.PASSWORD,
            },
        ).json()
        self.client.put(
            f"/api/teams/{self.team['id']}/members",
            json={"email": user["email"], "role": "maintainer", "reason": "Maintain"},
        )
        self.fixture.login(user["email"])
        response = self.client.post(
            f"/api/model-connections/{connection_id}/login",
            json={"expected_revision": 1, "reason": "Reconnect"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        session = response.json()
        self.assertEqual(session["user_code"], "TEST-CODE")
        self.assertNotEqual(session["id"], "s" * 32)
        self.fixture.login()
        path = f"/api/model-connections/{connection_id}/login/{session['id']}"
        self.assertEqual(self.client.post(path + "/poll").status_code, 403)
        self.client.post(
            f"/api/teams/{self.team['id']}/members/{user['id']}/remove",
            json={"reason": "Revoke"},
        )
        self.fixture.login(user["email"])
        self.assertEqual(self.client.post(path + "/poll").status_code, 404)
        self.control.poll_codex_login.assert_not_called()

    def test_approved_cancelled_and_uncertain_login_are_explicit_and_audited(
        self,
    ) -> None:
        managed = self.create_connection()
        connection_id = managed["id"]
        prefix = f"/api/model-connections/{connection_id}"
        start = self.client.post(
            prefix + "/login", json={"expected_revision": 1, "reason": "Connect"}
        )
        self.assertEqual(start.status_code, 200, start.text)
        early = self.client.post(prefix + f"/login/{start.json()['id']}/poll")
        self.assertEqual(early.json()["status"], "pending")
        self.control.poll_codex_login.assert_not_called()
        with psycopg.connect(os.environ["REVIEW_AGENT_POSTGRES_DSN"]) as connection:
            connection.execute(
                "UPDATE review_agent.model_login_sessions SET poll_after = statement_timestamp() WHERE id = %s",
                (start.json()["id"],),
            )
        response = self.client.post(prefix + f"/login/{start.json()['id']}/poll")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "approved")
        current = self.client.get(prefix).json()
        self.assertEqual(current["state"], "disabled")
        self.assertTrue(
            next(
                account
                for account in current["accounts"]
                if account["provider"] == "openai-codex"
            )["verified"]
        )
        started = self.client.post(
            prefix + "/login",
            json={"expected_revision": current["revision"], "reason": "Cancel example"},
        ).json()
        cancelled = self.client.post(prefix + f"/login/{started['id']}/cancel")
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assertEqual(cancelled.json()["status"], "cancelled")
        current = self.client.get(prefix).json()
        self.control.start_codex_login.side_effect = HermesControlError(
            "secret upstream response"
        )
        uncertain = self.client.post(
            prefix + "/login",
            json={
                "expected_revision": current["revision"],
                "reason": "Reconnect timeout",
            },
        )
        self.assertEqual(uncertain.status_code, 502, uncertain.text)
        self.assertNotIn("secret upstream", uncertain.text)
        current = self.client.get(prefix).json()
        self.assertEqual(current["state"], "needs_attention")
        rejected = self.client.post(
            prefix + "/enabled",
            json={
                "enabled": True,
                "expected_revision": current["revision"],
                "reason": "Try enable",
            },
        )
        self.assertEqual(rejected.status_code, 409, rejected.text)
        self.fixture.authorize_audit()
        events = self.client.get("/api/audit?action=provider_login").json()["items"]
        self.assertEqual(events[0]["outcome"], "failed")
        self.assertIn("succeeded", {event["outcome"] for event in events})
        self.assertNotIn("TEST-CODE", str(events))

    def test_expiry_cancels_remote_login_and_recovery_requires_a_new_runtime(
        self,
    ) -> None:
        from dataclasses import replace

        managed = self.create_connection()
        prefix = f"/api/model-connections/{managed['id']}"
        started = self.client.post(
            prefix + "/login",
            json={
                "expected_revision": managed["revision"],
                "reason": "Reconnect the account",
            },
        ).json()
        with psycopg.connect(test_admin_api.DSN) as connection:
            connection.execute(
                "UPDATE review_agent.model_login_sessions SET expires_at = now() - interval '1 second', poll_after = now() - interval '1 second' WHERE id = %s",
                (started["id"],),
            )
        expired = self.client.post(prefix + f"/login/{started['id']}/poll")
        self.assertEqual(expired.status_code, 200, expired.text)
        self.assertEqual(expired.json()["status"], "expired")
        self.control.cancel_codex_login.assert_called_once_with("s" * 32)
        self.control.poll_codex_login.assert_not_called()
        current = self.client.get(prefix).json()
        self.assertEqual(current["state"], "disabled")
        self.control.start_codex_login.side_effect = HermesControlError(
            "Provider did not answer"
        )
        result = self.client.post(
            prefix + "/login",
            json={
                "expected_revision": current["revision"],
                "reason": "Try the login again",
            },
        )
        self.assertEqual(result.status_code, 502, result.text)
        current = self.client.get(prefix).json()
        recovery = {
            "expected_revision": current["revision"],
            "reason": "Restarted Hermes and its provider control",
            "runtime_restarted": True,
        }
        self.assertEqual(
            self.client.post(prefix + "/reconcile", json=recovery).status_code, 409
        )
        self.control.managed_status.return_value = replace(
            self.observed, instance_id=UUID(int=13)
        )
        recovered = self.client.post(prefix + "/reconcile", json=recovery)
        self.assertEqual(recovered.status_code, 200, recovered.text)
        self.assertEqual(recovered.json()["state"], "disabled")
        self.assertIsNone(recovered.json()["active_login_id"])
        self.assertTrue(
            next(
                account
                for account in recovered.json()["accounts"]
                if account["provider"] == "openai-codex"
            )["verified"]
        )

    def test_account_replacement_is_explicit_and_increments_the_recorded_revision(
        self,
    ) -> None:
        from dataclasses import replace

        managed = self.create_connection()
        prefix = f"/api/model-connections/{managed['id']}"
        current = self.client.post(
            prefix + "/enabled",
            json={
                "enabled": True,
                "expected_revision": managed["revision"],
                "reason": "Enable the verified account",
            },
        ).json()
        with psycopg.connect(os.environ["REVIEW_AGENT_POSTGRES_DSN"]) as connection:
            connection.execute(
                "UPDATE review_agent.model_accounts SET quota_observed_at = statement_timestamp(), quota_wait_until = statement_timestamp() + interval '5 minutes' WHERE connection_id = %s AND provider = 'openai-codex'",
                (managed["id"],),
            )
        current = self.client.post(
            prefix + "/enabled",
            json={
                "enabled": False,
                "expected_revision": current["revision"],
                "reason": "Replace the provider account",
            },
        ).json()
        self.control.managed_status.return_value = replace(
            self.observed,
            accounts=(
                replace(self.observed.accounts[0], identity_sha256="b" * 64),
                self.observed.accounts[1],
            ),
        )
        blocked = self.client.post(
            prefix + "/enabled",
            json={
                "enabled": True,
                "expected_revision": current["revision"],
                "reason": "Try enabling the changed account",
            },
        )
        self.assertEqual(blocked.status_code, 409, blocked.text)
        reconciled = self.client.post(
            prefix + "/reconcile",
            json={
                "expected_revision": current["revision"],
                "reason": "Record the operator-installed replacement account",
            },
        )
        self.assertEqual(reconciled.status_code, 200, reconciled.text)
        account = next(
            item
            for item in reconciled.json()["accounts"]
            if item["provider"] == "openai-codex"
        )
        self.assertEqual(account["revision"], 2)
        self.assertEqual(reconciled.json()["state"], "disabled")
        with psycopg.connect(os.environ["REVIEW_AGENT_POSTGRES_DSN"]) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT quota_observed_at, quota_wait_until FROM review_agent.model_accounts WHERE connection_id = %s AND provider = 'openai-codex'",
                    (managed["id"],),
                ).fetchone(),
                (None, None),
            )
