from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bootstrap" / "plugins"))

from review_agent_tools import review_run_application  # noqa: E402
from review_agent_tools.documentation_scope import (
    ChangedPath,
    DocumentationScope,
    ScopeExclusion,
    SelectedArea,
)  # noqa: E402
from review_agent_tools.domain.documentation_review import (  # noqa: E402
    DocumentationOutcome,
    DocumentationReviewError,
    EvidenceRead,
    EvidenceRole,
    decode_scope,
)
from review_agent_tools.domain.review import ReviewPurpose, ReviewRunId  # noqa: E402
from review_agent_tools.postgres import documentation_reviews, review_runs  # noqa: E402
from review_agent_tools.postgres.runtime import PostgreSQLRuntime  # noqa: E402
from review_agent_tools.postgres_migrations import runner  # noqa: E402
from review_agent_tools.settings import PostgresDatabaseUrl  # noqa: E402

DSN = os.environ.get("REVIEW_AGENT_POSTGRES_DSN", "")
BASE, COMPARISON, HEAD = "b" * 40, "c" * 40, "a" * 40


def scope() -> DocumentationScope:
    return DocumentationScope(
        base_sha=BASE,
        comparison_sha=COMPARISON,
        head_sha=HEAD,
        status="scoped",
        active_policy=True,
        policy_hash="sha256:" + "d" * 64,
        proposal_status="unchanged",
        proposal_detail=None,
        changed_files=(ChangedPath("M", "src/api.py"),),
        areas=(
            SelectedArea(
                "api", "Keep usage accurate", ("src/api.py",), ("docs/api.md",)
            ),
        ),
        documents=("docs/api.md",),
        exclusions=(),
        unmapped_paths=(),
        incomplete_reasons=(),
    )


def evidence() -> EvidenceRead:
    return EvidenceRead(
        path="docs/api.md",
        role=EvidenceRole.HEAD,
        revision=HEAD,
        blob_sha="e" * 40,
        start_line=1,
        end_line=12,
        content_sha256="f" * 64,
        total_lines=12,
    )


class DocumentationReceiptContractTests(unittest.TestCase):
    def test_strict_receipt_decoding_and_successful_evidence_require_provenance(
        self,
    ) -> None:
        original = scope()
        self.assertEqual(decode_scope(original.to_json_obj()), original)
        with self.assertRaises(DocumentationReviewError):
            decode_scope({**original.to_json_obj(), "schema_version": True})
        with self.assertRaises(DocumentationReviewError):
            decode_scope({**original.to_json_obj(), "unknown": []})
        with self.assertRaises(DocumentationReviewError):
            replace(evidence(), blob_sha=None)
        with self.assertRaises(DocumentationReviewError):
            replace(evidence(), start_line=True)
        with self.assertRaises(DocumentationReviewError):
            replace(evidence(), total_lines=5)
        empty = replace(evidence(), start_line=None, end_line=None, total_lines=0)
        self.assertIsNone(empty.start_line)


