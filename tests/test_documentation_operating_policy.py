from __future__ import annotations

import os
import unittest

import psycopg

from tests import test_admin_api
from review_agent_tools.domain.documentation_operating_policy import (
    DocumentationMode,
    resolve_mode,
)
from review_agent_tools.postgres import registry


DSN = os.environ.get("REVIEW_AGENT_POSTGRES_DSN", "")


class DocumentationModeTests(unittest.TestCase):
    def test_manual_fallback_explicit_override_and_platform_stop(self) -> None:
        manual = resolve_mode(
            deployment_enabled=True, team_default=None, repository_override=None
        )
        self.assertEqual(manual.effective_mode, DocumentationMode.MANUAL)
        self.assertEqual(manual.source, "unassigned")
        override = resolve_mode(
            deployment_enabled=True,
            team_default=DocumentationMode.OFF,
            repository_override=DocumentationMode.AUTOMATIC,
        )
        self.assertEqual(override.effective_mode, DocumentationMode.AUTOMATIC)
        paused = resolve_mode(
            deployment_enabled=False,
            team_default=DocumentationMode.OFF,
            repository_override=DocumentationMode.AUTOMATIC,
        )
        self.assertEqual(paused.configured_mode, DocumentationMode.AUTOMATIC)
        self.assertEqual(paused.effective_mode, DocumentationMode.OFF)


