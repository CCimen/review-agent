from __future__ import annotations

import sys
import unittest
import os
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from threading import Barrier

import psycopg

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = ROOT / "bootstrap" / "plugins"
sys.path.insert(0, str(PACKAGE_ROOT))

from review_agent_tools import review_feedback_application  # noqa: E402
from review_agent_tools import (  # noqa: E402
    review_finding_application,
    review_run_application,
)
from review_agent_tools.domain.finding import FindingInput  # noqa: E402
from review_agent_tools.domain.feedback import (  # noqa: E402
    FeedbackStatus,
)
from review_agent_tools.domain.publication import (  # noqa: E402
    PublicationFindingInput,
    PublicationFindingOutcome,
    PublicationPartInput,
    PublicationPartType,
    resolve_publication_plan,
)
from review_agent_tools.domain.review import ReviewPhase  # noqa: E402
from review_agent_tools.feedback_commands import (  # noqa: E402
    parse_review_feedback_command,
)
from review_agent_tools.postgres import feedback as postgres_feedback  # noqa: E402
from review_agent_tools.postgres import publications, review_runs  # noqa: E402
from review_agent_tools.postgres import decisions as postgres_decisions  # noqa: E402
from review_agent_tools.postgres.runtime import PostgreSQLRuntime  # noqa: E402
from review_agent_tools.postgres_migrations import runner  # noqa: E402
from review_agent_tools.settings import PostgresDatabaseUrl  # noqa: E402

DSN = os.environ.get("REVIEW_AGENT_POSTGRES_DSN", "")


class PostgreSQLFeedbackAdmissionTests(unittest.TestCase):
    def test_invalid_authorization_version_rejects_before_pool_checkout(self) -> None:
        runtime = PostgreSQLRuntime(
            PostgresDatabaseUrl("postgresql://invalid@127.0.0.1:1/unreachable")
        )
        self.addCleanup(runtime.close)
        command = parse_review_feedback_command(
            "@review false-positive F1 Existing guard disproves this."
        )
        assert command is not None

        with self.assertRaisesRegex(
            review_feedback_application.ReviewFeedbackError,
            "authorization_version",
        ):
            review_feedback_application.record_postgres_feedback(
                runtime,
                event_id="github:issue-comment:500",
                repository="example-org/example-repository",
                pr_number=17,
                command=command,
                actor_user_id=999,
                actor_login="mallory",
                author_association="OWNER",
                authorization_version="not-a-version",
                source_comment_id=500,
                source_comment_url=(
                    "https://github.test/example-org/example-repository/"
                    "pull/17#issuecomment-500"
                ),
            )