@unittest.skipUnless(DSN, "REVIEW_AGENT_POSTGRES_DSN is required")
class PostgreSQLDocumentationReviewsTests(unittest.TestCase):
    def setUp(self) -> None:
        import psycopg

        with psycopg.connect(DSN, autocommit=True) as connection:
            connection.execute("DROP SCHEMA IF EXISTS review_agent CASCADE")
            runner.apply_migrations(connection)
        self.runtime = PostgreSQLRuntime(PostgresDatabaseUrl(DSN))
        self.runtime.open()
        self.addCleanup(self.runtime.close)

    def start(
        self, *, purpose: ReviewPurpose = ReviewPurpose.DOCUMENTATION
    ) -> ReviewRunId:
        started = review_run_application.start_postgres_review(
            self.runtime,
            review_run_application.PostgresRunRequest(
                provider="github",
                provider_repository_id=901,
                repository="team/repository",
                pr_number=17,
                base_sha=BASE,
                head_sha=HEAD,
                policy_revision="profile@1",
                resolved_config_schema_version=1,
                resolved_config={},
                request_key=f"request:{purpose.value}",
                purpose=purpose,
            ),
        )
        return started.run.id

    def test_preflight_persists_an_explicit_exclusion_without_semantic_context(self) -> None:
        from review_agent_tools import documentation_preflight
        from review_agent_tools.postgres import jobs
        from review_agent_tools.review_tool_runtime import GatewaySourceSession
        from tests.test_documentation_preflight import POLICY, policy

        run_id = self.start()
        review_run_application.register_postgres_changed_files(
            self.runtime, run_id=run_id,
            files=(review_run_application.PostgresChangedFile(path="tests/api.py", change_status="modified"),),
            changed_files_reported=1, registration_complete=True,
        )
        with self.runtime.transaction() as connection:
            documentation_reviews.initialize(connection, run_id=run_id, comparison_sha=COMPARISON)
            jobs.enqueue_run(connection, review_run_id=run_id, priority=0, max_attempts=3, active_job_limit=10)
            job = jobs.claim_next_job(connection, lease_owner="preflight-worker",
                lease_duration=timedelta(minutes=5), priority_aging_interval=timedelta(minutes=15))
        client = Mock()
        client.get_documentation_policy.side_effect = [policy(POLICY, BASE), policy(POLICY, HEAD)]
        source = GatewaySourceSession(run_id=int(run_id),
            lease=jobs.WorkerLeaseSession(job_id=job.id, lease_generation=job.lease_generation), client=client)
        with patch.object(documentation_preflight.github_app, "authorize_documentation_review"), patch.object(
            documentation_preflight, "initialize_review", return_value=Mock(file_index=Mock(registration_complete=True)),
        ), patch.object(documentation_preflight.repository_guidance_context, "load") as guidance:
            result = documentation_preflight.run_preflight(self.runtime, source, Mock())
        self.assertTrue(result.frozen)
        self.assertEqual(result.outcome, DocumentationOutcome.NOT_NEEDED)
        self.assertFalse(result.semantic_inference_used)
        self.assertTrue(result.coverage_complete)
        guidance.assert_not_called()
        with self.runtime.transaction() as connection:
            self.assertEqual(documentation_reviews.get_result(connection, run_id=run_id), result)

    def test_preflight_only_freezes_verified_comparison_absence(self) -> None:
        from review_agent_tools import documentation_preflight
        from review_agent_tools.github.gateway import GitHubGatewayRejected
        from review_agent_tools.postgres import jobs
        from review_agent_tools.review_tool_runtime import GatewaySourceSession
        from tests.test_documentation_preflight import POLICY, policy
        run_id = self.start()
        review_run_application.register_postgres_changed_files(self.runtime, run_id=run_id,
            files=(review_run_application.PostgresChangedFile(path="tests/api.py", change_status="modified"),),
            changed_files_reported=1, registration_complete=True)
        with self.runtime.transaction() as connection:
            jobs.enqueue_run(connection, review_run_id=run_id, priority=0, max_attempts=3, active_job_limit=10)
            job = jobs.claim_next_job(connection, lease_owner="preflight-worker", lease_duration=timedelta(minutes=5), priority_aging_interval=timedelta(minutes=15))
        client = Mock()
        client.get_documentation_policy.side_effect = [policy(POLICY, BASE), policy(POLICY, HEAD)]
        source = GatewaySourceSession(run_id=int(run_id), lease=jobs.WorkerLeaseSession(job_id=job.id, lease_generation=job.lease_generation), client=client)
        with patch.object(documentation_preflight.github_app, "authorize_documentation_review"), patch.object(documentation_preflight, "initialize_review", return_value=Mock(file_index=Mock(registration_complete=True))):
            client.get_documentation_comparison.side_effect = GitHubGatewayRejected("github_read_invalid")
            with self.assertRaisesRegex(GitHubGatewayRejected, "github_read_invalid"):
                documentation_preflight.run_preflight(self.runtime, source, Mock())
            with self.runtime.transaction() as connection:
                self.assertIsNone(documentation_reviews.get_result(connection, run_id=run_id))
            client.get_documentation_comparison.side_effect = GitHubGatewayRejected("documentation_comparison_unavailable")
            result = documentation_preflight.run_preflight(self.runtime, source, Mock())
        self.assertEqual(result.outcome, DocumentationOutcome.UNAVAILABLE)
        self.assertTrue(result.frozen)
        self.assertIsNone(result.comparison_sha)

    def test_preparation_rejects_cancelled_worker_before_advancing_phase(self) -> None:
        from review_agent_tools import review_publication_application
        from review_agent_tools.postgres import jobs
        run_id = self.start()
        with self.runtime.transaction() as connection:
            documentation_reviews.initialize(connection, run_id=run_id, comparison_sha=COMPARISON)
            documentation_reviews.freeze_scope(connection, run_id=run_id, scope=scope())
            documentation_reviews.finalize(connection, run_id=run_id, outcome=DocumentationOutcome.INCOMPLETE,
                semantic_inference_used=False, incomplete_reasons=("Provider document unavailable.",))
            jobs.enqueue_run(connection, review_run_id=run_id, priority=0, max_attempts=3, active_job_limit=10)
            job = jobs.claim_next_job(connection, lease_owner="preflight-worker", lease_duration=timedelta(minutes=5), priority_aging_interval=timedelta(minutes=15))
            connection.execute("UPDATE review_agent.review_jobs SET last_heartbeat_at = started_at, lease_expires_at = statement_timestamp() WHERE id = %s", (job.id,))
        with patch.object(review_runs, "advance_phase", wraps=review_runs.advance_phase) as advance:
            with self.assertRaises(jobs.ReviewJobLeaseLost):
                review_publication_application.prepare_postgres_documentation_publication(
                    self.runtime, run_id=int(run_id), max_comment_bytes=60_000,
                    review_job_id=job.id, review_lease_generation=job.lease_generation)
            advance.assert_not_called()
        with self.runtime.transaction() as connection:
            review_runs.fail_run(connection, run_id, failure_code="documentation_disabled")
            jobs.reconcile_run_jobs(connection, run_ids=(run_id,), status=review_runs.get_run(connection, run_id).status)
        with patch.object(review_runs, "advance_phase", wraps=review_runs.advance_phase) as advance:
            with self.assertRaises(jobs.ReviewJobLeaseLost):
                review_publication_application.prepare_postgres_documentation_publication(
                    self.runtime, run_id=int(run_id), max_comment_bytes=60_000,
                    review_job_id=job.id, review_lease_generation=job.lease_generation)
            advance.assert_not_called()
        with self.runtime.transaction() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM review_agent.publications WHERE review_run_id = %s", (run_id,)).fetchone(), (0,))

    def test_semantic_finalization_fences_lease_and_authority_before_atomic_receipt(
        self,
    ) -> None:
        from review_agent_tools.documentation_findings import (
            finalize_documentation_assessment,
        )
        from review_agent_tools.domain.review import ReviewPhase
        from review_agent_tools.postgres import findings, jobs, repository_decisions
        from review_agent_tools.repository_decision_context import not_configured
        from tests.test_documentation_findings import input_fixture, result_fixture

        run_id = self.start()
        observed = replace(result_fixture(), run_id=run_id)
        review_run_application.register_postgres_changed_files(
            self.runtime,
            run_id=run_id,
            files=(
                review_run_application.PostgresChangedFile(
                    path="src/api.py", change_status="modified"
                ),
            ),
            changed_files_reported=1,
            registration_complete=True,
        )
        with self.runtime.transaction() as connection:
            documentation_reviews.initialize(
                connection, run_id=run_id, comparison_sha=COMPARISON
            )
            documentation_reviews.freeze_scope(connection, run_id=run_id, scope=scope())
            for read in observed.evidence:
                documentation_reviews.record_evidence(
                    connection, run_id=run_id, evidence=read
                )
            repository_decisions.store_context(
                connection, run_id=run_id, context=not_configured(base_sha=BASE)
            )
            for phase in (
                ReviewPhase.FETCHING_PR,
                ReviewPhase.COLLECTING_DIFF,
                ReviewPhase.REVIEWING,
            ):
                review_runs.advance_phase(connection, run_id, phase)
            jobs.enqueue_run(
                connection,
                review_run_id=run_id,
                priority=0,
                max_attempts=3,
                active_job_limit=10,
            )
            job = jobs.claim_next_job(
                connection,
                lease_owner="docs-worker",
                lease_duration=timedelta(minutes=5),
                priority_aging_interval=timedelta(minutes=15),
            )
        assert job is not None
        args = dict(
            run_id=run_id,
            job_id=job.id,
            lease_generation=job.lease_generation,
            assessment=input_fixture(observed),
        )
        with patch(
            "review_agent_tools.documentation_findings.github_app.authorize_documentation_review"
        ) as authority:
            with self.assertRaises(jobs.ReviewJobLeaseLost):
                finalize_documentation_assessment(
                    self.runtime,
                    **{**args, "lease_generation": job.lease_generation + 1},
                )
            authority.side_effect = PermissionError("disabled")
            with self.assertRaises(PermissionError):
                finalize_documentation_assessment(self.runtime, **args)
            with self.runtime.transaction() as connection:
                retained = documentation_reviews.get_result(connection, run_id=run_id)
                assert retained is not None
                self.assertIsNone(retained.assessment)
                self.assertFalse(retained.frozen)
            authority.side_effect = None
            frozen = finalize_documentation_assessment(self.runtime, **args)
            self.assertTrue(frozen.frozen)
            self.assertEqual(frozen.outcome, DocumentationOutcome.FINDINGS)
            self.assertTrue(frozen.coverage_complete)
            self.assertEqual(authority.call_args.args[1], 901)
            with self.assertRaisesRegex(DocumentationReviewError, "frozen"):
                finalize_documentation_assessment(self.runtime, **args)
        with self.runtime.transaction() as connection:
            stored = documentation_reviews.get_result(connection, run_id=run_id)
            self.assertEqual(stored, frozen)
            rows = connection.execute(
                "SELECT identity.path FROM review_agent.finding_occurrences AS occurrence JOIN review_agent.finding_identities AS identity ON identity.id = occurrence.finding_id WHERE occurrence.review_run_id = %s",
                (run_id,),
            ).fetchall()
            self.assertEqual(rows, [("docs/api.md",)])
            from review_agent_tools.domain.finding import (
                resolve_finding_content,
                FindingContent,
            )

            from review_agent_tools import operator_application

            target = connection.execute(
                "SELECT identity.fingerprint, occurrence.id FROM review_agent.finding_occurrences occurrence JOIN review_agent.finding_identities identity ON identity.id = occurrence.finding_id WHERE occurrence.review_run_id = %s",
                (run_id,),
            ).fetchone()
            request = operator_application.OperatorDecisionRequest(
                repository="team/repository", fingerprint=target[0], occurrence_id=target[1],
                decision="false_positive", reason="The documentation already describes this behavior", actor="operator:test",
            )
            decision = operator_application.decide_finding_in_transaction(connection, request)
            self.assertEqual(int(decision.occurrence_id), target[1])
            with self.assertRaisesRegex(operator_application.OperatorInputError, "Documentation findings"):
                operator_application.decide_finding_in_transaction(
                    connection, replace(request, decision="intentional_by_design", adr_id="ADR-0001"),
                )

            payload = input_fixture(observed)["findings"][0]["content"]
            forged = resolve_finding_content(
                FindingContent(**{**payload, "title": "Unvalidated mutation"}),
                context_hash="f" * 64,
            )
            with self.assertRaises(findings.FindingConflict):
                findings.record_findings(
                    connection,
                    run_id=run_id,
                    expected_head_sha=HEAD,
                    definitions=(forged,),
                )

    def test_scope_evidence_and_outcome_are_idempotent_and_frozen_per_run(self) -> None:
        run_id = self.start()
        with self.runtime.transaction() as connection:
            initialized = documentation_reviews.initialize(
                connection, run_id=run_id, comparison_sha=COMPARISON
            )
            self.assertEqual((initialized.base_sha, initialized.head_sha), (BASE, HEAD))
            self.assertEqual(
                initialized,
                documentation_reviews.initialize(
                    connection, run_id=run_id, comparison_sha=COMPARISON
                ),
            )
            documentation_reviews.freeze_scope(connection, run_id=run_id, scope=scope())
            first = documentation_reviews.record_evidence(
                connection, run_id=run_id, evidence=evidence()
            )
            self.assertEqual(
                first,
                documentation_reviews.record_evidence(
                    connection, run_id=run_id, evidence=evidence()
                ),
            )
            self.assertEqual(len(first.evidence), 1)
            with self.assertRaisesRegex(DocumentationReviewError, "contradicts"):
                documentation_reviews.record_evidence(
                    connection,
                    run_id=run_id,
                    evidence=replace(evidence(), blob_sha="0" * 40),
                )
            frozen = documentation_reviews.finalize(
                connection,
                run_id=run_id,
                outcome=DocumentationOutcome.NO_MISMATCH_FOUND,
                semantic_inference_used=True,
                coverage_complete=True,
            )
            self.assertTrue(frozen.frozen)
            self.assertEqual(
                frozen, documentation_reviews.get_result(connection, run_id=run_id)
            )
            self.assertEqual(
                frozen,
                documentation_reviews.finalize(
                    connection,
                    run_id=run_id,
                    outcome=DocumentationOutcome.NO_MISMATCH_FOUND,
                    semantic_inference_used=True,
                    coverage_complete=True,
                ),
            )
            self.assertEqual(
                frozen,
                documentation_reviews.record_evidence(
                    connection, run_id=run_id, evidence=evidence()
                ),
            )
            with self.assertRaisesRegex(DocumentationReviewError, "frozen"):
                documentation_reviews.record_evidence(
                    connection,
                    run_id=run_id,
                    evidence=replace(evidence(), start_line=2),
                )
            with self.assertRaisesRegex(DocumentationReviewError, "rewritten"):
                documentation_reviews.freeze_scope(
                    connection, run_id=run_id, scope=replace(scope(), documents=())
                )
            with self.assertRaisesRegex(DocumentationReviewError, "rewritten"):
                documentation_reviews.finalize(
                    connection,
                    run_id=run_id,
                    outcome=DocumentationOutcome.FINDINGS,
                    semantic_inference_used=True,
                    coverage_complete=True,
                )

    def test_purpose_exact_role_revision_and_missing_comparison_are_enforced(
        self,
    ) -> None:
        code_id = self.start(purpose=ReviewPurpose.CODE)
        docs_id = self.start()
        with self.runtime.transaction() as connection:
            with self.assertRaisesRegex(DocumentationReviewError, "documentation run"):
                documentation_reviews.initialize(
                    connection, run_id=code_id, comparison_sha=COMPARISON
                )
            documentation_reviews.initialize(
                connection, run_id=docs_id, comparison_sha=COMPARISON
            )
            with self.assertRaisesRegex(DocumentationReviewError, "rewritten"):
                documentation_reviews.initialize(
                    connection, run_id=docs_id, comparison_sha=BASE
                )
            with self.assertRaisesRegex(DocumentationReviewError, "immutable run refs"):
                documentation_reviews.freeze_scope(
                    connection,
                    run_id=docs_id,
                    scope=replace(scope(), base_sha=COMPARISON),
                )
            with self.assertRaisesRegex(DocumentationReviewError, "revision"):
                documentation_reviews.record_evidence(
                    connection,
                    run_id=docs_id,
                    evidence=replace(
                        evidence(), role=EvidenceRole.COMPARISON, revision=BASE
                    ),
                )
            recorded = documentation_reviews.record_evidence(
                connection,
                run_id=docs_id,
                evidence=replace(
                    evidence(), role=EvidenceRole.COMPARISON, revision=COMPARISON
                ),
            )
            self.assertEqual(recorded.evidence[0].role, EvidenceRole.COMPARISON)

    def test_clean_requires_selected_coverage_and_incomplete_findings_remain_honest(
        self,
    ) -> None:
        run_id = self.start()
        with self.runtime.transaction() as connection:
            documentation_reviews.initialize(
                connection, run_id=run_id, comparison_sha=COMPARISON
            )
            documentation_reviews.freeze_scope(connection, run_id=run_id, scope=scope())
            with self.assertRaisesRegex(
                DocumentationReviewError, "verified selected coverage"
            ):
                documentation_reviews.finalize(
                    connection,
                    run_id=run_id,
                    outcome=DocumentationOutcome.NO_MISMATCH_FOUND,
                    semantic_inference_used=True,
                    coverage_complete=True,
                )
            documentation_reviews.record_evidence(
                connection,
                run_id=run_id,
                evidence=EvidenceRead(
                    path="docs/api.md",
                    role=EvidenceRole.HEAD,
                    revision=HEAD,
                    unavailable_reason="response_truncated",
                ),
            )
            with self.assertRaisesRegex(DocumentationReviewError, "incomplete"):
                documentation_reviews.finalize(
                    connection,
                    run_id=run_id,
                    outcome=DocumentationOutcome.NO_MISMATCH_FOUND,
                    semantic_inference_used=True,
                    coverage_complete=True,
                )
            result = documentation_reviews.finalize(
                connection,
                run_id=run_id,
                outcome=DocumentationOutcome.FINDINGS,
                semantic_inference_used=True,
            )
            self.assertFalse(result.coverage_complete)
            self.assertEqual(result.incomplete_reasons, ("response_truncated",))

    def test_explicit_exclusion_is_zero_model(self) -> None:
        run_id = self.start()
        with self.runtime.transaction() as connection:
            documentation_reviews.initialize(
                connection, run_id=run_id, comparison_sha=COMPARISON
            )
            excluded = replace(
                scope(),
                areas=(),
                documents=(),
                exclusions=(
                    ScopeExclusion("src/api.py", "change", "No public contract"),
                ),
            )
            documentation_reviews.freeze_scope(
                connection, run_id=run_id, scope=excluded
            )
            result = documentation_reviews.finalize(
                connection,
                run_id=run_id,
                outcome=DocumentationOutcome.NOT_NEEDED,
                semantic_inference_used=False,
            )
            self.assertTrue(result.coverage_complete)
            self.assertFalse(result.semantic_inference_used)

    def test_unmapped_input_cannot_be_skipped_and_evidence_limit_preserves_receipt(
        self,
    ) -> None:
        from psycopg.types.json import Jsonb

        run_id = self.start()
        with self.runtime.transaction() as connection:
            documentation_reviews.initialize(
                connection, run_id=run_id, comparison_sha=COMPARISON
            )
            documentation_reviews.freeze_scope(
                connection,
                run_id=run_id,
                scope=replace(
                    scope(), areas=(), documents=(), unmapped_paths=("src/api.py",)
                ),
            )
            with self.assertRaisesRegex(DocumentationReviewError, "unmapped"):
                documentation_reviews.finalize(
                    connection,
                    run_id=run_id,
                    outcome=DocumentationOutcome.NOT_NEEDED,
                    semantic_inference_used=False,
                )
            retained = [
                replace(evidence(), path=f"docs/{number}.md").to_json_obj()
                for number in range(512)
            ]
            connection.execute(
                "UPDATE review_agent.documentation_reviews SET evidence_json = %s WHERE review_run_id = %s",
                (Jsonb(retained), run_id),
            )
            with self.assertRaises(documentation_reviews.DocumentationEvidenceLimit):
                documentation_reviews.record_evidence(
                    connection, run_id=run_id, evidence=evidence()
                )
            result = documentation_reviews.finalize(
                connection,
                run_id=run_id,
                outcome=DocumentationOutcome.INCOMPLETE,
                semantic_inference_used=True,
                incomplete_reasons=("evidence_read_limit",),
            )
            self.assertEqual(len(result.evidence), 512)
            self.assertFalse(result.coverage_complete)

    def test_publication_and_terminal_run_prevent_mutation_before_explicit_freeze(
        self,
    ) -> None:
        run_id = self.start()
        with self.runtime.transaction() as connection:
            documentation_reviews.initialize(
                connection, run_id=run_id, comparison_sha=COMPARISON
            )
            documentation_reviews.freeze_scope(connection, run_id=run_id, scope=scope())
            with connection.transaction(force_rollback=True):
                connection.execute(
                    """
                    INSERT INTO review_agent.publications
                        (pull_request_id, review_run_id, purpose, review_number, publication_key,
                         rendered_markdown, rendered_blocks_schema_version, rendered_blocks,
                         rendered_hash, generated_at)
                    SELECT pull_request_id, id, purpose, 1, %s, '', 1, '[]'::jsonb, %s,
                           clock_timestamp()
                    FROM review_agent.review_runs WHERE id = %s
                    """,
                    ("sha256:" + "1" * 64, "2" * 64, run_id),
                )
                with self.assertRaisesRegex(DocumentationReviewError, "frozen"):
                    documentation_reviews.record_evidence(
                        connection, run_id=run_id, evidence=evidence()
                    )
            review_runs.fail_run(connection, run_id, failure_code="review_failed")
            with self.assertRaisesRegex(DocumentationReviewError, "no longer live"):
                documentation_reviews.record_evidence(
                    connection, run_id=run_id, evidence=evidence()
                )

    def test_byte_exhaustion_reserves_space_for_the_incomplete_result(self) -> None:
        run_id = self.start()
        with self.runtime.transaction() as connection:
            documentation_reviews.initialize(
                connection, run_id=run_id, comparison_sha=COMPARISON
            )
            documentation_reviews.freeze_scope(connection, run_id=run_id, scope=scope())
            for number in range(512):
                read = EvidenceRead(
                    path=f"docs/{number}/" + "x" * 450,
                    role=EvidenceRole.HEAD,
                    revision=HEAD,
                    unavailable_reason=f"{number}:" + "y" * 490,
                )
                try:
                    documentation_reviews.record_evidence(
                        connection, run_id=run_id, evidence=read
                    )
                except documentation_reviews.DocumentationEvidenceLimit:
                    break
            else:
                self.fail(
                    "long evidence receipts must reach the byte bound before the count bound"
                )
            result = documentation_reviews.finalize(
                connection,
                run_id=run_id,
                outcome=DocumentationOutcome.INCOMPLETE,
                semantic_inference_used=False,
                incomplete_reasons=("evidence_byte_limit",),
            )
            self.assertTrue(result.frozen)
            self.assertFalse(result.coverage_complete)
            self.assertIn("evidence_byte_limit", result.incomplete_reasons)

    def test_missing_comparison_and_terminal_run_cannot_accept_new_evidence(
        self,
    ) -> None:
        run_id = self.start()
        with self.runtime.transaction() as connection:
            documentation_reviews.initialize(
                connection, run_id=run_id, comparison_sha=None
            )
            missing = replace(
                scope(),
                comparison_sha=None,
                status="unavailable",
                incomplete_reasons=("comparison_base_unavailable",),
            )
            documentation_reviews.freeze_scope(connection, run_id=run_id, scope=missing)
            with self.assertRaisesRegex(DocumentationReviewError, "revision"):
                documentation_reviews.record_evidence(
                    connection,
                    run_id=run_id,
                    evidence=replace(
                        evidence(), role=EvidenceRole.COMPARISON, revision=COMPARISON
                    ),
                )
            result = documentation_reviews.finalize(
                connection,
                run_id=run_id,
                outcome=DocumentationOutcome.UNAVAILABLE,
                semantic_inference_used=False,
            )
            self.assertFalse(result.coverage_complete)
            self.assertEqual(
                result.incomplete_reasons, ("comparison_base_unavailable",)
            )
        with self.runtime.transaction() as connection:
            review_runs.fail_run(connection, run_id, failure_code="review_failed")
            with self.assertRaisesRegex(DocumentationReviewError, "frozen"):
                documentation_reviews.record_evidence(
                    connection, run_id=run_id, evidence=evidence()
                )


if __name__ == "__main__":
    unittest.main()
