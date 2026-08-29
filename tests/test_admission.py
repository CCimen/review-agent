from __future__ import annotations

from dataclasses import replace
import hashlib
import hmac
import http.client
import json
from pathlib import Path
import sys
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import MagicMock, Mock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bootstrap" / "plugins"))
sys.path.insert(0, str(ROOT / "tools"))

import review_agent_admission as admission_entrypoint  # noqa: E402

from review_agent_tools import (  # noqa: E402
    admission,
    review_contract,
)
from review_agent_tools.postgres.runtime import PostgreSQLUnavailable  # noqa: E402
from review_agent_tools.settings import PostgresDatabaseUrl, SettingsError  # noqa: E402


class AdmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = admission.AdmissionConfig(
            database_url=PostgresDatabaseUrl(
                "postgresql://review:secret@database/review"
            ),
            profile="sundsvall-standard",
            github_app_secret="app-webhook-secret",
            contract_environment={},
            webhook_delivery_max_attempts=4,
        )
        self.runtime = MagicMock()
        self.contract = review_contract.ReviewContract(
            profile="sundsvall-standard",
            hermes_image="hermes@test",
            model_provider="openai-codex",
            model="gpt-test",
            reasoning_effort="high",
            plugin_result_max_chars=160_000,
            profile_bundle_sha256="1" * 64,
            managed_config_sha256="2" * 64,
            engine_bundle_sha256="3" * 64,
            sha256="4" * 64,
        )

    def test_github_app_receipt_normalizes_before_one_short_transaction(self) -> None:
        body = b'{"signed":"raw bytes"}'
        payload = {
            "action": "created",
            "installation": {"id": 7001},
            "repository": {"id": 9001, "full_name": "CCimen/review-agent"},
            "issue": {"number": 42, "pull_request": {"url": "pr"}},
            "comment": {
                "id": 6001,
                "body": "/review",
                "author_association": "MEMBER",
            },
            "sender": {"id": 5001, "login": "ccimen", "type": "User"},
        }
        registration = Mock(
            spec=admission.webhook_deliveries.RegisteredDelivery
        )
        with patch.object(
            admission.webhook_deliveries,
            "register_delivery",
            return_value=registration,
        ) as register:
            response = admission.receive_github_app_delivery(
                body=body,
                payload=payload,
                delivery_id="688e2f40-35c1-11ef-9b3a-0242ac120002",
                event="issue_comment",
                config=self.config,
                runtime=self.runtime,
            )

        definition = register.call_args.kwargs["definition"]
        self.assertEqual(response.status, "received")
        self.assertEqual(definition.provider_repository_id, 9001)
        self.assertEqual(
            definition.command_category,
            admission.webhook_deliveries.CommandCategory.REVIEW,
        )
        self.assertEqual(definition.payload_sha256, hashlib.sha256(body).hexdigest())
        self.assertNotIn("body", definition.normalized_payload)
        self.runtime.transaction.assert_called_once_with()

    def test_large_installation_payload_normalizes_before_registration(self) -> None:
        payload = {
            "action": "created",
            "installation": {
                "id": 7001,
                "account": {"id": 8001, "login": "CCimen", "type": "User"},
                "repository_selection": "selected",
                "permissions": {
                    "contents": "read",
                    "issues": "write",
                    "pull_requests": "write",
                },
            },
            "repositories": [
                {"id": 10_000 + index, "full_name": f"CCimen/repository-{index}"}
                for index in range(1_300)
            ],
        }
        body = json.dumps(payload).encode("utf-8")
        self.assertGreater(len(body), 65_536)
        registration = Mock(spec=admission.webhook_deliveries.RegisteredDelivery)

        with patch.object(
            admission.webhook_deliveries,
            "register_delivery",
            return_value=registration,
        ) as register:
            response = admission.receive_github_app_delivery(
                body=body,
                payload=payload,
                delivery_id="688e2f40-35c1-11ef-9b3a-0242ac120002",
                event="installation",
                config=self.config,
                runtime=self.runtime,
            )

        definition = register.call_args.kwargs["definition"]
        self.assertEqual(response.status, "received")
        self.assertEqual(len(definition.normalized_payload["repositories"]), 1_300)

    def test_readiness_fails_when_configured_profile_is_not_packaged(self) -> None:
        self.runtime.database_url = self.config.database_url
        with (
            patch.object(
                admission.review_contract,
                "load_packaged_contract",
                return_value=replace(self.contract, profile="other-profile"),
            ),
            self.assertRaisesRegex(admission.AdmissionError, "configured profile"),
        ):
            admission.ready_check(self.config, self.runtime)

    def test_configuration_requires_the_app_webhook_secret(self) -> None:
        environment = {
            "REVIEW_AGENT_GITHUB_APP_WEBHOOK_SECRET": "app-secret",
            "REVIEW_AGENT_DATABASE_URL": "postgresql://review:secret@database/review",
            "REVIEW_AGENT_WEBHOOK_DELIVERY_MAX_ATTEMPTS": "5",
        }
        configured = admission.load_config(environment)
        self.assertEqual(configured.github_app_secret, "app-secret")
        self.assertEqual(configured.webhook_delivery_max_attempts, 5)
        self.assertNotIn(
            "REVIEW_AGENT_GITHUB_APP_WEBHOOK_SECRET",
            configured.contract_environment,
        )

        environment["REVIEW_AGENT_GITHUB_APP_WEBHOOK_SECRET"] = ""
        with self.assertRaisesRegex(SettingsError, "is required"):
            admission.load_config(environment)


class AdmissionHttpBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.secret = "admission-test-secret"
        config = admission.AdmissionConfig(
            database_url=PostgresDatabaseUrl(
                "postgresql://review:secret@database/review"
            ),
            profile="sundsvall-standard",
            github_app_secret="app-webhook-secret",
            contract_environment={},
        )
        self.runtime = MagicMock()
        self.server = admission_entrypoint.AdmissionServer(
            ("127.0.0.1", 0),
            config=config,
            runtime=self.runtime,
            github_app_max_body_bytes=100_000,
            max_concurrent_requests=2,
            request_timeout_seconds=2,
        )
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.addCleanup(self._stop_server)

    def _stop_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def _post_app(
        self,
        body: bytes = b"{}",
        *,
        event: str = "ping",
        valid_signature: bool = True,
    ) -> tuple[int, dict[str, str], bytes]:
        signature = "sha256=invalid"
        if valid_signature:
            signature = "sha256=" + hmac.new(
                self.server.config.github_app_secret.encode("utf-8"),
                body,
                hashlib.sha256,
            ).hexdigest()
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.server.server_port}"
            f"{admission.GITHUB_APP_PATH}",
            data=body,
            method="POST",
            headers={
                "X-GitHub-Delivery": "688e2f40-35c1-11ef-9b3a-0242ac120002",
                "X-GitHub-Event": event,
                "X-Hub-Signature-256": signature,
            },
        )
        try:
            response = urllib.request.urlopen(request, timeout=2)
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, dict(exc.headers.items()), exc.read()
        with response:
            return response.status, dict(response.headers.items()), response.read()

    def test_github_app_route_durably_acknowledges_without_a_github_read(self) -> None:
        with patch.object(
            admission_entrypoint.admission,
            "receive_github_app_delivery",
            return_value=admission.WebhookReceiptResponse("received"),
            create=True,
        ) as receive:
            status, _, body = self._post_app()

        self.assertEqual((status, json.loads(body)), (202, {"status": "received"}))
        receive.assert_called_once()

    def test_github_app_route_fails_before_receipt_and_preserves_conflicts(self) -> None:
        with patch.object(
            admission_entrypoint.admission,
            "receive_github_app_delivery",
            create=True,
        ) as receive:
            status, _, body = self._post_app(valid_signature=False)
        self.assertEqual((status, json.loads(body)), (401, {"status": "bad_signature"}))
        receive.assert_not_called()

        with patch.object(
            admission_entrypoint.admission,
            "receive_github_app_delivery",
            side_effect=admission.GitHubAppDeliveryConflict("conflict detail"),
            create=True,
        ):
            status, _, body = self._post_app()
        self.assertEqual(
            (status, json.loads(body)),
            (409, {"status": "delivery_conflict"}),
        )

        with patch.object(
            admission_entrypoint.admission,
            "receive_github_app_delivery",
            side_effect=PostgreSQLUnavailable("database secret"),
        ):
            status, _, body = self._post_app()
        self.assertEqual(
            (status, json.loads(body)),
            (503, {"status": "database_unavailable"}),
        )
        self.assertNotIn(b"secret", body)

    def test_github_app_route_acknowledges_unsupported_events_without_persistence(
        self,
    ) -> None:
        status, _, body = self._post_app(event="workflow_run")

        self.assertEqual((status, json.loads(body)), (202, {"status": "ignored"}))
        self.runtime.transaction.assert_not_called()

    def test_github_app_route_rejects_malformed_supported_events(self) -> None:
        status, _, body = self._post_app(event="issue_comment")

        self.assertEqual((status, json.loads(body)), (400, {"status": "bad_request"}))
        self.runtime.transaction.assert_not_called()

    def test_github_app_route_has_an_independent_body_limit(self) -> None:
        body = json.dumps(
            {
                "action": "created",
                "installation": {"id": 7001},
                "padding": "x" * 70_000,
            }
        ).encode("utf-8")
        self.assertLess(len(body), self.server.github_app_max_body_bytes)

        with patch.object(
            admission_entrypoint.admission,
            "receive_github_app_delivery",
            return_value=admission.WebhookReceiptResponse("received"),
        ) as receive:
            status, _, response_body = self._post_app(body, event="installation")

        self.assertEqual(
            (status, json.loads(response_body)),
            (202, {"status": "received"}),
        )
        receive.assert_called_once()

        with patch.object(
            admission_entrypoint.admission,
            "receive_github_app_delivery",
            create=True,
        ) as receive:
            status, _, response_body = self._post_app(b"x" * 100_001)
        self.assertEqual(
            (status, json.loads(response_body)),
            (413, {"status": "payload_too_large"}),
        )
        receive.assert_not_called()

    def test_server_config_rejects_an_app_bound_above_the_service_ceiling(
        self,
    ) -> None:
        with patch.object(
            admission_entrypoint.admission,
            "load_config",
            return_value=self.server.config,
        ):
            configured = admission_entrypoint.load_server_config({})
            self.assertEqual(configured.github_app_max_body_bytes, 2_097_152)

            with self.assertRaisesRegex(
                SettingsError,
                "REVIEW_AGENT_GITHUB_APP_MAX_BODY_BYTES must not exceed 2097152",
            ):
                admission_entrypoint.load_server_config(
                    {"REVIEW_AGENT_GITHUB_APP_MAX_BODY_BYTES": "2097153"}
                )

    def test_normalized_payload_overflow_is_an_observable_413(self) -> None:
        with (
            patch.object(
                admission_entrypoint.admission,
                "receive_github_app_delivery",
                side_effect=admission.GitHubAppPayloadTooLarge(
                    "normalized payload exceeds storage guard"
                ),
            ),
            patch("builtins.print") as logged,
        ):
            status, _, body = self._post_app(event="installation")

        self.assertEqual(
            (status, json.loads(body)),
            (413, {"status": "payload_too_large"}),
        )
        self.assertTrue(
            any("GitHub App webhook rejected" in str(call) for call in logged.mock_calls)
        )

    def test_signature_and_request_size_fail_before_receipt(self) -> None:
        with patch.object(
            admission_entrypoint.admission, "receive_github_app_delivery"
        ) as receive:
            status, _, body = self._post_app(valid_signature=False)
            self.assertEqual(
                (status, json.loads(body)), (401, {"status": "bad_signature"})
            )

            status, _, body = self._post_app(b"x" * 100_001)
            self.assertEqual(
                (status, json.loads(body)),
                (413, {"status": "payload_too_large"}),
            )
        receive.assert_not_called()

    def test_missing_content_length_is_rejected(self) -> None:
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_port, timeout=2
        )
        connection.putrequest("POST", admission.GITHUB_APP_PATH)
        connection.putheader("X-GitHub-Event", "issue_comment")
        connection.endheaders()
        response = connection.getresponse()
        body = response.read()
        connection.close()

        self.assertEqual(
            (response.status, json.loads(body)), (411, {"status": "missing_length"})
        )

    def test_failures_use_fixed_status_and_retry_contracts(self) -> None:
        cases = (
            (
                PostgreSQLUnavailable("database secret"),
                503,
                None,
                {"status": "database_unavailable"},
            ),
            (RuntimeError("internal secret"), 500, None, {"status": "internal_error"}),
        )
        for failure, expected_status, retry_after, expected_body in cases:
            with self.subTest(failure=type(failure).__name__):
                with patch.object(
                    admission_entrypoint.admission,
                    "receive_github_app_delivery",
                    side_effect=failure,
                ):
                    status, headers, body = self._post_app()
                self.assertEqual(status, expected_status)
                self.assertEqual(headers.get("Retry-After"), retry_after)
                self.assertEqual(json.loads(body), expected_body)
                self.assertNotIn(b"secret", body)


if __name__ == "__main__":
    unittest.main()
