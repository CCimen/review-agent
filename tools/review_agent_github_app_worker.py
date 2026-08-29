#!/usr/bin/env python3
"""Run the opt-in direct GitHub App admission worker."""

from __future__ import annotations

import argparse
from datetime import timedelta
import logging
import os
from pathlib import Path
import signal
import sys
import threading


def _load_package() -> None:
    candidates = (
        Path("/opt/review-agent-bootstrap/plugins"),
        Path(__file__).resolve().parents[1] / "bootstrap" / "plugins",
    )
    for candidate in candidates:
        if (candidate / "review_agent_tools" / "github" / "app_worker.py").is_file():
            sys.path.insert(0, str(candidate))
            return
    raise SystemExit("Could not locate the review_agent_tools package")


_load_package()

from review_agent_tools.github.app_processor import (  # noqa: E402
    GitHubAppProcessor,
    ProcessorConfig,
)
from review_agent_tools.github.app_worker import (  # noqa: E402
    GitHubAppWorker,
    GitHubAppWorkerConfigurationError,
    GitHubAppWorkerPolicy,
    default_github_app_worker_name,
)
from review_agent_tools.github.gateway import (  # noqa: E402
    GitHubGatewayProtocolError,
)
from review_agent_tools.github.gateway_client import (  # noqa: E402
    ReviewGitHubGatewayClient,
)
from review_agent_tools import review_contract  # noqa: E402
from review_agent_tools.postgres.runtime import (  # noqa: E402
    PostgreSQLRuntime,
    PostgreSQLRuntimeRole,
)
from review_agent_tools.settings import ReviewAgentSettings  # noqa: E402


def _positive_integer(name: str, default: str | None = None) -> int:
    raw = os.environ.get(name, default or "").strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise GitHubAppWorkerConfigurationError(f"{name} must be an integer") from exc
    if value < 1:
        raise GitHubAppWorkerConfigurationError(f"{name} must be positive")
    return value


def _nonnegative_integer(name: str, default: str) -> int:
    raw = os.environ.get(name, default).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise GitHubAppWorkerConfigurationError(f"{name} must be an integer") from exc
    if value < 0:
        raise GitHubAppWorkerConfigurationError(f"{name} must be zero or greater")
    return value


def _seconds(name: str, default: str) -> timedelta:
    raw = os.environ.get(name, default).strip()
    try:
        value = float(raw)
        return timedelta(seconds=value)
    except (OverflowError, ValueError) as exc:
        raise GitHubAppWorkerConfigurationError(f"{name} must be a number") from exc


def _gateway_client() -> ReviewGitHubGatewayClient:
    gateway_url = os.environ.get("REVIEW_AGENT_GITHUB_GATEWAY_URL", "").strip()
    if not gateway_url:
        raise GitHubAppWorkerConfigurationError(
            "REVIEW_AGENT_GITHUB_GATEWAY_URL is required"
        )
    try:
        return ReviewGitHubGatewayClient(gateway_url)
    except GitHubGatewayProtocolError as exc:
        raise GitHubAppWorkerConfigurationError(str(exc)) from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--once", action="store_true", help="claim at most one delivery"
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stdout,
        format="%(levelname)s %(name)s %(message)s",
    )
    stop = threading.Event()

    def request_stop(_signal: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    configured = ReviewAgentSettings.from_environment()
    try:
        gateway = _gateway_client()
        policy = GitHubAppWorkerPolicy(
            poll_interval=_seconds("REVIEW_AGENT_GITHUB_APP_POLL_SECONDS", "2"),
            recovery_interval=_seconds(
                "REVIEW_AGENT_GITHUB_APP_RECOVERY_SECONDS", "30"
            ),
            recovery_batch_size=_positive_integer(
                "REVIEW_AGENT_GITHUB_APP_RECOVERY_BATCH_SIZE", "100"
            ),
            database_backoff_initial=_seconds(
                "REVIEW_AGENT_GITHUB_APP_DATABASE_BACKOFF_SECONDS", "1"
            ),
            database_backoff_maximum=_seconds(
                "REVIEW_AGENT_GITHUB_APP_DATABASE_BACKOFF_MAX_SECONDS", "30"
            ),
        )
    except GitHubAppWorkerConfigurationError as exc:
        parser.error(str(exc))
    runtime = PostgreSQLRuntime(
        configured.postgres_database_url,
        role=PostgreSQLRuntimeRole.WORKER,
        worker_concurrency=1,
    )
    runtime.open()
    try:
        processor = GitHubAppProcessor(
            postgres=runtime,
            gateway=gateway,
            config=ProcessorConfig(
                profile=configured.profile,
                policy_revision=configured.policy_revision(),
                job_priority=_nonnegative_integer("REVIEW_AGENT_JOB_PRIORITY", "0"),
                job_max_attempts=_positive_integer(
                    "REVIEW_AGENT_JOB_MAX_ATTEMPTS", "3"
                ),
                active_job_limit=_positive_integer(
                    "REVIEW_AGENT_ACTIVE_JOB_LIMIT", "100"
                ),
                contract_environment=review_contract.deployment_environment(
                    os.environ
                ),
            ),
        )
        GitHubAppWorker(
            runtime,
            processor,
            policy,
            lease_owner=default_github_app_worker_name(),
            stop_event=stop,
        ).run(once=args.once)
        return 0
    finally:
        runtime.close()


if __name__ == "__main__":
    raise SystemExit(main())