@unittest.skipUnless(DSN, "run through scripts/check_postgres_schema.sh")
class PostgreSQLFeedbackTests(unittest.TestCase):
    def setUp(self) -> None:
        with psycopg.connect(DSN, autocommit=True) as connection:
            connection.execute("DROP SCHEMA IF EXISTS review_agent CASCADE")
        with psycopg.connect(DSN) as connection:
            runner.apply_migrations(connection)
        self.runtime = PostgreSQLRuntime(PostgresDatabaseUrl(DSN))
        self.runtime.open()
        self.addCleanup(self.runtime.close)

    @staticmethod
    def finding(**overrides: object) -> FindingInput:
        item = FindingInput(
            rule_id="authorization.missing-context",
            category="security",
            path="src/api/resources.py",
            line=42,
            symbol="update_resource",
            anchor="PUT /v1/resources/{resource_id}",
            title="Resource update omits authorization context",
            severity="High",
            publication_score=9,
            confidence=0.93,
            evidence="The changed query writes a caller-controlled resource scope.",
            disproof_checks="Checked the dependency and repository layer.",
            impact="Cross-scope write.",
            smallest_fix="Bind resource_scope_id from context.",
            introduced_by_diff=True,
        )
        return replace(item, **overrides)

    def publish(
        self,
        *,
        request_key: str = "github:issue-comment:review-1",
        head_sha: str = "a" * 40,
        with_finding: bool = True,
        key_character: str = "d",
    ) -> publications.StoredPublication:
        started = review_run_application.start_postgres_review(
            self.runtime,
            review_run_application.PostgresRunRequest(
                provider="github",
                provider_repository_id=981,
                repository="example-org/example-repository",
                pr_number=17,
                base_sha="b" * 40,
                head_sha=head_sha,
                policy_revision="profile@1",
                resolved_config_schema_version=1,
                resolved_config={"profile": "sundsvall-standard"},
                request_key=request_key,
            ),
        )
        assert isinstance(started, review_runs.StartedRun)
        review_run_application.register_postgres_changed_files(
            self.runtime,
            run_id=started.run.id,
            files=(
                review_run_application.PostgresChangedFile(
                    path="src/api/resources.py",
                    change_status="modified",
                ),
            ),
            changed_files_reported=1,
            registration_complete=True,
        )
        batch = review_finding_application.record_postgres_findings(
            self.runtime,
            run_id=started.run.id,
            head_sha=head_sha,
            findings=(self.finding(),) if with_finding else (),
            changed_files=(
                review_finding_application.ChangedFile(
                    path="src/api/resources.py",
                    context_hash="c" * 40,
                    context_hash_source="blob",
                ),
            ),
        )
        with self.runtime.transaction() as connection:
            for phase in (
                ReviewPhase.FETCHING_PR,
                ReviewPhase.COLLECTING_DIFF,
                ReviewPhase.REVIEWING,
                ReviewPhase.RENDERING,
            ):
                review_runs.advance_phase(connection, started.run.id, phase)

        publication_key = "sha256:" + (key_character * 64)
        finding_inputs = ()
        if batch.items:
            finding = batch.items[0]
            finding_inputs = (
                PublicationFindingInput(
                    finding_id=int(finding.finding_id),
                    source_finding_occurrence_id=int(finding.occurrence_id),
                    source_review_run_id=int(batch.run_id),
                    local_reference=finding.local_reference,
                    outcome=PublicationFindingOutcome.CURRENT,
                ),
            )
        plan = resolve_publication_plan(
            publication_key=publication_key,
            rendered_markdown="## Review\n\nExact persisted review.\n",
            rendered_blocks_schema_version=1,
            rendered_blocks=(
                {"kind": "header", "markdown": "## Review"},
                {"kind": "finding", "markdown": "Exact persisted review."},
            ),
            parts=(
                PublicationPartInput(
                    part_type=PublicationPartType.SUMMARY,
                    part_number=1,
                    payload_schema_version=1,
                    payload={
                        "body": (
                            "Exact persisted review.\n\n"
                            "<!-- review-agent:canonical publication="
                            + publication_key
                            + " part=1/1 -->"
                        )
                    },
                ),
            ),
            findings=finding_inputs,
        )
        with self.runtime.transaction() as connection:
            prepared = publications.prepare_publication(
                connection,
                run_id=started.run.id,
                plan=plan,
            )
        with self.runtime.transaction() as connection:
            claim = publications.claim_publication(connection, prepared.id)
        assert claim.publication.posting_started_at is not None
        with self.runtime.transaction() as connection:
            publications.acknowledge_part(
                connection,
                publication_id=prepared.id,
                part_type=PublicationPartType.SUMMARY,
                part_number=1,
                external_id=500 + int(prepared.id),
                posting_started_at=claim.publication.posting_started_at,
            )
            posted = publications.complete_publication(
                connection,
                publication_id=prepared.id,
                posting_started_at=claim.publication.posting_started_at,
            )
            review_runs.complete_run(
                connection,
                started.run.id,
                findings_count=len(batch.items),
            )
        return posted

    def feedback(
        self,
        body: str,
        *,
        event_id: str = "github:issue-comment:500",
        source_comment_id: int = 500,
    ):
        command = parse_review_feedback_command(body)
        assert command is not None
        return review_feedback_application.record_postgres_feedback(
            self.runtime,
            event_id=event_id,
            repository="example-org/example-repository",
            pr_number=17,
            command=command,
            actor_user_id=12345,
            actor_login="alice",
            author_association="OWNER",
            authorization_version="sha256:" + ("a" * 64),
            source_comment_id=source_comment_id,
            source_comment_url=(
                "https://github.test/example-org/example-repository/"
                f"pull/17#issuecomment-{source_comment_id}"
            ),
        )

    def test_false_positive_records_decision_audit_and_replays_outcome(self) -> None:
        self.publish()

        first = self.feedback(
            "@review false-positive F1 Existing guard disproves this."
        )
        replay = self.feedback(
            "@review false-positive F1 Existing guard disproves this."
        )

        self.assertEqual(first.status, "recorded")
        self.assertFalse(first.replayed)
        self.assertEqual(first.local_reference, "F1")
        self.assertEqual(first.context_hash, "c" * 40)
        self.assertEqual(replay.status, "recorded")
        self.assertTrue(replay.replayed)
        with self.runtime.transaction() as connection:
            counts = connection.execute(
                """
                SELECT
                    (SELECT count(*) FROM review_agent.finding_decisions),
                    (SELECT count(*) FROM review_agent.decision_audit),
                    (SELECT count(*) FROM review_agent.processed_feedback_events)
                """
            ).fetchone()
            audit = connection.execute(
                """
                SELECT actor_user_id, source_comment_id, authorization_version
                FROM review_agent.decision_audit
                """
            ).fetchone()
        self.assertEqual(counts, (1, 1, 1))
        assert audit is not None
        self.assertEqual(audit[:2], ("12345", 500))
        self.assertRegex(str(audit[2]), r"^sha256:[0-9a-f]{64}$")

    def test_missing_publication_persists_no_mapping_for_replay(self) -> None:
        first = self.feedback(
            "@review false-positive F1 Existing guard disproves this."
        )
        replay = self.feedback(
            "@review false-positive F1 Existing guard disproves this."
        )

        self.assertEqual(first.status, "no_mapping")
        self.assertFalse(first.replayed)
        self.assertEqual(replay.status, "no_mapping")
        self.assertTrue(replay.replayed)
        with self.runtime.transaction() as connection:
            event = connection.execute(
                """
                SELECT outcome FROM review_agent.processed_feedback_events
                WHERE event_id = 'github:issue-comment:500'
                """
            ).fetchone()
            decisions = connection.execute(
                "SELECT count(*) FROM review_agent.finding_decisions"
            ).fetchone()
        self.assertEqual(event, ("no_mapping",))
        self.assertEqual(decisions, (0,))

    def test_missed_issue_records_quality_feedback_without_a_finding(self) -> None:
        self.publish(with_finding=False)

        first = self.feedback(
            "@review feedback missed The review missed rollback risk."
        )
        replay = self.feedback(
            "@review feedback missed The review missed rollback risk."
        )

        self.assertEqual(first.status, "recorded")
        self.assertIsNotNone(first.feedback_id)
        self.assertEqual(replay.status, "recorded")
        self.assertTrue(replay.replayed)
        with self.runtime.transaction() as connection:
            row = connection.execute(
                """
                SELECT category, reason, local_reference, actor_user_id
                FROM review_agent.review_quality_feedback
                """
            ).fetchone()
        self.assertEqual(
            row,
            ("missed_issue", "The review missed rollback risk.", None, "12345"),
        )

    def test_scope_feedback_maps_to_the_exact_current_reference(self) -> None:
        publication = self.publish()

        result = self.feedback(
            "@review feedback scope F1 This finding is inherited branch noise."
        )

        self.assertEqual(result.status, "recorded")
        with self.runtime.transaction() as connection:
            row = connection.execute(
                """
                SELECT publication_id, local_reference, category, reason
                FROM review_agent.review_quality_feedback
                """
            ).fetchone()
        self.assertEqual(
            row,
            (
                int(publication.id),
                "F1",
                "scope_confusion",
                "This finding is inherited branch noise.",
            ),
        )

    def test_superseded_reference_cannot_receive_feedback(self) -> None:
        self.publish()
        self.publish(
            request_key="github:issue-comment:review-2",
            head_sha="e" * 40,
            with_finding=False,
            key_character="f",
        )

        result = self.feedback(
            "@review false-positive F1 Existing guard disproves this."
        )

        self.assertEqual(result.status, "not_current")
        self.assertEqual(result.local_reference, "F1")
        with self.runtime.transaction() as connection:
            row = connection.execute(
                """
                SELECT outcome FROM review_agent.processed_feedback_events
                WHERE event_id = 'github:issue-comment:500'
                """
            ).fetchone()
            decisions = connection.execute(
                "SELECT count(*) FROM review_agent.finding_decisions"
            ).fetchone()
        self.assertEqual(row, ("not_current",))
        self.assertEqual(decisions, (0,))

    def test_audit_conflict_rolls_back_event_and_second_decision(self) -> None:
        self.publish()
        self.feedback("@review false-positive F1 Existing guard disproves this.")

        with self.assertRaises(postgres_decisions.DecisionAuditConflict):
            self.feedback(
                "@review false-positive F1 Another duplicate delivery id.",
                event_id="github:issue-comment:501",
                source_comment_id=500,
            )

        with self.runtime.transaction() as connection:
            event = connection.execute(
                """
                SELECT outcome FROM review_agent.processed_feedback_events
                WHERE event_id = 'github:issue-comment:501'
                """
            ).fetchone()
            counts = connection.execute(
                """
                SELECT
                    (SELECT count(*) FROM review_agent.finding_decisions),
                    (SELECT count(*) FROM review_agent.decision_audit)
                """
            ).fetchone()
        self.assertIsNone(event)
        self.assertEqual(counts, (1, 1))

    def test_concurrent_claim_waits_for_and_replays_committed_outcome(self) -> None:
        event_id = "github:issue-comment:500"
        processed_at = datetime.now(timezone.utc)
        barrier = Barrier(2)

        def replay_claim() -> FeedbackStatus | None:
            with psycopg.connect(DSN) as connection:
                with connection.transaction():
                    barrier.wait()
                    return postgres_feedback.claim_event(
                        connection,
                        event_id=event_id,
                        processed_at=processed_at,
                    )

        with ThreadPoolExecutor(max_workers=1) as executor:
            with psycopg.connect(DSN) as connection:
                with connection.transaction():
                    claimed = postgres_feedback.claim_event(
                        connection,
                        event_id=event_id,
                        processed_at=processed_at,
                    )
                    self.assertIsNone(claimed)
                    future = executor.submit(replay_claim)
                    barrier.wait()
                    with self.assertRaises(FutureTimeoutError):
                        future.result(timeout=0.2)
                    postgres_feedback.complete_event(
                        connection,
                        event_id=event_id,
                        outcome=FeedbackStatus.RECORDED,
                    )
            self.assertEqual(future.result(timeout=2), FeedbackStatus.RECORDED)

        with self.runtime.transaction() as connection:
            rows = connection.execute(
                """
                SELECT event_id, outcome
                FROM review_agent.processed_feedback_events
                """
            ).fetchall()
        self.assertEqual(rows, [(event_id, "recorded")])
