from __future__ import annotations

import os
import sys
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import psycopg


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = ROOT / "bootstrap" / "plugins"
sys.path.insert(0, str(PACKAGE_ROOT))

from review_agent_tools import (  # noqa: E402
    operator_application,
    review_finding_application,
    review_run_application,
)
from review_agent_tools.domain.coaching import (  # noqa: E402
    CoachCandidateInput,
    CoachInterventionOutcome,
    CoachInterventionOutcomeInput,
    CoachRunInput,
    resolve_coach_run,
    resolve_intervention_outcome,
)
from review_agent_tools.domain.finding import FindingInput  # noqa: E402
from review_agent_tools.domain.review import RepositoryId  # noqa: E402
from review_agent_tools.domain.verification import (  # noqa: E402
    resolve_candidate_verification,
    resolve_reconciliation,
    resolve_verification_run,
)
from review_agent_tools.postgres import (  # noqa: E402
    coaching as postgres_coaching,
    review_runs as postgres_review_runs,
    verification as postgres_verification,
)
from review_agent_tools.postgres.runtime import PostgreSQLRuntime  # noqa: E402
from review_agent_tools.postgres_migrations import runner  # noqa: E402
from review_agent_tools.settings import PostgresDatabaseUrl  # noqa: E402


DSN = os.environ.get("REVIEW_AGENT_POSTGRES_DSN", "")


def sha256_id(character: str) -> str:
    return "sha256:" + (character * 64)


