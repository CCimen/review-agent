from __future__ import annotations

import sys
import os
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bootstrap/plugins"))

from review_agent_tools import operator_application  # noqa: E402
from review_agent_tools.postgres.team_access import AccessRequest
from review_agent_tools.admin_quality_api import create_router  # noqa: E402
from review_agent_tools.domain.finding import (  # noqa: E402
    DecisionKind,
    FindingDecisionId,
    FindingOccurrenceId,
)
from review_agent_tools.postgres import admin_quality  # noqa: E402
from review_agent_tools.postgres.runtime import PostgreSQLRuntime  # noqa: E402
from review_agent_tools.postgres_migrations import runner  # noqa: E402
from review_agent_tools.settings import PostgresDatabaseUrl  # noqa: E402
from tests.test_postgres_reporting_cli import (  # noqa: E402
    PostgreSQLOperatorReportingTests,
)


ADMIN_ID = UUID("11111111-1111-1111-1111-111111111111")
DSN = os.environ.get("REVIEW_AGENT_POSTGRES_DSN", "")


class _Auth:
    @staticmethod
    def current_scope() -> AccessRequest:
        return AccessRequest(ADMIN_ID)

    @staticmethod
    def current_user() -> object:
        return SimpleNamespace(id=ADMIN_ID, email="viewer@example.test")

    @staticmethod
    def current_admin() -> object:
        return SimpleNamespace(id=ADMIN_ID, email="admin@example.test")


class AdminQualityAPITests(unittest.TestCase):
    def setUp(self) -> None:
        app = FastAPI()
        self.runtime = Mock()
        app.include_router(create_router(self.runtime, _Auth()))  # type: ignore[arg-type]
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()

    def test_decision_requires_exact_occurrence_and_passes_authenticated_identity(
        self,
    ) -> None:
        now = datetime(2026, 9, 8, tzinfo=timezone.utc)
        result = operator_application.OperatorDecisionResult(
            id=FindingDecisionId(1),
            fingerprint="a" * 64,
            occurrence_id=FindingOccurrenceId(42),
            decision=DecisionKind.FALSE_POSITIVE,
            reason="The guard disproves this occurrence.",
            actor=f"admin:{ADMIN_ID}",
            context_hash="b" * 64,
            adr_id=None,
            created_at=now,
            expires_at=now,
        )
        with patch(
            "review_agent_tools.admin_quality_api.admin_application.decide_finding",
            return_value=result,
        ) as decide:
            response = self.client.post(
                f"/api/findings/{'a' * 64}/decisions",
                json={
                    "repository": "owner/repository",
                    "occurrence_id": 42,
                    "decision": "false_positive",
                    "reason": "The guard disproves this occurrence.",
                },
            )
        self.assertEqual(response.status_code, 200, response.text)
        request = decide.call_args.args[1]
        self.assertEqual(request.occurrence_id, 42)
        self.assertFalse(request.latest)
        self.assertIsNone(request.pr_number)
        self.assertEqual(request.local_reference, "")
        self.assertEqual(decide.call_args.kwargs["access"], AccessRequest(ADMIN_ID))
        self.assertEqual(request.actor, "")

        missing = self.client.post(
            f"/api/findings/{'a' * 64}/decisions",
            json={
                "repository": "owner/repository",
                "decision": "resolved",
                "reason": "Fixed.",
            },
        )
        self.assertEqual(missing.status_code, 422)

    def test_contract_does_not_accept_an_actor_or_ambiguous_target(self) -> None:
        schema = self.client.get("/openapi.json").json()["components"]["schemas"]
        request = schema["FindingDecisionRequest"]
        self.assertIn("occurrence_id", request["required"])
        self.assertNotIn("actor", request["properties"])
        self.assertNotIn("latest", request["properties"])
        triage = schema["QualityTriageRequest"]
        self.assertNotIn("actor", triage["properties"])
        rejected = self.client.post(
            f"/api/findings/{'a' * 64}/decisions",
            json={
                "repository": "owner/repository",
                "occurrence_id": 1,
                "decision": "resolved",
                "reason": "Fixed.",
                "actor": "submitted-by-client",
            },
        )
        self.assertEqual(rejected.status_code, 422)