@unittest.skipUnless(DSN, "requires an isolated PostgreSQL test database")
class DocumentationPolicyAPITests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = test_admin_api.AdminAPITests("runTest")
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.fixture.login()
        self.client = self.fixture.client
        self.team = self.client.post(
            "/api/teams", json={"name": "Platform", "reason": "Create"}
        ).json()["id"]
        self.other = self.client.post(
            "/api/teams", json={"name": "Payments", "reason": "Create"}
        ).json()["id"]
        with psycopg.connect(DSN) as connection, connection.transaction():
            self.repositories = [
                int(
                    registry.ensure_repository(
                        connection,
                        registry.RepositoryDefinition(
                            provider="github",
                            provider_repository_id=930 + index,
                            full_name=f"example/repo-{index}",
                        ),
                    ).id
                )
                for index in range(3)
            ]
        for repository_id in self.repositories[:2]:
            response = self.client.put(
                f"/api/repository-ownership/{repository_id}",
                json={
                    "team_id": self.team,
                    "expected_team_id": None,
                    "reason": "Assign",
                },
            )
            self.assertEqual(response.status_code, 204, response.text)

    def repository(self, repository_id: int):
        response = self.client.get(f"/api/repositories/{repository_id}/documentation")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def team_preview(self, mode: str, *, team_id: int | None = None, **params):
        response = self.client.get(
            f"/api/teams/{team_id or self.team}/documentation",
            params={"proposed_mode": mode, **params},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def save_team(self, mode: str, *, team_id: int | None = None):
        preview = self.team_preview(mode, team_id=team_id)
        response = self.client.put(
            f"/api/teams/{team_id or self.team}/documentation",
            json={"mode": mode, "expected_revision": preview["revision"]},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def save_repository(self, repository_id: int, mode: str | None):
        current = self.repository(repository_id)
        response = self.client.put(
            f"/api/repositories/{repository_id}/documentation",
            json={"mode": mode, "expected_revision": current["revision"]},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def enable_global(self, enabled: bool) -> None:
        page = self.client.get("/api/settings").json()
        response = self.client.put(
            "/api/settings",
            json={
                "settings": {
                    **page["settings"],
                    "documentation_review_enabled": enabled,
                },
                "expected_revision": page["revision"],
                "reason": "Set documentation availability",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)

    def test_inheritance_override_global_stop_and_honest_readiness(self) -> None:
        inherited_id, override_id, unassigned_id = self.repositories
        self.assertEqual(
            self.repository(inherited_id)["resolved"],
            {
                "configured_mode": "manual",
                "effective_mode": "off",
                "source": "team",
                "deployment_enabled": False,
            },
        )
        self.assertEqual(
            self.repository(unassigned_id)["resolved"]["source"], "unassigned"
        )
        self.enable_global(True)
        self.save_repository(override_id, "automatic")
        preview = self.team_preview("off")
        self.assertEqual(
            (preview["inherited_count"], preview["exception_count"]), (1, 1)
        )
        modes = {
            item["repository_id"]: item["after"]["configured_mode"]
            for item in preview["repositories"]
        }
        self.assertEqual(modes, {inherited_id: "off", override_id: "automatic"})
        self.save_team("off")
        self.assertEqual(
            self.repository(inherited_id)["resolved"]["effective_mode"], "off"
        )
        self.assertEqual(
            self.repository(override_id)["resolved"]["effective_mode"], "automatic"
        )
        self.save_repository(override_id, None)
        self.assertEqual(self.repository(override_id)["resolved"]["source"], "team")
        self.enable_global(False)
        unassigned = self.repository(unassigned_id)
        self.assertEqual(unassigned["resolved"]["configured_mode"], "manual")
        self.assertEqual(unassigned["resolved"]["effective_mode"], "off")
        self.assertEqual(
            (unassigned["capability"], unassigned["configuration"]),
            ("access_unavailable", "not_checked"),
        )
        with psycopg.connect(DSN) as connection:
            row = connection.execute(
                "SELECT details FROM review_agent.admin_audit_events WHERE subject = %s ORDER BY id DESC LIMIT 1",
                (f"repository-documentation:{override_id}",),
            ).fetchone()
        self.assertEqual(row[0]["previous_source"], "repository")
        self.assertEqual(row[0]["source"], "team")

    def test_paginated_impact_tokens_and_repository_saves_reject_stale_scope(
        self,
    ) -> None:
        first = self.team_preview("automatic", limit=1)
        second = self.team_preview(
            "automatic", limit=1, after_id=first["next_after_id"]
        )
        self.assertEqual(first["revision"], second["revision"])
        old_repository = self.repository(self.repositories[0])
        self.save_repository(self.repositories[1], "off")
        response = self.client.put(
            f"/api/teams/{self.team}/documentation",
            json={"mode": "automatic", "expected_revision": first["revision"]},
        )
        self.assertEqual(response.status_code, 409, response.text)
        response = self.client.put(
            f"/api/repositories/{self.repositories[0]}/documentation",
            json={"mode": "automatic", "expected_revision": old_repository["revision"]},
        )
        self.assertEqual(response.status_code, 409, response.text)
        fresh = self.team_preview("automatic")
        self.enable_global(True)
        response = self.client.put(
            f"/api/teams/{self.team}/documentation",
            json={"mode": "automatic", "expected_revision": fresh["revision"]},
        )
        self.assertEqual(response.status_code, 409, response.text)

    def test_viewers_other_team_members_and_former_maintainers_cannot_write(
        self,
    ) -> None:
        for email, team_id, role in (
            ("maintainer@example.com", self.team, "maintainer"),
            ("reader@example.com", self.team, "viewer"),
            ("other@example.com", self.other, "maintainer"),
        ):
            self.client.post(
                "/api/users", json={"email": email, "password": test_admin_api.PASSWORD}
            )
            self.client.put(
                f"/api/teams/{team_id}/members",
                json={"email": email, "role": role, "reason": "Grant team access"},
            )
        repository_id = self.repositories[0]
        owner_snapshot = self.repository(repository_id)
        for email, status in (("reader@example.com", 403), ("other@example.com", 404)):
            self.fixture.login(email)
            response = self.client.put(
                f"/api/repositories/{repository_id}/documentation",
                json={
                    "mode": "automatic",
                    "expected_revision": owner_snapshot["revision"],
                },
            )
            self.assertEqual(response.status_code, status, response.text)
        self.fixture.login("maintainer@example.com")
        self.save_team("manual")
        self.assertEqual(
            self.client.get(
                f"/api/repositories/{self.repositories[2]}/documentation"
            ).status_code,
            404,
        )
        self.assertEqual(self.client.get("/api/settings").status_code, 403)
        self.fixture.login()
        self.client.put(
            f"/api/repository-ownership/{repository_id}",
            json={
                "team_id": self.other,
                "expected_team_id": self.team,
                "reason": "Transfer",
            },
        )
        self.fixture.login("maintainer@example.com")
        response = self.client.put(
            f"/api/repositories/{repository_id}/documentation",
            json={"mode": "automatic", "expected_revision": owner_snapshot["revision"]},
        )
        self.assertEqual(response.status_code, 404, response.text)

    def test_waiting_former_maintainer_write_observes_committed_transfer(self) -> None:
        from concurrent.futures import ThreadPoolExecutor, TimeoutError
        from threading import Event
        from uuid import UUID
        from review_agent_tools import admin_application
        from review_agent_tools.postgres import repository_requests, team_access
        from review_agent_tools.postgres.runtime import PostgreSQLRuntime
        from review_agent_tools.settings import PostgresDatabaseUrl

        user = self.client.post(
            "/api/users",
            json={"email": "race@example.com", "password": test_admin_api.PASSWORD},
        ).json()
        self.client.put(
            f"/api/teams/{self.team}/members",
            json={
                "email": user["email"],
                "role": "maintainer",
                "reason": "Maintain team",
            },
        )
        repository_id = self.repositories[0]
        revision = self.repository(repository_id)["revision"]
        runtime = PostgreSQLRuntime(PostgresDatabaseUrl(DSN))
        runtime.open()
        self.addCleanup(runtime.close)
        entered = Event()

        def save():
            entered.set()
            return admin_application.save_repository_documentation_policy(
                runtime,
                access=team_access.AccessRequest(UUID(user["id"])),
                repository_id=repository_id,
                mode=DocumentationMode.AUTOMATIC,
                expected_revision=revision,
            )

        with ThreadPoolExecutor(max_workers=1) as executor:
            with psycopg.connect(DSN) as connection, connection.transaction():
                team_access.lock_access_change(connection)
                owner_id = connection.execute(
                    "SELECT id FROM review_agent.admin_users WHERE email = 'admin@example.com'"
                ).fetchone()[0]
                owner = team_access.resolve_scope(
                    connection, team_access.AccessRequest(owner_id)
                )
                pending = executor.submit(save)
                self.assertTrue(entered.wait(2))
                with self.assertRaises(TimeoutError):
                    pending.result(timeout=0.05)
                repository_requests.assign(
                    connection,
                    owner,
                    repository_id=repository_id,
                    team_id=self.other,
                    expected_team_id=self.team,
                    reason="Transfer",
                )
            with self.assertRaises(team_access.ResourceNotFound):
                pending.result(timeout=3)

    def test_material_ownership_change_requires_current_mode_preview(self) -> None:
        repository_id = self.repositories[0]
        self.save_team("automatic", team_id=self.other)
        url = f"/api/repository-ownership/{repository_id}"
        body = {
            "team_id": self.other,
            "expected_team_id": self.team,
            "reason": "Transfer",
        }
        self.assertEqual(self.client.put(url, json=body).status_code, 409)
        preview = self.client.get(
            url + "/preview", params={"destination_team_id": self.other}
        ).json()
        self.assertEqual(preview["after"]["configured_mode"], "automatic")
        self.save_team("off", team_id=self.other)
        self.assertEqual(
            self.client.put(
                url,
                json={**body, "expected_documentation_revision": preview["revision"]},
            ).status_code,
            409,
        )
        preview = self.client.get(
            url + "/preview", params={"destination_team_id": self.other}
        ).json()
        response = self.client.put(
            url, json={**body, "expected_documentation_revision": preview["revision"]}
        )
        self.assertEqual(response.status_code, 204, response.text)
        self.assertEqual(
            self.repository(repository_id)["resolved"]["configured_mode"], "off"
        )
        with psycopg.connect(DSN) as connection:
            rows = connection.execute(
                "SELECT details FROM review_agent.admin_audit_events WHERE action = 'repository_transferred' AND subject = %s",
                (str(repository_id),),
            ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row[0]["documentation_mode"] == "off" for row in rows))
        override_id = self.repositories[1]
        self.save_repository(override_id, "automatic")
        response = self.client.put(
            f"/api/repository-ownership/{override_id}",
            json={
                "team_id": self.other,
                "expected_team_id": self.team,
                "reason": "Transfer explicit override",
            },
        )
        self.assertEqual(response.status_code, 204, response.text)
        self.assertEqual(
            self.repository(override_id)["resolved"]["configured_mode"], "automatic"
        )
        self.assertEqual(
            self.repository(override_id)["resolved"]["source"], "repository"
        )


@unittest.skipUnless(DSN, "requires an isolated PostgreSQL test database")
class DocumentationPolicyMigrationTests(unittest.TestCase):
    def test_existing_team_and_repository_upgrade_to_manual_inheritance(self) -> None:
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from review_agent_tools.postgres_migrations import runner

        with TemporaryDirectory() as directory:
            old = Path(directory)
            for migration in runner.discover_migrations():
                if migration.version < 32:
                    (old / migration.name).write_bytes(migration.sql)
            with psycopg.connect(DSN, autocommit=True) as connection:
                connection.execute("DROP SCHEMA IF EXISTS review_agent CASCADE")
                runner.apply_migrations(connection, directory=old)
                with connection.transaction():
                    repository = registry.ensure_repository(
                        connection,
                        registry.RepositoryDefinition(
                            provider="github",
                            provider_repository_id=991,
                            full_name="example/retained",
                        ),
                    )
                    team_id = connection.execute(
                        "INSERT INTO review_agent.teams (name, description) VALUES ('Retained team', 'Existing description') RETURNING id"
                    ).fetchone()[0]
                runner.apply_migrations(connection)
                row = connection.execute(
                    "SELECT name, description, documentation_mode, documentation_revision FROM review_agent.teams WHERE id = %s",
                    (team_id,),
                ).fetchone()
                self.assertEqual(
                    row, ("Retained team", "Existing description", "manual", 1)
                )
                row = connection.execute(
                    "SELECT full_name, documentation_mode, documentation_revision FROM review_agent.repositories WHERE id = %s",
                    (repository.id,),
                ).fetchone()
                self.assertEqual(row, ("example/retained", None, 1))


if __name__ == "__main__":
    unittest.main()
