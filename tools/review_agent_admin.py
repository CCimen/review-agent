#!/usr/bin/env python3
"""Operate and diagnose one Review Agent deployment."""

from __future__ import annotations

import argparse
from collections.abc import Callable
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.request

import psycopg


def _load_package() -> None:
    candidates = (
        Path("/opt/review-agent-bootstrap/plugins"),
        Path(os.environ.get("HERMES_HOME", "/opt/data")) / "plugins",
        Path(__file__).resolve().parents[1] / "bootstrap" / "plugins",
    )
    for candidate in candidates:
        if (candidate / "review_agent_tools" / "operator_setup.py").is_file():
            sys.path.insert(0, str(candidate))
            return
    raise SystemExit("Could not locate the review_agent_tools package")


_load_package()

from review_agent_tools import operator_application, operator_setup  # noqa: E402
from review_agent_tools.github import app_auth, app_inventory  # noqa: E402
from review_agent_tools.github.gateway import (  # noqa: E402
    GitHubGatewayError,
    GitHubGatewayRetryable,
)
from review_agent_tools.github.gateway_client import (  # noqa: E402
    ReviewGitHubGatewayClient,
)
from review_agent_tools.postgres import github_app, jobs, registry  # noqa: E402
from review_agent_tools.postgres.runtime import (  # noqa: E402
    PostgreSQLRuntime,
    PostgreSQLRuntimeError,
    PostgreSQLRuntimeRole,
)
from review_agent_tools.settings import ReviewAgentSettings  # noqa: E402
from review_agent_tools.postgres_migrations import runner  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser(
        "capabilities", help="Describe shipped product behavior."
    )

    commands.add_parser(
        "preflight", help="Validate local configuration without side effects."
    )

    commands.add_parser(
        "doctor", help="Run read-only checks against a deployed instance."
    )

    smoke = commands.add_parser(
        "smoke-test", help="Prove one enabled pull request without writing."
    )
    smoke.add_argument("--dry-run", action="store_true", required=True)
    smoke.add_argument("--repository", required=True)
    smoke.add_argument("--pr", type=_positive_argument, required=True)

    database = commands.add_parser("database", help="Manage PostgreSQL readiness.")
    database_commands = database.add_subparsers(
        dest="database_command", required=True
    )
    for command, help_text in (
        ("migrate", "Apply pending PostgreSQL migrations."),
        ("ready", "Verify PostgreSQL and migration readiness."),
    ):
        database_commands.add_parser(command, help=help_text)

    job_parser = commands.add_parser("jobs", help="Inspect or recover review jobs.")
    job_commands = job_parser.add_subparsers(dest="job_command", required=True)
    job_list = job_commands.add_parser("list", help="List a bounded job snapshot.")
    job_list.add_argument("--limit", type=_positive_argument)
    job_list.add_argument(
        "--status",
        action="append",
        choices=tuple(status.value for status in jobs.ReviewJobStatus),
    )
    for command in ("retry", "cancel"):
        job_change = job_commands.add_parser(command, help=f"{command.title()} one job.")
        job_change.add_argument("job_id", type=_positive_argument)

    queues = commands.add_parser(
        "queues", help="Inspect review and publication recovery state."
    )
    queue_commands = queues.add_subparsers(dest="queue_command", required=True)
    queue_commands.add_parser(
        "inspect", help="Read bounded queue health counters."
    )

    github_app = commands.add_parser("github-app", help="Prepare GitHub App setup.")
    github_app_commands = github_app.add_subparsers(
        dest="github_app_command", required=True
    )
    registration = github_app_commands.add_parser(
        "registration-url", help="Build a prefilled GitHub App registration URL."
    )
    registration.add_argument("--owner", required=True)
    registration.add_argument(
        "--owner-type", choices=("user", "organization"), default="user"
    )
    registration.add_argument("--public-url", required=True)
    registration.add_argument("--name")
    registration.add_argument("--json", action="store_true")

    installations = commands.add_parser(
        "installations", help="Inspect or reconcile GitHub App installations."
    )
    installation_commands = installations.add_subparsers(
        dest="installation_command", required=True
    )
    installation_list = installation_commands.add_parser(
        "list", help="List durable installations."
    )
    installation_list.add_argument("--limit", type=_positive_argument)
    installation_list.add_argument("--after-id", type=_nonnegative_argument, default=0)
    installation_sync = installation_commands.add_parser(
        "sync", help="Reconcile one selected-repository installation."
    )
    installation_sync.add_argument("installation_id", type=_positive_argument)
    installation_sync.add_argument("--actor", required=True)
    installation_sync.add_argument("--reason", required=True)

    repositories = commands.add_parser(
        "repositories", help="Inspect or change repository enablement."
    )
    repository_commands = repositories.add_subparsers(
        dest="repository_command", required=True
    )
    repository_list = repository_commands.add_parser(
        "list", help="List durable repository access."
    )
    repository_list.add_argument("--limit", type=_positive_argument)
    repository_list.add_argument("--after-id", type=_nonnegative_argument, default=0)
    repository_enable = repository_commands.add_parser(
        "enable", help="Enable manual reviews for one available repository."
    )
    repository_enable.add_argument("repository_id", type=_positive_argument)
    repository_enable.add_argument("--profile", required=True)
    repository_enable.add_argument("--actor", required=True)
    repository_enable.add_argument("--reason", required=True)
    repository_disable = repository_commands.add_parser(
        "disable", help="Disable reviews for one repository."
    )
    repository_disable.add_argument("repository_id", type=_positive_argument)
    repository_disable.add_argument("--actor", required=True)
    repository_disable.add_argument("--reason", required=True)
    return parser


