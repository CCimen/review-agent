from __future__ import annotations

import os
import sys
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import psycopg


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = ROOT / "bootstrap" / "plugins"
sys.path.insert(0, str(PACKAGE_ROOT))

from review_agent_tools import (  # noqa: E402
    review_finding_application,
    review_run_application,
)
from review_agent_tools.domain.finding import (  # noqa: E402
    DecisionKind,
    FindingInput,
    suppression_is_active,
)
from review_agent_tools.postgres import (  # noqa: E402
    decisions as postgres_decisions,
    review_runs as postgres_review_runs,
    suggestions as postgres_suggestions,
)
from review_agent_tools.postgres.runtime import PostgreSQLRuntime  # noqa: E402
from review_agent_tools.postgres_migrations import runner  # noqa: E402
from review_agent_tools.settings import PostgresDatabaseUrl  # noqa: E402


DSN = os.environ.get("REVIEW_AGENT_POSTGRES_DSN", "")


class FindingDecisionDomainTests(unittest.TestCase):
    def test_suppression_requires_an_unexpired_exact_context_match(self) -> None:
        now = datetime(2026, 8, 24, tzinfo=timezone.utc)

        self.assertTrue(
            suppression_is_active(
                decision=DecisionKind.FALSE_POSITIVE,
                decision_context_hash="a" * 40,
                current_context_hash="a" * 40,
                expires_at=now + timedelta(days=1),
                now=now,
            )
        )
        self.assertFalse(
            suppression_is_active(
                decision=DecisionKind.FALSE_POSITIVE,
                decision_context_hash="a" * 40,
                current_context_hash="b" * 40,
                expires_at=now + timedelta(days=1),
                now=now,
            )
        )

    def test_intentional_suppression_requires_current_adr_evidence(self) -> None:
        now = datetime(2026, 8, 24, tzinfo=timezone.utc)

        self.assertFalse(
            suppression_is_active(
                decision=DecisionKind.INTENTIONAL_BY_DESIGN,
                decision_context_hash="a" * 40,
                current_context_hash="a" * 40,
                expires_at=now + timedelta(days=1),
                now=now,
            )
        )
        self.assertTrue(
            suppression_is_active(
                decision=DecisionKind.INTENTIONAL_BY_DESIGN,
                decision_context_hash="a" * 40,
                current_context_hash="a" * 40,
                expires_at=now + timedelta(days=1),
                intentional_evidence_current=True,
                now=now,
            )
        )
        self.assertFalse(
            suppression_is_active(
                decision=DecisionKind.RESOLVED,
                decision_context_hash="a" * 40,
                current_context_hash="a" * 40,
                expires_at=None,
                now=now,
            )
        )
        self.assertFalse(
            suppression_is_active(
                decision=DecisionKind.FALSE_POSITIVE,
                decision_context_hash="a" * 40,
                current_context_hash="a" * 40,
                expires_at=now,
                now=now,
            )
        )


