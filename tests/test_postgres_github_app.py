from __future__ import annotations

import os
import sys
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psycopg


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = ROOT / "bootstrap" / "plugins"
sys.path.insert(0, str(PACKAGE_ROOT))

from review_agent_tools.postgres import github_app  # noqa: E402
from review_agent_tools.postgres_migrations import runner  # noqa: E402


DSN = os.environ.get("REVIEW_AGENT_POSTGRES_DSN", "")


@unittest.skipUnless(DSN, "run through scripts/check_postgres_schema.sh")
class PostgreSQLGitHubAppTests(unittest.TestCase):
    def setUp(self) -> None:
        with psycopg.connect(DSN, autocommit=True) as connection:
            connection.execute("DROP SCHEMA IF EXISTS review_agent CASCADE")
        with psycopg.connect(DSN) as connection:
            runner.apply_migrations(connection)

    def installation(self) -> github_app.GitHubAppInstallation:
        with psycopg.connect(DSN) as connection:
            with connection.transaction():
                return github_app.sync_installation(
                    connection,
                    github_app.InstallationDefinition(
                        provider_installation_id=7001,
                        account_id=8001,
                        account_login="CCimen",
                        account_type=github_app.AccountType.USER,
                        repository_selection=github_app.RepositorySelection.SELECTED,
                        contents_permission=github_app.PermissionLevel.READ,
                        issues_permission=github_app.PermissionLevel.WRITE,
                        pull_requests_permission=github_app.PermissionLevel.WRITE,
                    ),
                )

    def test_selected_repository_starts_available_and_disabled(self) -> None:
        installation = self.installation()

        with psycopg.connect(DSN) as connection:
            with connection.transaction():
                access = github_app.grant_repository_access(
                    connection,
                    installation_id=installation.id,
                    provider_repository_id=9001,
                    full_name="CCimen/review-agent",
                    actor="github-app:installation_repositories",
                    reason="repository selected during pilot installation",
                )
                repeated = github_app.grant_repository_access(
                    connection,
                    installation_id=installation.id,
                    provider_repository_id=9001,
                    full_name="CCimen/review-agent",
                    actor="github-app:installation_repositories",
                    reason="redelivered repository selection",
                )
                events = github_app.list_repository_access_events(
                    connection, access.repository_id
                )

        self.assertEqual(access.access_state, github_app.RepositoryAccess.AVAILABLE)
        self.assertFalse(access.enabled)
        self.assertEqual(repeated, access)
        self.assertEqual(access.trigger_mode, github_app.TriggerMode.MANUAL)
        self.assertIsNone(access.profile_key)
        self.assertEqual(events[0].event_kind, github_app.AccessEvent.GRANTED)
        self.assertFalse(events[0].enabled)

    def test_review_read_authorization_requires_current_enabled_access(self) -> None:
        installation = self.installation()
        with psycopg.connect(DSN) as connection:
            with connection.transaction():
                access = github_app.grant_repository_access(
                    connection,
                    installation_id=installation.id,
                    provider_repository_id=9001,
                    full_name="CCimen/review-agent",
                    actor="github-app:installation_repositories",
                    reason="repository selected",
                )
                with self.assertRaises(github_app.GitHubAppReviewReadUnauthorized):
                    github_app.authorize_review_read(connection, 9001)
                github_app.enable_repository(
                    connection,
                    repository_id=access.repository_id,
                    profile_key="sundsvall-standard",
                    trigger_mode=github_app.TriggerMode.MANUAL,
                    actor="operator:ccimen",
                    reason="approve pilot",
                )
                authorization = github_app.authorize_review_read(
                    connection, 9001
                )
                github_app.disable_repository(
                    connection,
                    repository_id=access.repository_id,
                    actor="operator:ccimen",
                    reason="pause reviews",
                )
                with self.assertRaises(github_app.GitHubAppReviewReadUnauthorized):
                    github_app.authorize_review_read(connection, 9001)

        self.assertEqual(authorization.repository_id, access.repository_id)
        self.assertEqual(authorization.provider_repository_id, 9001)
        self.assertEqual(authorization.provider_installation_id, 7001)

    def test_final_review_authorization_locks_exact_installation_and_profile(self) -> None:
        installation = self.installation()
        with psycopg.connect(DSN) as connection:
            with connection.transaction():
                access = github_app.grant_repository_access(
                    connection,
                    installation_id=installation.id,
                    provider_repository_id=9001,
                    full_name="CCimen/review-agent",
                    actor="github-app:installation_repositories",
                    reason="repository selected",
                )
                github_app.enable_repository(
                    connection,
                    repository_id=access.repository_id,
                    profile_key="sundsvall-standard",
                    trigger_mode=github_app.TriggerMode.MANUAL,
                    actor="operator:ccimen",
                    reason="approve pilot",
                )
                authorized = github_app.authorize_review_admission(
                    connection,
                    provider_repository_id=9001,
                    provider_installation_id=7001,
                    profile_key="sundsvall-standard",
                )
                with self.assertRaises(
                    github_app.GitHubAppReviewReadUnauthorized
                ):
                    github_app.authorize_review_admission(
                        connection,
                        provider_repository_id=9001,
                        provider_installation_id=7001,
                        profile_key="other-profile",
                    )

        self.assertEqual(authorized.repository_id, access.repository_id)
        self.assertEqual(authorized.full_name, "CCimen/review-agent")

    def test_stale_repository_removal_cannot_fence_new_installation(self) -> None:
        original = self.installation()
        with psycopg.connect(DSN) as connection:
            with connection.transaction():
                first = github_app.grant_repository_access(
                    connection,
                    installation_id=original.id,
                    provider_repository_id=9001,
                    full_name="CCimen/review-agent",
                    actor="github-app:installation_repositories",
                    reason="first installation",
                )
                replacement = github_app.sync_installation(
                    connection,
                    github_app.InstallationDefinition(
                        provider_installation_id=7002,
                        account_id=8001,
                        account_login="CCimen",
                        account_type=github_app.AccountType.USER,
                        repository_selection=github_app.RepositorySelection.SELECTED,
                        contents_permission=github_app.PermissionLevel.READ,
                        issues_permission=github_app.PermissionLevel.WRITE,
                        pull_requests_permission=github_app.PermissionLevel.WRITE,
                    ),
                )
                current = github_app.grant_repository_access(
                    connection,
                    installation_id=replacement.id,
                    provider_repository_id=9001,
                    full_name="CCimen/review-agent",
                    actor="github-app:installation_repositories",
                    reason="replacement installation",
                )
                stale = github_app.remove_repository_access_for_installation(
                    connection,
                    provider_repository_id=9001,
                    expected_provider_installation_id=7001,
                    actor="github-app:installation_repositories",
                    reason="late removal",
                )
                stored = github_app.get_repository_access(
                    connection, current.repository_id
                )
                stale_add = github_app.grant_repository_access(
                    connection,
                    installation_id=original.id,
                    provider_repository_id=9001,
                    full_name="CCimen/review-agent",
                    actor="github-app:installation_repositories",
                    reason="late addition",
                )

        self.assertEqual(first.repository_id, current.repository_id)
        self.assertIsNone(stale)
        self.assertEqual(stored.installation_id, replacement.id)
        self.assertEqual(stored.access_state, github_app.RepositoryAccess.AVAILABLE)
        self.assertEqual(stale_add.installation_id, replacement.id)

    def test_suspension_fences_an_enabled_repository_until_reenabled(self) -> None:
        installation = self.installation()
        with psycopg.connect(DSN) as connection:
            with connection.transaction():
                access = github_app.grant_repository_access(
                    connection,
                    installation_id=installation.id,
                    provider_repository_id=9001,
                    full_name="CCimen/review-agent",
                    actor="github-app:installation_repositories",
                    reason="repository selected",
                )
                enabled = github_app.enable_repository(
                    connection,
                    repository_id=access.repository_id,
                    profile_key="sundsvall-standard",
                    trigger_mode=github_app.TriggerMode.MANUAL,
                    actor="operator:ccimen",
                    reason="approve personal-account pilot",
                )
                suspended = github_app.set_installation_status(
                    connection,
                    installation_id=installation.id,
                    status=github_app.InstallationStatus.SUSPENDED,
                    actor="github-app:installation",
                    reason="installation suspended by GitHub",
                )
                fenced = github_app.get_repository_access(
                    connection, enabled.repository_id
                )
                with self.assertRaisesRegex(
                    github_app.GitHubAppStateError,
                    "repository access is not available",
                ):
                    github_app.enable_repository(
                        connection,
                        repository_id=enabled.repository_id,
                        profile_key="sundsvall-standard",
                        trigger_mode=github_app.TriggerMode.MANUAL,
                        actor="operator:ccimen",
                        reason="must remain fenced",
                    )

        self.assertEqual(suspended.status, github_app.InstallationStatus.SUSPENDED)
        self.assertEqual(
            fenced.access_state, github_app.RepositoryAccess.INSTALLATION_SUSPENDED
        )
        self.assertFalse(fenced.enabled)
        self.assertIsNotNone(fenced.disabled_at)

        with psycopg.connect(DSN) as connection:
            with connection.transaction():
                github_app.set_installation_status(
                    connection,
                    installation_id=installation.id,
                    status=github_app.InstallationStatus.ACTIVE,
                    actor="github-app:installation",
                    reason="installation unsuspended by GitHub",
                )
                restored = github_app.get_repository_access(
                    connection, enabled.repository_id
                )
                installation_events = github_app.list_installation_events(
                    connection, installation.id
                )

        self.assertEqual(restored.access_state, github_app.RepositoryAccess.AVAILABLE)
        self.assertFalse(restored.enabled)
        self.assertEqual(
            tuple(
                (event.previous_status, event.status)
                for event in installation_events
            ),
            (
                (
                    github_app.InstallationStatus.ACTIVE,
                    github_app.InstallationStatus.SUSPENDED,
                ),
                (
                    github_app.InstallationStatus.SUSPENDED,
                    github_app.InstallationStatus.ACTIVE,
                ),
            ),
        )

    def test_enabled_repository_can_be_disabled_and_reenabled(self) -> None:
        installation = self.installation()
        with psycopg.connect(DSN) as connection:
            with connection.transaction():
                access = github_app.grant_repository_access(
                    connection,
                    installation_id=installation.id,
                    provider_repository_id=9001,
                    full_name="CCimen/review-agent",
                    actor="github-app:installation_repositories",
                    reason="repository selected",
                )
                github_app.enable_repository(
                    connection,
                    repository_id=access.repository_id,
                    profile_key="sundsvall-standard",
                    trigger_mode=github_app.TriggerMode.MANUAL,
                    actor="operator:ccimen",
                    reason="approve pilot",
                )
                disabled = github_app.disable_repository(
                    connection,
                    repository_id=access.repository_id,
                    actor="operator:ccimen",
                    reason="pause reviews",
                )
                repeated = github_app.disable_repository(
                    connection,
                    repository_id=access.repository_id,
                    actor="operator:ccimen",
                    reason="redelivered operator request",
                )
                reenabled = github_app.enable_repository(
                    connection,
                    repository_id=access.repository_id,
                    profile_key="sundsvall-standard",
                    trigger_mode=github_app.TriggerMode.MANUAL,
                    actor="operator:ccimen",
                    reason="resume reviews",
                )
                events = github_app.list_repository_access_events(
                    connection, access.repository_id
                )

        self.assertFalse(disabled.enabled)
        self.assertIsNotNone(disabled.disabled_at)
        self.assertEqual(repeated, disabled)
        self.assertTrue(reenabled.enabled)
        self.assertIsNone(reenabled.disabled_at)
        self.assertEqual(
            tuple(event.event_kind for event in events),
            (
                github_app.AccessEvent.GRANTED,
                github_app.AccessEvent.ENABLED,
                github_app.AccessEvent.DISABLED,
                github_app.AccessEvent.ENABLED,
            ),
        )

    def test_unavailable_repository_cannot_be_enabled(self) -> None:
        installation = self.installation()
        with psycopg.connect(DSN) as connection:
            with connection.transaction():
                access = github_app.grant_repository_access(
                    connection,
                    installation_id=installation.id,
                    provider_repository_id=9001,
                    full_name="CCimen/review-agent",
                    actor="github-app:installation_repositories",
                    reason="repository selected",
                )
                github_app.remove_repository_access(
                    connection,
                    repository_id=access.repository_id,
                    actor="github-app:installation_repositories",
                    reason="repository removed from App installation",
                )
                github_app.remove_repository_access(
                    connection,
                    repository_id=access.repository_id,
                    actor="github-app:installation_repositories",
                    reason="redelivered repository removal",
                )
                github_app.set_installation_status(
                    connection,
                    installation_id=installation.id,
                    status=github_app.InstallationStatus.SUSPENDED,
                    actor="github-app:installation",
                    reason="installation suspended",
                )
                github_app.set_installation_status(
                    connection,
                    installation_id=installation.id,
                    status=github_app.InstallationStatus.ACTIVE,
                    actor="github-app:installation",
                    reason="installation restored",
                )
                still_removed = github_app.get_repository_access(
                    connection, access.repository_id
                )
                events = github_app.list_repository_access_events(
                    connection, access.repository_id
                )
                with self.assertRaisesRegex(
                    github_app.GitHubAppStateError,
                    "repository access is not available",
                ):
                    github_app.enable_repository(
                        connection,
                        repository_id=access.repository_id,
                        profile_key="sundsvall-standard",
                        trigger_mode=github_app.TriggerMode.MANUAL,
                        actor="operator:ccimen",
                        reason="must fail closed",
                    )

        self.assertEqual(
            still_removed.access_state, github_app.RepositoryAccess.REMOVED
        )
        self.assertEqual(
            tuple(event.event_kind for event in events),
            (github_app.AccessEvent.GRANTED, github_app.AccessEvent.REMOVED),
        )

    def test_deletion_preserves_when_installation_was_suspended(self) -> None:
        installation = self.installation()
        with psycopg.connect(DSN) as connection:
            with connection.transaction():
                suspended = github_app.set_installation_status(
                    connection,
                    installation_id=installation.id,
                    status=github_app.InstallationStatus.SUSPENDED,
                    actor="github-app:installation",
                    reason="installation suspended",
                )
                deleted = github_app.set_installation_status(
                    connection,
                    installation_id=installation.id,
                    status=github_app.InstallationStatus.DELETED,
                    actor="github-app:installation",
                    reason="installation deleted",
                )

        self.assertIsNotNone(suspended.suspended_at)
        self.assertEqual(deleted.suspended_at, suspended.suspended_at)
        self.assertIsNotNone(deleted.deleted_at)

    def test_repository_rename_retains_access_by_provider_id(self) -> None:
        installation = self.installation()
        with psycopg.connect(DSN) as connection:
            with connection.transaction():
                original = github_app.grant_repository_access(
                    connection,
                    installation_id=installation.id,
                    provider_repository_id=9001,
                    full_name="CCimen/review-agent",
                    actor="github-app:installation_repositories",
                    reason="repository selected",
                )
                enabled = github_app.enable_repository(
                    connection,
                    repository_id=original.repository_id,
                    profile_key="sundsvall-standard",
                    trigger_mode=github_app.TriggerMode.MANUAL,
                    actor="operator:ccimen",
                    reason="approve pilot",
                )
                renamed = github_app.grant_repository_access(
                    connection,
                    installation_id=installation.id,
                    provider_repository_id=9001,
                    full_name="CCimen/review-agent-renamed",
                    actor="github-app:repository",
                    reason="repository renamed",
                )

        self.assertEqual(renamed.repository_id, original.repository_id)
        self.assertEqual(renamed.full_name, "CCimen/review-agent-renamed")
        self.assertEqual(renamed.access_state, github_app.RepositoryAccess.AVAILABLE)
        self.assertTrue(renamed.enabled)
        self.assertEqual(renamed.enabled_at, enabled.enabled_at)

    def test_operator_lookup_resolves_stable_provider_repository_id(self) -> None:
        installation = self.installation()
        with psycopg.connect(DSN) as connection:
            with connection.transaction():
                access = github_app.grant_repository_access(
                    connection,
                    installation_id=installation.id,
                    provider_repository_id=9001,
                    full_name="CCimen/review-agent",
                    actor="github-app:installation_repositories",
                    reason="repository selected",
                )
                resolved = github_app.get_repository_access_by_provider_id(
                    connection, 9001
                )

        self.assertEqual(resolved, access)

    def test_concurrent_suspend_cannot_overwrite_terminal_deletion(self) -> None:
        installation = self.installation()
        with psycopg.connect(DSN) as connection:
            with connection.transaction():
                access = github_app.grant_repository_access(
                    connection,
                    installation_id=installation.id,
                    provider_repository_id=9001,
                    full_name="CCimen/review-agent",
                    actor="github-app:installation_repositories",
                    reason="repository selected",
                )

        def suspend() -> github_app.GitHubAppInstallation:
            with psycopg.connect(
                DSN, application_name="github-app-transition-race"
            ) as connection:
                with connection.transaction():
                    return github_app.set_installation_status(
                        connection,
                        installation_id=installation.id,
                        status=github_app.InstallationStatus.SUSPENDED,
                        actor="github-app:installation",
                        reason="concurrent stale suspension",
                    )

        with ThreadPoolExecutor(max_workers=1) as executor:
            with psycopg.connect(DSN) as delete_connection:
                with delete_connection.transaction():
                    delete_connection.execute(
                        """
                        SELECT id
                        FROM review_agent.github_app_installations
                        WHERE id = %s
                        FOR UPDATE
                        """,
                        (installation.id,),
                    )
                    future = executor.submit(suspend)
                    deadline = time.monotonic() + 5
                    with psycopg.connect(DSN, autocommit=True) as observer:
                        while time.monotonic() < deadline:
                            waiting = observer.execute(
                                """
                                SELECT wait_event_type = 'Lock'
                                FROM pg_stat_activity
                                WHERE application_name = 'github-app-transition-race'
                                """
                            ).fetchone()
                            if waiting == (True,):
                                break
                            time.sleep(0.02)
                        else:
                            self.fail("concurrent transition did not wait on the lock")
                    deleted = github_app.set_installation_status(
                        delete_connection,
                        installation_id=installation.id,
                        status=github_app.InstallationStatus.DELETED,
                        actor="github-app:installation",
                        reason="installation deleted",
                    )

            self.assertEqual(deleted.status, github_app.InstallationStatus.DELETED)
            with self.assertRaisesRegex(
                github_app.GitHubAppStateError,
                "deleted installation cannot transition",
            ):
                future.result(timeout=5)

        with psycopg.connect(DSN) as connection:
            with connection.transaction():
                stored = github_app.get_installation(connection, installation.id)
                fenced = github_app.get_repository_access(
                    connection, access.repository_id
                )
        self.assertEqual(stored.status, github_app.InstallationStatus.DELETED)
        self.assertEqual(
            fenced.access_state, github_app.RepositoryAccess.INSTALLATION_DELETED
        )

    def test_concurrent_enable_fails_cleanly_after_repository_removal(self) -> None:
        installation = self.installation()
        with psycopg.connect(DSN) as connection:
            with connection.transaction():
                access = github_app.grant_repository_access(
                    connection,
                    installation_id=installation.id,
                    provider_repository_id=9001,
                    full_name="CCimen/review-agent",
                    actor="github-app:installation_repositories",
                    reason="repository selected",
                )

        def enable() -> github_app.RepositoryAccessState:
            with psycopg.connect(
                DSN, application_name="github-app-enable-race"
            ) as connection:
                with connection.transaction():
                    return github_app.enable_repository(
                        connection,
                        repository_id=access.repository_id,
                        profile_key="sundsvall-standard",
                        trigger_mode=github_app.TriggerMode.MANUAL,
                        actor="operator:ccimen",
                        reason="concurrent enable",
                    )

        with ThreadPoolExecutor(max_workers=1) as executor:
            with psycopg.connect(DSN) as remove_connection:
                with remove_connection.transaction():
                    remove_connection.execute(
                        """
                        SELECT repository_id
                        FROM review_agent.github_app_repository_access
                        WHERE repository_id = %s
                        FOR UPDATE
                        """,
                        (access.repository_id,),
                    )
                    future = executor.submit(enable)
                    deadline = time.monotonic() + 5
                    with psycopg.connect(DSN, autocommit=True) as observer:
                        while time.monotonic() < deadline:
                            waiting = observer.execute(
                                """
                                SELECT wait_event_type = 'Lock'
                                FROM pg_stat_activity
                                WHERE application_name = 'github-app-enable-race'
                                """
                            ).fetchone()
                            if waiting == (True,):
                                break
                            time.sleep(0.02)
                        else:
                            self.fail("concurrent enable did not wait on the lock")
                    github_app.remove_repository_access(
                        remove_connection,
                        repository_id=access.repository_id,
                        actor="github-app:installation_repositories",
                        reason="repository removed",
                    )

            with self.assertRaisesRegex(
                github_app.GitHubAppStateError,
                "repository access is not available",
            ):
                future.result(timeout=5)


if __name__ == "__main__":
    unittest.main()
