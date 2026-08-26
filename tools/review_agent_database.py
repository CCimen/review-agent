#!/usr/bin/env python3
"""Apply and verify the Review Agent PostgreSQL schema."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys


def _load_package() -> None:
    candidates = (
        Path("/opt/review-agent-bootstrap/plugins"),
        Path(os.environ.get("HERMES_HOME", "/opt/data")) / "plugins",
        Path(__file__).resolve().parents[1] / "bootstrap" / "plugins",
    )
    for candidate in candidates:
        if (candidate / "review_agent_tools" / "postgres_migrations").is_dir():
            sys.path.insert(0, str(candidate))
            return
    raise SystemExit("Could not locate the review_agent_tools package")


_load_package()

import psycopg  # noqa: E402

from review_agent_tools import operator_application  # noqa: E402
from review_agent_tools.github import app_auth, app_inventory  # noqa: E402
from review_agent_tools.postgres import github_app, jobs, registry  # noqa: E402
from review_agent_tools.postgres.runtime import (  # noqa: E402
    PostgreSQLRuntime,
    PostgreSQLRuntimeError,
    PostgreSQLRuntimeRole,
)
from review_agent_tools.postgres_migrations import runner  # noqa: E402
from review_agent_tools.settings import ReviewAgentSettings  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=(
            "migrate",
            "ready",
            "jobs",
            "retry-job",
            "cancel-job",
            "enable-github-app-repository",
            "disable-github-app-repository",
            "sync-github-app-installation",
        ),
    )
    parser.add_argument("--job-id", type=int)
    parser.add_argument("--provider-repository-id", type=int)
    parser.add_argument("--provider-installation-id", type=int)
    parser.add_argument("--profile")
    parser.add_argument("--actor")
    parser.add_argument("--reason")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument(
        "--status",
        action="append",
        choices=tuple(status.value for status in jobs.ReviewJobStatus),
    )
    args = parser.parse_args(argv)
    if args.command == "sync-github-app-installation":
        for option, value in (
            ("--provider-installation-id", args.provider_installation_id),
            ("--actor", args.actor),
            ("--reason", args.reason),
        ):
            if value is None:
                parser.error(f"{option} is required for {args.command}")
        if args.provider_installation_id < 1:
            parser.error("--provider-installation-id must be positive")
    database_url = ReviewAgentSettings.from_environment().postgres_database_url
    if args.command == "migrate":
        with psycopg.connect(database_url) as connection:
            applied = runner.apply_migrations(connection)
            status = runner.inspect_migrations(connection)
        applied_text = (
            ",".join(str(version) for version in applied) if applied else "none"
        )
        print(
            "PostgreSQL schema ready: "
            f"migration={status.applied_version} applied={applied_text}."
        )
        return 0
    runtime = PostgreSQLRuntime(database_url, role=PostgreSQLRuntimeRole.OPERATOR)
    try:
        runtime.open()
    except PostgreSQLRuntimeError:
        if args.command != "sync-github-app-installation":
            raise
        print(
            "GitHub App installation sync is retryable: database unavailable",
            file=sys.stderr,
        )
        return os.EX_TEMPFAIL
    try:
        if args.command == "ready":
            readiness = runtime.readiness()
            print(
                "PostgreSQL ready: "
                f"server={readiness.server_version} "
                f"migration={readiness.applied_migration_version}."
            )
            return 0
        if args.command == "jobs":
            statuses = (
                tuple(jobs.ReviewJobStatus(value) for value in args.status)
                if args.status
                else operator_application.ACTIVE_JOB_STATUSES
            )
            reports = operator_application.list_review_jobs(
                runtime, statuses=statuses, limit=args.limit
            )
            print(
                json.dumps(
                    [
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
                    ],
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
            return 0
        if args.command in {
            "enable-github-app-repository",
            "disable-github-app-repository",
        }:
            for option, value in (
                ("--provider-repository-id", args.provider_repository_id),
                ("--actor", args.actor),
                ("--reason", args.reason),
            ):
                if value is None:
                    parser.error(f"{option} is required for {args.command}")
            if args.command == "enable-github-app-repository" and args.profile is None:
                parser.error(f"--profile is required for {args.command}")
            if args.command == "enable-github-app-repository":
                access = operator_application.enable_github_app_repository(
                    runtime,
                    provider_repository_id=args.provider_repository_id,
                    profile=args.profile,
                    actor=args.actor,
                    reason=args.reason,
                )
            else:
                access = operator_application.disable_github_app_repository(
                    runtime,
                    provider_repository_id=args.provider_repository_id,
                    actor=args.actor,
                    reason=args.reason,
                )
            print(
                json.dumps(
                    {
                        "enabled": access.enabled,
                        "profile": access.profile_key,
                        "provider_repository_id": access.provider_repository_id,
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "sync-github-app-installation":
            raw_app_id = os.environ.get("REVIEW_AGENT_GITHUB_APP_ID", "").strip()
            raw_key_path = os.environ.get(
                "REVIEW_AGENT_GITHUB_APP_PRIVATE_KEY_FILE", ""
            ).strip()
            try:
                app_id = int(raw_app_id)
                if app_id < 1:
                    raise ValueError
            except ValueError:
                parser.error("REVIEW_AGENT_GITHUB_APP_ID must be a positive integer")
            if not raw_key_path:
                parser.error(
                    "REVIEW_AGENT_GITHUB_APP_PRIVATE_KEY_FILE is required"
                )
            try:
                authenticator = app_auth.GitHubAppAuthenticator(
                    app_id=app_id,
                    private_key_pem=app_auth.load_private_key_file(raw_key_path),
                )
                result = operator_application.sync_github_app_installation(
                    runtime,
                    authenticator,
                    provider_installation_id=args.provider_installation_id,
                    actor=args.actor,
                    reason=args.reason,
                )
            except (
                app_auth.GitHubAppTokenRetryable,
                app_inventory.GitHubAppInventoryRetryable,
            ) as exc:
                print(f"GitHub App installation sync is retryable: {exc}", file=sys.stderr)
                return os.EX_TEMPFAIL
            except (
                app_auth.GitHubAppConfigurationError,
                app_auth.GitHubAppTokenPermanent,
                app_inventory.GitHubAppInventoryPermanent,
                github_app.GitHubAppStateError,
                operator_application.OperatorInputError,
                registry.RegistryError,
            ) as exc:
                print(f"GitHub App installation sync failed: {exc}", file=sys.stderr)
                return 1
            except (PostgreSQLRuntimeError, psycopg.Error):
                print(
                    "GitHub App installation sync is retryable: database unavailable",
                    file=sys.stderr,
                )
                return os.EX_TEMPFAIL
            print(
                json.dumps(
                    {
                        "installation_status": result.installation.status.value,
                        "provider_installation_id": (
                            result.installation.provider_installation_id
                        ),
                        "repositories_enabled": result.repositories_enabled,
                        "repositories_removed": result.repositories_removed,
                        "repositories_seen": result.repositories_seen,
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
            return 0
        if args.job_id is None:
            parser.error(f"--job-id is required for {args.command}")
        job = (
            operator_application.retry_review_job(runtime, job_id=args.job_id)
            if args.command == "retry-job"
            else operator_application.cancel_review_job(runtime, job_id=args.job_id)
        )
        print(
            json.dumps(
                {
                    "job_id": job.id,
                    "run_id": int(job.review_run_id),
                    "status": job.status.value,
                },
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        return 0
    finally:
        runtime.close()


if __name__ == "__main__":
    raise SystemExit(main())
