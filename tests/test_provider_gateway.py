from __future__ import annotations

import json
import sys
import threading
import unittest
from pathlib import Path
from uuid import UUID
from unittest.mock import Mock
from urllib import error, request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bootstrap/plugins"))
from review_agent_tools.hermes_control import (  # noqa: E402
    HermesControlClient,
    LoginSession,
    ProviderStatus,
    Cancellation,
    HermesRuntimeClient,
    HermesRuntimeStatus,
    RuntimeCheck,
)
from review_agent_tools.provider_gateway import ProviderGateway  # noqa: E402
from review_agent_tools.model_accounts import ModelProvider  # noqa: E402
from review_agent_tools.model_quota import (  # noqa: E402
    AccountQuota,
    ManagedQuota,
    ObservedQuota,
    QuotaBucket,
    QuotaSnapshot,
    QuotaWindow,
)

SESSION = "a" * 22


class ProviderGatewayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.upstream = Mock(spec=HermesControlClient)
        self.runtime = Mock(spec=HermesRuntimeClient)
        self.server = ProviderGateway(
            ("127.0.0.1", 0),
            token="local-test-token",
            control=self.upstream,
            runtime=self.runtime,
        )
        thread = threading.Thread(target=self.server.serve_forever)
        thread.start()

        def close() -> None:
            self.server.shutdown()
            thread.join()
            self.server.server_close()

        self.addCleanup(close)
        self.origin = f"http://127.0.0.1:{self.server.server_port}"

    def test_admin_client_reads_status_and_completes_fixed_device_flow(self) -> None:
        self.upstream.provider_statuses.return_value = (
            ProviderStatus(
                "openai-codex", "Codex", True, "device_code", "Connect", None
            ),
            ProviderStatus(
                "anthropic", "Anthropic", False, "external_cli", "Connect", None
            ),
        )
        self.upstream.start_codex_login.return_value = LoginSession(
            SESSION,
            "pending",
            "123-456",
            "https://auth.openai.com/codex/device",
            600,
            5,
        )
        self.upstream.poll_codex_login.return_value = LoginSession(
            SESSION, "approved", None, None, None, 5
        )
        self.upstream.cancel_codex_login.return_value = Cancellation(True, SESSION)
        client = HermesControlClient(self.origin, "local-test-token")
        self.assertTrue(client.provider_statuses()[0].connected)
        self.assertEqual(client.start_codex_login().session_id, SESSION)
        self.assertEqual(client.poll_codex_login(SESSION).status, "approved")
        self.assertTrue(client.cancel_codex_login(SESSION).cancelled)
        self.upstream.poll_codex_login.assert_called_once_with(SESSION)

    def test_runtime_status_crosses_companion_boundary_without_the_api_key(
        self,
    ) -> None:
        self.runtime.status.return_value = HermesRuntimeStatus(
            "ok",
            "0.21.0",
            "gpt-test",
            0,
            False,
            True,
            True,
            tuple(
                RuntimeCheck(name, "ok")
                for name in (
                    "state_db",
                    "session_store",
                    "config",
                    "model",
                    "disk",
                    "gateway",
                    "background_queues",
                )
            ),
        )
        status = HermesControlClient(self.origin, "local-test-token").runtime_status()
        self.assertEqual(status.version, "0.21.0")
        self.assertTrue(status.chat_available)
        self.runtime.status.assert_called_once_with()

    def test_rejects_missing_authorization_arbitrary_routes_and_bodies(self) -> None:
        calls = (
            ("/api/runtime", {}, None, "GET", 401),
            ("/api/providers/oauth/openai-codex/start", {}, None, "POST", 401),
            (
                "/api/settings",
                {"X-Hermes-Session-Token": "local-test-token"},
                None,
                "GET",
                404,
            ),
            (
                "/api/providers/oauth/openai-codex/start",
                {"X-Hermes-Session-Token": "local-test-token"},
                b"{}",
                "POST",
                400,
            ),
        )
        for path, headers, body, method, expected in calls:
            with self.subTest(path=path, expected=expected):
                with self.assertRaises(error.HTTPError) as caught:
                    request.urlopen(
                        request.Request(
                            self.origin + path,
                            headers=headers,
                            data=body,
                            method=method,
                        ),
                        timeout=2,
                    )
                self.assertEqual(caught.exception.code, expected)
                self.assertNotIn("token", json.loads(caught.exception.read()))
                caught.exception.close()
        self.assertEqual(self.upstream.mock_calls, [])
        self.assertEqual(self.runtime.mock_calls, [])

    def test_account_quota_crosses_the_fixed_authenticated_boundary(self) -> None:
        observed = ManagedQuota(
            "payments",
            UUID(int=12),
            ObservedQuota(
                "a" * 64,
                AccountQuota(
                    ModelProvider.CODEX,
                    QuotaSnapshot(
                        1788897600,
                        "Pro",
                        (
                            QuotaBucket(
                                "codex",
                                None,
                                None,
                                True,
                                False,
                                (QuotaWindow("primary", 0.5, 18000, 1788900000),),
                            ),
                        ),
                        0,
                        None,
                        None,
                    ),
                    False,
                    False,
                    1788897900,
                    None,
                ),
            ),
        )
        self.runtime.quota.return_value = observed
        client = HermesControlClient(self.origin, "local-test-token")
        self.assertEqual(client.quota(ModelProvider.CODEX, refresh=True), observed)
        self.runtime.quota.assert_called_once_with(ModelProvider.CODEX, refresh=True)
