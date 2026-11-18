from __future__ import annotations

import asyncio
import os
import threading
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

from aiohttp import ServerDisconnectedError, web
from aiohttp.test_utils import TestClient, TestServer
import jwt
import psycopg

from tests import test_postgres_jobs
from review_agent.hermes import (
    quota as hermes_quota,
    runtime_api as hermes_runtime_api,
    window as hermes_window,
)
from review_agent.review import tool_runtime as review_tool_runtime
from review_agent.domain.model_execution import ModelWindow
from review_agent.review import contract as review_contract
from review_agent.hermes.control import MANAGED_REVIEW_PATH
from review_agent.domain.model_connections import (
    AccountAvailability,
    ModelProvider,
    RuntimeAccount,
)
from review_agent.hermes.accounts import account_identity
from review_agent.postgres import history as postgres_history, jobs


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
            environment={
                "HERMES_IMAGE": "hermes@sha256:" + "a" * 64,
                "REVIEW_AGENT_MODEL": "test-review-model",
            },
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
                hermes_quota, "read_accounts", side_effect=lambda: self.accounts()
            )
        )
        self.quota_response = self.enterContext(
            patch.object(hermes_quota, "fetch_account_quota", return_value=None)
        )
        self.window = ModelWindow(
            context_tokens=500_000,
            result_chars=100_000,
            turn_chars=200_000,
            compaction_tokens=400_000,
        )
        self.resolve_window = self.enterContext(
            patch.object(
                hermes_runtime_api.hermes_window, "resolve", return_value=self.window
            )
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
        run_id = int(claimed.review_run_id)
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
        self.fail_handler = False
        self.error_response = False
        self.windows_seen: list[object] = []
        # Open executions another connection saw when the window was registered.
        self.reserved_at_registration: list[int] = []
        register = review_tool_runtime.register_execution_window

        def register_after_commit(
            job_id: int, lease_generation: int, window: ModelWindow
        ) -> None:
            with self.fixture.runtime.transaction() as connection:
                self.reserved_at_registration.append(
                    jobs.active_model_executions(connection, connection_id=1)
                )
            register(job_id, lease_generation, window)

        self.enterContext(
            patch.object(
                review_tool_runtime,
                "register_execution_window",
                side_effect=register_after_commit,
            )
        )
        self.addCleanup(
            review_tool_runtime.release_execution_window,
            self.session.job_id,
            self.session.lease_generation,
        )

        @review_tool_runtime.worker_lease_fence()
        def tool(args: dict[str, object], **context: object) -> str:
            self.windows_seen.append(review_tool_runtime.session_window(context))
            return "{}"

        async def chat(request: web.Request) -> web.Response:
            self.calls += 1
            # A tool call during inference passes the lease fence and gets the
            # window admission registered for its session.
            result = await asyncio.to_thread(
                tool,
                {"run_id": run_id},
                session_id=request.headers["X-Hermes-Session-Id"],
            )
            assert result == "{}", result
            if self.reject_handler:
                raise web.HTTPBadRequest(text="Synthetic request rejection")
            if self.cancel_handler:
                raise asyncio.CancelledError()
            if self.fail_handler:
                raise RuntimeError("synthetic handler failure")
            if self.error_response:
                return web.json_response({"error": {"message": "synthetic"}}, status=500)
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
        self.resolve_window.assert_called_with(
            self.contract.model_provider, self.contract.model
        )
        self.assertEqual(self.windows_seen, [self.window])
        # The reservation was committed before the window became readable.
        self.assertEqual(self.reserved_at_registration, [1])
        # The handler's confirmed return releases the window.
        with self.assertRaises(review_tool_runtime.ToolInputError):
            review_tool_runtime.execution_window(
                self.session.job_id, self.session.lease_generation
            )
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
        snapshot = hermes_quota.QuotaSnapshot(1788897600, "Pro", (), 0, None, None)
        with (
            patch.object(
                hermes_quota, "read_accounts", return_value=self.accounts.return_value
            ) as accounts,
            patch.object(
                hermes_quota, "fetch_account_quota", return_value=snapshot
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
        # Cancellation does not prove inference stopped, so its tools keep the window.
        self.assertEqual(
            review_tool_runtime.execution_window(
                self.session.job_id, self.session.lease_generation
            ),
            self.window,
        )

    async def test_unexpected_native_handler_failure_retains_execution_reservation(
        self,
    ) -> None:
        self.fail_handler = True
        with self.assertLogs("aiohttp.server", level="ERROR"):
            response = await self.client.post(
                MANAGED_REVIEW_PATH, json=self.body, headers=self.headers
            )
        self.assertEqual(response.status, 500)
        self.assertEqual(self.calls, 1)
        # The tool call succeeded, so the 500 is the handler's own failure.
        self.assertEqual(self.windows_seen, [self.window])
        with self.fixture.runtime.transaction() as connection:
            self.assertEqual(
                jobs.active_model_executions(connection, connection_id=1), 1
            )
        # An unexpected failure does not prove inference stopped either.
        self.assertEqual(
            review_tool_runtime.execution_window(
                self.session.job_id, self.session.lease_generation
            ),
            self.window,
        )

    async def test_returned_error_response_releases_execution_reservation(self) -> None:
        self.error_response = True
        response = await self.client.post(
            MANAGED_REVIEW_PATH, json=self.body, headers=self.headers
        )
        # The same status as an unexpected failure, but the handler returned.
        self.assertEqual(response.status, 500)
        self.assertEqual(self.windows_seen, [self.window])
        with self.assertRaises(review_tool_runtime.ToolInputError):
            review_tool_runtime.execution_window(
                self.session.job_id, self.session.lease_generation
            )
        with self.fixture.runtime.transaction() as connection:
            self.assertEqual(
                jobs.active_model_executions(connection, connection_id=1), 0
            )

    async def test_unrecorded_completion_keeps_the_reservation_and_drops_the_window(
        self,
    ) -> None:
        window_at_write: list[bool] = []

        def fail_write(*_args: object, **_kwargs: object) -> None:
            try:
                review_tool_runtime.execution_window(
                    self.session.job_id, self.session.lease_generation
                )
                window_at_write.append(True)
            except review_tool_runtime.ToolInputError:
                window_at_write.append(False)
            raise psycopg.OperationalError("synthetic write failure")

        with (
            patch.object(
                hermes_runtime_api.jobs, "finish_model_execution", side_effect=fail_write
            ),
            self.assertLogs(hermes_runtime_api.logger, level="WARNING"),
        ):
            response = await self.client.post(
                MANAGED_REVIEW_PATH, json=self.body, headers=self.headers
            )
        self.assertEqual(response.status, 200, await response.text())
        # The window was already gone when the completion write started.
        self.assertEqual(window_at_write, [False])
        # The handler returned, so no tool call of this execution can follow; the
        # reservation stays because its completion was never recorded.
        with self.assertRaises(review_tool_runtime.ToolInputError):
            review_tool_runtime.execution_window(
                self.session.job_id, self.session.lease_generation
            )
        with self.fixture.runtime.transaction() as connection:
            self.assertEqual(
                jobs.active_model_executions(connection, connection_id=1), 1
            )

    async def test_unresolved_model_window_admits_nothing(self) -> None:
        self.resolve_window.side_effect = hermes_window.ModelWindowError(
            "model context window could not be resolved"
        )
        response = await self.client.post(
            MANAGED_REVIEW_PATH, json=self.body, headers=self.headers
        )
        self.assertEqual(response.status, 503)
        self.assertEqual(self.calls, 0)
        with self.fixture.runtime.transaction() as connection:
            self.assertEqual(
                jobs.active_model_executions(connection, connection_id=1), 0
            )
        with self.assertRaises(review_tool_runtime.ToolInputError):
            review_tool_runtime.execution_window(
                self.session.job_id, self.session.lease_generation
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
        self.quota_response.return_value = hermes_quota.QuotaSnapshot(
            fetched,
            "Pro",
            (
                hermes_quota.QuotaBucket(
                    "codex",
                    None,
                    None,
                    False,
                    True,
                    (hermes_quota.QuotaWindow("primary", 100, 7200, fetched + 90),),
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
        with patch.object(hermes_quota, "monotonic", return_value=0) as clock:
            response = await self.client.post(
                MANAGED_REVIEW_PATH, json=self.body, headers=self.headers
            )
            self.assertEqual(response.status, 409, await response.text())
            self.assertEqual(self.calls, 0)
            with self.fixture.runtime.transaction() as connection:
                current = jobs.get_job(connection, self.session.job_id)
                self.assertEqual(current.status, jobs.ReviewJobStatus.QUEUED)
                self.assertEqual(current.attempt_count, 0)
                detail = postgres_history.review_detail(
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
            self.quota_response.return_value = hermes_quota.QuotaSnapshot(
                fetched + 1,
                "Pro",
                (
                    hermes_quota.QuotaBucket(
                        "codex",
                        None,
                        None,
                        True,
                        False,
                        (hermes_quota.QuotaWindow("primary", 100, 7200, fetched + 90),),
                    ),
                    hermes_quota.QuotaBucket(
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
        with patch.object(hermes_quota, "PREFLIGHT_WAIT_SECONDS", 0.01):
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
