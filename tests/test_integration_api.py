from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone
from hashlib import sha256

from tests import test_admin_api, test_postgres_reporting_cli
from review_agent_tools.postgres.runtime import PostgreSQLRuntime
from review_agent_tools.settings import PostgresDatabaseUrl


DSN = os.environ.get("REVIEW_AGENT_POSTGRES_DSN", "")


@unittest.skipUnless(DSN, "requires an isolated PostgreSQL test database")
class IntegrationAPITests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = test_admin_api.AdminAPITests("runTest")
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.client = self.fixture.client
        self.fixture.login()
        self.owner_id = self.client.get("/api/me").json()["id"]
        self.teams = [
            self.client.post(
                "/api/teams", json={"name": name, "reason": "Reporting access"}
            ).json()["id"]
            for name in ("Payments", "Private")
        ]
        self.helper = test_postgres_reporting_cli.PostgreSQLOperatorReportingTests(
            "runTest"
        )
        self.helper.runtime = PostgreSQLRuntime(PostgresDatabaseUrl(DSN))
        self.helper.runtime.open()
        self.addCleanup(self.helper.runtime.close)
        self.runs = []
        for index, name in enumerate(("payments", "private", "unassigned")):
            self.helper.repository = f"example/{name}"
            self.helper.provider_repository_id = 930 + index
            self.helper.register_repository()
            self.runs.append(
                self.helper.start(pr_number=1, request_suffix=f"integration-{index}")
            )
            if index < 2:
                with self.helper.runtime.transaction() as connection:
                    connection.execute(
                        "INSERT INTO review_agent.team_repositories (repository_id, team_id, assigned_by) SELECT id, %s, %s FROM review_agent.repositories WHERE full_name = %s",
                        (self.teams[index], self.owner_id, self.helper.repository),
                    )
        now = datetime.now(timezone.utc)
        self.window = {
            "start": (now - timedelta(days=1)).isoformat(),
            "end": (now + timedelta(hours=1)).isoformat(),
        }

    def create_integration(self, **changes):
        self.fixture.login()
        response = self.client.post(
            "/api/integrations",
            json={
                "name": "Reporting application",
                "team_ids": [self.teams[0]],
                "deployment_wide": False,
                "read_review_content": False,
                "expires_at": (
                    datetime.now(timezone.utc) + timedelta(days=30)
                ).isoformat(),
                "reason": "Read approved team reports",
                **changes,
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def use(self, issued) -> None:
        self.client.cookies.clear()
        self.client.headers["Authorization"] = f"Bearer {issued['token']}"

    def test_team_reports_match_console_without_granting_control_access(self) -> None:
        expected = self.client.get(
            "/api/overview", params={**self.window, "team_id": self.teams[0]}
        ).json()
        issued = self.create_integration()
        listing = self.client.get("/api/integrations").json()
        self.assertNotIn(issued["token"], str(listing))
        with self.helper.runtime.transaction() as connection:
            stored = connection.execute(
                "SELECT credential_sha256 FROM review_agent.integrations WHERE id = %s",
                (issued["integration"]["id"],),
            ).fetchone()[0]
            self.assertEqual(stored, sha256(issued["token"].encode()).hexdigest())
        self.assertNotIn(stored, str(listing))
        self.use(issued)
        response = self.client.get("/api/v1/overview", params=self.window)
        self.assertEqual(response.status_code, 200, response.text)
        report = response.json()
        self.assertEqual(report["metrics_version"], 1)
        self.assertEqual(report["data"]["window"], expected["window"])
        self.assertEqual(report["data"]["repository_count"], 1)
        self.assertEqual(report["evidence_scope"], "retained_records")
        self.assertIsNone(report["data"]["review_capacity"])
        with self.helper.runtime.transaction() as connection:
            details = connection.execute(
                "SELECT details FROM review_agent.admin_audit_events WHERE action = 'integration_read' ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]
            self.assertEqual(details["start"], self.window["start"])
            self.assertEqual(details["end"], self.window["end"])
        repositories = self.client.get(
            "/api/v1/repositories", params={**self.window, "search": " payments "}
        ).json()["data"]
        self.assertEqual(repositories["window_days"], 2)
        self.assertEqual(
            [item["repository"] for item in repositories["items"]],
            ["example/payments"],
        )
        with self.helper.runtime.transaction() as connection:
            details = connection.execute(
                "SELECT details FROM review_agent.admin_audit_events WHERE action = 'integration_read' ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]
            self.assertEqual(details["search"], "payments")
            self.assertEqual(details["after_id"], 0)
        self.assertEqual(
            self.client.get(
                "/api/v1/overview",
                params={**self.window, "team_id": self.teams[1]},
            ).status_code,
            404,
        )
        for path in ("/api/users", "/api/operations", "/api/integrations"):
            self.assertEqual(self.client.get(path).status_code, 401, path)
        self.assertEqual(self.client.post("/api/v1/overview").status_code, 405)
        schema = self.client.get("/api/v1/openapi.json").json()
        self.assertIn("/api/v1/overview", schema["paths"])
        self.assertNotIn("/api/users", schema["paths"])
        with self.helper.runtime.transaction() as connection:
            event = connection.execute(
                "SELECT actor_id, actor_role, details FROM review_agent.admin_audit_events WHERE action = 'integration_read' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            self.assertIsNone(event[0])
            self.assertEqual(event[1], "integration")
            self.assertEqual(event[2]["integration_id"], issued["integration"]["id"])
            self.assertNotIn(issued["token"], str(event))

    def test_revocation_is_rechecked_by_the_reporting_application(self) -> None:
        from review_agent_tools import admin_application
        from review_agent_tools.postgres.team_access import (
            AccessDenied,
            IntegrationAccessRequest,
        )

        issued = self.create_integration()
        request = IntegrationAccessRequest(sha256(issued["token"].encode()).hexdigest())
        admin_application.overview(self.helper.runtime, access=request)
        revoked = self.client.post(
            f"/api/integrations/{issued['integration']['id']}/revoke",
            json={"reason": "Consumer retired"},
        )
        self.assertEqual(revoked.status_code, 200, revoked.text)
        with self.assertRaises(AccessDenied):
            admin_application.overview(self.helper.runtime, access=request)
        self.use(issued)
        response = self.client.get("/api/v1/overview", params=self.window)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers["www-authenticate"], "Bearer")

    def test_content_requires_its_own_grant_and_current_repository_ownership(
        self,
    ) -> None:
        self.helper.repository = "example/payments"
        self.helper.provider_repository_id = 930
        batch = self.helper.record_finding(self.runs[0])
        self.helper.publish_run(self.runs[0], batch, key_character="d")
        aggregate = self.create_integration()
        content = self.create_integration(read_review_content=True)
        path = f"/api/v1/reviews/{self.runs[0].run.id}/content"
        self.use(aggregate)
        self.assertEqual(self.client.get(path).status_code, 403)
        summaries = self.client.get("/api/v1/reviews", params=self.window).json()
        self.assertNotIn("Exact persisted review", str(summaries))
        self.use(content)
        result = self.client.get(path)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertIn("Exact persisted review", result.json()["data"]["markdown"])
        self.assertFalse(result.json()["data"]["can_maintain"])
        quality = self.client.get("/api/v1/quality", params=self.window).json()
        self.assertEqual(quality["data"]["completed_reviews"], 1)
        with self.helper.runtime.transaction() as connection:
            connection.execute(
                "UPDATE review_agent.team_repositories SET team_id = %s WHERE team_id = %s",
                (self.teams[1], self.teams[0]),
            )
        self.assertEqual(self.client.get(path).status_code, 404)

    def test_history_watermark_excludes_newer_rows_and_pages_by_stable_ids(
        self,
    ) -> None:
        issued = self.create_integration(team_ids=[self.teams[0], self.teams[1]])
        self.use(issued)
        first = self.client.get(
            "/api/v1/reviews", params={**self.window, "limit": 1}
        ).json()["data"]
        self.assertEqual(first["items"][0]["id"], self.runs[1].run.id)
        self.helper.repository = "example/payments"
        self.helper.provider_repository_id = 930
        self.helper.start(pr_number=2, request_suffix="after-watermark")
        second = self.client.get(
            "/api/v1/reviews",
            params={
                **self.window,
                "limit": 1,
                "before_id": first["next_cursor"],
                "watermark_id": first["watermark_id"],
            },
        ).json()["data"]
        self.assertEqual(
            [item["id"] for item in second["items"]], [self.runs[0].run.id]
        )
        self.assertIsNone(second["next_cursor"])
        self.assertEqual(second["total"], 2)
        self.assertEqual(second["watermark_id"], first["watermark_id"])
        for params in ({"limit": 101}, {"before_id": 0}, {"start": "2026-01-01"}):
            self.assertEqual(
                self.client.get(
                    "/api/v1/reviews", params={**self.window, **params}
                ).status_code,
                422,
            )

    def test_global_scope_requires_an_admin_and_expiry_is_enforced(self) -> None:
        issued = self.create_integration(team_ids=[], deployment_wide=True)
        self.use(issued)
        response = self.client.get("/api/v1/overview", params=self.window)
        self.assertEqual(response.json()["data"]["repository_count"], 3)
        with self.helper.runtime.transaction() as connection:
            connection.execute(
                "UPDATE review_agent.integrations SET created_at = statement_timestamp() - interval '2 days', expires_at = statement_timestamp() - interval '1 second' WHERE id = %s",
                (issued["integration"]["id"],),
            )
        self.assertEqual(
            self.client.get("/api/v1/overview", params=self.window).status_code, 401
        )
        self.fixture.login()
        self.client.post(
            "/api/users",
            json={"email": "reader@example.com", "password": test_admin_api.PASSWORD},
        )
        self.fixture.login("reader@example.com")
        self.assertEqual(self.client.get("/api/integrations").status_code, 403)
        self.assertEqual(
            self.client.post(
                "/api/integrations",
                json={
                    "name": "Unauthorized application",
                    "deployment_wide": True,
                    "expires_at": (
                        datetime.now(timezone.utc) + timedelta(days=1)
                    ).isoformat(),
                    "reason": "Not an administrator",
                },
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                f"/api/integrations/{issued['integration']['id']}/revoke",
                json={"reason": "Not an administrator"},
            ).status_code,
            403,
        )

    def test_repository_pages_keep_the_first_watermark(self) -> None:
        issued = self.create_integration(team_ids=[], deployment_wide=True)
        self.use(issued)
        first = self.client.get(
            "/api/v1/repositories", params={**self.window, "limit": 1}
        ).json()["data"]
        self.assertEqual(first["items"][0]["repository"], "example/payments")
        self.helper.repository = "example/new-repository"
        self.helper.provider_repository_id = 999
        self.helper.register_repository()
        second = self.client.get(
            "/api/v1/repositories",
            params={
                **self.window,
                "limit": 100,
                "after_id": first["next_after_id"],
                "watermark_id": first["watermark_id"],
            },
        ).json()["data"]
        self.assertEqual(
            [item["repository"] for item in second["items"]],
            ["example/private", "example/unassigned"],
        )
        self.assertIsNone(second["next_after_id"])
        self.assertEqual(second["total"], 3)
        self.assertEqual(second["watermark_id"], first["watermark_id"])
