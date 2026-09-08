from __future__ import annotations

import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bootstrap/plugins"))
from review_agent_tools.admin_deployment_api import (
    DokployDeployment,
    DokployError,
    create_router,
)  # noqa: E402


class AdminDeploymentTests(unittest.TestCase):
    def test_reads_only_the_configured_application_and_returns_safe_fields(
        self,
    ) -> None:
        opener = Mock()
        opener.open.side_effect = [
            io.BytesIO(
                json.dumps(
                    {
                        "appName": "reviews",
                        "composeType": "docker-compose",
                        "serverId": None,
                        "env": "private",
                    }
                ).encode()
            ),
            io.BytesIO(
                json.dumps(
                    [
                        {
                            "containerId": "abc123",
                            "name": "reviews-worker-1",
                            "state": "running",
                            "status": "Up 1 hour",
                            "env": "private",
                        }
                    ]
                ).encode()
            ),
        ]
        with patch("urllib.request.build_opener", return_value=opener):
            result = DokployDeployment(
                "https://deploy.example.test", "test-key", "approved-compose"
            ).status()
        self.assertEqual(result.containers[0].service, "worker-1")
        self.assertNotIn("private", result.model_dump_json())
        first, second = [call.args[0] for call in opener.open.call_args_list]
        self.assertEqual(
            first.full_url,
            "https://deploy.example.test/api/compose.one?composeId=approved-compose",
        )
        self.assertEqual(
            second.full_url,
            "https://deploy.example.test/api/docker.getContainersByAppNameMatch?appName=reviews&appType=docker-compose",
        )
        self.assertEqual(first.get_header("X-api-key"), "test-key")
        self.assertEqual(first.get_method(), "GET")

    def test_response_bounds_and_invalid_origins_fail_closed(self) -> None:
        for body in (b"[", b"x" * 524289):
            opener = Mock()
            opener.open.return_value = io.BytesIO(body)
            with patch("urllib.request.build_opener", return_value=opener):
                with self.assertRaises(DokployError):
                    DokployDeployment(
                        "https://deploy.example.test", "test-key", "approved-compose"
                    ).status()
        with self.assertRaises(DokployError):
            DokployDeployment(
                "https://user:password@example.test", "test-key", "approved-compose"
            )

    def test_missing_configuration_is_explicit_and_viewers_are_denied(self) -> None:
        class Auth:
            def current_admin(self) -> object:
                return object()

        auth = Auth()
        app = FastAPI()
        with patch.dict(os.environ, {}, clear=True):
            app.include_router(create_router(auth))  # type: ignore[arg-type]
        client = TestClient(app)
        self.assertFalse(client.get("/api/deployment").json()["configured"])

        def denied() -> None:
            raise HTTPException(403, "Forbidden")

        app.dependency_overrides[auth.current_admin] = denied
        self.assertEqual(client.get("/api/deployment").status_code, 403)
        self.assertEqual(client.post("/api/deployment").status_code, 405)