def _json(value: object) -> None:
    print(json.dumps(value, separators=(",", ":"), sort_keys=True))


def _positive_argument(value: str) -> int:
    try:
        resolved = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if resolved < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return resolved


def _nonnegative_argument(value: str) -> int:
    try:
        resolved = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if resolved < 0:
        raise argparse.ArgumentTypeError("must be nonnegative")
    return resolved


def _page_limit(value: int | None) -> int:
    maximum = ReviewAgentSettings.from_environment().operator_page_max_items
    if value is None:
        return maximum
    if value > maximum:
        raise ValueError(
            f"limit exceeds REVIEW_AGENT_OPERATOR_PAGE_MAX_ITEMS ({maximum})"
        )
    return value


def _json_error(*, code: str, retryable: bool) -> None:
    print(
        json.dumps(
            {"error": {"code": code, "retryable": retryable}},
            separators=(",", ":"),
            sort_keys=True,
        ),
        file=sys.stderr,
    )


def _runtime() -> PostgreSQLRuntime:
    runtime = PostgreSQLRuntime(
        ReviewAgentSettings.from_environment().postgres_database_url,
        role=PostgreSQLRuntimeRole.OPERATOR,
    )
    runtime.open()
    return runtime


def _operator_client() -> ReviewGitHubGatewayClient:
    settings = ReviewAgentSettings.from_environment()
    return ReviewGitHubGatewayClient(
        settings.github_gateway_url,
        operator_key=os.environ.get("API_SERVER_KEY", ""),
    )


