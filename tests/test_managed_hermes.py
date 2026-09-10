from __future__ import annotations

import asyncio
import os
import threading
from pathlib import Path
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

from aiohttp import ServerDisconnectedError, web
from aiohttp.test_utils import TestClient, TestServer
import jwt

from tests import test_postgres_jobs
from review_agent_tools import hermes_runtime_api, model_quota, review_contract
from review_agent_tools.hermes_control import MANAGED_REVIEW_PATH
from review_agent_tools.model_accounts import (
    AccountAvailability,
    ModelProvider,
    RuntimeAccount,
    account_identity,
)
from review_agent_tools.postgres import admin_reporting, jobs


class AccountIdentityTests(unittest.TestCase):
    def test_identity_survives_token_refresh_and_changes_with_account(self) -> None:
        def token(account: str, issued: int) -> str:
            return jwt.encode(
                {
                    "sub": "test-user",
                    "iat": issued,
                    "https://api.openai.com/auth": {"chatgpt_account_id": account},
                },
                "local-test-signing-key-at-least-32-bytes",
                algorithm="HS256",
            )

        first = account_identity(
            ModelProvider.CODEX, token=token("first", 1), auth_type="oauth"
        )
        refreshed = account_identity(
            ModelProvider.CODEX, token=token("first", 2), auth_type="oauth"
        )
        other = account_identity(
            ModelProvider.CODEX, token=token("other", 2), auth_type="oauth"
        )
        self.assertIsNotNone(first)
        self.assertEqual(first, refreshed)
        self.assertNotEqual(first, other)
        self.assertIsNone(
            account_identity(ModelProvider.CODEX, token="invalid", auth_type="oauth")
        )
        self.assertIsNone(
            account_identity(
                ModelProvider.ANTHROPIC, token="opaque-oauth", auth_type="oauth"
            )
        )


