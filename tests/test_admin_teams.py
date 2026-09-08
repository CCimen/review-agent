from __future__ import annotations

import os
import unittest

import psycopg

from tests import test_admin_api, test_postgres_reporting_cli
from review_agent_tools.postgres.runtime import PostgreSQLRuntime
from review_agent_tools.settings import PostgresDatabaseUrl


DSN = os.environ.get("REVIEW_AGENT_POSTGRES_DSN", "")


@unittest.skipUnless(DSN, "requires an isolated PostgreSQL test database")
class AdminTeamTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = test_admin_api.AdminAPITests("runTest")
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.client = self.fixture.client
        self.fixture.login()

    def test_repository_scope_covers_lists_totals_and_direct_history(self) -> None:
        team = self.client.post(
            "/api/teams", json={"name": "Payments", "reason": "Onboard"}
        ).json()
        user = self.client.post(
            "/api/users",
            json={"email": "reader@example.com", "password": test_admin_api.PASSWORD},
        ).json()
        self.client.put(
            f"/api/teams/{team['id']}/members",
            json={
                "email": user["email"],
                "role": "viewer",
                "reason": "Review visibility",
            },
        )
        helper = test_postgres_reporting_cli.PostgreSQLOperatorReportingTests("runTest")
        helper.runtime = PostgreSQLRuntime(PostgresDatabaseUrl(DSN))
        helper.runtime.open()
        self.addCleanup(helper.runtime.close)
        runs = []
        for index, repository in enumerate(("example/payments", "example/private")):
            helper.repository = repository
            helper.provider_repository_id = 930 + index
            helper.register_repository()
            runs.append(helper.start(pr_number=1, request_suffix=f"scope-{index}"))
        with helper.runtime.transaction() as connection:
            connection.execute(
                "INSERT INTO review_agent.team_repositories (repository_id, team_id, assigned_by) SELECT id, %s, %s FROM review_agent.repositories WHERE full_name = 'example/payments'",
                (team["id"], user["id"]),
            )
        self.fixture.login("reader@example.com")
        repositories = self.client.get("/api/repositories").json()
        self.assertEqual(
            [item["repository"] for item in repositories["items"]], ["example/payments"]
        )
        self.assertEqual(repositories["total"], 1)
        self.assertEqual(repositories["totals"]["active_requests"], 1)
        for path in ("/api/history", "/api/pull-requests"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["total"], 1)
            self.assertEqual(len(response.json()["items"]), 1)
            self.assertEqual(
                self.client.get(f"{path}?repository=example/private").json()["items"],
                [],
            )
        overview = self.client.get("/api/overview").json()
        self.assertEqual(overview["repository_count"], 1)
        self.assertEqual(overview["active_requests"], 1)
        self.assertEqual(overview["lifetime"]["requests"], 1)
        self.assertIsNone(overview["review_capacity"])
        self.assertEqual(
            self.client.get(f"/api/history/{runs[0].run.id}").status_code, 200
        )
        self.assertEqual(
            self.client.get(f"/api/history/{runs[1].run.id}").status_code, 404
        )

    def test_team_membership_requires_maintainer_and_revocation_is_immediate(
        self,
    ) -> None:
        response = self.client.post(
            "/api/teams",
            json={
                "name": "Payments",
                "description": "Payment services",
                "reason": "Onboard the team",
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        team_id = response.json()["id"]
        users = []
        for email in ("maintainer@example.com", "reader@example.com"):
            response = self.client.post(
                "/api/users",
                json={"email": email, "password": test_admin_api.PASSWORD},
            )
            self.assertEqual(response.status_code, 201, response.text)
            self.assertEqual(response.json()["role"], "member")
            users.append(response.json()["id"])
        response = self.client.put(
            f"/api/teams/{team_id}/members",
            json={
                "email": "maintainer@example.com",
                "role": "maintainer",
                "reason": "Team owner",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)

        self.fixture.login("maintainer@example.com")
        teams = self.client.get("/api/teams").json()["items"]
        self.assertEqual(
            [(item["id"], item["role"]) for item in teams], [(team_id, "maintainer")]
        )
        self.assertEqual(self.client.get("/api/users").status_code, 403)
        self.assertEqual(
            self.client.post(
                "/api/teams", json={"name": "Other", "reason": "Try creation"}
            ).status_code,
            403,
        )
        response = self.client.put(
            f"/api/teams/{team_id}/members",
            json={
                "email": "reader@example.com",
                "role": "viewer",
                "reason": "Review visibility",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)

        self.fixture.login("reader@example.com")
        self.assertEqual(
            self.client.get(f"/api/teams/{team_id}").json()["role"], "viewer"
        )
        access_revision = self.client.get("/api/me").json()["access_revision"]
        self.assertEqual(
            self.client.put(
                f"/api/teams/{team_id}/members",
                json={
                    "email": "reader@example.com",
                    "role": "maintainer",
                    "reason": "Attempt escalation",
                },
            ).status_code,
            403,
        )
        reader_cookie = self.client.cookies.get("__Host-review_agent_session")
        self.fixture.login("maintainer@example.com")
        response = self.client.post(
            f"/api/teams/{team_id}/members/{users[1]}/remove",
            json={"reason": "Access no longer needed"},
        )
        self.assertEqual(response.status_code, 204, response.text)
        headers = {"Cookie": f"__Host-review_agent_session={reader_cookie}"}
        self.assertEqual(
            self.client.get(f"/api/teams/{team_id}", headers=headers).status_code, 404
        )
        self.assertEqual(
            self.client.get(
                f"/api/overview?team_id={team_id}", headers=headers
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.get("/api/teams", headers=headers).json()["items"], []
        )
        self.assertGreater(
            self.client.get("/api/me", headers=headers).json()["access_revision"],
            access_revision,
        )

    def test_quality_scope_and_maintainer_decisions_use_current_membership(
        self,
    ) -> None:
        team = self.client.post(
            "/api/teams", json={"name": "Payments", "reason": "Onboard"}
        ).json()
        user = self.client.post(
            "/api/users",
            json={"email": "reader@example.com", "password": test_admin_api.PASSWORD},
        ).json()
        owner_cookie = self.client.cookies.get("__Host-review_agent_session")
        owner_headers = {"Cookie": f"__Host-review_agent_session={owner_cookie}"}
        self.client.put(
            f"/api/teams/{team['id']}/members",
            json={
                "email": user["email"],
                "role": "viewer",
                "reason": "Review visibility",
            },
        )
        helper = test_postgres_reporting_cli.PostgreSQLOperatorReportingTests("runTest")
        helper.runtime = PostgreSQLRuntime(PostgresDatabaseUrl(DSN))
        helper.runtime.open()
        self.addCleanup(helper.runtime.close)
        batches = []
        runs = []
        feedback = []
        for index, repository in enumerate(("example/payments", "example/private")):
            helper.repository = repository
            helper.provider_repository_id = 930 + index
            helper.register_repository()
            run = helper.start(pr_number=1, request_suffix=f"quality-scope-{index}")
            batch = helper.record_finding(run)
            publication = helper.publish_run(run, batch, key_character=str(index))
            with helper.runtime.transaction() as connection:
                feedback.append(
                    connection.execute(
                        "INSERT INTO review_agent.review_quality_feedback (pull_request_id, publication_id, category, reason, actor_user_id, created_at) VALUES (%s, %s, 'missed_issue', 'A retained miss', '1', statement_timestamp()) RETURNING id",
                        (run.run.pull_request_id, publication.id),
                    ).fetchone()[0]
                )
            batches.append(batch)
            runs.append(run)
        with helper.runtime.transaction() as connection:
            connection.execute(
                "INSERT INTO review_agent.team_repositories (repository_id, team_id, assigned_by) SELECT id, %s, %s FROM review_agent.repositories WHERE full_name = 'example/payments'",
                (team["id"], user["id"]),
            )
        self.fixture.login("reader@example.com")
        report = self.client.get("/api/quality")
        self.assertEqual(report.status_code, 200, report.text)
        self.assertEqual(report.json()["completed_reviews"], 1)
        self.assertEqual(report.json()["published_findings"], 1)
        self.assertEqual(report.json()["triage_backlog"], 1)
        self.assertEqual(
            [item["repository"] for item in report.json()["cohorts"]],
            ["example/payments"],
        )
        self.assertEqual(
            [
                item["id"]
                for item in self.client.get("/api/quality/feedback").json()["items"]
            ],
            [feedback[0]],
        )
        for index, expected in enumerate((200, 404)):
            item = batches[index].items[0]
            self.assertEqual(
                self.client.get(
                    f"/api/history/{runs[index].run.id}/findings"
                ).status_code,
                expected,
            )
            self.assertEqual(
                self.client.get(
                    f"/api/findings/{item.fingerprint}",
                    params={
                        "repository": "example/payments"
                        if index == 0
                        else "example/private",
                        "occurrence_id": int(item.occurrence_id),
                    },
                ).status_code,
                expected,
            )
        item = batches[0].items[0]
        url = f"/api/findings/{item.fingerprint}/decisions"
        decision = {
            "repository": "example/payments",
            "occurrence_id": int(item.occurrence_id),
            "decision": "resolved",
            "reason": "The exact occurrence was fixed",
        }
        self.assertEqual(self.client.post(url, json=decision).status_code, 403)
        self.assertEqual(
            self.client.post(
                f"/api/quality/feedback/{feedback[0]}/triage",
                json={"status": "insufficient", "reason": "Needs a reproduction"},
            ).status_code,
            403,
        )
        promoted = self.client.put(
            f"/api/teams/{team['id']}/members",
            json={
                "email": user["email"],
                "role": "maintainer",
                "reason": "Team review responsibility",
            },
            headers=owner_headers,
        )
        self.assertEqual(promoted.status_code, 200, promoted.text)
        result = self.client.post(url, json=decision)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["actor"], f"team-maintainer:{user['id']}")
        foreign = batches[1].items[0]
        self.assertEqual(
            self.client.post(
                f"/api/findings/{foreign.fingerprint}/decisions",
                json={
                    **decision,
                    "repository": "example/private",
                    "occurrence_id": int(foreign.occurrence_id),
                },
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.post(
                f"/api/quality/feedback/{feedback[1]}/triage",
                json={"status": "insufficient", "reason": "Needs a reproduction"},
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.post(
                f"/api/quality/feedback/{feedback[0]}/triage",
                json={"status": "insufficient", "reason": "Needs a reproduction"},
            ).status_code,
            200,
        )

    def test_owner_hierarchy_protects_privileged_accounts_and_last_owner(self) -> None:
        owner = self.client.get("/api/me").json()
        self.assertEqual(owner["role"], "owner")
        admin = self.client.post(
            "/api/users",
            json={
                "email": "admin2@example.com",
                "password": test_admin_api.PASSWORD,
                "role": "admin",
            },
        ).json()
        self.fixture.login("admin2@example.com")
        for target in (owner, admin):
            response = self.client.patch(
                f"/api/users/{target['id']}",
                json={"password": "replacement-test-password"},
            )
            self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(
            self.client.post(
                "/api/users",
                json={
                    "email": "escalated@example.com",
                    "password": test_admin_api.PASSWORD,
                    "role": "owner",
                },
            ).status_code,
            403,
        )
        member = self.client.post(
            "/api/users",
            json={"email": "member@example.com", "password": test_admin_api.PASSWORD},
        ).json()
        self.assertEqual(member["role"], "member")
        self.assertEqual(
            self.client.patch(
                f"/api/users/{member['id']}", json={"role": "admin"}
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.patch(
                f"/api/users/{member['id']}", json={"active": False}
            ).status_code,
            200,
        )
        self.fixture.login()
        self.assertEqual(
            self.client.patch(
                f"/api/users/{owner['id']}", json={"role": "admin"}
            ).status_code,
            409,
        )
        promoted = self.client.patch(
            f"/api/users/{admin['id']}", json={"role": "owner"}
        )
        self.assertEqual(promoted.status_code, 200, promoted.text)
        self.assertEqual(
            self.client.patch(
                f"/api/users/{owner['id']}", json={"role": "admin"}
            ).status_code,
            200,
        )
        self.assertEqual(self.client.get("/api/me").status_code, 401)

    def test_audit_visibility_follows_the_target_and_preserves_actor(self) -> None:
        owner = self.client.get("/api/me").json()
        admin = self.client.post(
            "/api/users",
            json={
                "email": "admin2@example.com",
                "password": test_admin_api.PASSWORD,
                "role": "admin",
            },
        ).json()
        team = self.client.post(
            "/api/teams", json={"name": "Payments", "reason": "Onboard payment reviews"}
        ).json()
        self.fixture.login("admin2@example.com")
        member = self.client.post(
            "/api/users",
            json={"email": "member@example.com", "password": test_admin_api.PASSWORD},
        ).json()
        result = self.client.get("/api/audit")
        self.assertEqual(result.status_code, 200, result.text)
        events = result.json()["items"]
        self.assertTrue(events)
        self.assertTrue(all(not event["owner_only"] for event in events))
        self.assertTrue(
            any(
                event["action"] == "team_created"
                and event["actor_id"] == owner["id"]
                and event["team_id"] == team["id"]
                for event in events
            )
        )
        self.assertTrue(
            any(
                event["subject"] == member["id"] and event["actor_id"] == admin["id"]
                for event in events
            )
        )
        self.assertFalse(
            any(event["subject"] in (owner["id"], admin["id"]) for event in events)
        )
        self.fixture.login()
        own = self.client.get("/api/audit?limit=1").json()
        self.assertIsNotNone(own["next_before_id"])
        older = self.client.get(f"/api/audit?before_id={own['next_before_id']}").json()
        self.assertNotIn(
            own["items"][0]["id"], [event["id"] for event in older["items"]]
        )
        self.assertTrue(any(event["owner_only"] for event in older["items"]))
        self.assertNotIn(test_admin_api.PASSWORD, self.client.get("/api/audit").text)
        self.fixture.login("member@example.com")
        self.assertEqual(self.client.get("/api/audit").status_code, 403)
        self.assertEqual(
            self.client.get(f"/api/teams/{team['id']}/events").status_code, 403
        )

    def test_audit_search_and_exports_share_filters_and_preserve_values(self) -> None:
        import csv
        import io
        import json
        from datetime import datetime, timedelta, timezone

        owner = self.client.get("/api/me").json()
        start = datetime.now(timezone.utc) - timedelta(seconds=1)
        selected = self.client.post(
            "/api/teams", json={"name": "Invoices", "reason": "=invoice approval"}
        ).json()
        self.client.post("/api/teams", json={"name": "Other", "reason": "Other work"})
        filters = {
            "search": "invoice",
            "action": "team_created",
            "actor_id": owner["id"],
        }
        response = self.client.get("/api/audit", params=filters)
        self.assertEqual(response.status_code, 200, response.text)
        items = response.json()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["team_id"], selected["id"])
        self.assertEqual(items[0]["reason"], "=invoice approval")
        for extra in ({"outcome": "failed"}, {"until": start.isoformat()}):
            self.assertEqual(
                self.client.get("/api/audit", params={**filters, **extra}).json()[
                    "items"
                ],
                [],
            )
        self.assertEqual(
            self.client.get(
                "/api/audit", params={"since": "2026-09-01T00:00:00"}
            ).status_code,
            422,
        )
        self.assertEqual(
            self.client.get(
                "/api/audit",
                params={
                    "since": "2026-09-02T00:00:00Z",
                    "until": "2026-09-01T00:00:00Z",
                },
            ).status_code,
            422,
        )
        for format in ("json", "csv", "jsonl", "otlp"):
            with self.subTest(format=format):
                export = self.client.get(
                    "/api/audit/export", params={**filters, "format": format}
                )
                self.assertEqual(export.status_code, 200, export.text)
                self.assertEqual(export.headers["x-audit-count"], "1")
                self.assertIn("attachment;", export.headers["content-disposition"])
                self.assertEqual(export.headers["cache-control"], "no-store")
                if format == "json":
                    self.assertEqual(export.json()["items"], items)
                elif format == "jsonl":
                    self.assertEqual(
                        [json.loads(line) for line in export.text.splitlines()], items
                    )
                elif format == "csv":
                    rows = list(csv.DictReader(io.StringIO(export.text)))
                    self.assertEqual(rows[0]["reason"], "'=invoice approval")
                    self.assertEqual(rows[0]["actor_id"], owner["id"])
                else:
                    resource = export.json()["resourceLogs"][0]
                    records = resource["scopeLogs"][0]["logRecords"]
                    self.assertEqual(len(records), 1)
                    self.assertEqual(
                        records[0]["body"]["stringValue"], "=invoice approval"
                    )
                    self.assertIsInstance(records[0]["timeUnixNano"], str)
                    self.assertEqual(records[0]["severityNumber"], 9)
                    self.assertNotIn("traceId", records[0])
                    attributes = {
                        item["key"]: item["value"] for item in records[0]["attributes"]
                    }
                    self.assertEqual(
                        attributes["review_agent.audit.event_id"],
                        {"intValue": str(items[0]["id"])},
                    )
        page = self.client.get(
            "/api/audit/export",
            params={"format": "json", "action": "team_created", "limit": 1},
        )
        self.assertEqual(page.status_code, 200, page.text)
        next_id = page.headers["x-audit-next-before-id"]
        older = self.client.get(
            "/api/audit/export",
            params={
                "format": "json",
                "action": "team_created",
                "limit": 1,
                "before_id": next_id,
            },
        )
        self.assertNotEqual(
            page.json()["items"][0]["id"], older.json()["items"][0]["id"]
        )
        self.assertEqual(
            self.client.get("/api/audit/export?format=json&limit=1001").status_code, 422
        )

    def test_audit_exports_enforce_role_and_event_audience(self) -> None:
        self.client.post(
            "/api/users",
            json={
                "email": "admin2@example.com",
                "password": test_admin_api.PASSWORD,
                "role": "admin",
            },
        )
        self.client.post(
            "/api/users",
            json={"email": "member@example.com", "password": test_admin_api.PASSWORD},
        )
        self.client.post(
            "/api/teams", json={"name": "Payments", "reason": "Team access"}
        )
        self.fixture.login("admin2@example.com")
        result = self.client.get("/api/audit/export?format=json")
        self.assertEqual(result.status_code, 200, result.text)
        self.assertTrue(result.json()["items"])
        self.assertTrue(
            all(not event["owner_only"] for event in result.json()["items"])
        )
        self.fixture.login("member@example.com")
        self.assertEqual(
            self.client.get("/api/audit/export?format=otlp").status_code, 403
        )

    def test_repository_approval_is_atomic_idempotent_and_requires_platform_admin(
        self,
    ) -> None:
        from unittest.mock import Mock, patch
        from review_agent_tools.github import app_inventory
        from review_agent_tools.postgres import github_app

        team = self.client.post(
            "/api/teams", json={"name": "Payments", "reason": "Onboard"}
        ).json()
        other = self.client.post(
            "/api/teams", json={"name": "Other", "reason": "Onboard"}
        ).json()
        self.client.post(
            "/api/users",
            json={
                "email": "maintainer@example.com",
                "password": test_admin_api.PASSWORD,
            },
        )
        self.client.put(
            f"/api/teams/{team['id']}/members",
            json={
                "email": "maintainer@example.com",
                "role": "maintainer",
                "reason": "Team owner",
            },
        )
        self.fixture.login("maintainer@example.com")
        body = {
            "repository": "https://github.com/example/payments",
            "reason": "Review payment changes",
        }
        response = self.client.post(
            f"/api/teams/{team['id']}/repository-requests", json=body
        )
        self.assertEqual(response.status_code, 201, response.text)
        request = response.json()
        self.assertEqual(request["repository_name"], "example/payments")
        self.assertEqual(
            self.client.post(
                f"/api/teams/{team['id']}/repository-requests", json=body
            ).json()["id"],
            request["id"],
        )
        self.assertEqual(
            self.client.post(
                f"/api/repository-requests/{request['id']}/approve",
                json={"reason": "Self approval"},
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                f"/api/repository-requests/{request['id']}/reject",
                json={"reason": "Self rejection"},
            ).status_code,
            403,
        )
        inventory = app_inventory.RepositoryInventory(
            app_inventory.InstallationMetadata(
                github_app.InstallationDefinition(
                    provider_installation_id=7001,
                    account_id=8001,
                    account_login="example",
                    account_type=github_app.AccountType.ORGANIZATION,
                    repository_selection=github_app.RepositorySelection.SELECTED,
                    contents_permission=github_app.PermissionLevel.READ,
                    issues_permission=github_app.PermissionLevel.WRITE,
                    pull_requests_permission=github_app.PermissionLevel.WRITE,
                ),
                github_app.InstallationStatus.ACTIVE,
            ),
            github_app.InstallationRepositoryDefinition(9001, "example/payments"),
        )
        self.fixture.login()
        with (
            patch(
                "review_agent_tools.operator_setup.github_app_authenticator",
                return_value=Mock(),
            ),
            patch.object(
                app_inventory, "read_repository_inventory", return_value=inventory
            ) as verify,
        ):
            url = f"/api/repository-requests/{request['id']}/approve"
            approved = self.client.post(
                url,
                json={
                    "reason": "Ownership and grant checked",
                    "profile": "default-standard",
                },
            )
            self.assertEqual(approved.status_code, 200, approved.text)
            self.assertEqual(approved.json()["status"], "approved")
            self.assertEqual(
                self.client.post(url, json={"reason": "Retry after disconnect"}).json(),
                approved.json(),
            )
            verify.assert_called_once()
            repos = self.client.get(f"/api/teams/{team['id']}/repositories").json()[
                "items"
            ]
            self.assertEqual(
                [(repo["repository"], repo["enabled"]) for repo in repos],
                [("example/payments", True)],
            )
            competing = self.client.post(
                f"/api/teams/{other['id']}/repository-requests", json=body
            ).json()
            conflict = self.client.post(
                f"/api/repository-requests/{competing['id']}/approve",
                json={"reason": "Competing ownership", "profile": "other-profile"},
            )
            self.assertEqual(conflict.status_code, 409, conflict.text)
            self.assertEqual(
                self.client.get(f"/api/teams/{team['id']}/repositories").json()[
                    "items"
                ][0]["profile"],
                "default-standard",
            )
            self.assertEqual(
                self.client.get(f"/api/teams/{other['id']}/repositories").json()[
                    "items"
                ],
                [],
            )
            self.assertEqual(
                self.client.get("/api/repository-requests?status=pending").json()[
                    "pending"
                ],
                1,
            )
        events = self.client.get(f"/api/teams/{team['id']}/events").json()["items"]
        self.assertEqual(
            sum(event["action"] == "request_approved" for event in events), 1
        )
        with psycopg.connect(DSN) as connection, connection.transaction():
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM review_agent.github_app_repository_access_events WHERE event_kind = 'enabled'"
                ).fetchone()[0],
                1,
            )

    def test_approval_preserves_a_grant_revoked_during_provider_verification(
        self,
    ) -> None:
        from unittest.mock import Mock, patch
        from review_agent_tools.github import app_inventory
        from review_agent_tools.postgres import github_app

        team = self.client.post(
            "/api/teams", json={"name": "Payments", "reason": "Onboard"}
        ).json()
        request = self.client.post(
            f"/api/teams/{team['id']}/repository-requests",
            json={"repository": "example/payments", "reason": "Review changes"},
        ).json()
        definition = github_app.InstallationDefinition(
            provider_installation_id=7001,
            account_id=8001,
            account_login="example",
            account_type=github_app.AccountType.ORGANIZATION,
            repository_selection=github_app.RepositorySelection.SELECTED,
            contents_permission=github_app.PermissionLevel.READ,
            issues_permission=github_app.PermissionLevel.WRITE,
            pull_requests_permission=github_app.PermissionLevel.WRITE,
        )
        inventory = app_inventory.RepositoryInventory(
            app_inventory.InstallationMetadata(
                definition, github_app.InstallationStatus.ACTIVE
            ),
            github_app.InstallationRepositoryDefinition(9001, "example/payments"),
        )
        with psycopg.connect(DSN) as connection, connection.transaction():
            installation = github_app.sync_installation(connection, definition)
            granted = github_app.grant_repository_access(
                connection,
                installation_id=installation.id,
                provider_repository_id=9001,
                full_name="example/payments",
                actor="test:bootstrap",
                reason="Existing grant",
            )

        def revoked_during_verification(*args, **kwargs):
            with psycopg.connect(DSN) as connection, connection.transaction():
                github_app.remove_repository_access(
                    connection,
                    repository_id=granted.repository_id,
                    actor="webhook:removal",
                    reason="GitHub removed the grant",
                )
            return inventory

        with (
            patch(
                "review_agent_tools.operator_setup.github_app_authenticator",
                return_value=Mock(),
            ),
            patch.object(
                app_inventory,
                "read_repository_inventory",
                side_effect=revoked_during_verification,
            ),
        ):
            response = self.client.post(
                f"/api/repository-requests/{request['id']}/approve",
                json={"reason": "Verify and enable"},
            )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(
            self.client.get(f"/api/teams/{team['id']}/repositories").json()["items"], []
        )
        self.assertEqual(
            self.client.get("/api/repository-requests?status=pending").json()[
                "pending"
            ],
            1,
        )
        with psycopg.connect(DSN) as connection, connection.transaction():
            current = github_app.get_repository_access(
                connection, granted.repository_id
            )
        self.assertFalse(current.enabled)
        self.assertEqual(current.access_state, github_app.RepositoryAccess.REMOVED)

    def test_maintainer_run_actions_are_scoped_and_use_the_authenticated_actor(
        self,
    ) -> None:
        from tests import test_admin_run_actions

        owner_cookie = self.client.cookies.get("__Host-review_agent_session")
        owner_headers = {"Cookie": f"__Host-review_agent_session={owner_cookie}"}
        team = self.client.post(
            "/api/teams",
            json={"name": "Review operations", "reason": "Own review work"},
        ).json()
        user = self.client.post(
            "/api/users",
            json={
                "email": "maintainer@example.com",
                "password": test_admin_api.PASSWORD,
            },
        ).json()
        self.client.put(
            f"/api/teams/{team['id']}/members",
            json={
                "email": user["email"],
                "role": "viewer",
                "reason": "Initial read access",
            },
        )
        helper = test_admin_run_actions.AdminRunActionTests("runTest")
        helper.runtime = PostgreSQLRuntime(PostgresDatabaseUrl(DSN))
        helper.runtime.open()
        self.addCleanup(helper.runtime.close)
        own, foreign = (helper.queued_job(suffix=index) for index in (1, 2))
        with helper.runtime.transaction() as connection:
            repository_id = connection.execute(
                "SELECT pr.repository_id FROM review_agent.pull_requests pr JOIN review_agent.review_runs run ON run.pull_request_id = pr.id WHERE run.id = %s",
                (own.review_run_id,),
            ).fetchone()[0]
        self.assertEqual(
            self.client.put(
                f"/api/repository-ownership/{repository_id}",
                json={
                    "team_id": team["id"],
                    "expected_team_id": None,
                    "reason": "Team responsibility",
                },
            ).status_code,
            204,
        )
        self.fixture.login(user["email"])
        self.assertFalse(
            self.client.get(f"/api/history/{own.review_run_id}").json()["can_maintain"]
        )
        self.assertEqual(
            self.client.get(f"/api/history/{own.review_run_id}/controls").status_code,
            403,
        )
        self.client.put(
            f"/api/teams/{team['id']}/members",
            json={
                "email": user["email"],
                "role": "maintainer",
                "reason": "Review responsibility",
            },
            headers=owner_headers,
        )
        self.assertTrue(
            self.client.get(f"/api/history/{own.review_run_id}").json()["can_maintain"]
        )
        controls = self.client.get(f"/api/history/{own.review_run_id}/controls")
        self.assertEqual(controls.status_code, 200, controls.text)
        self.assertFalse(controls.json()["actions"]["mark_stalled"]["available"])
        self.assertEqual(
            self.client.get(
                f"/api/history/{foreign.review_run_id}/controls"
            ).status_code,
            404,
        )
        for job, expected in ((foreign, 404), (own, 200)):
            response = self.client.post(
                f"/api/history/{job.review_run_id}/actions",
                json={
                    "action": "cancel",
                    "expected_job_id": int(job.id),
                    "expected_lease_generation": job.lease_generation,
                    "expected_status": job.status.value,
                    "expected_available_at": job.available_at.isoformat(),
                    "reason": "No longer needed",
                },
            )
            self.assertEqual(response.status_code, expected, response.text)
        self.assertEqual(
            self.client.get(f"/api/teams/{team['id']}/events").status_code, 403
        )
        self.fixture.login()
        events = self.client.get(f"/api/teams/{team['id']}/events").json()["items"]
        action = next(event for event in events if event["action"] == "run_action")
        self.assertEqual(action["actor_id"], user["id"])
        self.assertEqual(action["actor_email"], user["email"])
        self.assertEqual(action["actor_role"], "team-maintainer")

    def test_console_pool_can_serve_concurrent_page_queries(self) -> None:
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from review_agent_tools.postgres.runtime import PostgreSQLRuntimeRole

        runtime = PostgreSQLRuntime(
            PostgresDatabaseUrl(DSN), role=PostgreSQLRuntimeRole.OPERATOR
        )
        runtime.open()
        self.addCleanup(runtime.close)
        started = Barrier(4, timeout=2)

        def query() -> int:
            with runtime.transaction() as connection:
                started.wait()
                return connection.execute(
                    "SELECT count(*) FROM review_agent.teams"
                ).fetchone()[0]

        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda _: query(), range(4)))
        self.assertEqual(results, [0, 0, 0, 0])
        self.assertLessEqual(runtime.pool_metrics().maximum_size, 4)
