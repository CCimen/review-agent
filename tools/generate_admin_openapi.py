#!/usr/bin/env python3
"""Export the admin API contract without opening a database or reading secrets."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bootstrap/plugins"))

from review_agent_tools.admin_api import create_app  # noqa: E402
from review_agent_tools.postgres.runtime import PostgreSQLRuntime  # noqa: E402
from review_agent_tools.settings import PostgresDatabaseUrl  # noqa: E402


def main() -> None:
    runtime = PostgreSQLRuntime(
        PostgresDatabaseUrl("postgresql://localhost/schema_only")
    )
    app = create_app(
        runtime, public_url="https://admin.example.test", static_dir=ROOT / "admin/dist"
    )
    print(json.dumps(app.openapi(), indent=2))


if __name__ == "__main__":
    main()