@unittest.skipUnless(DSN, "requires an isolated PostgreSQL test database")
class AdminQualityPostgreSQLTests(unittest.TestCase):
    def setUp(self) -> None:
        import psycopg

        with psycopg.connect(DSN, autocommit=True) as connection:
            connection.execute("DROP SCHEMA IF EXISTS review_agent CASCADE")
        with psycopg.connect(DSN) as connection:
            runner.apply_migrations(connection)
        self.runtime = PostgreSQLRuntime(PostgresDatabaseUrl(DSN))
        self.runtime.open()
        self.addCleanup(self.runtime.close)
        helper = PostgreSQLOperatorReportingTests("runTest")
        helper.runtime = self.runtime
        self.helper = helper
        self.helper.register_repository()

    def test_review_findings_use_the_exact_published_occurrence(self) -> None:
        first_run = self.helper.start(pr_number=81, request_suffix="quality-1")
        first = self.helper.record_finding(first_run)
        self.helper.publish_run(first_run, first, key_character="7")

        later_run = self.helper.start(
            pr_number=82,
            request_suffix="quality-2",
            head_sha="d" * 40,
        )
        later = self.helper.record_finding(
            later_run,
            findings=(
                replace(
                    self.helper.finding(),
                    title="Newer draft occurrence",
                    evidence="Newer draft evidence.",
                ),
            ),
            context_hash="d" * 40,
            head_sha="d" * 40,
        )
        other_run = self.helper.start(
            pr_number=83,
            request_suffix="quality-3",
            paths=("src/other.py",),
        )
        other = self.helper.record_finding(
            other_run,
            findings=(
                replace(
                    self.helper.finding(),
                    rule_id="correctness.other-boundary",
                    path="src/other.py",
                    anchor="other boundary",
                    title="Other finding",
                ),
            ),
        )
        self.helper.publish_run(other_run, other, key_character="8")

        with self.runtime.transaction() as connection:
            page = admin_quality.review_findings(
                connection, run_id=int(first_run.run.id)
            )

        self.assertEqual(page.total, 1)
        self.assertEqual(len(page.items), 1)
        self.assertEqual(page.items[0].fingerprint, first.items[0].fingerprint)
        self.assertEqual(page.items[0].occurrence_id, int(first.items[0].occurrence_id))
        self.assertNotEqual(
            page.items[0].occurrence_id, int(later.items[0].occurrence_id)
        )
        self.assertNotEqual(page.items[0].fingerprint, other.items[0].fingerprint)
        exact = operator_application.show_finding(
            self.runtime,
            repository=self.helper.repository,
            fingerprint=first.items[0].fingerprint,
            occurrence_id=int(first.items[0].occurrence_id),
        )
        self.assertEqual(exact.finding.occurrence_id, first.items[0].occurrence_id)
        self.assertEqual(exact.finding.title, "Safe mode defaults to disabled")
        decision = operator_application.decide_finding(
            self.runtime,
            operator_application.OperatorDecisionRequest(
                repository=self.helper.repository,
                fingerprint=first.items[0].fingerprint,
                occurrence_id=int(first.items[0].occurrence_id),
                decision="resolved",
                reason="The exact published occurrence was fixed.",
                actor="admin:test",
            ),
        )
        self.assertEqual(decision.occurrence_id, first.items[0].occurrence_id)

    def test_feedback_backlog_uses_latest_triage_and_reports_truncation(self) -> None:
        run = self.helper.start(pr_number=91, request_suffix="quality-4")
        finding = self.helper.record_finding(run)
        publication = self.helper.publish_run(run, finding, key_character="9")
        with self.runtime.transaction() as connection:
            feedback_ids = tuple(
                int(row[0])
                for row in connection.execute(
                    """
                    INSERT INTO review_agent.review_quality_feedback (
                        pull_request_id, publication_id, category, reason,
                        actor_user_id, created_at
                    ) VALUES
                        (%s, %s, 'missed_issue', 'Older retained miss.', '1', %s),
                        (%s, %s, 'missed_issue', 'New actionable miss.', '2', %s)
                    RETURNING id
                    """,
                    (
                        run.run.pull_request_id,
                        publication.id,
                        datetime(2024, 1, 1, tzinfo=timezone.utc),
                        run.run.pull_request_id,
                        publication.id,
                        datetime(2026, 9, 1, tzinfo=timezone.utc),
                    ),
                ).fetchall()
            )
        operator_application.triage_review_feedback(
            self.runtime,
            feedback_id=feedback_ids[1],
            status="actionable",
            stable_key="review.missed-boundary",
            target_owner="review_rule",
            evidence_reference="",
            path="src/flags.py",
            category="correctness",
            actor="admin:test",
            reason="The focused reproduction confirms the miss.",
        )

        with self.runtime.transaction() as connection:
            truncated = admin_quality.feedback_backlog(
                connection, repository=self.helper.repository, limit=1, offset=0
            )
            next_page = admin_quality.feedback_backlog(
                connection, repository=self.helper.repository, limit=1, offset=1
            )

        self.assertEqual((truncated.total, truncated.pending), (2, 1))
        self.assertTrue(truncated.has_more)
        self.assertEqual(truncated.next_offset, 1)
        self.assertEqual(truncated.items[0].id, feedback_ids[0])
        self.assertFalse(next_page.has_more)
        self.assertIsNone(next_page.next_offset)
        self.assertEqual(next_page.items[0].triage_status, "actionable")
        self.assertEqual(next_page.items[0].stable_key, "review.missed-boundary")

    def test_bounded_decision_history_returns_latest_then_pages_older(self) -> None:
        run = self.helper.start(pr_number=101, request_suffix="quality-5")
        finding = self.helper.record_finding(run)
        occurrence_id = int(finding.items[0].occurrence_id)
        fingerprint = finding.items[0].fingerprint
        stored_ids: list[int] = []
        for index in range(5):
            stored = operator_application.decide_finding(
                self.runtime,
                operator_application.OperatorDecisionRequest(
                    repository=self.helper.repository,
                    fingerprint=fingerprint,
                    occurrence_id=occurrence_id,
                    decision="resolved" if index % 2 == 0 else "reopen",
                    reason=f"Decision evidence {index}.",
                    actor="admin:test",
                ),
            )
            stored_ids.append(int(stored.id))

        latest = operator_application.show_finding(
            self.runtime,
            repository=self.helper.repository,
            fingerprint=fingerprint,
            decision_limit=3,
        )
        older = operator_application.show_finding(
            self.runtime,
            repository=self.helper.repository,
            fingerprint=fingerprint,
            decision_limit=3,
            decision_before_id=int(latest.decisions[-1].id),
        )

        self.assertEqual(
            [int(item.id) for item in latest.decisions],
            list(reversed(stored_ids[-3:])),
        )
        self.assertEqual(
            [int(item.id) for item in older.decisions],
            list(reversed(stored_ids[:2])),
        )


if __name__ == "__main__":
    unittest.main()
