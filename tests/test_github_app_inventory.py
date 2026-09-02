from __future__ import annotations

from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import sys
from typing import cast
import unittest
from unittest.mock import ANY, Mock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "bootstrap" / "plugins"))

from review_agent_tools import operator_application  # noqa: E402
from review_agent_tools.github import app_auth, app_inventory  # noqa: E402
from review_agent_tools.postgres import github_app  # noqa: E402
from review_agent_tools.postgres.runtime import (  # noqa: E402
    PostgreSQLRuntime,
    PostgreSQLUnavailable,
)
from tools import review_agent_admin as admin_cli  # noqa: E402


NOW = datetime(2026, 8, 26, 9, tzinfo=timezone.utc)


class _Authenticator:
    def __init__(self, *, selection: str = "selected", total: int = 101) -> None:
        self.selection = selection
        self.total = total
        self.app_paths: list[str] = []
        self.installation_paths: list[str] = []
        self.token_requests: list[tuple[int, dict[str, str] | None]] = []
        self.token = app_auth.InstallationToken(
            value="installation-token",
            expires_at=datetime(2026, 8, 26, 10, tzinfo=timezone.utc),
        )

    def app_json(self, path: str, *, now: datetime | None = None) -> object:
        self.app_paths.append(path)
        return {
            "id": 7001,
            "account": {"id": 8001, "login": "CCimen", "type": "User"},
            "repository_selection": self.selection,
            "permissions": {
                "contents": "read",
                "issues": "write",
                "pull_requests": "write",
            },
            "suspended_at": None,
        }

    def installation_token(
        self,
        provider_installation_id: int,
        *,
        permissions: dict[str, str] | None = None,
        now: datetime | None = None,
    ) -> app_auth.InstallationToken:
        self.token_requests.append((provider_installation_id, permissions))
        return self.token

    def installation_json(
        self, path: str, token: app_auth.InstallationToken
    ) -> object:
        self.assert_token(token)
        self.installation_paths.append(path)
        page = len(self.installation_paths)
        if page == 1:
            count = min(self.total, 100)
            start = 1
        else:
            count = self.total - 100
            start = 101
        return {
            "total_count": self.total,
            "repositories": [
                {"id": 9000 + index, "full_name": f"CCimen/repo-{index}"}
                for index in range(start, start + count)
            ],
        }

    def assert_token(self, token: app_auth.InstallationToken) -> None:
        if token is not self.token:
            raise AssertionError("inventory did not reuse one installation token")