@unittest.skipUnless(DSN, "run through scripts/check_postgres_schema.sh")
class PostgreSQLVerificationCoachingTests(unittest.TestCase):
    head_sha = "a" * 40
    now = datetime(2026, 8, 24, 8, 0, tzinfo=timezone.utc)

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
        return replace(item, **overrides)

    def start(self, pr_number: int) -> postgres_review_runs.StartedRun:
        result = review_run_application.start_postgres_review(
            self.runtime,
            review_run_application.PostgresRunRequest(
                provider="github",
                provider_repository_id=930,
                repository="example-org/example-repository",
                pr_number=pr_number,
                base_sha="b" * 40,
                head_sha=self.head_sha,
                policy_revision="profile@1",
                resolved_config_schema_version=1,
                resolved_config={"profile": "sundsvall-standard"},
                request_key=f"github:issue-comment:{4000 + pr_number}",
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
        return result

    def record_occurrence(
        self, run: postgres_review_runs.StartedRun
    ) -> review_finding_application.PostgresFindingBatch:
        return review_finding_application.record_postgres_findings(
            self.runtime,
            run_id=run.run.id,
            head_sha=self.head_sha,
            findings=(self.finding(),),
            changed_files=(
                review_finding_application.ChangedFile(
                    path="src/flags.py",
                    context_hash="c" * 40,
                    context_hash_source="blob",
                ),
            ),
        )

    def test_candidate_verification_is_unique_and_rejects_cross_run_occurrence(
        self,
    ) -> None:
        first = self.start(31)
        second = self.start(32)
        first_batch = self.record_occurrence(first)
        second_batch = self.record_occurrence(second)
        run_definition = resolve_verification_run(
            provider="claude",
            model="opus",
            mode="advise",
            status="completed",
            now=self.now,
        )
        candidate = resolve_candidate_verification(
            verdict="confirmed",
            confidence=0.93,
            notes="The evidence matches the exact reviewed bytes.",
            now=self.now,
        )

        with self.runtime.transaction() as connection:
            verification = postgres_verification.record_run(
                connection,
                review_run_id=first.run.id,
                definition=run_definition,
            )
            stored = postgres_verification.record_candidate(
                connection,
                verification_run_id=verification.id,
                occurrence_id=first_batch.items[0].occurrence_id,
                definition=candidate,
            )
        self.assertEqual(stored.verdict, "confirmed")

        with self.assertRaises(postgres_verification.CandidateVerificationConflict):
            with self.runtime.transaction() as connection:
                postgres_verification.record_candidate(
                    connection,
                    verification_run_id=verification.id,
                    occurrence_id=first_batch.items[0].occurrence_id,
                    definition=candidate,
                )
        with self.assertRaises(postgres_verification.VerificationScopeError):
            with self.runtime.transaction() as connection:
                postgres_verification.record_candidate(
                    connection,
                    verification_run_id=verification.id,
                    occurrence_id=second_batch.items[0].occurrence_id,
                    definition=candidate,
                )

        with self.runtime.transaction() as connection:
            count = connection.execute(
                "SELECT count(*) FROM review_agent.candidate_verifications"
            ).fetchone()
        self.assertEqual(count, (1,))

    def test_reconciliation_freezes_after_publication_preparation(self) -> None:
        first = self.start(31)
        second = self.start(32)
        first_batch = self.record_occurrence(first)
        self.record_occurrence(second)
        with self.runtime.transaction() as connection:
            other_verification = postgres_verification.record_run(
                connection,
                review_run_id=second.run.id,
                definition=resolve_verification_run(
                    provider="claude",
                    model="opus",
                    mode="advise",
                    status="completed",
                    now=self.now,
                ),
            )

        with self.assertRaises(postgres_verification.VerificationScopeError):
            with self.runtime.transaction() as connection:
                postgres_verification.reconcile_candidate(
                    connection,
                    review_run_id=first.run.id,
                    occurrence_id=first_batch.items[0].occurrence_id,
                    verification_run_id=other_verification.id,
                    definition=resolve_reconciliation(
                        final_decision="drop",
                        reason="Verifier evidence belongs to another run.",
                        now=self.now,
                    ),
                )

        with self.runtime.transaction() as connection:
            postgres_verification.reconcile_candidate(
                connection,
                review_run_id=first.run.id,
                occurrence_id=first_batch.items[0].occurrence_id,
                verification_run_id=None,
                definition=resolve_reconciliation(
                    final_decision="publish", now=self.now
                ),
            )
            reconciled = postgres_verification.reconcile_candidate(
                connection,
                review_run_id=first.run.id,
                occurrence_id=first_batch.items[0].occurrence_id,
                verification_run_id=None,
                definition=resolve_reconciliation(
                    final_decision="drop",
                    reason="The final falsification pass disproved the finding.",
                    now=self.now,
                ),
            )
            connection.execute(
                """
                INSERT INTO review_agent.publications (
                    pull_request_id, review_run_id, review_number,
                    publication_key, rendered_markdown,
                    rendered_blocks_schema_version, rendered_blocks,
                    rendered_hash, generated_at
                ) VALUES (%s, %s, 1, %s, 'review', 1, '[]'::jsonb, %s,
                          CURRENT_TIMESTAMP)
                """,
                (
                    first.run.pull_request_id,
                    first.run.id,
                    sha256_id("d"),
                    "e" * 64,
                ),
            )
        self.assertEqual(reconciled.final_decision, "drop")

        with self.assertRaises(postgres_verification.ReconciliationFrozen):
            with self.runtime.transaction() as connection:
                postgres_verification.reconcile_candidate(
                    connection,
                    review_run_id=first.run.id,
                    occurrence_id=first_batch.items[0].occurrence_id,
                    verification_run_id=None,
                    definition=resolve_reconciliation(
                        final_decision="publish", now=self.now
                    ),
                )

        with self.runtime.transaction() as connection:
            rows = postgres_verification.reconciliations_for_run(
                connection, review_run_id=first.run.id
            )
        self.assertEqual([item.final_decision for item in rows], ["drop"])

    def test_concurrent_publication_preparation_freezes_reconciliation(self) -> None:
        review = self.start(33)
        batch = self.record_occurrence(review)
        with self.runtime.transaction() as connection:
            postgres_verification.reconcile_candidate(
                connection,
                review_run_id=review.run.id,
                occurrence_id=batch.items[0].occurrence_id,
                verification_run_id=None,
                definition=resolve_reconciliation(
                    final_decision="publish", now=self.now
                ),
            )

        publication = psycopg.connect(DSN)
        self.addCleanup(publication.close)
        publication.execute(
            """
            INSERT INTO review_agent.publications (
                pull_request_id, review_run_id, review_number,
                publication_key, rendered_markdown,
                rendered_blocks_schema_version, rendered_blocks,
                rendered_hash, generated_at
            ) VALUES (%s, %s, 1, %s, 'review', 1, '[]'::jsonb, %s,
                      CURRENT_TIMESTAMP)
            """,
            (
                review.run.pull_request_id,
                review.run.id,
                sha256_id("f"),
                "1" * 64,
            ),
        )

        def revise() -> str:
            with psycopg.connect(
                DSN,
                application_name="review-agent-reconciliation-freeze-test",
                options="-c lock_timeout=7000 -c statement_timeout=10000",
            ) as connection:
                with connection.transaction():
                    try:
                        postgres_verification.reconcile_candidate(
                            connection,
                            review_run_id=review.run.id,
                            occurrence_id=batch.items[0].occurrence_id,
                            verification_run_id=None,
                            definition=resolve_reconciliation(
                                final_decision="drop",
                                reason=(
                                    "Concurrent publication owns the final "
                                    "decision."
                                ),
                                now=self.now,
                            ),
                        )
                    except postgres_verification.ReconciliationFrozen:
                        return "frozen"
            raise AssertionError("concurrent reconciliation was not frozen")

        with ThreadPoolExecutor(max_workers=1) as executor:
            result = executor.submit(revise)
            try:
                deadline = time.monotonic() + 5
                with psycopg.connect(DSN, autocommit=True) as observer:
                    while time.monotonic() < deadline:
                        waiting = observer.execute(
                            """
                            SELECT wait_event_type
                            FROM pg_stat_activity
                            WHERE application_name =
                                  'review-agent-reconciliation-freeze-test'
                            """
                        ).fetchone()
                        if waiting == ("Lock",):
                            break
                        time.sleep(0.01)
                    else:
                        self.fail("reconciliation did not wait for publication")
            finally:
                publication.commit()
            self.assertEqual(result.result(timeout=5), "frozen")

    @staticmethod
    def coach_input(*, route: str) -> CoachRunInput:
        return CoachRunInput(
            repository="example-org/example-repository",
            source_event_set_id=sha256_id("a"),
            source_snapshot_id=sha256_id("b"),
            proposal_set_id=sha256_id("c" if route == "skill" else "d"),
            decision="propose",
            events_considered=7,
            artifact_dir="/tmp/coach-artifacts",
            candidates=(
                CoachCandidateInput(
                    candidate_key="judgment-false-positive-abc123",
                    target_owner=route,
                    suggested_route="judgment_or_procedure",
                    event_type="false_positive",
                    independent_episode_count=2,
                    evidence_event_ids=(f"decision:{route}",),
                    evidence_events_total=2,
                ),
            ),
        )

    def test_coach_runs_retain_exact_candidate_sets_and_roll_back_together(
        self,
    ) -> None:
        review = self.start(31)
        with self.runtime.transaction() as connection:
            repository_id = RepositoryId(
                int(
                    connection.execute(
                        "SELECT repository_id FROM review_agent.pull_requests "
                        "WHERE id = %s",
                        (review.run.pull_request_id,),
                    ).fetchone()[0]
                )
            )
            first = postgres_coaching.record_run(
                connection,
                repository_id=repository_id,
                definition=resolve_coach_run(self.coach_input(route="skill")),
            )
            second = postgres_coaching.record_run(
                connection,
                repository_id=repository_id,
                definition=resolve_coach_run(self.coach_input(route="procedure")),
            )

        with self.runtime.transaction() as connection:
            stored_first = postgres_coaching.load_run(connection, run_id=first.id)
            stored_second = postgres_coaching.load_run(connection, run_id=second.id)
        self.assertEqual(stored_first.candidates[0].target_owner, "skill")
        self.assertEqual(stored_second.candidates[0].target_owner, "procedure")
        self.assertEqual(stored_first.candidates[0].evidence_event_ids, ("decision:skill",))
        self.assertEqual(
            stored_second.candidates[0].evidence_event_ids,
            ("decision:procedure",),
        )

        no_change_input = replace(
            self.coach_input(route="skill"),
            decision="no_change",
            proposal_set_id=sha256_id("e"),
            candidates=(),
        )
        with self.runtime.transaction() as connection:
            no_change = postgres_coaching.record_run(
                connection,
                repository_id=repository_id,
                definition=resolve_coach_run(no_change_input),
            )
            stored_no_change = postgres_coaching.load_run(
                connection, run_id=no_change.id
            )
        self.assertEqual(stored_no_change.decision, "no_change")
        self.assertEqual(stored_no_change.candidates, ())

        valid = resolve_coach_run(
            replace(
                self.coach_input(route="skill"),
                proposal_set_id=sha256_id("f"),
            )
        )
        duplicate = replace(
            valid,
            candidates=(valid.candidates[0], valid.candidates[0]),
        )
        with self.assertRaises(postgres_coaching.CoachCandidateConflict):
            with self.runtime.transaction() as connection:
                postgres_coaching.record_run(
                    connection,
                    repository_id=repository_id,
                    definition=duplicate,
                )

        inconsistent = replace(valid, candidates=())
        with self.assertRaises(postgres_coaching.CoachingStoreError):
            with self.runtime.transaction() as connection:
                postgres_coaching.record_run(
                    connection,
                    repository_id=repository_id,
                    definition=inconsistent,
                )

        with self.runtime.transaction() as connection:
            counts = connection.execute(
                "SELECT "
                "(SELECT count(*) FROM review_agent.coach_runs), "
                "(SELECT count(*) FROM review_agent.coach_candidates)"
            ).fetchone()
        self.assertEqual(counts, (3, 2))

    def intervention_candidate(self) -> postgres_coaching.CoachCandidate:
        review = self.start(41)
        with self.runtime.transaction() as connection:
            repository_id = RepositoryId(
                int(
                    connection.execute(
                        "SELECT repository_id FROM review_agent.pull_requests "
                        "WHERE id = %s",
                        (review.run.pull_request_id,),
                    ).fetchone()[0]
                )
            )
            run = postgres_coaching.record_run(
                connection,
                repository_id=repository_id,
                definition=resolve_coach_run(self.coach_input(route="profile")),
            )
        return run.candidates[0]

    @staticmethod
    def intervention_definition(
        candidate: postgres_coaching.CoachCandidate,
        *,
        outcome: CoachInterventionOutcome = "accepted",
        base_character: str = "2",
        include_evaluation: bool = True,
    ):
        return resolve_intervention_outcome(
            CoachInterventionOutcomeInput(
                coach_candidate_id=int(candidate.id),
                candidate_key=candidate.candidate_key,
                target_owner=candidate.target_owner,
                proposal_content_hash=sha256_id("1"),
                base_contract_hash=sha256_id(base_character),
                diff_hash=sha256_id("3") if include_evaluation else "",
                validation_receipt_hash=(
                    sha256_id("4") if include_evaluation else ""
                ),
                outcome=outcome,
                reason="Focused replay evaluated the exact intervention.",
                actor="github:maintainer",
            )
        )

    def test_intervention_outcomes_persist_evaluated_and_weak_results(self) -> None:
        candidate = self.intervention_candidate()
        accepted = self.intervention_definition(candidate)
        regression = self.intervention_definition(
            candidate,
            outcome="rejected_regression",
            base_character="5",
        )
        insufficient = self.intervention_definition(
            candidate,
            outcome="rejected_insufficient_evidence",
            base_character="6",
            include_evaluation=False,
        )

        with self.runtime.transaction() as connection:
            stored = tuple(
                postgres_coaching.record_intervention_outcome(connection, item)
                for item in (accepted, regression, insufficient)
            )

        self.assertEqual(
            tuple(item.outcome for item in stored),
            ("accepted", "rejected_regression", "rejected_insufficient_evidence"),
        )
        self.assertIsNone(stored[2].diff_hash)
        self.assertIsNone(stored[2].validation_receipt_hash)

    def test_duplicate_intervention_key_is_rejected(self) -> None:
        candidate = self.intervention_candidate()
        definition = self.intervention_definition(candidate)

        with self.runtime.transaction() as connection:
            postgres_coaching.record_intervention_outcome(connection, definition)
        with self.assertRaises(postgres_coaching.CoachInterventionConflict):
            with self.runtime.transaction() as connection:
                postgres_coaching.record_intervention_outcome(connection, definition)

    def test_operator_records_and_reads_one_private_intervention(self) -> None:
        candidate = self.intervention_candidate()
        stored = operator_application.record_coach_intervention(
            self.runtime,
            operator_application.CoachInterventionOutcomeRequest(
                repository="example-org/example-repository",
                proposal_set_id=sha256_id("d"),
                candidate_key=candidate.candidate_key,
                target_owner=candidate.target_owner,
                proposal_content_hash=sha256_id("1"),
                base_contract_hash=sha256_id("2"),
                diff_hash=sha256_id("3"),
                validation_receipt_hash=sha256_id("4"),
                outcome="accepted",
                reason="Focused replay evaluated the exact intervention.",
                actor="github:maintainer",
            ),
        )
        history = operator_application.coach_intervention_history(
            self.runtime,
            repository="example-org/example-repository",
            candidate_key=candidate.candidate_key,
            limit=1,
        )

        self.assertEqual(history.interventions, (stored,))
        with self.assertRaisesRegex(
            operator_application.OperatorInputError, "must not exceed 100"
        ):
            operator_application.coach_intervention_history(
                self.runtime,
                repository="example-org/example-repository",
                candidate_key=candidate.candidate_key,
                limit=101,
            )

    def test_candidate_resolution_requires_exact_repository_and_candidate(self) -> None:
        candidate = self.intervention_candidate()
        with self.runtime.transaction() as connection:
            resolved = postgres_coaching.resolve_intervention_candidate(
                connection,
                repository="example-org/example-repository",
                proposal_set_id=sha256_id("d"),
                candidate_key=candidate.candidate_key,
                target_owner="profile",
            )
        self.assertEqual(resolved.id, candidate.id)

        with self.runtime.transaction() as connection:
            original_run = postgres_coaching.load_run(
                connection, run_id=candidate.coach_run_id
            )
            assert original_run.repository_id is not None
            duplicate = postgres_coaching.record_run(
                connection,
                repository_id=original_run.repository_id,
                definition=resolve_coach_run(self.coach_input(route="profile")),
            )
            resolved_after_replay = postgres_coaching.resolve_intervention_candidate(
                connection,
                repository="example-org/example-repository",
                proposal_set_id=sha256_id("d"),
                candidate_key=candidate.candidate_key,
                target_owner="profile",
            )
        self.assertNotEqual(duplicate.candidates[0].id, candidate.id)
        self.assertEqual(resolved_after_replay.id, candidate.id)

        with self.assertRaises(postgres_coaching.CoachCandidateNotFound):
            with self.runtime.transaction() as connection:
                postgres_coaching.resolve_intervention_candidate(
                    connection,
                    repository="example-org/example-repository",
                    proposal_set_id=sha256_id("d"),
                    candidate_key="unknown-candidate",
                    target_owner="profile",
                )
        with self.assertRaises(postgres_coaching.CoachCandidateProvenanceMismatch):
            with self.runtime.transaction() as connection:
                postgres_coaching.resolve_intervention_candidate(
                    connection,
                    repository="another-org/another-repository",
                    proposal_set_id=sha256_id("d"),
                    candidate_key=candidate.candidate_key,
                    target_owner="profile",
                )

    def test_intervention_history_is_bounded_and_newest_first(self) -> None:
        candidate = self.intervention_candidate()
        definitions = tuple(
            self.intervention_definition(candidate, base_character=character)
            for character in ("7", "8", "9")
        )
        with self.runtime.transaction() as connection:
            stored = tuple(
                postgres_coaching.record_intervention_outcome(connection, item)
                for item in definitions
            )
        with self.runtime.transaction() as connection:
            history = postgres_coaching.intervention_history(
                connection,
                repository="example-org/example-repository",
                candidate_key=candidate.candidate_key,
                limit=2,
            )

        self.assertEqual(history.repository, "example-org/example-repository")
        self.assertEqual(history.candidate_key, candidate.candidate_key)
        self.assertEqual(history.target_owners, ("profile",))
        self.assertEqual(history.coach_run_count, 1)
        self.assertEqual(history.maximum_independent_episodes, 2)
        self.assertEqual(
            tuple(item.id for item in history.interventions),
            (stored[2].id, stored[1].id),
        )


if __name__ == "__main__":
    unittest.main()
