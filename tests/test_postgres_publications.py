from __future__ import annotations

import hashlib
import os
import sys
import unittest
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import psycopg


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = ROOT / "bootstrap" / "plugins"
sys.path.insert(0, str(PACKAGE_ROOT))

from review_agent_tools import (  # noqa: E402
    review_finding_application,
    review_publication_application,
    review_run_application,
)
from review_agent_tools.domain.finding import FindingInput  # noqa: E402
from review_agent_tools.domain.publication import (  # noqa: E402
    PublicationDomainError,
    PublicationFindingInput,
    PublicationFindingOutcome,
    PublicationPartInput,
    PublicationPartType,
    PublicationPlan,
    resolve_publication_plan,
)
from review_agent_tools.domain.review import ReviewPhase, ReviewStatus  # noqa: E402
from review_agent_tools.github.publication import (  # noqa: E402
    GitHubPublicationError,
    InlineReviewComment,
    IssueComment,
    PullRequestReview,
    PullRequestReviewComment,
    PullRequestState,
)
from review_agent_tools.postgres import publications  # noqa: E402
from review_agent_tools.postgres import review_runs  # noqa: E402
from review_agent_tools.postgres.runtime import PostgreSQLRuntime  # noqa: E402
from review_agent_tools.postgres_migrations import runner  # noqa: E402
from review_agent_tools.settings import PostgresDatabaseUrl  # noqa: E402


DSN = os.environ.get("REVIEW_AGENT_POSTGRES_DSN", "")


class ProcessDeath(BaseException):
    pass


class FakePostgresPublicationGitHub:
    def __init__(self, runtime: PostgreSQLRuntime) -> None:
        self.runtime = runtime
        self.comments: list[IssueComment] = []
        self.review_comments: list[PullRequestReviewComment] = []
        self.next_comment_id = 700
        self.next_review_id = 800
        self.kill_after_create = False
        self.fail_on_call = False
        self.head_sha = "a" * 40
        self.issue_comments_newest_first = False
        self.create_calls = 0
        self.fail_on_create_call: int | None = None
        self.fail_on_review_create = False
        self.list_issue_comments_calls = 0
        self.fail_on_list_call: int | None = None

    def _outside_transaction(self) -> None:
        if self.fail_on_call:
            raise AssertionError("GitHub must not be called on direct-ID recovery")
        metrics = self.runtime.pool_metrics()
        if metrics.available != metrics.size:
            raise AssertionError("database connection remained checked out during GitHub")

    def current_user_login(self) -> str:
        self._outside_transaction()
        return "review-agent[bot]"

    def get_pull_request(self, repository: str, pr_number: int) -> PullRequestState:
        self._outside_transaction()
        return PullRequestState(
            state="open",
            draft=False,
            base_sha="b" * 40,
            head_sha=self.head_sha,
        )

    def list_issue_comments(
        self,
        repository: str,
        issue_number: int,
        *,
        max_pages: int = 3,
        newest_first: bool = False,
    ) -> list[IssueComment]:
        self._outside_transaction()
        self.list_issue_comments_calls += 1
        if self.fail_on_list_call == self.list_issue_comments_calls:
            raise GitHubPublicationError("github_unreachable")
        self.issue_comments_newest_first = (
            self.issue_comments_newest_first or newest_first
        )
        comments = list(self.comments)
        return list(reversed(comments)) if newest_first else comments

    def create_issue_comment(
        self, repository: str, issue_number: int, body: str
    ) -> IssueComment:
        self._outside_transaction()
        self.create_calls += 1
        if self.fail_on_create_call == self.create_calls:
            raise GitHubPublicationError("github_unreachable")
        comment = IssueComment(
            comment_id=self.next_comment_id,
            body=body,
            author_login="review-agent[bot]",
        )
        self.next_comment_id += 1
        self.comments.append(comment)
        if self.kill_after_create:
            self.kill_after_create = False
            raise ProcessDeath
        return comment

    def update_issue_comment(
        self, repository: str, comment_id: int, body: str
    ) -> IssueComment:
        self._outside_transaction()
        for index, comment in enumerate(self.comments):
            if comment.comment_id != comment_id:
                continue
            updated = IssueComment(
                comment_id=comment_id,
                body=body,
                author_login=comment.author_login,
            )
            self.comments[index] = updated
            return updated
        raise GitHubPublicationError("comment_not_found")

    def delete_issue_comment(self, repository: str, comment_id: int) -> None:
        self._outside_transaction()
        self.comments = [
            comment for comment in self.comments if comment.comment_id != comment_id
        ]

    def create_pull_request_review(
        self,
        repository: str,
        pr_number: int,
        *,
        commit_id: str,
        body: str,
        comments: Sequence[InlineReviewComment],
    ) -> PullRequestReview:
        self._outside_transaction()
        if self.fail_on_review_create:
            raise GitHubPublicationError("github_unreachable")
        review_id = self.next_review_id
        self.next_review_id += 1
        for index, comment in enumerate(comments, start=1):
            self.review_comments.append(
                PullRequestReviewComment(
                    comment_id=review_id * 10 + index,
                    review_id=review_id,
                    body=comment.body,
                    author_login="review-agent[bot]",
                    path=comment.path,
                    commit_id=commit_id,
                    line=comment.line,
                    side=comment.side,
                    start_line=comment.start_line,
                    start_side=comment.start_side,
                )
            )
        return PullRequestReview(
            review_id=review_id,
            body=body,
            author_login="review-agent[bot]",
            commit_id=commit_id,
            state="COMMENTED",
        )

    def list_pull_request_review_comments(
        self, repository: str, pr_number: int, *, max_pages: int = 3
    ) -> list[PullRequestReviewComment]:
        self._outside_transaction()
        return list(self.review_comments)


