from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

import psycopg


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = ROOT / "bootstrap" / "plugins"
sys.path.insert(0, str(PACKAGE_ROOT))

from review_agent_tools import repository_decision_context  # noqa: E402
from review_agent_tools.domain import repository_decisions as decision_domain  # noqa: E402
from review_agent_tools.postgres import repository_decisions  # noqa: E402
from review_agent_tools.postgres.review_runs import ReviewRunId  # noqa: E402
from review_agent_tools.settings import PostgresDatabaseUrl  # noqa: E402
from review_agent_tools.postgres_migrations import runner  # noqa: E402
from review_agent_tools import review_run_application  # noqa: E402


DSN = os.environ.get("REVIEW_AGENT_POSTGRES_DSN", "")


@unittest.skipUnless(DSN, "run through scripts/check_postgres_schema.sh")
class PostgreSQLRepositoryDecisionTests(unittest.TestCase):
    def setUp(self) -> None:
        with psycopg.connect(DSN, autocommit=True) as connection:
            connection.execute("DROP SCHEMA IF EXISTS review_agent CASCADE")
        with psycopg.connect(DSN) as connection:
            runner.apply_migrations(connection)
        self.runtime = review_run_application.PostgreSQLRuntime(
            PostgresDatabaseUrl(DSN)
        )
        self.runtime.open()

    def tearDown(self) -> None:
        self.runtime.close()

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
                request_key="request-1",
            ),
        )
        return result.run.id

    def context(self) -> repository_decision_context.RepositoryDecisionContext:
        entry = decision_domain.DecisionIndexEntry(
            id="ADR-0007",
            adr_path=".review-agent/decisions/ADR-0007.md",
            applies_to=("src/rag/**",),
        )
        decision = decision_domain.parse_adr(
            """+++
id = "ADR-0007"
title = "Keep the retrieval budget coupled"
status = "accepted"
invariant = "Chunk size, overlap, and top-k form one budget."
on_change = ["Run the retrieval evaluation."]
evidence = "docs/evaluations/rag.md"
origin_pr = 1234
+++
""",
            match=decision_domain.DecisionIndexMatch(
                entry=entry,
                matched_path_count=1,
            ),
        )
        return repository_decision_context.loaded(
            base_sha="a" * 40,
            index_hash="sha256:" + "c" * 64,
            decisions=(decision,),
        )

    def test_loaded_snapshot_is_immutable_and_keeps_base_provenance(self) -> None:
        run_id = self.start_run()
        context = self.context()

        with self.runtime.transaction() as connection:
            stored = repository_decisions.store_context(
                connection, run_id=run_id, context=context
            )
        self.assertIsNotNone(stored.snapshot_id)
        self.assertEqual(stored.snapshot_hash, context.snapshot_hash)

        replacement = repository_decision_context.failed(
            "unavailable",
            base_sha="a" * 40,
            failure_code="decision_source_unavailable",
        )
        with self.runtime.transaction() as connection:
            repeated = repository_decisions.store_context(
                connection, run_id=run_id, context=replacement
            )
            loaded = repository_decisions.load_context(connection, run_id=run_id)

        self.assertEqual(repeated, stored)
        self.assertEqual(loaded, stored)

    def test_failure_context_stores_one_aggregate_without_partial_items(self) -> None:
        run_id = self.start_run()
        context = repository_decision_context.failed(
            "too_many_matches",
            base_sha="a" * 40,
            index_hash="sha256:" + "c" * 64,
            failure_code="decision_match_limit_exceeded",
        )
        with self.runtime.transaction() as connection:
            stored = repository_decisions.store_context(
                connection, run_id=run_id, context=context
            )
            count = connection.execute(
                "SELECT count(*) FROM review_agent.review_decision_snapshots"
            ).fetchone()

        self.assertIsNotNone(stored.snapshot_id)
        self.assertEqual(count, (1,))


if __name__ == "__main__":
    unittest.main()
