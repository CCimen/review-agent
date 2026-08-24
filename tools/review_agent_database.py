#!/usr/bin/env python3
"""Apply and verify the Review Agent PostgreSQL schema."""

from __future__ import annotations

import argparse
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

from review_agent_tools.postgres.runtime import (  # noqa: E402
    PostgreSQLRuntime,
    PostgreSQLRuntimeRole,
)
from review_agent_tools.postgres_migrations import runner  # noqa: E402
from review_agent_tools.settings import ReviewAgentSettings  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("migrate", "ready"))
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
    runtime = PostgreSQLRuntime(
        database_url, role=PostgreSQLRuntimeRole.OPERATOR
    )
    runtime.open()
    try:
        readiness = runtime.readiness()
        print(
            "PostgreSQL ready: "
            f"server={readiness.server_version} "
            f"migration={readiness.applied_migration_version}."
        )
        return 0
    finally:
        runtime.close()


if __name__ == "__main__":
    raise SystemExit(main())
