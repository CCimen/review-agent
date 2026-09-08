from __future__ import annotations

import io
import json
import os
import sys
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bootstrap/plugins"))

from review_agent_tools.admin_provider_api import create_router  # noqa: E402
from review_agent_tools.hermes_control import (  # noqa: E402
    Cancellation,
    HermesControlClient,
    HermesRuntimeClient,
    HermesControlConfigurationError,
    HermesControlError,
    LoginSession,
    ProviderModel,
    ProviderStatus,
)


SESSION_ID = "A" * 22
TOKEN = "dashboard-secret-token"


class _Response:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, _limit: int) -> bytes:
        return self.body


class _Opener:
    def __init__(self, responses: list[bytes | Exception]) -> None:
        self.responses = responses
        self.requests: list[urllib.request.Request] = []

    def open(self, request: urllib.request.Request, *, timeout: float) -> _Response:
        self.requests.append(request)
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return _Response(result)


class FakeAuth:
    def __init__(self, *, admin: bool = True) -> None:
        self.admin = admin

    def current_admin(self) -> object:
        if not self.admin:
            raise HTTPException(403, "Forbidden")
        return SimpleNamespace(id="admin")


class HermesControlTests(unittest.TestCase):
    def test_runtime_diagnostics_project_fixed_status_fields_without_provider_details(
        self,
    ) -> None:
        checks = {
            name: {"status": "ok", "detail": "private path"}
            for name in (
                "state_db",
                "session_store",
                "config",
                "model",
                "disk",
                "gateway",
                "background_queues",
            )
        }
        health = {
            "status": "ok",
            "version": "0.21.0",
            "active_agents": 2,
            "gateway_busy": True,
            "gateway_drainable": True,
            "readiness": {"checks": checks},
            "exit_reason": "private diagnostic",
            "pid": 88,
        }
        capabilities = {
            "model": "gpt-test",
            "features": {"chat_completions": True},
            "auth": {"token": "secret"},
        }
        client = HermesRuntimeClient("http://hermes.test", TOKEN)
        with patch.object(
            client.opener,
            "open",
            side_effect=[
                _Response(json.dumps(health).encode()),
                _Response(json.dumps(capabilities).encode()),
            ],
        ) as opening:
            status = client.status()
        self.assertEqual(status.active_agents, 2)
        self.assertEqual(status.model, "gpt-test")
        self.assertEqual(len(status.checks), 7)
        self.assertNotIn("private", repr(status))
        self.assertNotIn("secret", repr(status))
        self.assertEqual(
            [call.args[0].full_url for call in opening.call_args_list],
            [
                "http://hermes.test/health/detailed",
                "http://hermes.test/v1/capabilities",
            ],
        )
        for call in opening.call_args_list:
            self.assertEqual(
                call.args[0].get_header("Authorization"), f"Bearer {TOKEN}"
            )
            self.assertEqual(call.kwargs["timeout"], 5)
        del checks["disk"]
        with (
            patch.object(
                client.opener,
                "open",
                side_effect=[
                    _Response(json.dumps(health).encode()),
                    _Response(json.dumps(capabilities).encode()),
                ],
            ),
            self.assertRaises(HermesControlError),
        ):
            client.status()

    def test_allowlisted_status_login_poll_cancel_and_models(self) -> None:
        opener = _Opener(
            [
                b'{"providers":['
                b'{"id":"openai-codex","status":{"logged_in":false,"token_preview":"SECRET","error":"raw"}},'
                b'{"id":"anthropic","status":{"logged_in":true,"source_label":"SECRET_FILE"}},'
                b'{"id":"other","status":{"logged_in":true,"token_preview":"OTHER_SECRET"}}]}',
                b'{"providers":[{"slug":"openai-codex","models":["gpt-5",{"id":"gpt-6"}]},'
                b'{"slug":"anthropic","models":["claude-sonnet"]},'
                b'{"slug":"other","models":["private-model"]}]}',
                (
                    b'{"session_id":"' + SESSION_ID.encode() + b'","flow":"device_code",'
                    b'"user_code":"ABCD-EFGH","verification_url":"https://auth.openai.com/codex/device",'
                    b'"expires_in":900,"poll_interval":5,"device_code":"provider-secret"}'
                ),
                b'{"session_id":"ignored","status":"approved","error_message":"provider-secret"}',
                b'{"ok":true,"session_id":"ignored","token":"provider-secret"}',
            ]
        )
        client = HermesControlClient(
            "http://hermes-dashboard:3000", TOKEN, opener=opener  # type: ignore[arg-type]
        )

        statuses = client.provider_statuses()
        models = client.models()
        started = client.start_codex_login()
        polled = client.poll_codex_login(SESSION_ID)
        cancelled = client.cancel_codex_login(SESSION_ID)

        self.assertEqual([item.provider for item in statuses], ["openai-codex", "anthropic"])
        self.assertEqual(statuses[1].action, "hermes auth add anthropic")
        self.assertEqual([item.model for item in models], ["gpt-5", "gpt-6", "claude-sonnet"])
        self.assertEqual(started.user_code, "ABCD-EFGH")
        self.assertEqual(started.verification_url, "https://auth.openai.com/codex/device")
        self.assertEqual(polled.status, "approved")
        self.assertTrue(cancelled.cancelled)
        self.assertEqual(
            [request.get_method() for request in opener.requests],
            ["GET", "GET", "POST", "GET", "DELETE"],
        )
        self.assertTrue(
            all(request.get_header("X-hermes-session-token") == TOKEN for request in opener.requests)
        )
        safe = repr((statuses, models, started, polled, cancelled))
        for secret in ("SECRET", "raw", "provider-secret", "private-model"):
            self.assertNotIn(secret, safe)

    def test_configuration_payload_and_redirect_rules_are_strict(self) -> None:
        for invalid in (
            "https://user:pass@hermes.test",
            "https://hermes.test/api",
            "https://hermes.test?token=x",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(
                HermesControlConfigurationError
            ):
                HermesControlClient(invalid, TOKEN)
        bad_verification = _Opener(
            [
                b'{"session_id":"' + SESSION_ID.encode() + b'","user_code":"ABCD",'
                b'"verification_url":"https://evil.example/codex/device",'
                b'"expires_in":900,"poll_interval":5}'
            ]
        )
        with self.assertRaises(HermesControlError):
            HermesControlClient(
                "http://hermes.test", TOKEN, opener=bad_verification  # type: ignore[arg-type]
            ).start_codex_login()
        redirect = urllib.error.HTTPError(
            "http://hermes.test/api/providers/oauth",
            302,
            "redirect",
            {"Location": "https://evil.example/steal"},
            io.BytesIO(),
        )
        redirecting = _Opener([redirect])
        with self.assertRaises(HermesControlError):
            HermesControlClient(
                "http://hermes.test", TOKEN, opener=redirecting  # type: ignore[arg-type]
            ).provider_statuses()
        self.assertEqual(len(redirecting.requests), 1)


class AdminProviderAPITests(unittest.TestCase):
    def app(self, *, admin: bool = True) -> FastAPI:
        app = FastAPI()
        app.include_router(create_router(FakeAuth(admin=admin)))  # type: ignore[arg-type]
        return app

    def test_unconfigured_and_admin_only(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            client = TestClient(self.app())
            providers = client.get("/api/providers")
            models = client.get("/api/providers/models")
            runtime = client.get("/api/providers/runtime")
            start = client.post("/api/providers/openai-codex/login")
            self.assertEqual(providers.status_code, 200)
            self.assertEqual(runtime.status_code, 200)
            self.assertIsNone(runtime.json()["runtime"])
            self.assertEqual(
                TestClient(self.app(admin=False))
                .get("/api/providers/runtime")
                .status_code,
                403,
            )
            self.assertEqual(
                providers.json(),
                {
                    "capability": {
                        "configured": False,
                        "detail": "Hermes provider control is not configured.",
                    },
                    "items": [],
                },
            )
            self.assertEqual(models.status_code, 200)
            self.assertEqual(models.json()["items"], [])
            self.assertEqual(start.status_code, 503)
            denied = TestClient(self.app(admin=False))
            self.assertEqual(denied.get("/api/providers").status_code, 403)

    def test_api_returns_only_typed_safe_fields_and_validates_session_id(self) -> None:
        control = Mock()
        control.provider_statuses.return_value = (
            ProviderStatus(
                provider="openai-codex",
                name="ChatGPT or Codex subscription",
                connected=False,
                flow="device_code",
                action="Connect in browser",
                expires_at=None,
            ),
            ProviderStatus(
                provider="anthropic",
                name="Anthropic",
                connected=False,
                flow="external_cli",
                action="hermes auth add anthropic",
                expires_at=None,
            ),
        )
        control.models.return_value = (ProviderModel("openai-codex", "gpt-6"),)
        control.start_codex_login.return_value = LoginSession(
            SESSION_ID,
            "pending",
            "ABCD-EFGH",
            "https://auth.openai.com/codex/device",
            900,
            5,
        )
        control.poll_codex_login.return_value = LoginSession(
            SESSION_ID, "approved", None, None, None, 5
        )
        control.cancel_codex_login.return_value = Cancellation(True, SESSION_ID)
        environment = {
            "REVIEW_AGENT_HERMES_CONTROL_URL": "http://hermes.test",
            "REVIEW_AGENT_HERMES_CONTROL_TOKEN": TOKEN,
        }
        with patch.dict(os.environ, environment, clear=True), patch(
            "review_agent_tools.admin_provider_api.HermesControlClient",
            return_value=control,
        ):
            client = TestClient(self.app())
            responses = (
                client.get("/api/providers"),
                client.get("/api/providers/models"),
                client.post("/api/providers/openai-codex/login"),
                client.get(f"/api/providers/openai-codex/login/{SESSION_ID}"),
                client.post(f"/api/providers/openai-codex/login/{SESSION_ID}/cancel"),
            )
            invalid = client.get("/api/providers/openai-codex/login/not-safe")

        self.assertEqual([response.status_code for response in responses], [200] * 5)
        self.assertEqual(invalid.status_code, 422)
        combined = " ".join(response.text for response in responses)
        self.assertNotIn(TOKEN, combined)
        self.assertNotIn("token_preview", combined)
        self.assertNotIn("credentials", combined)

    def test_invalid_upstream_payload_is_redacted(self) -> None:
        control = Mock()
        control.provider_statuses.side_effect = HermesControlError(
            "provider raw error with SECRET"
        )
        environment = {
            "REVIEW_AGENT_HERMES_CONTROL_URL": "http://hermes.test",
            "REVIEW_AGENT_HERMES_CONTROL_TOKEN": TOKEN,
        }
        with patch.dict(os.environ, environment, clear=True), patch(
            "review_agent_tools.admin_provider_api.HermesControlClient",
            return_value=control,
        ):
            response = TestClient(self.app()).get("/api/providers")
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("SECRET", response.text)


if __name__ == "__main__":
    unittest.main()