class PublicationDomainTests(unittest.TestCase):
    def test_plan_freezes_exact_bytes_and_canonical_payload_hashes(self) -> None:
        plan = resolve_publication_plan(
            publication_key="sha256:" + ("a" * 64),
            rendered_markdown="## Review\n\nExact bytes.\n",
            rendered_blocks_schema_version=1,
            rendered_blocks=(
                {"kind": "header", "markdown": "## Review"},
                {"kind": "finding", "markdown": "Exact bytes."},
            ),
            parts=(
                PublicationPartInput(
                    part_type=PublicationPartType.SUMMARY,
                    part_number=1,
                    payload_schema_version=1,
                    payload={
                        "body": (
                            "Exact bytes.\n\n<!-- review-agent:canonical publication="
                            + ("sha256:" + ("a" * 64))
                            + " part=1/1 -->"
                        ),
                        "labels": ["review", "safe"],
                    },
                ),
            ),
            findings=(
                PublicationFindingInput(
                    finding_id=11,
                    source_finding_occurrence_id=21,
                    source_review_run_id=31,
                    local_reference="F1",
                    outcome=PublicationFindingOutcome.CURRENT,
                ),
            ),
        )

        self.assertEqual(
            plan.rendered_hash,
            hashlib.sha256(plan.rendered_markdown.encode("utf-8")).hexdigest(),
        )
        self.assertEqual(
            plan.parts[0].payload.canonical_json,
            (
                '{"body":"Exact bytes.\\n\\n<!-- review-agent:canonical '
                'publication=sha256:'
                + ("a" * 64)
                + ' part=1/1 -->","labels":["review","safe"]}'
            ),
        )
        self.assertEqual(
            plan.parts[0].payload.sha256,
            hashlib.sha256(
                plan.parts[0].payload.canonical_json.encode("utf-8")
            ).hexdigest(),
        )

    def test_plan_rejects_blocks_outside_the_version_one_renderer_contract(self) -> None:
        with self.assertRaisesRegex(PublicationDomainError, "kind is unsupported"):
            resolve_publication_plan(
                publication_key="sha256:" + ("1" * 64),
                rendered_markdown="review\n",
                rendered_blocks_schema_version=1,
                rendered_blocks=({"kind": "summary", "markdown": "review"},),
                parts=(
                    PublicationPartInput(
                        part_type=PublicationPartType.SUMMARY,
                        part_number=1,
                        payload_schema_version=1,
                        payload={
                            "body": (
                                "review\n\n<!-- review-agent:canonical publication="
                                + ("sha256:" + ("1" * 64))
                                + " part=1/1 -->"
                            )
                        },
                    ),
                ),
                findings=(),
            )

    def test_current_finding_rejects_cross_run_evidence(self) -> None:
        with self.assertRaisesRegex(
            PublicationDomainError, "current finding must not have outcome evidence"
        ):
            resolve_publication_plan(
                publication_key="sha256:" + ("a" * 64),
                rendered_markdown="review\n",
                rendered_blocks_schema_version=1,
                rendered_blocks=({"kind": "header", "markdown": "review"},),
                parts=(
                    PublicationPartInput(
                        part_type=PublicationPartType.SUMMARY,
                        part_number=1,
                        payload_schema_version=1,
                        payload={
                            "body": (
                                "review\n\n<!-- review-agent:canonical publication="
                                + ("sha256:" + ("a" * 64))
                                + " part=1/1 -->"
                            )
                        },
                    ),
                ),
                findings=(
                    PublicationFindingInput(
                        finding_id=11,
                        source_finding_occurrence_id=21,
                        source_review_run_id=31,
                        local_reference="F1",
                        outcome=PublicationFindingOutcome.CURRENT,
                        outcome_evidence="not allowed",
                    ),
                ),
            )

    def test_issue_comment_payload_requires_its_publication_part_marker(self) -> None:
        with self.assertRaisesRegex(
            PublicationDomainError, "exact publication-part marker"
        ):
            resolve_publication_plan(
                publication_key="sha256:" + ("7" * 64),
                rendered_markdown="review\n",
                rendered_blocks_schema_version=1,
                rendered_blocks=({"kind": "header", "markdown": "review"},),
                parts=(
                    PublicationPartInput(
                        part_type=PublicationPartType.SUMMARY,
                        part_number=1,
                        payload_schema_version=1,
                        payload={"body": "marker missing"},
                    ),
                ),
                findings=(),
            )


