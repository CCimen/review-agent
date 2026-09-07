from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import psycopg
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bootstrap/plugins"))

from review_agent_tools.admin_api import create_app  # noqa: E402
from review_agent_tools.admin_auth import bootstrap_admin  # noqa: E402
from review_agent_tools.postgres.runtime import (  # noqa: E402
    PostgreSQLRuntime,
    PostgreSQLUnavailable,
)
from review_agent_tools.postgres_migrations import runner  # noqa: E402
from review_agent_tools.settings import PostgresDatabaseUrl  # noqa: E402

DSN = os.environ.get("REVIEW_AGENT_POSTGRES_DSN", "")
ORIGIN = "https://admin.example.test"
PASSWORD = "local-test-password-only"


@unittest.skipUnless(DSN, "run through scripts/check_postgres_schema.sh")
class AdminAPITests(unittest.TestCase):
    def setUp(self) -> None:
        with psycopg.connect(DSN, autocommit=True) as connection:
            connection.execute("DROP SCHEMA IF EXISTS review_agent CASCADE")
        with psycopg.connect(DSN) as connection:
            runner.apply_migrations(connection)
        asyncio.run(
            bootstrap_admin(PostgresDatabaseUrl(DSN), "admin@example.com", PASSWORD)
        )
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        static = Path(directory.name)
        (static / "index.html").write_text("<html>Admin</html>")
        self.app = create_app(
            PostgreSQLRuntime(PostgresDatabaseUrl(DSN)),
            public_url=ORIGIN,
            static_dir=static,
        )
        self.client = self.enterContext(
            TestClient(self.app, base_url=ORIGIN, headers={"Origin": ORIGIN})
        )

    def login(self, email: str = "admin@example.com", password: str = PASSWORD) -> None:
        self.assertEqual(
            self.client.post(
                "/api/auth/login", data={"username": email, "password": password}
            ).status_code,
            204,
        )

    def test_viewer_cannot_manage_users_and_logout_revokes_session(self) -> None:
        for path in (
            "/api/me",
            "/api/repositories",
            "/api/history",
            "/api/history/1",
            "/api/pull-requests",
            "/api/overview",
            "/api/operations",
            "/api/operations/events",
            "/api/users",
            "/api/openapi.json",
        ):
            self.assertEqual(self.client.get(path).status_code, 401, path)
        self.assertEqual(
            self.client.post("/api/auth/register", json={}).status_code, 404
        )
        self.assertEqual(
            self.client.post(
                "/api/auth/login",
                data={"username": "admin@example.com", "password": "incorrect"},
            ).status_code,
            400,
        )
        self.login()
        cookie = self.client.cookies.get("__Host-review_agent_session")
        self.assertTrue(cookie)
        self.assertEqual(self.client.get("/api/repositories").json()["items"], [])
        with patch(
            "review_agent_tools.admin_application.repositories",
            side_effect=PostgreSQLUnavailable("private database details"),
        ):
            unavailable = self.client.get("/api/repositories")
        self.assertEqual(unavailable.status_code, 503)
        self.assertEqual(unavailable.headers["cache-control"], "no-store")
        self.assertNotIn("private database details", unavailable.text)
        created = self.client.post(
            "/api/users",
            json={
                "email": "viewer@example.com",
                "password": PASSWORD,
                "role": "viewer",
            },
        )
        self.assertEqual(created.status_code, 201, created.text)
        viewer = created.json()
        self.assertNotIn("hashed_password", viewer)
        users = self.client.get("/api/users?limit=1").json()
        self.assertEqual(users["total"], 2)
        self.assertEqual(users["admin_count"], 1)
        self.assertEqual(len(users["items"]), 1)
        self.assertTrue(users["has_more"])
        beyond = self.client.get("/api/users?offset=5").json()
        self.assertEqual((beyond["items"], beyond["total"]), ([], 2))
        self.assertEqual(self.client.get("/api/users").status_code, 200)
        self.assertEqual(self.client.post("/api/auth/logout").status_code, 204)
        self.assertEqual(
            self.client.get(
                "/api/me", headers={"Cookie": f"__Host-review_agent_session={cookie}"}
            ).status_code,
            401,
        )
        self.login("viewer@example.com")
        self.assertEqual(self.client.get("/api/me").json()["role"], "viewer")
        self.assertEqual(self.client.get("/api/history").status_code, 200)
        self.assertEqual(self.client.get("/api/history/1").status_code, 404)
        self.assertEqual(self.client.get("/api/history/0").status_code, 422)
        self.assertEqual(self.client.get("/api/history/1?before_id=0").status_code, 422)
        self.assertEqual(self.client.get("/history/1").status_code, 200)
        self.assertEqual(self.client.get("/api/pull-requests").status_code, 200)
        self.assertEqual(self.client.get("/api/overview").status_code, 200)
        self.assertEqual(self.client.get("/api/operations").status_code, 403)
        self.assertEqual(self.client.get("/api/operations/events").status_code, 403)
        self.assertEqual(self.client.get("/api/users").status_code, 403)
        self.assertEqual(
            self.client.post(
                "/api/users",
                json={
                    "email": "intruder@example.com",
                    "password": PASSWORD,
                    "role": "admin",
                },
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.patch(
                f"/api/users/{viewer['id']}", json={"role": "admin"}
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                "/api/auth/logout", headers={"Origin": "https://other.example"}
            ).status_code,
            403,
        )
        for query in (
            "days=0",
            "limit=101",
            "status=unknown",
            "repository=invalid",
            "before_id=-1",
        ):
            for path in ("/api/history", "/api/pull-requests"):
                self.assertEqual(
                    self.client.get(f"{path}?{query}").status_code, 422, query
                )

    def test_overview_window_and_empty_operations(self) -> None:
        self.login()
        response = self.client.get("/api/overview")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["lifetime"]["published_reviews"], 0)
        self.assertEqual(response.json()["repository_count"], 0)
        self.assertIsNone(response.json()["window"]["total_tokens"])
        operations = self.client.get("/api/operations")
        self.assertEqual(operations.status_code, 200, operations.text)
        self.assertEqual(operations.json()["workers"], [])
        self.assertEqual(len(operations.json()["queues"]), 3)
        for query in (
            "start=2026-01-01T00:00:00Z",
            "start=2026-01-01T00:00:00&end=2026-02-01T00:00:00Z",
            "start=2026-02-01T00:00:00Z&end=2026-01-01T00:00:00Z",
            "start=2025-01-01T00:00:00Z&end=2026-09-01T00:00:00Z",
        ):
            self.assertEqual(
                self.client.get(f"/api/overview?{query}").status_code, 422, query
            )

    def test_disabling_or_resetting_account_revokes_sessions_and_preserves_last_admin(
        self,
    ) -> None:
        self.login()
        own_id = self.client.get("/api/me").json()["id"]
        self.assertEqual(
            self.client.patch(
                f"/api/users/{own_id}", json={"role": "viewer"}
            ).status_code,
            409,
        )
        created = self.client.post(
            "/api/users",
            json={
                "email": "viewer@example.com",
                "password": PASSWORD,
                "role": "viewer",
            },
        ).json()
        admin_cookie = self.client.cookies.get("__Host-review_agent_session")
        self.login("viewer@example.com")
        viewer_cookie = self.client.cookies.get("__Host-review_agent_session")
        admin_headers = {"Cookie": f"__Host-review_agent_session={admin_cookie}"}
        response = self.client.patch(
            f"/api/users/{created['id']}",
            json={"password": "replacement-test-password"},
            headers=admin_headers,
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            self.client.get(
                "/api/me",
                headers={"Cookie": f"__Host-review_agent_session={viewer_cookie}"},
            ).status_code,
            401,
        )
        self.login("viewer@example.com", "replacement-test-password")
        response = self.client.patch(
            f"/api/users/{created['id']}", json={"active": False}, headers=admin_headers
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.client.get("/api/me").status_code, 401)
        self.assertEqual(
            self.client.post(
                "/api/auth/login",
                data={
                    "username": "viewer@example.com",
                    "password": "replacement-test-password",
                },
            ).status_code,
            400,
        )

    def test_password_change_requires_current_password_and_revokes_session(
        self,
    ) -> None:
        self.login()
        invalid = self.client.post(
            "/api/account/password",
            json={"current_password": PASSWORD, "password": "too-short"},
        )
        self.assertEqual(invalid.status_code, 422)
        self.assertNotIn(PASSWORD, invalid.text)
        self.assertNotIn("too-short", invalid.text)
        self.assertEqual(
            self.client.post(
                "/api/account/password",
                json={
                    "current_password": "incorrect",
                    "password": "replacement-test-password",
                },
            ).status_code,
            400,
        )
        changed = self.client.post(
            "/api/account/password",
            json={
                "current_password": PASSWORD,
                "password": "replacement-test-password",
            },
        )
        self.assertEqual(changed.status_code, 204, changed.text)
        self.assertEqual(self.client.get("/api/me").status_code, 401)
        self.assertEqual(
            self.client.post(
                "/api/auth/login",
                data={"username": "admin@example.com", "password": PASSWORD},
            ).status_code,
            400,
        )
        self.login(password="replacement-test-password")
