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
from review_agent_tools.postgres import jobs  # noqa: E402
from review_agent_tools.postgres.runtime import (  # noqa: E402
    PostgreSQLRuntime,
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
        ),
    )
    parser.add_argument("--job-id", type=int)
    parser.add_argument("--provider-repository-id", type=int)
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
    runtime.open()
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