def _hermes_probe() -> bool:
    url = ReviewAgentSettings.from_environment().hermes_health_url
    request = urllib.request.Request(
        url,
        method="GET",
        headers={"Accept": "application/json", "User-Agent": "Review-Agent-Operator/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            body = response.read(4_097)
    except (urllib.error.URLError, TimeoutError):
        return False
    return len(body) <= 4_096 and 200 <= response.status < 300


def _installation_json(
    installation: github_app.GitHubAppInstallation,
) -> dict[str, object]:
    return {
        "account": installation.account_login,
        "account_type": installation.account_type.value,
        "contents_permission": installation.contents_permission.value,
        "installation_id": installation.provider_installation_id,
        "issues_permission": installation.issues_permission.value,
        "pull_requests_permission": installation.pull_requests_permission.value,
        "repository_selection": installation.repository_selection.value,
        "status": installation.status.value,
    }


def _repository_json(
    repository: github_app.RepositoryAccessState,
) -> dict[str, object]:
    return {
        "access": repository.access_state.value,
        "enabled": repository.enabled,
        "profile": repository.profile_key,
        "repository": repository.full_name,
        "repository_id": repository.provider_repository_id,
        "trigger_mode": repository.trigger_mode.value,
    }


def _installation_inventory(
    runtime: PostgreSQLRuntime, *, limit: int, after_id: int
) -> int:
    installations = operator_application.list_github_app_installations(
        runtime,
        limit=limit,
        after_provider_installation_id=after_id,
    )
    _json(
        {
            "installations": [_installation_json(item) for item in installations],
            "next_after_id": (
                installations[-1].provider_installation_id
                if len(installations) == limit
                else None
            ),
        }
    )
    return 0


def _repository_inventory(
    runtime: PostgreSQLRuntime, *, limit: int, after_id: int
) -> int:
    repositories = operator_application.list_github_app_repositories(
        runtime,
        limit=limit,
        after_provider_repository_id=after_id,
    )
    _json(
        {
            "repositories": [
                _repository_json(item) for item in repositories
            ],
            "next_after_id": (
                repositories[-1].provider_repository_id
                if len(repositories) == limit
                else None
            ),
        }
    )
    return 0


def _queue_inventory(runtime: PostgreSQLRuntime) -> int:
    snapshot = operator_application.queue_health(runtime)
    _json(
        {
            "publications": {
                "expired_exhausted": snapshot.publication_queue.expired_exhausted,
                "expired_recoverable": snapshot.publication_queue.expired_recoverable,
                "pending": snapshot.publication_queue.pending,
                "posting": snapshot.publication_queue.posting,
            },
            "reviews": {
                "active": snapshot.review_queue.active,
                "dead_letters": snapshot.review_queue.dead_letters,
                "expired_leases": snapshot.review_queue.expired_leases,
                "leased": snapshot.review_queue.leased,
                "queued": snapshot.review_queue.queued,
            },
        }
    )
    return 0


def _read_inventory(operation: Callable[[PostgreSQLRuntime], int]) -> int:
    runtime: PostgreSQLRuntime | None = None
    try:
        runtime = _runtime()
        return operation(runtime)
    except (PostgreSQLRuntimeError, psycopg.Error):
        _json_error(code="database_unavailable", retryable=True)
        return os.EX_TEMPFAIL
    finally:
        if runtime is not None:
            runtime.close()


def _doctor() -> int:
    runtime: PostgreSQLRuntime | None = None
    try:
        runtime = _runtime()
        report = operator_setup.doctor(
            os.environ,
            runtime=runtime,
            gateway=_operator_client(),
            hermes_probe=_hermes_probe,
        )
    except (PostgreSQLRuntimeError, psycopg.Error):
        _json_error(code="doctor_unavailable", retryable=True)
        return os.EX_TEMPFAIL
    except (GitHubGatewayError, ValueError):
        _json_error(code="doctor_failed", retryable=False)
        return 1
    finally:
        if runtime is not None:
            runtime.close()
    _json(report.to_json_obj())
    return 0 if report.ready else 1


def _smoke_test(args: argparse.Namespace) -> int:
    runtime: PostgreSQLRuntime | None = None
    try:
        runtime = _runtime()
        report = operator_setup.smoke_test(
            os.environ,
            runtime=runtime,
            gateway=_operator_client(),
            repository=args.repository,
            pr_number=args.pr,
        )
    except (
        GitHubGatewayRetryable,
        operator_setup.OperatorCapacityUnavailable,
        PostgreSQLRuntimeError,
        psycopg.Error,
    ):
        _json_error(code="smoke_test_unavailable", retryable=True)
        return os.EX_TEMPFAIL
    except (GitHubGatewayError, ValueError):
        _json_error(code="smoke_test_failed", retryable=False)
        return 1
    finally:
        if runtime is not None:
            runtime.close()
    _json(report.to_json_obj())
    return 0


def _repository_change(args: argparse.Namespace) -> int:
    try:
        runtime = _runtime()
    except (PostgreSQLRuntimeError, psycopg.Error):
        _json_error(code="database_unavailable", retryable=True)
        return os.EX_TEMPFAIL
    try:
        if args.repository_command == "enable":
            access = operator_application.enable_github_app_repository(
                runtime,
                provider_repository_id=args.repository_id,
                profile=args.profile,
                actor=args.actor,
                reason=args.reason,
            )
        else:
            access = operator_application.disable_github_app_repository(
                runtime,
                provider_repository_id=args.repository_id,
                actor=args.actor,
                reason=args.reason,
            )
    except (PostgreSQLRuntimeError, psycopg.OperationalError):
        _json_error(code="database_unavailable", retryable=True)
        return os.EX_TEMPFAIL
    except (
        github_app.GitHubAppStateError,
        operator_application.OperatorInputError,
        registry.RegistryError,
        psycopg.Error,
        ValueError,
    ):
        _json_error(code="repository_change_failed", retryable=False)
        return 1
    finally:
        runtime.close()
    _json(
        {
            "access": access.access_state.value,
            "enabled": access.enabled,
            "profile": access.profile_key,
            "repository": access.full_name,
            "repository_id": access.provider_repository_id,
            "trigger_mode": access.trigger_mode.value,
        }
    )
    return 0


def _database_command(args: argparse.Namespace) -> int:
    try:
        database_url = ReviewAgentSettings.from_environment().postgres_database_url
        if args.database_command == "migrate":
            with psycopg.connect(database_url) as connection:
                applied = runner.apply_migrations(connection)
                readiness = runner.inspect_migrations(connection)
            _json(
                {
                    "applied": list(applied),
                    "migration": readiness.applied_version,
                    "ready": not readiness.pending_versions,
                }
            )
            return 0
        runtime = PostgreSQLRuntime(
            database_url, role=PostgreSQLRuntimeRole.OPERATOR
        )
        readiness = runtime.open()
    except (PostgreSQLRuntimeError, psycopg.Error, ValueError):
        _json_error(code="database_unavailable", retryable=True)
        return os.EX_TEMPFAIL
    try:
        _json(
            {
                "database_ahead": readiness.database_ahead,
                "migration": readiness.applied_migration_version,
                "ready": True,
                "server_version": readiness.server_version,
            }
        )
        return 0
    finally:
        runtime.close()


def _job_command(args: argparse.Namespace) -> int:
    try:
        runtime = _runtime()
    except (PostgreSQLRuntimeError, psycopg.Error):
        _json_error(code="database_unavailable", retryable=True)
        return os.EX_TEMPFAIL
    try:
        if args.job_command == "list":
            statuses = (
                tuple(jobs.ReviewJobStatus(value) for value in args.status)
                if args.status
                else operator_application.ACTIVE_JOB_STATUSES
            )
            reports = operator_application.list_review_jobs(
                runtime, statuses=statuses, limit=_page_limit(args.limit)
            )
            _json(
                {
                    "jobs": [
                        {
                            "attempt_count": report.job.attempt_count,
                            "available_at": report.job.available_at.isoformat(),
                            "job_id": report.job.id,
                            "max_attempts": report.job.max_attempts,
                            "pr_number": report.pr_number,
                            "repository": report.repository,
                            "run_id": int(report.job.review_run_id),
                            "status": report.job.status.value,
                        }
                        for report in reports
                    ]
                }
            )
            return 0
        job = (
            operator_application.retry_review_job(runtime, job_id=args.job_id)
            if args.job_command == "retry"
            else operator_application.cancel_review_job(runtime, job_id=args.job_id)
        )
        _json(
            {
                "job_id": job.id,
                "run_id": int(job.review_run_id),
                "status": job.status.value,
            }
        )
        return 0
    except (PostgreSQLRuntimeError, psycopg.OperationalError):
        _json_error(code="database_unavailable", retryable=True)
        return os.EX_TEMPFAIL
    except (
        jobs.ReviewJobError,
        operator_application.OperatorInputError,
        psycopg.Error,
        ValueError,
    ):
        _json_error(code="job_operation_failed", retryable=False)
        return 1
    finally:
        runtime.close()


def _sync_installation(args: argparse.Namespace) -> int:
    try:
        runtime = _runtime()
    except (PostgreSQLRuntimeError, psycopg.Error):
        _json_error(code="database_unavailable", retryable=True)
        return os.EX_TEMPFAIL
    try:
        authenticator = operator_setup.github_app_authenticator(os.environ)
        result = operator_application.sync_github_app_installation(
            runtime,
            authenticator,
            provider_installation_id=args.installation_id,
            actor=args.actor,
            reason=args.reason,
        )
    except (
        app_auth.GitHubAppTokenRetryable,
        app_inventory.GitHubAppInventoryRetryable,
        PostgreSQLRuntimeError,
        psycopg.OperationalError,
    ):
        _json_error(code="installation_sync_unavailable", retryable=True)
        return os.EX_TEMPFAIL
    except (
        app_auth.GitHubAppConfigurationError,
        app_auth.GitHubAppTokenPermanent,
        app_inventory.GitHubAppInventoryPermanent,
        github_app.GitHubAppStateError,
        operator_application.OperatorInputError,
        registry.RegistryError,
        psycopg.Error,
        ValueError,
    ):
        _json_error(code="installation_sync_failed", retryable=False)
        return 1
    finally:
        runtime.close()
    _json(
        {
            "installation_status": result.installation.status.value,
            "provider_installation_id": result.installation.provider_installation_id,
            "repositories_enabled": result.repositories_enabled,
            "repositories_removed": result.repositories_removed,
            "repositories_seen": result.repositories_seen,
        }
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "capabilities":
        _json(operator_setup.capabilities().to_json_obj())
        return 0
    if args.command == "preflight":
        report = operator_setup.preflight(os.environ)
        _json(report.to_json_obj())
        return 0 if report.ready else 1
    if args.command == "doctor":
        return _doctor()
    if args.command == "smoke-test":
        return _smoke_test(args)
    if args.command == "database":
        return _database_command(args)
    if args.command == "jobs":
        return _job_command(args)
    if args.command == "queues" and args.queue_command == "inspect":
        return _read_inventory(_queue_inventory)
    if args.command == "github-app" and args.github_app_command == "registration-url":
        url = operator_setup.github_app_registration_url(
            owner=args.owner,
            owner_type=args.owner_type,
            public_url=args.public_url,
            app_name=args.name,
        )
        if args.json:
            _json({"registration_url": url})
        else:
            print(url)
        return 0
    if args.command == "installations" and args.installation_command == "list":
        try:
            limit = _page_limit(args.limit)
        except ValueError:
            _json_error(code="invalid_page_limit", retryable=False)
            return 1
        return _read_inventory(
            lambda runtime: _installation_inventory(
                runtime,
                limit=limit,
                after_id=args.after_id,
            )
        )
    if args.command == "installations" and args.installation_command == "sync":
        return _sync_installation(args)
    if args.command == "repositories" and args.repository_command == "list":
        try:
            limit = _page_limit(args.limit)
        except ValueError:
            _json_error(code="invalid_page_limit", retryable=False)
            return 1
        return _read_inventory(
            lambda runtime: _repository_inventory(
                runtime,
                limit=limit,
                after_id=args.after_id,
            )
        )
    if args.command == "repositories" and args.repository_command in {
        "enable",
        "disable",
    }:
        return _repository_change(args)
    parser.error("unsupported command")


if __name__ == "__main__":
    raise SystemExit(main())
