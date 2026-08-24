#!/usr/bin/env python3
"""Run one serial PostgreSQL-backed Review Agent worker process."""

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
        Path(os.environ.get("HERMES_HOME", "/opt/data")) / "plugins",
        Path(__file__).resolve().parents[1] / "bootstrap" / "plugins",
    )
    for candidate in candidates:
        if (candidate / "review_agent_tools" / "worker.py").is_file():
            sys.path.insert(0, str(candidate))
            return
    raise SystemExit("Could not locate the review_agent_tools package")


_load_package()

from review_agent_tools.postgres.runtime import (  # noqa: E402
    PostgreSQLRuntime,
    PostgreSQLRuntimeRole,
)
from review_agent_tools.settings import ReviewAgentSettings  # noqa: E402
from review_agent_tools.worker import (  # noqa: E402
    HermesChatClient,
    HermesChatSettings,
    ReviewWorker,
    WorkerConfigurationError,
    WorkerPolicy,
    default_lease_owner,
)


def _seconds(name: str, default: str) -> timedelta:
    raw = os.environ.get(name, default).strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise WorkerConfigurationError(f"{name} must be a number") from exc
    return timedelta(seconds=value)


def _positive_integer(name: str, default: str) -> int:
    raw = os.environ.get(name, default).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise WorkerConfigurationError(f"{name} must be an integer") from exc
    if value < 1:
        raise WorkerConfigurationError(f"{name} must be positive")
    return value


def _policy() -> WorkerPolicy:
    return WorkerPolicy(
        lease_duration=_seconds("REVIEW_AGENT_JOB_LEASE_SECONDS", "120"),
        heartbeat_interval=_seconds("REVIEW_AGENT_JOB_HEARTBEAT_SECONDS", "30"),
        retry_delay=_seconds("REVIEW_AGENT_JOB_RETRY_SECONDS", "30"),
        poll_interval=_seconds("REVIEW_AGENT_JOB_POLL_SECONDS", "2"),
        request_timeout=_seconds("REVIEW_AGENT_HERMES_TIMEOUT_SECONDS", "7200"),
        recovery_interval=_seconds("REVIEW_AGENT_JOB_RECOVERY_SECONDS", "30"),
        recovery_batch_size=_positive_integer(
            "REVIEW_AGENT_JOB_RECOVERY_BATCH_SIZE", "100"
        ),
        priority_aging_interval=_seconds(
            "REVIEW_AGENT_JOB_PRIORITY_AGING_SECONDS", "900"
        ),
    )


def _chat_settings() -> HermesChatSettings:
    hermes_home = Path(os.environ.get("HERMES_HOME", "/opt/data"))
    return HermesChatSettings(
        endpoint=os.environ.get(
            "REVIEW_AGENT_HERMES_CHAT_URL",
            "http://127.0.0.1:8642/v1/chat/completions",
        ).strip(),
        bearer_token=os.environ.get("API_SERVER_KEY", "").strip(),
        skill_path=Path(
            os.environ.get(
                "REVIEW_AGENT_SKILL_PATH",
                str(hermes_home / "skills" / "review-agent-pr" / "SKILL.md"),
            )
        ),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="claim at most one job")
    args = parser.parse_args(argv)

    stop = threading.Event()

    def request_stop(_signal: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    configured = ReviewAgentSettings.from_environment()
    policy = _policy()
    chat_settings = _chat_settings()
    lease_owner = default_lease_owner(os.environ)
    runtime = PostgreSQLRuntime(
        configured.postgres_database_url,
        role=PostgreSQLRuntimeRole.WORKER,
    )
    runtime.open()
    try:
        worker = ReviewWorker(
            runtime,
            HermesChatClient(chat_settings),
            policy,
            lease_owner=lease_owner,
            stop_event=stop,
        )
        worker.run(once=args.once)
        return 0
    finally:
        runtime.close()


if __name__ == "__main__":
    raise SystemExit(main())