@unittest.skipUnless(
    os.environ.get("REVIEW_AGENT_POSTGRES_DSN"), "requires isolated PostgreSQL"
)
class ManagedHermesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.fixture = test_postgres_jobs.PostgreSQLJobTests("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        contract = review_contract.load_packaged_contract(
            "default-standard",
            Path(__file__).resolve().parents[1] / "bootstrap",
            environment={"HERMES_IMAGE": "hermes@sha256:" + "a" * 64},
        )
        self.contract = contract
        self.enterContext(
            patch.object(test_postgres_jobs, "TEST_REVIEW_CONTRACT", contract)
        )
        self.enterContext(
            patch.object(
                hermes_runtime_api.review_contract,
                "load_installed_contract",
                return_value=contract,
            )
        )
        self.enterContext(
            patch.object(
                hermes_runtime_api.review_tool_runtime,
                "postgres_runtime",
                return_value=self.fixture.runtime,
            )
        )
        self.accounts = self.enterContext(
            patch.object(
                hermes_runtime_api,
                "read_accounts",
                return_value=(
                    RuntimeAccount(
                        ModelProvider.CODEX, AccountAvailability.AVAILABLE, 1, "a" * 64
                    ),
                    RuntimeAccount(
                        ModelProvider.ANTHROPIC,
                        AccountAvailability.DISCONNECTED,
                        0,
                        None,
                    ),
                ),
            )
        )
        self.enterContext(
            patch.object(
                model_quota, "read_accounts", side_effect=lambda: self.accounts()
            )
        )
        self.quota_response = self.enterContext(
            patch.object(model_quota, "fetch_account_quota", return_value=None)
        )
        self.enterContext(
            patch.dict(
                os.environ,
                {
                    "API_SERVER_KEY": "local-native-test-token",
                    "REVIEW_AGENT_MODEL_CONNECTION": "shared",
                },
            )
        )
        pr = self.fixture.pull_request(provider_id=9102, number=1)
        subject = self.fixture.subject(pr, head_character="a")
        self.fixture.accept_job(pr, subject, request_key="native-review")
        with self.fixture.runtime.transaction() as connection:
            claimed = self.fixture.claim_job(
                connection,
                lease_owner="native-worker",
                lease_duration=timedelta(minutes=2),
            )
        assert claimed is not None
        self.session = jobs.WorkerLeaseSession(claimed.id, claimed.lease_generation)
        self.headers = {
            "Authorization": "Bearer local-native-test-token",
            "X-Hermes-Session-Id": self.session.encode(),
            "Idempotency-Key": self.session.encode(),
        }
        self.body = {
            "messages": [{"role": "user", "content": "Review the assigned test."}],
            "stream": False,
            "provider": contract.model_provider,
            "model": contract.model,
            "model_options": {
                "reasoning": {"enabled": True, "effort": contract.reasoning_effort}
            },
        }
        self.calls = 0
        self.cancel_handler = False
        self.reject_handler = False

        async def chat(_request: web.Request) -> web.Response:
            self.calls += 1
            if self.reject_handler:
                raise web.HTTPBadRequest(text="Synthetic request rejection")
            if self.cancel_handler:
                raise asyncio.CancelledError()
            return web.json_response({"choices": []})

        app = web.Application()
        app.router.add_post("/v1/chat/completions", chat)
        hermes_runtime_api.wire_api(app, object())
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    async def test_native_endpoint_checks_account_and_contract_before_inference(
        self,
    ) -> None:
        response = await self.client.post(MANAGED_REVIEW_PATH, json=self.body)
        self.assertEqual(response.status, 401)
        self.accounts.assert_not_called()
        response = await self.client.post(
            MANAGED_REVIEW_PATH,
            json={**self.body, "model": "other-model"},
            headers=self.headers,
        )
        self.assertEqual(response.status, 409)
        self.assertEqual(self.calls, 0)
        response = await self.client.post(
            MANAGED_REVIEW_PATH, json=self.body, headers=self.headers
        )
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual(self.calls, 1)
        with self.fixture.runtime.transaction() as connection:
            self.assertEqual(
                jobs.active_model_executions(connection, connection_id=1), 0
            )
        response = await self.client.post(
            MANAGED_REVIEW_PATH, json=self.body, headers=self.headers
        )
        self.assertEqual(response.status, 409)
        self.assertEqual(self.calls, 1)

    async def test_quota_endpoint_authenticates_and_returns_a_cached_observation(
        self,
    ) -> None:
        snapshot = model_quota.QuotaSnapshot(1788897600, "Pro", (), 0, None, None)
        with (
            patch.object(
                model_quota, "read_accounts", return_value=self.accounts.return_value
            ) as accounts,
            patch.object(
                model_quota, "fetch_account_quota", return_value=snapshot
            ) as remote,
        ):
            response = await self.client.get("/v1/review-agent/quota/openai-codex")
            self.assertEqual(response.status, 401)
            accounts.assert_not_called()
            response = await self.client.get(
                "/v1/review-agent/quota/openai-codex", headers=self.headers
            )
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            first = await response.json()
            self.assertEqual(first["runtime_key"], "shared")
            self.assertTrue(first["observation"]["data"]["refreshing"])
            response = await self.client.get(
                "/v1/review-agent/quota/openai-codex", headers=self.headers
            )
            self.assertEqual(response.status, 200)
            second = await response.json()
            self.assertEqual(
                second["observation"]["data"]["snapshot"]["reset_credits_available"], 0
            )
            self.assertEqual(remote.call_count, 1)

    async def test_rejected_native_handler_releases_execution_reservation(self) -> None:
        self.reject_handler = True
        response = await self.client.post(
            MANAGED_REVIEW_PATH, json=self.body, headers=self.headers
        )
        self.assertEqual(response.status, 400)
        with self.fixture.runtime.transaction() as connection:
            self.assertEqual(
                jobs.active_model_executions(connection, connection_id=1), 0
            )

    async def test_cancelled_native_handler_retains_execution_reservation(self) -> None:
        self.cancel_handler = True
        with self.assertRaises(ServerDisconnectedError):
            await self.client.post(
                MANAGED_REVIEW_PATH, json=self.body, headers=self.headers
            )
        self.assertEqual(self.calls, 1)
        with self.fixture.runtime.transaction() as connection:
            self.assertEqual(
                jobs.active_model_executions(connection, connection_id=1), 1
            )

    async def test_pause_after_claim_returns_unstarted_review_to_queue(self) -> None:
        with self.fixture.runtime.transaction() as connection:
            connection.execute(
                "UPDATE review_agent.model_connections SET state = 'disabled' WHERE id = 1"
            )
        response = await self.client.post(
            MANAGED_REVIEW_PATH, json=self.body, headers=self.headers
        )
        self.assertEqual(response.status, 409)
        self.assertEqual(self.calls, 0)
        with self.fixture.runtime.transaction() as connection:
            current = jobs.get_job(connection, self.session.job_id)
            self.assertEqual(current.status, jobs.ReviewJobStatus.QUEUED)
            self.assertEqual(current.attempt_count, 0)
            self.assertEqual(
                jobs.active_model_executions(connection, connection_id=1), 0
            )

    async def test_exhausted_account_waits_without_attempts_until_fresh_quota_allows_work(
        self,
    ) -> None:
        import time

        fetched = time.time()
        self.quota_response.return_value = model_quota.QuotaSnapshot(
            fetched,
            "Pro",
            (
                model_quota.QuotaBucket(
                    "codex",
                    None,
                    None,
                    False,
                    True,
                    (model_quota.QuotaWindow("primary", 100, 7200, fetched + 90),),
                ),
            ),
            2,
            None,
            None,
        )
        other_pr = self.fixture.pull_request(provider_id=9103, number=1)
        other_subject = self.fixture.subject(other_pr, head_character="b")
        self.fixture.accept_job(
            other_pr, other_subject, request_key="second-quota-waiter"
        )
        with patch.object(model_quota, "monotonic", return_value=0) as clock:
            response = await self.client.post(
                MANAGED_REVIEW_PATH, json=self.body, headers=self.headers
            )
            self.assertEqual(response.status, 409, await response.text())
            self.assertEqual(self.calls, 0)
            with self.fixture.runtime.transaction() as connection:
                current = jobs.get_job(connection, self.session.job_id)
                self.assertEqual(current.status, jobs.ReviewJobStatus.QUEUED)
                self.assertEqual(current.attempt_count, 0)
                detail = admin_reporting.review_detail(
                    connection,
                    run_id=current.review_run_id,
                    before_id=None,
                    now=datetime.now(timezone.utc),
                )
                assert detail is not None
                self.assertIsNotNone(detail.item.quota_wait_until)
                self.assertEqual(
                    jobs.active_model_executions(connection, connection_id=1), 0
                )
                self.assertIsNone(
                    self.fixture.claim_job(
                        connection,
                        lease_owner="probe",
                        lease_duration=timedelta(minutes=2),
                    )
                )

            async def probe() -> int:
                with self.fixture.runtime.transaction() as connection:
                    connection.execute(
                        "UPDATE review_agent.model_accounts SET quota_wait_until = statement_timestamp() - interval '1 second'"
                    )
                    claimed = self.fixture.claim_job(
                        connection,
                        lease_owner="probe",
                        lease_duration=timedelta(minutes=2),
                    )
                assert claimed is not None
                session = jobs.WorkerLeaseSession(
                    claimed.id, claimed.lease_generation
                ).encode()
                response = await self.client.post(
                    MANAGED_REVIEW_PATH,
                    json=self.body,
                    headers={
                        **self.headers,
                        "X-Hermes-Session-Id": session,
                        "Idempotency-Key": session,
                    },
                )
                return response.status

            clock.return_value = 301
            self.quota_response.return_value = None
            self.assertEqual(await probe(), 409)
            self.assertEqual(self.calls, 0)
            clock.return_value = 332
            self.quota_response.return_value = model_quota.QuotaSnapshot(
                fetched + 1,
                "Pro",
                (
                    model_quota.QuotaBucket(
                        "codex",
                        None,
                        None,
                        True,
                        False,
                        (model_quota.QuotaWindow("primary", 100, 7200, fetched + 90),),
                    ),
                    model_quota.QuotaBucket(
                        "another-model", None, "other", False, True, ()
                    ),
                ),
                2,
                None,
                None,
            )
            self.assertEqual(await probe(), 200)
            self.assertEqual(self.calls, 1)
            with self.fixture.runtime.transaction() as connection:
                self.assertEqual(
                    jobs.get_job(connection, self.session.job_id).attempt_count, 1
                )
                self.assertIsNone(
                    connection.execute(
                        "SELECT quota_wait_until FROM review_agent.model_accounts WHERE provider = 'openai-codex'"
                    ).fetchone()[0]
                )

    async def test_slow_quota_refresh_does_not_hold_review_startup(self) -> None:
        release = threading.Event()

        def fetch(_account: RuntimeAccount) -> None:
            release.wait(3)

        self.quota_response.side_effect = fetch
        with patch.object(model_quota, "PREFLIGHT_WAIT_SECONDS", 0.01):
            try:
                response = await asyncio.wait_for(
                    self.client.post(
                        MANAGED_REVIEW_PATH, json=self.body, headers=self.headers
                    ),
                    timeout=1,
                )
                self.assertEqual(response.status, 200)
                self.assertEqual(self.calls, 1)
                self.assertFalse(release.is_set())
            finally:
                release.set()