@unittest.skipUnless(DSN, "run through scripts/check_postgres_schema.sh")
class PostgreSQLPublicationTests(unittest.TestCase):
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
            path="backend/changed.py",
            line=7,
            symbol="handler",
            anchor="feature default",
            title="Boolean default remains disabled",
            severity="High",
            publication_score=9,
            confidence=0.9,
            evidence="Concrete evidence.",
            disproof_checks="Checked the guard.",
            impact="The feature remains unavailable.",
            smallest_fix="Restore the enabled default.",
            introduced_by_diff=True,
        )
        return replace(item, **overrides)

    def start_recorded_run(
        self,
        *,
        request_key: str = "github:issue-comment:publication-1",
        findings: tuple[FindingInput, ...] | None = None,
    ) -> tuple[review_runs.ReviewRunId, review_finding_application.PostgresFindingBatch]:
        result = review_run_application.start_postgres_review(
            self.runtime,
            review_run_application.PostgresRunRequest(
                provider="github",
                provider_repository_id=981,
                repository="team/service",
                pr_number=41,
                base_sha="b" * 40,
                head_sha="a" * 40,
                policy_revision="profile@1",
                resolved_config_schema_version=1,
                resolved_config={"profile": "sundsvall-standard"},
                request_key=request_key,
            ),
        )
        assert isinstance(result, review_runs.StartedRun)
        review_run_application.register_postgres_changed_files(
            self.runtime,
            run_id=result.run.id,
            files=(
                review_run_application.PostgresChangedFile(
                    path="backend/changed.py", change_status="modified"
                ),
            ),
            changed_files_reported=1,
            registration_complete=True,
        )
        batch = review_finding_application.record_postgres_findings(
            self.runtime,
            run_id=result.run.id,
            head_sha="a" * 40,
            findings=findings if findings is not None else (self.finding(),),
            changed_files=(
                review_finding_application.ChangedFile(
                    path="backend/changed.py",
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
                review_runs.advance_phase(connection, result.run.id, phase)
        return result.run.id, batch

    @staticmethod
    def plan(
        batch: review_finding_application.PostgresFindingBatch,
        *,
        key_character: str = "d",
        detailed_history: bool = False,
    ) -> PublicationPlan:
        finding = batch.items[0]
        publication_key = "sha256:" + (key_character * 64)
        blocks = (
            {"kind": "header", "markdown": "## Review"},
            {"kind": "finding", "markdown": "Exact persisted review."},
            {"kind": "feedback_help", "markdown": "React to give feedback."},
            {"kind": "metadata", "markdown": "<!-- review-agent: exact -->"},
        )
        if detailed_history:
            blocks = (
                {"kind": "header", "markdown": "## Review"},
                {
                    "kind": "finding",
                    "markdown": "First historical evidence.\n\n" + ("A" * 450),
                },
                {
                    "kind": "finding",
                    "markdown": "Second historical evidence.\n\n" + ("B" * 450),
                },
                {"kind": "feedback_help", "markdown": "Stale feedback instructions."},
                {"kind": "metadata", "markdown": "<!-- stale metadata -->"},
            )
        rendered_markdown = "\n\n".join(
            str(block["markdown"]).rstrip() for block in blocks
        ).rstrip() + "\n"
        return resolve_publication_plan(
            publication_key=publication_key,
            rendered_markdown=rendered_markdown,
            rendered_blocks_schema_version=1,
            rendered_blocks=blocks,
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
                            + " part=1/2 -->"
                        )
                    },
                ),
                PublicationPartInput(
                    part_type=PublicationPartType.CONTINUATION,
                    part_number=2,
                    payload_schema_version=1,
                    payload={
                        "body": (
                            "Exact continuation.\n\n"
                            "<!-- review-agent:canonical publication="
                            + publication_key
                            + " part=2/2 -->"
                        )
                    },
                ),
            ),
            findings=(
                PublicationFindingInput(
                    finding_id=int(finding.finding_id),
                    source_finding_occurrence_id=int(finding.occurrence_id),
                    source_review_run_id=int(batch.run_id),
                    local_reference=finding.local_reference,
                    outcome=PublicationFindingOutcome.CURRENT,
                ),
            ),
        )

    @staticmethod
    def suggestion_plan(
        batch: review_finding_application.PostgresFindingBatch,
    ) -> PublicationPlan:
        finding = batch.items[0]
        key = "sha256:" + ("8" * 64)
        return resolve_publication_plan(
            publication_key=key,
            rendered_markdown="review with suggestion\n",
            rendered_blocks_schema_version=1,
            rendered_blocks=(
                {"kind": "header", "markdown": "review with suggestion"},
            ),
            parts=(
                PublicationPartInput(
                    part_type=PublicationPartType.SUMMARY,
                    part_number=1,
                    payload_schema_version=1,
                    payload={
                        "body": (
                            "review with suggestion\n\n"
                            f"<!-- review-agent:canonical publication={key} "
                            "part=1/1 -->"
                        )
                    },
                ),
                PublicationPartInput(
                    part_type=PublicationPartType.SUGGESTION_REVIEW,
                    part_number=1,
                    payload_schema_version=1,
                    payload={
                        "body": "Optional atomic patch",
                        "comments": [
                            {
                                "path": "backend/changed.py",
                                "body": (
                                    "```suggestion\nTrue\n```\n\n"
                                    f"review-agent:canonical publication={key}"
                                ),
                                "line": 7,
                                "side": "RIGHT",
                            }
                        ],
                    },
                ),
            ),
            findings=(
                PublicationFindingInput(
                    finding_id=int(finding.finding_id),
                    source_finding_occurrence_id=int(finding.occurrence_id),
                    source_review_run_id=int(batch.run_id),
                    local_reference=finding.local_reference,
                    outcome=PublicationFindingOutcome.CURRENT,
                ),
            ),
        )

    def test_prepare_atomically_persists_exact_plan_and_provenance(self) -> None:
        run_id, batch = self.start_recorded_run()
        plan = self.plan(batch)

        with self.runtime.transaction() as connection:
            prepared = publications.prepare_publication(
                connection, run_id=run_id, plan=plan
            )
        with self.runtime.transaction() as connection:
            loaded = publications.get_publication(connection, prepared.id)

        self.assertEqual(loaded.plan, plan)
        self.assertEqual(loaded.status, publications.PublicationStatus.GENERATED)
        self.assertEqual(loaded.review_number, 1)
        self.assertEqual(
            loaded.plan.findings[0].source_review_run_id, int(run_id)
        )

        with self.runtime.transaction() as connection:
            same = publications.prepare_publication(
                connection, run_id=run_id, plan=plan
            )
        self.assertEqual(same.id, prepared.id)

    def test_application_prepares_and_reuses_the_frozen_postgres_plan(self) -> None:
        run_id, _ = self.start_recorded_run(
            request_key="github:issue-comment:application-preparation"
        )

        first = review_publication_application.prepare_postgres_publication(
            self.runtime,
            run_id=int(run_id),
            previous_verdicts=None,
            feedback_enabled=True,
            max_comment_bytes=60_000,
        )
        repeated = review_publication_application.prepare_postgres_publication(
            self.runtime,
            run_id=int(run_id),
            previous_verdicts=None,
            feedback_enabled=True,
            max_comment_bytes=60_000,
        )

        self.assertEqual(first, repeated)
        self.assertEqual(first.findings_count, 1)
        self.assertEqual(first.suggestions_count, 0)
        with self.runtime.transaction() as connection:
            stored = publications.get_publication(
                connection, publications.PublicationId(first.publication_id)
            )
        self.assertEqual(stored.status, publications.PublicationStatus.GENERATED)
        self.assertIn(
            f"review-agent:canonical publication={stored.plan.publication_key}",
            stored.parts[0].delivery.body,
        )

    def test_claim_acknowledge_and_complete_use_short_exact_transitions(self) -> None:
        run_id, batch = self.start_recorded_run()
        with self.runtime.transaction() as connection:
            prepared = publications.prepare_publication(
                connection, run_id=run_id, plan=self.plan(batch)
            )
        with self.runtime.transaction() as connection:
            claim = publications.claim_publication(connection, prepared.id)
        self.assertTrue(claim.acquired)
        assert claim.publication.posting_started_at is not None

        with self.assertRaises(publications.InvalidPublicationTransition):
            with self.runtime.transaction() as connection:
                publications.complete_publication(
                    connection,
                    publication_id=prepared.id,
                    posting_started_at=claim.publication.posting_started_at,
                )

        for part, external_id in zip(
            claim.publication.parts, (501, 502), strict=True
        ):
            with self.runtime.transaction() as connection:
                acknowledged = publications.acknowledge_part(
                    connection,
                    publication_id=prepared.id,
                    part_type=part.part_type,
                    part_number=part.part_number,
                    external_id=external_id,
                    posting_started_at=claim.publication.posting_started_at,
                )
            self.assertEqual(acknowledged.external_id, external_id)

        with self.runtime.transaction() as connection:
            posted = publications.complete_publication(
                connection,
                publication_id=prepared.id,
                posting_started_at=claim.publication.posting_started_at,
            )
        self.assertEqual(posted.status, publications.PublicationStatus.POSTED)

    def test_only_one_concurrent_claim_acquires_generated_delivery(self) -> None:
        run_id, batch = self.start_recorded_run()
        with self.runtime.transaction() as connection:
            prepared = publications.prepare_publication(
                connection, run_id=run_id, plan=self.plan(batch)
            )

        def claim() -> bool:
            with self.runtime.transaction() as connection:
                return publications.claim_publication(
                    connection, prepared.id
                ).acquired

        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=2) as executor:
            acquired = list(executor.map(lambda _: claim(), range(2)))
        self.assertEqual(sorted(acquired), [False, True])

    def test_process_death_after_github_success_recovers_marker_without_duplicate(
        self,
    ) -> None:
        run_id, batch = self.start_recorded_run()
        with self.runtime.transaction() as connection:
            prepared = publications.prepare_publication(
                connection, run_id=run_id, plan=self.plan(batch)
            )
        github = FakePostgresPublicationGitHub(self.runtime)
        github.kill_after_create = True

        with self.assertRaises(ProcessDeath):
            review_publication_application.publish_postgres_publication(
                self.runtime,
                publication_id=int(prepared.id),
                github=github,
                max_comment_bytes=60_000,
            )
        self.assertEqual(len(github.comments), 1)

        recovered = review_publication_application.publish_postgres_publication(
            self.runtime,
            publication_id=int(prepared.id),
            github=github,
            recover_posting=True,
            max_comment_bytes=60_000,
        )

        self.assertEqual(recovered.status, "posted")
        self.assertEqual(recovered.recovered_parts, 1)
        self.assertEqual(len(github.comments), 2)
        self.assertEqual(
            tuple(part.external_id for part in recovered.published_parts),
            (700, 701),
        )
        self.assertTrue(github.issue_comments_newest_first)

        github.fail_on_call = True
        direct = review_publication_application.publish_postgres_publication(
            self.runtime,
            publication_id=int(prepared.id),
            github=github,
            max_comment_bytes=60_000,
        )
        self.assertEqual(
            tuple(part.external_id for part in direct.published_parts),
            (700, 701),
        )

    def test_posting_reentry_requires_an_explicit_recovery_call(self) -> None:
        run_id, batch = self.start_recorded_run(
            request_key="github:issue-comment:serialized-posting"
        )
        with self.runtime.transaction() as connection:
            prepared = publications.prepare_publication(
                connection, run_id=run_id, plan=self.plan(batch)
            )
            publications.claim_publication(connection, prepared.id)
        github = FakePostgresPublicationGitHub(self.runtime)
        github.fail_on_call = True

        result = review_publication_application.publish_postgres_publication(
            self.runtime,
            publication_id=int(prepared.id),
            github=github,
            max_comment_bytes=60_000,
        )

        self.assertEqual(result.status, "posting")
        self.assertEqual(github.comments, [])

    def test_partial_github_failure_reclaims_only_unfinished_parts(self) -> None:
        run_id, batch = self.start_recorded_run(
            request_key="github:issue-comment:retry-failed-publication"
        )
        with self.runtime.transaction() as connection:
            prepared = publications.prepare_publication(
                connection, run_id=run_id, plan=self.plan(batch)
            )
        github = FakePostgresPublicationGitHub(self.runtime)
        github.fail_on_create_call = 2

        failed = review_publication_application.publish_postgres_publication(
            self.runtime,
            publication_id=int(prepared.id),
            github=github,
            max_comment_bytes=60_000,
        )
        with self.runtime.transaction() as connection:
            failed_run = review_runs.get_run(connection, run_id)
        retried = review_publication_application.publish_postgres_publication(
            self.runtime,
            publication_id=int(prepared.id),
            github=github,
            max_comment_bytes=60_000,
        )
        with self.runtime.transaction() as connection:
            completed_run = review_runs.get_run(connection, run_id)

        self.assertEqual(failed.status, "publish_failed")
        self.assertEqual(failed_run.status, ReviewStatus.RUNNING)
        self.assertEqual(failed_run.phase, ReviewPhase.PUBLISHING)
        self.assertEqual(retried.status, "posted")
        self.assertEqual(completed_run.status, ReviewStatus.COMPLETED)
        self.assertEqual(completed_run.phase, ReviewPhase.POSTED)
        self.assertEqual(
            tuple(part.external_id for part in retried.published_parts),
            (700, 701),
        )
        self.assertEqual(len(github.comments), 2)

    def test_second_posted_review_supersedes_the_first_in_one_transaction(self) -> None:
        first_run, first_batch = self.start_recorded_run(
            request_key="github:issue-comment:supersession-1"
        )
        with self.runtime.transaction() as connection:
            first = publications.prepare_publication(
                connection,
                run_id=first_run,
                plan=self.plan(first_batch, detailed_history=True),
            )
        github = FakePostgresPublicationGitHub(self.runtime)
        first_result = review_publication_application.publish_postgres_publication(
            self.runtime,
            publication_id=int(first.id),
            github=github,
            max_comment_bytes=1_200,
        )
        self.assertEqual(first_result.status, "posted")

        second_run, second_batch = self.start_recorded_run(
            request_key="github:issue-comment:supersession-2"
        )
        with self.runtime.transaction() as connection:
            second = publications.prepare_publication(
                connection,
                run_id=second_run,
                plan=self.plan(second_batch, key_character="e"),
            )
        second_result = review_publication_application.publish_postgres_publication(
            self.runtime,
            publication_id=int(second.id),
            github=github,
            max_comment_bytes=1_200,
        )
        self.assertEqual(second_result.status, "posted")

        with self.runtime.transaction() as connection:
            recorded = connection.execute(
                """
                SELECT supersession_rendered_at, supersession_failure_code
                FROM review_agent.publications
                WHERE id = %s
                """,
                (first.id,),
            ).fetchone()
            supersession = connection.execute(
                """
                SELECT superseded_by_publication_id, superseded_at IS NOT NULL
                FROM review_agent.publications
                WHERE id = %s
                """,
                (first.id,),
            ).fetchone()
        self.assertEqual(supersession, (int(second.id), True))
        self.assertEqual(second_result.superseded_publication_id, int(first.id))
        self.assertTrue(second_result.supersession_rendered)
        self.assertIsNone(second_result.supersession_failure_code)
        self.assertIsNotNone(recorded)
        assert recorded is not None
        self.assertIsNotNone(recorded[0])
        self.assertIsNone(recorded[1])
        self.assertIn("Superseded by [Review 2]", github.comments[0].body)
        self.assertIn("Superseded by [Review 2]", github.comments[1].body)
        self.assertIn("First historical evidence", github.comments[0].body)
        self.assertIn("Second historical evidence", github.comments[1].body)
        self.assertNotIn("Stale feedback instructions", github.comments[0].body)
        self.assertNotIn("stale metadata", github.comments[0].body)
        self.assertNotIn("[truncated]", github.comments[0].body)

    def test_missing_historical_comment_records_supersession_failure(self) -> None:
        first_run, first_batch = self.start_recorded_run(
            request_key="github:issue-comment:supersession-failure-1"
        )
        with self.runtime.transaction() as connection:
            first = publications.prepare_publication(
                connection, run_id=first_run, plan=self.plan(first_batch)
            )
        github = FakePostgresPublicationGitHub(self.runtime)
        review_publication_application.publish_postgres_publication(
            self.runtime,
            publication_id=int(first.id),
            github=github,
            max_comment_bytes=60_000,
        )
        missing = github.comments.pop(0)
        second_run, second_batch = self.start_recorded_run(
            request_key="github:issue-comment:supersession-failure-2"
        )
        with self.runtime.transaction() as connection:
            second = publications.prepare_publication(
                connection,
                run_id=second_run,
                plan=self.plan(second_batch, key_character="f"),
            )
        review_publication_application.publish_postgres_publication(
            self.runtime,
            publication_id=int(second.id),
            github=github,
            max_comment_bytes=60_000,
        )

        with self.runtime.transaction() as connection:
            result = connection.execute(
                """
                SELECT supersession_rendered_at, supersession_failure_code
                FROM review_agent.publications
                WHERE id = %s
                """,
                (first.id,),
            ).fetchone()

        self.assertIsNotNone(result)
        assert result is not None
        self.assertIsNotNone(result[0])
        self.assertEqual(result[1], "superseded_comment_missing")

        github.comments.append(missing)
        third_run, third_batch = self.start_recorded_run(
            request_key="github:issue-comment:supersession-failure-3"
        )
        with self.runtime.transaction() as connection:
            third = publications.prepare_publication(
                connection,
                run_id=third_run,
                plan=self.plan(third_batch, key_character="7"),
            )
        third_result = review_publication_application.publish_postgres_publication(
            self.runtime,
            publication_id=int(third.id),
            github=github,
            max_comment_bytes=60_000,
        )
        self.assertTrue(third_result.supersession_rendered)
        self.assertEqual(third_result.superseded_publication_id, int(second.id))
        drained = review_publication_application.publish_postgres_publication(
            self.runtime,
            publication_id=int(third.id),
            github=github,
            max_comment_bytes=60_000,
        )
        self.assertTrue(drained.supersession_rendered)
        self.assertEqual(drained.superseded_publication_id, int(first.id))
        with self.runtime.transaction() as connection:
            retried = connection.execute(
                """
                SELECT supersession_rendered_at, supersession_failure_code
                FROM review_agent.publications
                WHERE id = %s
                """,
                (first.id,),
            ).fetchone()
        self.assertIsNotNone(retried)
        assert retried is not None
        self.assertIsNotNone(retried[0])
        self.assertIsNone(retried[1])

    def test_supersession_list_failure_is_recorded_after_posting(self) -> None:
        first_run, first_batch = self.start_recorded_run(
            request_key="github:issue-comment:supersession-list-1"
        )
        with self.runtime.transaction() as connection:
            first = publications.prepare_publication(
                connection, run_id=first_run, plan=self.plan(first_batch)
            )
        github = FakePostgresPublicationGitHub(self.runtime)
        review_publication_application.publish_postgres_publication(
            self.runtime,
            publication_id=int(first.id),
            github=github,
            max_comment_bytes=60_000,
        )
        second_run, second_batch = self.start_recorded_run(
            request_key="github:issue-comment:supersession-list-2"
        )
        with self.runtime.transaction() as connection:
            second = publications.prepare_publication(
                connection,
                run_id=second_run,
                plan=self.plan(second_batch, key_character="6"),
            )
        github.fail_on_list_call = github.list_issue_comments_calls + 2

        result = review_publication_application.publish_postgres_publication(
            self.runtime,
            publication_id=int(second.id),
            github=github,
            max_comment_bytes=60_000,
        )

        self.assertEqual(result.status, "posted")
        self.assertFalse(result.supersession_rendered)
        self.assertEqual(result.supersession_failure_code, "github_unreachable")

    def test_stale_head_is_checked_immediately_before_first_external_write(self) -> None:
        run_id, batch = self.start_recorded_run()
        with self.runtime.transaction() as connection:
            prepared = publications.prepare_publication(
                connection, run_id=run_id, plan=self.plan(batch)
            )
        github = FakePostgresPublicationGitHub(self.runtime)
        github.head_sha = "e" * 40

        result = review_publication_application.publish_postgres_publication(
            self.runtime,
            publication_id=int(prepared.id),
            github=github,
            max_comment_bytes=60_000,
        )

        self.assertEqual(result.status, "stale")
        self.assertEqual(github.comments, [])

    def test_failure_status_is_idempotent_and_cleared_by_a_posted_review(self) -> None:
        failed_run, _ = self.start_recorded_run(
            request_key="github:issue-comment:failed-status"
        )
        with self.runtime.transaction() as connection:
            review_runs.fail_run(
                connection,
                failed_run,
                failure_code="review_deliver_error",
            )
        github = FakePostgresPublicationGitHub(self.runtime)

        first = review_publication_application.publish_postgres_run_failure_status(
            self.runtime,
            run_id=int(failed_run),
            github=github,
        )
        repeated = review_publication_application.publish_postgres_run_failure_status(
            self.runtime,
            run_id=int(failed_run),
            github=github,
        )

        self.assertEqual(repeated.comment_id, first.comment_id)
        self.assertEqual(len(github.comments), 1)
        with self.runtime.transaction() as connection:
            stored = review_runs.failure_status_target(connection, failed_run)
        self.assertEqual(stored.comment_id, first.comment_id)

        posted_run, posted_batch = self.start_recorded_run(
            request_key="github:issue-comment:posted-after-failure"
        )
        with self.runtime.transaction() as connection:
            prepared = publications.prepare_publication(
                connection,
                run_id=posted_run,
                plan=self.plan(posted_batch, key_character="9"),
            )
        result = review_publication_application.publish_postgres_publication(
            self.runtime,
            publication_id=int(prepared.id),
            github=github,
            max_comment_bytes=60_000,
        )

        self.assertEqual(result.status, "posted")
        self.assertNotIn(first.comment_id, [item.comment_id for item in github.comments])
        with self.runtime.transaction() as connection:
            cleared = review_runs.failure_status_target(connection, failed_run)
        self.assertIsNone(cleared.comment_id)

        retry_run, _ = self.start_recorded_run(
            request_key="github:issue-comment:failed-status-retry"
        )
        with self.runtime.transaction() as connection:
            review_runs.fail_run(
                connection,
                retry_run,
                failure_code="review_deliver_error",
            )
        retry_status = review_publication_application.publish_postgres_run_failure_status(
            self.runtime,
            run_id=int(retry_run),
            github=github,
        )
        direct = review_publication_application.publish_postgres_publication(
            self.runtime,
            publication_id=int(prepared.id),
            github=github,
            max_comment_bytes=60_000,
        )

        self.assertEqual(direct.status, "posted")
        self.assertNotIn(
            retry_status.comment_id,
            [item.comment_id for item in github.comments],
        )
        with self.runtime.transaction() as connection:
            retried_cleanup = review_runs.failure_status_target(connection, retry_run)
        self.assertIsNone(retried_cleanup.comment_id)

    def test_structured_suggestion_part_posts_from_the_exact_persisted_payload(
        self,
    ) -> None:
        run_id, batch = self.start_recorded_run(
            request_key="github:issue-comment:suggestion-publication"
        )
        key = "sha256:" + ("8" * 64)
        plan = self.suggestion_plan(batch)
        with self.runtime.transaction() as connection:
            prepared = publications.prepare_publication(
                connection, run_id=run_id, plan=plan
            )
        repeated = review_publication_application.prepare_postgres_publication(
            self.runtime,
            run_id=int(run_id),
            previous_verdicts=None,
            feedback_enabled=False,
            max_comment_bytes=60_000,
        )
        github = FakePostgresPublicationGitHub(self.runtime)

        result = review_publication_application.publish_postgres_publication(
            self.runtime,
            publication_id=int(prepared.id),
            github=github,
            max_comment_bytes=60_000,
        )

        self.assertEqual(repeated.suggestions_count, 1)
        self.assertEqual(
            tuple(
                (part.part_type, part.external_id)
                for part in result.published_parts
            ),
            (
                (PublicationPartType.SUGGESTION_REVIEW, 800),
                (PublicationPartType.SUMMARY, 700),
            ),
        )
        self.assertEqual(
            github.review_comments[0].body,
            "```suggestion\nTrue\n```\n\n"
            + f"review-agent:canonical publication={key}",
        )

    def test_suggestion_failure_does_not_publish_a_misleading_summary(self) -> None:
        run_id, batch = self.start_recorded_run(
            request_key="github:issue-comment:suggestion-failure"
        )
        with self.runtime.transaction() as connection:
            prepared = publications.prepare_publication(
                connection,
                run_id=run_id,
                plan=self.suggestion_plan(batch),
            )
        github = FakePostgresPublicationGitHub(self.runtime)
        github.fail_on_review_create = True

        result = review_publication_application.publish_postgres_publication(
            self.runtime,
            publication_id=int(prepared.id),
            github=github,
            max_comment_bytes=60_000,
        )

        self.assertEqual(result.status, "publish_failed")
        self.assertEqual(github.comments, [])

    def test_invalid_cross_run_finding_rolls_back_the_whole_plan(self) -> None:
        run_id, batch = self.start_recorded_run()
        plan = self.plan(batch)
        invalid = replace(
            plan,
            findings=(
                replace(
                    plan.findings[0],
                    source_review_run_id=int(run_id) + 1000,
                ),
            ),
        )

        with self.assertRaises(publications.PublicationConflict):
            with self.runtime.transaction() as connection:
                publications.prepare_publication(
                    connection, run_id=run_id, plan=invalid
                )

        with self.runtime.transaction() as connection:
            count = connection.execute(
                "SELECT count(*) FROM review_agent.publications"
            ).fetchone()
        assert count is not None
        self.assertEqual(count[0], 0)

    def test_prepare_persists_every_explicit_finding_outcome(self) -> None:
        prior_inputs = tuple(
            self.finding(
                rule_id=f"correctness.prior-outcome-{index}",
                anchor=f"prior outcome {index}",
                line=10 + index,
            )
            for index in range(4)
        )
        first_run, first_batch = self.start_recorded_run(
            request_key="github:issue-comment:outcomes-1",
            findings=prior_inputs,
        )
        with self.runtime.transaction() as connection:
            review_runs.fail_run(
                connection,
                first_run,
                failure_code="fixture_completed",
                findings_count=4,
            )

        result = review_run_application.start_postgres_review(
            self.runtime,
            review_run_application.PostgresRunRequest(
                provider="github",
                provider_repository_id=981,
                repository="team/service",
                pr_number=41,
                base_sha="b" * 40,
                head_sha="e" * 40,
                policy_revision="profile@1",
                resolved_config_schema_version=1,
                resolved_config={"profile": "sundsvall-standard"},
                request_key="github:issue-comment:outcomes-2",
            ),
        )
        assert isinstance(result, review_runs.StartedRun)
        review_run_application.register_postgres_changed_files(
            self.runtime,
            run_id=result.run.id,
            files=(
                review_run_application.PostgresChangedFile(
                    path="backend/changed.py", change_status="modified"
                ),
            ),
            changed_files_reported=1,
            registration_complete=True,
        )
        current_batch = review_finding_application.record_postgres_findings(
            self.runtime,
            run_id=result.run.id,
            head_sha="e" * 40,
            findings=(
                self.finding(
                    rule_id="correctness.current-outcome",
                    anchor="current outcome",
                    line=30,
                ),
            ),
            changed_files=(
                review_finding_application.ChangedFile(
                    path="backend/changed.py",
                    context_hash="f" * 40,
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
                review_runs.advance_phase(connection, result.run.id, phase)

        current = current_batch.items[0]
        outcome_inputs = [
            PublicationFindingInput(
                finding_id=int(current.finding_id),
                source_finding_occurrence_id=int(current.occurrence_id),
                source_review_run_id=int(result.run.id),
                local_reference=current.local_reference,
                outcome=PublicationFindingOutcome.CURRENT,
            )
        ]
        for item, outcome in zip(
            first_batch.items,
            (
                PublicationFindingOutcome.RESOLVED,
                PublicationFindingOutcome.INVALIDATED,
                PublicationFindingOutcome.SUPPRESSED,
                PublicationFindingOutcome.NOT_CHECKED,
            ),
            strict=True,
        ):
            outcome_inputs.append(
                PublicationFindingInput(
                    finding_id=int(item.finding_id),
                    source_finding_occurrence_id=int(item.occurrence_id),
                    source_review_run_id=int(first_run),
                    local_reference=item.local_reference,
                    outcome=outcome,
                    outcome_evidence=f"Evidence for {outcome.value}.",
                )
            )
        plan = resolve_publication_plan(
            publication_key="sha256:" + ("9" * 64),
            rendered_markdown="all outcomes\n",
            rendered_blocks_schema_version=1,
            rendered_blocks=({"kind": "header", "markdown": "all outcomes"},),
            parts=(
                PublicationPartInput(
                    part_type=PublicationPartType.SUMMARY,
                    part_number=1,
                    payload_schema_version=1,
                    payload={
                        "body": (
                            "all outcomes\n\n<!-- review-agent:canonical publication="
                            + ("sha256:" + ("9" * 64))
                            + " part=1/1 -->"
                        )
                    },
                ),
            ),
            findings=tuple(outcome_inputs),
        )

        with self.runtime.transaction() as connection:
            prepared = publications.prepare_publication(
                connection, run_id=result.run.id, plan=plan
            )

        self.assertEqual(
            {finding.outcome for finding in prepared.plan.findings},
            set(PublicationFindingOutcome),
        )


if __name__ == "__main__":
    unittest.main()
