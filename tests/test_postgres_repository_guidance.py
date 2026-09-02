from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

import psycopg


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = ROOT / "bootstrap" / "plugins"
sys.path.insert(0, str(PACKAGE_ROOT))

from review_agent_tools import (  # noqa: E402
    repository_guidance_context,
    review_run_application,
)
from review_agent_tools.domain.review import ReviewRunId  # noqa: E402
from review_agent_tools.postgres import repository_guidance  # noqa: E402
from review_agent_tools.postgres.runtime import PostgreSQLRuntime  # noqa: E402
from review_agent_tools.postgres_migrations import runner  # noqa: E402
from review_agent_tools.settings import PostgresDatabaseUrl  # noqa: E402


DSN = os.environ.get("REVIEW_AGENT_POSTGRES_DSN", "")


@unittest.skipUnless(DSN, "run through scripts/check_postgres_schema.sh")
class PostgreSQLRepositoryGuidanceTests(unittest.TestCase):
    def setUp(self) -> None:
        with psycopg.connect(DSN, autocommit=True) as connection:
            connection.execute("DROP SCHEMA IF EXISTS review_agent CASCADE")
        with psycopg.connect(DSN) as connection:
            runner.apply_migrations(connection)
        self.runtime = PostgreSQLRuntime(PostgresDatabaseUrl(DSN))
        self.runtime.open()
        self.addCleanup(self.runtime.close)

    def start_run(self) -> ReviewRunId:
        result = review_run_application.start_postgres_review(
            self.runtime,
            review_run_application.PostgresRunRequest(
                provider="github",
                provider_repository_id=123,
                repository="example-org/example-repository",
                pr_number=7,
                base_sha="a" * 40,
                head_sha="b" * 40,
                policy_revision="policy-v1",
                resolved_config_schema_version=1,
                resolved_config={"profile": "test"},
                request_key="request-guidance-1",
            ),
        )
        return result.run.id

    def context(
        self,
    ) -> repository_guidance_context.RepositoryGuidanceContext:
        return repository_guidance_context.loaded(
            base_sha="a" * 40,
            config_hash="sha256:" + "c" * 64,
            instructions=repository_guidance_context.guidance_file(
                ".review-agent/instructions.md",
                "Prefer the smallest complete long-term solution.",
            ),
            context_files=(
                repository_guidance_context.guidance_file(
                    ".review-agent/context/platform.md",
                    "The platform owns authentication and authorization.",
                ),
            ),
        )

    def test_snapshot_is_immutable_and_keeps_exact_base_provenance(self) -> None:
        run_id = self.start_run()
        context = self.context()

        with self.runtime.transaction() as connection:
            stored = repository_guidance.store_context(
                connection,
                run_id=run_id,
                context=context,
            )
        self.assertIsNotNone(stored.snapshot_id)
        self.assertEqual(stored.snapshot_hash, context.snapshot_hash)

        replacement = repository_guidance_context.failed(
            "unavailable",
            base_sha="a" * 40,
            failure_code="guidance_source_unavailable",
        )
        with self.runtime.transaction() as connection:
            repeated = repository_guidance.store_context(
                connection,
                run_id=run_id,
                context=replacement,
            )
            loaded = repository_guidance.load_context(
                connection,
                run_id=run_id,
            )

        self.assertEqual(repeated, stored)
        self.assertEqual(loaded, stored)

    def test_failed_snapshot_contains_no_partial_repository_content(self) -> None:
        run_id = self.start_run()
        context = repository_guidance_context.failed(
            "invalid",
            base_sha="a" * 40,
            config_hash="sha256:" + "c" * 64,
            failure_code="guidance_context_file_missing",
        )

        with self.runtime.transaction() as connection:
            stored = repository_guidance.store_context(
                connection,
                run_id=run_id,
                context=context,
            )
            row = connection.execute(
                "SELECT context_file_count, instructions_present "
                "FROM review_agent.review_guidance_snapshots"
            ).fetchone()

        self.assertEqual(stored.status, "invalid")
        self.assertEqual(row, (0, False))


if __name__ == "__main__":
    unittest.main()
