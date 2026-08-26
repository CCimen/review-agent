#!/usr/bin/env python3
"""Run one recoverable Review Agent publisher process."""

from __future__ import annotations

import argparse
from datetime import timedelta
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
        if (candidate / "review_agent_tools" / "publisher.py").is_file():
            sys.path.insert(0, str(candidate))
            return
    raise SystemExit("Could not locate the review_agent_tools package")


_load_package()

from review_agent_tools.github.publication import GitHubIssueCommentGateway  # noqa: E402
from review_agent_tools.postgres.runtime import (  # noqa: E402
    PostgreSQLRuntime,
    PostgreSQLRuntimeRole,
)
from review_agent_tools.publisher import (  # noqa: E402
    PublicationWorker,
    PublisherConfigurationError,
    PublisherPolicy,
    default_publisher_name,
)
from review_agent_tools.settings import ReviewAgentSettings  # noqa: E402


def _seconds(name: str, default: str) -> timedelta:
    raw = os.environ.get(name, default).strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise PublisherConfigurationError(f"{name} must be a number") from exc
    return timedelta(seconds=value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="claim at most one publication")
    args = parser.parse_args(argv)
    stop = threading.Event()

    def request_stop(_signal: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    configured = ReviewAgentSettings.from_environment()
    policy = PublisherPolicy(
        lease_duration=_seconds("REVIEW_AGENT_PUBLICATION_LEASE_SECONDS", "120"),
        heartbeat_interval=_seconds(
            "REVIEW_AGENT_PUBLICATION_HEARTBEAT_SECONDS", "30"
        ),
        retry_delay=_seconds("REVIEW_AGENT_PUBLICATION_RETRY_SECONDS", "30"),
        poll_interval=_seconds("REVIEW_AGENT_PUBLICATION_POLL_SECONDS", "2"),
        max_comment_bytes=configured.publish_max_bytes,
    )
    runtime = PostgreSQLRuntime(
        configured.postgres_database_url,
        role=PostgreSQLRuntimeRole.WORKER,
    )
    runtime.open()
    try:
        PublicationWorker(
            runtime,
            GitHubIssueCommentGateway(
                configured.github_publish_token,
            ),
            policy,
            lease_owner=default_publisher_name(),
            stop_event=stop,
        ).run(once=args.once)
        return 0
    finally:
        runtime.close()


if __name__ == "__main__":
    raise SystemExit(main())