@unittest.skipUnless(DSN, "run through scripts/check_postgres_schema.sh")
class PostgreSQLSuggestionDecisionTests(unittest.TestCase):
    head_sha = "a" * 40

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
        finding = FindingInput(
            rule_id="correctness.boolean-default",
            category="correctness",
            path="src/flags.py",
            line=2,
            symbol="safe",
            anchor="safe default",
            title="Safe mode defaults to disabled",
            severity="High",
            publication_score=9,
            confidence=0.95,
            evidence="The changed default is false.",
            disproof_checks="Checked all callers.",
            impact="Requests can run without the expected guard.",
            smallest_fix="Restore the true default.",
            introduced_by_diff=True,
        )
        return replace(finding, **overrides)

    def start(self) -> postgres_review_runs.ReviewRunId:
        result = review_run_application.start_postgres_review(
            self.runtime,
            review_run_application.PostgresRunRequest(
                provider="github",
                provider_repository_id=930,
                repository="example-org/example-repository",
                pr_number=31,
                base_sha="b" * 40,
                head_sha=self.head_sha,
                policy_revision="profile@1",
                resolved_config_schema_version=1,
                resolved_config={"profile": "sundsvall-standard"},
                request_key="github:issue-comment:4001",
            ),
        )
        assert isinstance(result, postgres_review_runs.StartedRun)
        review_run_application.register_postgres_changed_files(
            self.runtime,
            run_id=result.run.id,
            files=(
                review_run_application.PostgresChangedFile(
                    path="src/flags.py",
                    change_status="modified",
                ),
            ),
            changed_files_reported=1,
            registration_complete=True,
        )
        return result.run.id

    @staticmethod
    def changed_file() -> review_finding_application.ChangedFile:
        return review_finding_application.ChangedFile(
            path="src/flags.py",
            context_hash="c" * 40,
            context_hash_source="blob",
            patch=(
                "@@ -1,3 +1,3 @@\n"
                " before\n"
                "-safe = None\n"
                "+safe = False\n"
                " after"
            ),
        )

    @staticmethod
    def suggestion(*, replacement_text: str = "safe = True") -> dict[str, object]:
        return {
            "start_line": 2,
            "end_line": 2,
            "expected_text": "safe = False",
            "replacement_text": replacement_text,
        }

    def test_suggestion_validation_holds_no_connection_and_accepts_deletion(self) -> None:
        run_id = self.start()

        def load_head(path: str, _start_line: int, _end_line: int) -> str:
            self.assertEqual(path, "src/flags.py")
            metrics = self.runtime.pool_metrics()
            self.assertEqual(metrics.available, metrics.size)
            return "safe = False"

        result = review_finding_application.record_postgres_findings_with_suggestions(
            self.runtime,
            run_id=run_id,
            head_sha=self.head_sha,
            findings=(self.finding(),),
            changed_files=(self.changed_file(),),
            suggestions=(self.suggestion(replacement_text=""),),
            head_file_loader=load_head,
        )

        self.assertEqual(result.suggestions_recorded, 1)
        self.assertEqual(result.suggestion_statuses, ("recorded",))
        with self.runtime.transaction() as connection:
            row = connection.execute(
                "SELECT replacement_text FROM review_agent.finding_suggestions"
            ).fetchone()
        self.assertEqual(row, ("",))

    def test_suggestion_storage_failure_does_not_rollback_findings(self) -> None:
        run_id = self.start()
        with mock.patch(
            "review_agent_tools.review_finding_application."
            "postgres_suggestions.replace_suggestions",
            side_effect=psycopg.OperationalError("storage unavailable"),
        ):
            result = (
                review_finding_application.record_postgres_findings_with_suggestions(
                    self.runtime,
                    run_id=run_id,
                    head_sha=self.head_sha,
                    findings=(self.finding(),),
                    changed_files=(self.changed_file(),),
                    suggestions=(self.suggestion(),),
                    head_file_loader=lambda _path, _start, _end: "safe = False",
                )
            )

        self.assertEqual(result.suggestions_recorded, 0)
        self.assertEqual(result.suggestion_statuses, ("suggestion_storage_failed",))
        with self.runtime.transaction() as connection:
            counts = connection.execute(
                "SELECT "
                "(SELECT count(*) FROM review_agent.finding_occurrences), "
                "(SELECT count(*) FROM review_agent.finding_suggestions)"
            ).fetchone()
        self.assertEqual(counts, (1, 0))

    def test_same_head_reuses_canonical_patch_and_later_omission_clears_it(
        self,
    ) -> None:
        run_id = self.start()
        first = review_finding_application.record_postgres_findings_with_suggestions(
            self.runtime,
            run_id=run_id,
            head_sha=self.head_sha,
            findings=(self.finding(),),
            changed_files=(self.changed_file(),),
            suggestions=(self.suggestion(),),
            head_file_loader=lambda _path, _start, _end: "safe = False",
        )
        with self.runtime.transaction() as connection:
            original = connection.execute(
                "SELECT expected_hash, replacement_text, suggestion_key "
                "FROM review_agent.finding_suggestions"
            ).fetchone()

        second = review_finding_application.record_postgres_findings_with_suggestions(
            self.runtime,
            run_id=run_id,
            head_sha=self.head_sha,
            findings=(self.finding(),),
            changed_files=(self.changed_file(),),
            suggestions=(self.suggestion(replacement_text="safe = maybe"),),
            head_file_loader=None,
        )
        with self.runtime.transaction() as connection:
            reused = connection.execute(
                "SELECT expected_hash, replacement_text, suggestion_key "
                "FROM review_agent.finding_suggestions"
            ).fetchone()

        self.assertEqual(first.batch, second.batch)
        self.assertEqual(second.suggestion_statuses, ("recorded",))
        self.assertEqual(reused, original)

        with self.runtime.transaction() as connection:
            with self.assertRaises(postgres_suggestions.SuggestionStoreError):
                postgres_suggestions.replace_suggestions(
                    connection,
                    batch=second.batch,
                    selected={
                        second.batch.items[0].occurrence_id: {
                            "path": "src/other.py",
                            "start_line": 2,
                            "end_line": 2,
                            "expected_hash": "f" * 64,
                            "replacement_text": "safe = True",
                            "suggestion_key": "sha256:" + ("e" * 64),
                        }
                    },
                )

        cleared = review_finding_application.record_postgres_findings_with_suggestions(
            self.runtime,
            run_id=run_id,
            head_sha=self.head_sha,
            findings=(self.finding(),),
            changed_files=(self.changed_file(),),
            suggestions=(None,),
            head_file_loader=None,
        )
        with self.runtime.transaction() as connection:
            count = connection.execute(
                "SELECT count(*) FROM review_agent.finding_suggestions"
            ).fetchone()
        self.assertEqual(cleared.suggestion_statuses, ("not_requested",))
        self.assertEqual(count, (0,))

    def test_context_transaction_failure_preserves_existing_suggestion(self) -> None:
        run_id = self.start()
        first = review_finding_application.record_postgres_findings_with_suggestions(
            self.runtime,
            run_id=run_id,
            head_sha=self.head_sha,
            findings=(self.finding(),),
            changed_files=(self.changed_file(),),
            suggestions=(self.suggestion(),),
            head_file_loader=lambda _path, _start, _end: "safe = False",
        )
        self.assertEqual(first.suggestion_statuses, ("recorded",))

        for target in (
            "postgres_suggestions.load_context",
            "postgres_decisions.latest_decisions",
        ):
            with self.subTest(target=target), mock.patch(
                "review_agent_tools.review_finding_application." + target,
                side_effect=psycopg.OperationalError("context unavailable"),
            ):
                second = (
                    review_finding_application.record_postgres_findings_with_suggestions(
                        self.runtime,
                        run_id=run_id,
                        head_sha=self.head_sha,
                        findings=(self.finding(),),
                        changed_files=(self.changed_file(),),
                        suggestions=(self.suggestion(),),
                        head_file_loader=None,
                    )
                )

            with self.runtime.transaction() as connection:
                count = connection.execute(
                    "SELECT count(*) FROM review_agent.finding_suggestions"
                ).fetchone()
            self.assertEqual(
                second.suggestion_statuses, ("suggestion_storage_failed",)
            )
            self.assertEqual(count, (1,))

    def test_loader_failure_degrades_without_losing_the_finding_result(self) -> None:
        run_id = self.start()

        def fail_loader(_path: str, _start_line: int, _end_line: int) -> str:
            raise RuntimeError("provider read failed")

        result = review_finding_application.record_postgres_findings_with_suggestions(
            self.runtime,
            run_id=run_id,
            head_sha=self.head_sha,
            findings=(self.finding(),),
            changed_files=(self.changed_file(),),
            suggestions=(self.suggestion(),),
            head_file_loader=fail_loader,
        )

        self.assertEqual(len(result.batch.items), 1)
        self.assertEqual(
            result.suggestion_statuses, ("suggestion_head_file_unavailable",)
        )

    def test_overlapping_and_high_risk_suggestions_are_omitted(self) -> None:
        run_id = self.start()
        findings = (
            self.finding(),
            self.finding(
                rule_id="correctness.second-default",
                symbol="second_safe",
                anchor="second safe default",
                severity="Medium",
                publication_score=8,
            ),
            self.finding(
                rule_id="security.unsafe-default",
                category="security",
                symbol="unsafe",
                anchor="unsafe default",
            ),
        )

        result = review_finding_application.record_postgres_findings_with_suggestions(
            self.runtime,
            run_id=run_id,
            head_sha=self.head_sha,
            findings=findings,
            changed_files=(self.changed_file(),),
            suggestions=(self.suggestion(), self.suggestion(), self.suggestion()),
            head_file_loader=lambda _path, _start, _end: "safe = False",
        )

        self.assertEqual(result.suggestions_recorded, 1)
        self.assertEqual(
            result.suggestion_statuses,
            (
                "recorded",
                "suggestion_overlaps_higher_priority_patch",
                "suggestion_high_risk_category",
            ),
        )

    def test_decision_and_audit_are_atomic_and_context_scoped(self) -> None:
        run_id = self.start()
        decision_time = datetime(2026, 8, 24, 12, tzinfo=timezone.utc)
        batch = review_finding_application.record_postgres_findings(
            self.runtime,
            run_id=run_id,
            head_sha=self.head_sha,
            findings=(self.finding(),),
            changed_files=(self.changed_file(),),
        )
        target = batch.items[0]

        decision = review_finding_application.append_postgres_governance_decision(
            self.runtime,
            finding_id=target.finding_id,
            occurrence_id=target.occurrence_id,
            decision="false_positive",
            reason="The guard is provided by the caller.",
            actor="reviewer@example.com",
            expires_days=30,
            audit=review_finding_application.DecisionAudit(
                actor_user_id="501",
                actor_login="reviewer",
                author_association="MEMBER",
                authorization_version="sha256:" + ("d" * 64),
                source_comment_id=9901,
                source_comment_url="https://github.example/comments/9901",
            ),
            now=decision_time + timedelta(hours=1),
        )

        self.assertEqual(decision.context_hash, "c" * 40)
        self.assertIsNotNone(
            review_finding_application.load_postgres_active_suppression(
                self.runtime,
                finding_id=target.finding_id,
                run_id=batch.run_id,
                context_hash="c" * 40,
            )
        )
        self.assertIsNone(
            review_finding_application.load_postgres_active_suppression(
                self.runtime,
                finding_id=target.finding_id,
                run_id=batch.run_id,
                context_hash="e" * 40,
            )
        )
        suggestion_result = (
            review_finding_application.record_postgres_findings_with_suggestions(
                self.runtime,
                run_id=run_id,
                head_sha=self.head_sha,
                findings=(self.finding(),),
                changed_files=(self.changed_file(),),
                suggestions=(self.suggestion(),),
                head_file_loader=lambda _path, _start, _end: "safe = False",
            )
        )
        self.assertEqual(
            suggestion_result.suggestion_statuses,
            ("suggestion_finding_suppressed",),
        )

        with self.assertRaises(postgres_decisions.DecisionAuditConflict):
            review_finding_application.append_postgres_governance_decision(
                self.runtime,
                finding_id=target.finding_id,
                occurrence_id=target.occurrence_id,
                decision="accepted_risk",
                reason="Accepted for this exact context.",
                actor="reviewer@example.com",
                expires_days=30,
                audit=review_finding_application.DecisionAudit(
                    actor_user_id="501",
                    actor_login="reviewer",
                    author_association="MEMBER",
                    authorization_version="sha256:" + ("d" * 64),
                    source_comment_id=9901,
                    source_comment_url="https://github.example/comments/9901",
                ),
            )

        with self.runtime.transaction() as connection:
            counts = connection.execute(
                "SELECT "
                "(SELECT count(*) FROM review_agent.finding_decisions), "
                "(SELECT count(*) FROM review_agent.decision_audit)"
            ).fetchone()
        self.assertEqual(counts, (1, 1))

        review_finding_application.append_postgres_governance_decision(
            self.runtime,
            finding_id=target.finding_id,
            occurrence_id=target.occurrence_id,
            decision="reopen",
            reason="The finding must be reviewed again.",
            actor="reviewer@example.com",
            audit=review_finding_application.DecisionAudit(
                actor_user_id="501",
                actor_login="reviewer",
                author_association="MEMBER",
                authorization_version="sha256:" + ("d" * 64),
                source_comment_id=9902,
                source_comment_url="https://github.example/comments/9902",
            ),
            now=decision_time,
        )
        self.assertIsNone(
            review_finding_application.load_postgres_active_suppression(
                self.runtime,
                finding_id=target.finding_id,
                run_id=batch.run_id,
                context_hash="c" * 40,
                now=decision_time + timedelta(hours=2),
            )
        )