class GitHubAppInventoryTests(unittest.TestCase):
    def test_resolves_installation_id_from_repository_name(self) -> None:
        authenticator = _Authenticator(total=1)

        installation_id = app_inventory.installation_id_for_repository(
            cast(app_auth.GitHubAppAuthenticator, authenticator),
            repository="CCimen/review-agent",
            now=NOW,
        )

        self.assertEqual(installation_id, 7001)
        self.assertEqual(
            authenticator.app_paths,
            ["/repos/CCimen/review-agent/installation"],
        )

    def test_rejects_invalid_repository_installation_identity(self) -> None:
        class InvalidAuthenticator(_Authenticator):
            def app_json(
                self, path: str, *, now: datetime | None = None
            ) -> object:
                self.app_paths.append(path)
                return {"id": False}

        with self.assertRaisesRegex(
            app_inventory.GitHubAppInventoryPermanent,
            "installation id",
        ):
            app_inventory.installation_id_for_repository(
                cast(app_auth.GitHubAppAuthenticator, InvalidAuthenticator()),
                repository="CCimen/review-agent",
                now=NOW,
            )

    def test_reads_selected_installation_and_every_repository_page(self) -> None:
        authenticator = _Authenticator()

        inventory = app_inventory.read_installation_inventory(
            cast(app_auth.GitHubAppAuthenticator, authenticator),
            provider_installation_id=7001,
            now=NOW,
        )

        self.assertEqual(inventory.definition.provider_installation_id, 7001)
        self.assertEqual(
            inventory.definition.repository_selection,
            github_app.RepositorySelection.SELECTED,
        )
        self.assertEqual(inventory.status, github_app.InstallationStatus.ACTIVE)
        self.assertEqual(len(inventory.repositories), 101)
        self.assertEqual(inventory.repositories[0].provider_repository_id, 9001)
        self.assertEqual(inventory.repositories[-1].full_name, "CCimen/repo-101")
        self.assertEqual(authenticator.app_paths, ["/app/installations/7001"])
        self.assertEqual(
            authenticator.token_requests,
            [(7001, {"metadata": "read"})],
        )
        self.assertEqual(
            authenticator.installation_paths,
            [
                "/installation/repositories?per_page=100&page=1",
                "/installation/repositories?per_page=100&page=2",
            ],
        )

    def test_all_repository_mode_is_explicitly_unsupported(self) -> None:
        authenticator = _Authenticator(selection="all")

        with self.assertRaisesRegex(
            app_inventory.GitHubAppInventoryUnsupported, "selected"
        ):
            app_inventory.read_installation_inventory(
                cast(app_auth.GitHubAppAuthenticator, authenticator),
                provider_installation_id=7001,
                now=NOW,
            )

        self.assertEqual(authenticator.token_requests, [])
        self.assertEqual(authenticator.installation_paths, [])

    def test_partial_or_duplicate_repository_inventory_is_rejected(self) -> None:
        for total, expected_error in (
            (99, app_inventory.GitHubAppInventoryRetryable),
            (101, app_inventory.GitHubAppInventoryPermanent),
        ):
            with self.subTest(total=total):
                class IncompleteAuthenticator(_Authenticator):
                    def installation_json(
                        self, path: str, token: app_auth.InstallationToken
                    ) -> object:
                        result = super().installation_json(path, token)
                        assert isinstance(result, dict)
                        repositories = result["repositories"]
                        assert isinstance(repositories, list)
                        if total == 99 and len(self.installation_paths) == 1:
                            repositories.pop()
                        elif total == 101 and len(self.installation_paths) == 2:
                            repositories[0] = {
                                "id": 9001,
                                "full_name": "CCimen/other",
                            }
                        return result

                authenticator = IncompleteAuthenticator(total=total)
                with self.assertRaises(expected_error):
                    app_inventory.read_installation_inventory(
                        cast(app_auth.GitHubAppAuthenticator, authenticator),
                        provider_installation_id=7001,
                        now=NOW,
                    )

    def test_operator_fetches_provider_inventory_before_database_transaction(
        self,
    ) -> None:
        events: list[str] = []
        inventory = app_inventory.InstallationInventory(
            definition=github_app.InstallationDefinition(
                provider_installation_id=7001,
                account_id=8001,
                account_login="CCimen",
                account_type=github_app.AccountType.USER,
                repository_selection=github_app.RepositorySelection.SELECTED,
                contents_permission=github_app.PermissionLevel.READ,
                issues_permission=github_app.PermissionLevel.WRITE,
                pull_requests_permission=github_app.PermissionLevel.WRITE,
            ),
            status=github_app.InstallationStatus.ACTIVE,
            repositories=(
                github_app.InstallationRepositoryDefinition(
                    9001, "CCimen/review-agent"
                ),
            ),
        )
        installation = Mock(
            provider_installation_id=7001,
            status=github_app.InstallationStatus.ACTIVE,
        )
        expected = github_app.InstallationReconciliationResult(
            installation=cast(github_app.GitHubAppInstallation, installation),
            repositories_seen=1,
            repositories_removed=0,
            repositories_enabled=0,
        )

        class Runtime:
            @contextmanager
            def transaction(self):
                events.append("database")
                yield object()

        def read_inventory(
            *_args: object, **_kwargs: object
        ) -> app_inventory.InstallationInventory:
            events.append("provider")
            return inventory

        with (
            patch.object(
                app_inventory,
                "read_installation_inventory",
                side_effect=read_inventory,
            ),
            patch.object(
                github_app, "reconcile_selected_installation", return_value=expected
            ) as reconcile,
        ):
            result = operator_application.sync_github_app_installation(
                cast(PostgreSQLRuntime, Runtime()),
                cast(app_auth.GitHubAppAuthenticator, object()),
                provider_installation_id=7001,
                actor="operator:ccimen",
                reason="repair inventory",
                now=NOW,
            )

        self.assertIs(result, expected)
        self.assertEqual(events, ["provider", "database"])
        repositories = reconcile.call_args.kwargs["repositories"]
        self.assertEqual(
            repositories,
            (
                github_app.InstallationRepositoryDefinition(
                    provider_repository_id=9001,
                    full_name="CCimen/review-agent",
                ),
            ),
        )

    def test_onboard_enables_named_repository_from_the_selected_installation(self) -> None:
        reconciliation = Mock(installation=Mock(id=7))
        current = Mock(installation_id=7, repository_id=11)
        enabled = Mock()

        class Runtime:
            @contextmanager
            def transaction(self):
                yield object()

        with (
            patch.object(
                app_inventory,
                "installation_id_for_repository",
                return_value=7001,
            ) as resolve_installation,
            patch.object(
                operator_application,
                "sync_github_app_installation",
                return_value=reconciliation,
            ) as sync,
            patch.object(
                github_app,
                "get_repository_access_by_full_name",
                return_value=current,
            ) as get_access,
            patch.object(github_app, "enable_repository", return_value=enabled) as enable,
        ):
            result = operator_application.onboard_github_app_repository(
                cast(PostgreSQLRuntime, Runtime()),
                cast(app_auth.GitHubAppAuthenticator, object()),
                repository="CCimen/review-agent",
                profile="default-standard",
                actor="github:CCimen",
                reason="approved repository onboarding",
            )

        self.assertIs(result.reconciliation, reconciliation)
        self.assertIs(result.access, enabled)
        resolve_installation.assert_called_once_with(
            ANY,
            repository="CCimen/review-agent",
        )
        sync.assert_called_once_with(
            ANY,
            ANY,
            provider_installation_id=7001,
            actor="github:CCimen",
            reason="approved repository onboarding",
        )
        get_access.assert_called_once_with(ANY, "CCimen/review-agent")
        enable.assert_called_once_with(
            ANY,
            repository_id=11,
            profile_key="default-standard",
            trigger_mode=github_app.TriggerMode.MANUAL,
            actor="github:CCimen",
            reason="approved repository onboarding",
        )

    def test_onboard_refuses_a_repository_from_another_installation(self) -> None:
        reconciliation = Mock(installation=Mock(id=7))
        current = Mock(installation_id=8)

        class Runtime:
            @contextmanager
            def transaction(self):
                yield object()

        with (
            patch.object(
                app_inventory,
                "installation_id_for_repository",
                return_value=7001,
            ),
            patch.object(
                operator_application,
                "sync_github_app_installation",
                return_value=reconciliation,
            ),
            patch.object(
                github_app,
                "get_repository_access_by_full_name",
                return_value=current,
            ),
            self.assertRaises(operator_application.OperatorInputError),
        ):
            operator_application.onboard_github_app_repository(
                cast(PostgreSQLRuntime, Runtime()),
                cast(app_auth.GitHubAppAuthenticator, object()),
                repository="CCimen/review-agent",
                profile="default-standard",
                actor="github:CCimen",
                reason="approved repository onboarding",
            )

    def test_cli_requires_sync_identity_before_loading_runtime(self) -> None:
        stderr = io.StringIO()
        with (
            redirect_stderr(stderr),
            self.assertRaises(SystemExit) as raised,
        ):
            admin_cli.main(
                [
                    "installations",
                    "sync",
                    "7001",
                    "--reason",
                    "repair inventory",
                ]
            )

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("required: --actor", stderr.getvalue())

        stderr = io.StringIO()
        with (
            redirect_stderr(stderr),
            self.assertRaises(SystemExit) as raised,
        ):
            admin_cli.main(
                [
                    "installations",
                    "sync",
                    "0",
                    "--actor",
                    "operator:ccimen",
                    "--reason",
                    "repair inventory",
                ]
            )
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("must be positive", stderr.getvalue())

    def test_cli_reports_bounded_success_and_distinct_failure_classes(self) -> None:
        installation = Mock(
            provider_installation_id=7001,
            status=github_app.InstallationStatus.ACTIVE,
        )
        success = github_app.InstallationReconciliationResult(
            installation=cast(github_app.GitHubAppInstallation, installation),
            repositories_seen=3,
            repositories_removed=1,
            repositories_enabled=1,
        )
        arguments = [
            "installations",
            "sync",
            "7001",
            "--actor",
            "operator:ccimen",
            "--reason",
            "repair inventory",
        ]

        def run(
            outcome: object,
            *,
            open_error: PostgreSQLUnavailable | None = None,
        ) -> tuple[int, str, str]:
            runtime = Mock()
            runtime.open.side_effect = open_error
            stdout = io.StringIO()
            stderr = io.StringIO()
            side_effect = outcome if isinstance(outcome, Exception) else None
            return_value = None if side_effect is not None else outcome
            with (
                patch.object(admin_cli, "_runtime", side_effect=open_error or None, return_value=runtime),
                patch.object(
                    admin_cli.operator_setup,
                    "github_app_authenticator",
                    return_value=object(),
                ),
                patch.object(
                    operator_application,
                    "sync_github_app_installation",
                    side_effect=side_effect,
                    return_value=return_value,
                ),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                result = admin_cli.main(arguments)
            if open_error is None:
                runtime.close.assert_called_once()
            else:
                runtime.close.assert_not_called()
            return result, stdout.getvalue(), stderr.getvalue()

        code, stdout, stderr = run(success)
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(
            set(json.loads(stdout)),
            {
                "installation_status",
                "provider_installation_id",
                "repositories_enabled",
                "repositories_removed",
                "repositories_seen",
            },
        )
        self.assertNotIn("private PEM", stdout)

        code, stdout, stderr = run(
            app_auth.GitHubAppTokenRetryable("provider unavailable")
        )
        self.assertEqual((code, stdout), (os.EX_TEMPFAIL, ""))
        error = json.loads(stderr)["error"]
        self.assertTrue(error["retryable"])
        self.assertNotIn("provider unavailable", stderr)

        code, stdout, stderr = run(
            success,
            open_error=PostgreSQLUnavailable("host=secret.internal port=5432"),
        )
        self.assertEqual((code, stdout), (os.EX_TEMPFAIL, ""))
        error = json.loads(stderr)["error"]
        self.assertEqual(error["code"], "database_unavailable")
        self.assertTrue(error["retryable"])
        self.assertNotIn("secret.internal", stderr)

        code, stdout, stderr = run(
            github_app.GitHubAppStateError("installation conflict")
        )
        self.assertEqual((code, stdout), (1, ""))
        error = json.loads(stderr)["error"]
        self.assertEqual(error["code"], "installation_sync_failed")
        self.assertFalse(error["retryable"])


if __name__ == "__main__":
    unittest.main()
