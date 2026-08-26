from __future__ import annotations

import hashlib
import os
import sys
import threading
import unittest
from collections.abc import Callable, Sequence
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

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
from review_agent_tools.github.gateway import GitHubGatewayRejected  # noqa: E402
from review_agent_tools.github.publication_gateway import (  # noqa: E402
    PublicationGatewayRequest,
    ReviewPublicationGateway,
)
from review_agent_tools.postgres import publications  # noqa: E402
from review_agent_tools.postgres import github_app  # noqa: E402
from review_agent_tools.postgres import jobs  # noqa: E402
from review_agent_tools.postgres import review_runs  # noqa: E402
from review_agent_tools.postgres.runtime import (  # noqa: E402
    PostgreSQLRuntime,
    PostgreSQLUnavailable,
)
from review_agent_tools.postgres_migrations import runner  # noqa: E402
from review_agent_tools.publisher import (  # noqa: E402
    PublicationWorker,
    PublisherPolicy,
)
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
        self.create_error: GitHubPublicationError | None = None
        self.fail_on_review_create = False
        self.list_issue_comments_calls = 0
        self.fail_on_list_call: int | None = None
        self.before_target_read: Callable[[], None] | None = None
        self.publication_leases: list[tuple[int, str, int]] = []
        self.posted_publications: list[int] = []
        self.failure_status_leases: list[tuple[int, str, int]] = []

    def for_publication(
        self,
        *,
        publication_id: int,
        lease_owner: str,
        lease_generation: int,
    ) -> FakePostgresPublicationGitHub:
        self.publication_leases.append(
            (publication_id, lease_owner, lease_generation)
        )
        return self

    def for_failure_status(
        self,
        *,
        run_id: int,
        lease_owner: str,
        lease_generation: int,
    ) -> FakePostgresPublicationGitHub:
        self.failure_status_leases.append((run_id, lease_owner, lease_generation))
        return self

    def for_posted_publication(
        self, *, publication_id: int
    ) -> FakePostgresPublicationGitHub:
        self.posted_publications.append(publication_id)
        return self

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
        before_target_read = self.before_target_read
        self.before_target_read = None
        if before_target_read is not None:
            before_target_read()
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
        if self.create_error is not None:
            raise self.create_error
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
        pr_number: int = 41,
    ) -> tuple[review_runs.ReviewRunId, review_finding_application.PostgresFindingBatch]:
        result = review_run_application.start_postgres_review(
            self.runtime,
            review_run_application.PostgresRunRequest(
                provider="github",
                provider_repository_id=981,
                repository="team/service",
                pr_number=pr_number,
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
        *,
        two_comments: bool = False,
    ) -> PublicationPlan:
        finding = batch.items[0]
        key = "sha256:" + ("8" * 64)
        comments: list[dict[str, object]] = [
            {
                "path": "backend/changed.py",
                "body": (
                    "```suggestion\nTrue\n```\n\n"
                    f"review-agent:canonical publication={key}"
                ),
                "line": 7,
                "side": "RIGHT",
            }
        ]
        if two_comments:
            comments.append(
                {
                    "path": "backend/changed.py",
                    "body": (
                        "```suggestion\nFalse\n```\n\n"
                        f"review-agent:canonical publication={key}"
                    ),
                    "line": 8,
                    "side": "RIGHT",
                }
            )
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
                        "comments": comments,
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

    def test_expired_delivery_reclaims_with_a_new_fenced_generation(self) -> None:
        run_id, batch = self.start_recorded_run(
            request_key="github:issue-comment:delivery-reclaim"
        )
        with self.runtime.transaction() as connection:
            prepared = publications.prepare_publication(
                connection, run_id=run_id, plan=self.plan(batch)
            )
        with self.runtime.transaction() as connection:
            first = publications.claim_next_publication(
                connection,
                lease_owner="publisher-one",
                lease_duration=timedelta(seconds=30),
            )
        assert first is not None
        self.assertEqual(first.publication.id, prepared.id)
        self.assertEqual(first.publication.delivery_lease_generation, 1)

        with self.runtime.transaction() as connection:
            connection.execute(
                """
                UPDATE review_agent.publications
                SET delivery_lease_expires_at = statement_timestamp()
                    - INTERVAL '1 second'
                WHERE id = %s
                """,
                (prepared.id,),
            )
        with self.runtime.transaction() as connection:
            reclaimed = publications.claim_next_publication(
                connection,
                lease_owner="publisher-two",
                lease_duration=timedelta(seconds=30),
            )
        assert reclaimed is not None
        self.assertEqual(reclaimed.publication.delivery_lease_generation, 2)
        self.assertEqual(reclaimed.publication.delivery_recovery_count, 1)

        with self.assertRaises(publications.PublicationLeaseLost):
            with self.runtime.transaction() as connection:
                publications.heartbeat_publication(
                    connection,
                    publication_id=prepared.id,
                    lease_owner="publisher-one",
                    lease_generation=1,
                    lease_duration=timedelta(seconds=30),
                )

    def test_generation_takeover_before_create_blocks_the_old_publisher(self) -> None:
        run_id, batch = self.start_recorded_run(
            request_key="github:issue-comment:lease-takeover-before-write"
        )
        with self.runtime.transaction() as connection:
            prepared = publications.prepare_publication(
                connection, run_id=run_id, plan=self.plan(batch)
            )
            first = publications.claim_publication(
                connection,
                prepared.id,
                lease_owner="publisher-one",
                lease_duration=timedelta(seconds=30),
            )

        def take_over() -> None:
            with self.runtime.transaction() as connection:
                connection.execute(
                    "UPDATE review_agent.publications "
                    "SET delivery_lease_expires_at = statement_timestamp() "
                    "- INTERVAL '1 second' WHERE id = %s",
                    (prepared.id,),
                )
                publications.claim_publication(
                    connection,
                    prepared.id,
                    lease_owner="publisher-two",
                    lease_duration=timedelta(seconds=30),
                    recover_expired=True,
                )

        github = FakePostgresPublicationGitHub(self.runtime)
        github.before_target_read = take_over
        with self.assertRaises(publications.PublicationLeaseLost):
            review_publication_application.publish_postgres_publication(
                self.runtime,
                publication_id=int(prepared.id),
                github=github,
                max_comment_bytes=60_000,
                lease_owner="publisher-one",
                lease_generation=first.publication.delivery_lease_generation,
                posted_github=github,
            )

        self.assertEqual(github.create_calls, 0)

    def test_publication_heartbeat_survives_only_transient_database_failure(
        self,
    ) -> None:
        run_id, batch = self.start_recorded_run(
            request_key="github:issue-comment:publication-heartbeat"
        )
        with self.runtime.transaction() as connection:
            prepared = publications.prepare_publication(
                connection, run_id=run_id, plan=self.plan(batch)
            )
            claim = publications.claim_publication(
                connection,
                prepared.id,
                lease_owner="publisher-one",
                lease_duration=timedelta(seconds=1),
            )
        worker = PublicationWorker(
            self.runtime,
            FakePostgresPublicationGitHub(self.runtime),
            PublisherPolicy(
                lease_duration=timedelta(seconds=1),
                heartbeat_interval=timedelta(milliseconds=1),
                retry_delay=timedelta(seconds=1),
                poll_interval=timedelta(milliseconds=1),
                max_comment_bytes=60_000,
            ),
            lease_owner="publisher-one",
            stop_event=threading.Event(),
        )
        lease_lost = threading.Event()

        with (
            patch.object(
                publications,
                "heartbeat_publication",
                side_effect=(
                    PostgreSQLUnavailable("temporary outage"),
                    publications.PublicationLeaseLost("lease replaced"),
                ),
            ) as heartbeat,
            self.assertLogs("review_agent_tools.publisher", level="WARNING"),
        ):
            worker._heartbeat(claim.publication, threading.Event(), lease_lost)

        self.assertTrue(lease_lost.is_set())
        self.assertEqual(heartbeat.call_count, 2)

    def test_posted_finalization_has_narrow_authority_after_lease_release(
        self,
    ) -> None:
        first_run, first_batch = self.start_recorded_run(
            request_key="github:issue-comment:posted-finalization-first"
        )
        with self.runtime.transaction() as connection:
            first = publications.prepare_publication(
                connection, run_id=first_run, plan=self.plan(first_batch)
            )
        direct_github = FakePostgresPublicationGitHub(self.runtime)
        review_publication_application.publish_postgres_publication(
            self.runtime,
            publication_id=int(first.id),
            github=direct_github,
            max_comment_bytes=60_000,
        )

        second_run, second_batch = self.start_recorded_run(
            request_key="github:issue-comment:posted-finalization-second"
        )
        with self.runtime.transaction() as connection:
            second = publications.prepare_publication(
                connection,
                run_id=second_run,
                plan=self.plan(second_batch, key_character="e"),
            )
            claim = publications.claim_publication(
                connection,
                second.id,
                lease_owner="publisher-one",
                lease_duration=timedelta(seconds=30),
            )
        with patch.object(
            review_publication_application,
            "_render_postgres_supersession",
            return_value=None,
        ):
            review_publication_application.publish_postgres_publication(
                self.runtime,
                publication_id=int(second.id),
                github=direct_github,
                max_comment_bytes=60_000,
                lease_owner="publisher-one",
                lease_generation=claim.publication.delivery_lease_generation,
                posted_github=direct_github,
            )

        tokens = Mock()
        tokens.token_for.return_value = Mock(value="installation-token")
        provider = Mock()
        provider.list_issue_comments.return_value = []
        provider.update_issue_comment.return_value = IssueComment(
            direct_github.comments[0].comment_id,
            "superseded",
            "review-agent[bot]",
        )
        gateway = ReviewPublicationGateway(
            postgres=self.runtime,
            tokens=tokens,
            profile="sundsvall-standard",
            github_factory=Mock(return_value=provider),
        )
        active = PublicationGatewayRequest.from_mapping(
            {
                "scope_kind": "publication",
                "scope_id": int(second.id),
                "lease_owner": "publisher-one",
                "lease_generation": claim.publication.delivery_lease_generation,
                "operation": "current_user",
            }
        )
        posted = PublicationGatewayRequest.from_mapping(
            {
                "scope_kind": "posted_publication",
                "scope_id": int(second.id),
                "operation": "list_issue_comments",
                "max_pages": 1,
                "newest_first": False,
            }
        )
        arbitrary_update = PublicationGatewayRequest.from_mapping(
            {
                "scope_kind": "posted_publication",
                "scope_id": int(second.id),
                "operation": "update_issue_comment",
                "comment_id": 999_999,
                "body": "not authorized",
            }
        )
        authorized_update = PublicationGatewayRequest.from_mapping(
            {
                "scope_kind": "posted_publication",
                "scope_id": int(second.id),
                "operation": "update_issue_comment",
                "comment_id": direct_github.comments[0].comment_id,
                "body": "superseded",
            }
        )

        with patch.object(
            github_app, "authorize_review_publication", return_value=Mock()
        ):
            with self.assertRaises(GitHubGatewayRejected) as rejected:
                gateway.execute(active)
            self.assertEqual(gateway.execute(posted), [])
            with self.assertRaises(GitHubGatewayRejected) as arbitrary:
                gateway.execute(arbitrary_update)
            result = gateway.execute(authorized_update)

            with self.runtime.transaction() as connection:
                publications.record_supersession_result(
                    connection,
                    publication_id=first.id,
                    failure_code=None,
                )
            with self.assertRaises(GitHubGatewayRejected) as completed:
                gateway.execute(posted)

        self.assertEqual(rejected.exception.reason, "publication_lease_lost")
        self.assertEqual(
            arbitrary.exception.reason, "publication_comment_not_authorized"
        )
        self.assertEqual(
            completed.exception.reason, "publication_finalization_complete"
        )
        self.assertEqual(result, provider.update_issue_comment.return_value)

    def test_unavailable_database_before_create_blocks_the_provider_write(self) -> None:
        run_id, batch = self.start_recorded_run(
            request_key="github:issue-comment:database-fails-before-write"
        )
        with self.runtime.transaction() as connection:
            prepared = publications.prepare_publication(
                connection, run_id=run_id, plan=self.plan(batch)
            )
            claim = publications.claim_publication(
                connection,
                prepared.id,
                lease_owner="publisher-one",
                lease_duration=timedelta(seconds=30),
            )

        github = FakePostgresPublicationGitHub(self.runtime)
        held_connections = ExitStack()

        def exhaust_pool() -> None:
            for _ in range(self.runtime.pool_metrics().maximum_size):
                held_connections.enter_context(self.runtime.transaction())

        github.before_target_read = exhaust_pool
        with held_connections:
            with self.assertRaises(PostgreSQLUnavailable):
                review_publication_application.publish_postgres_publication(
                    self.runtime,
                    publication_id=int(prepared.id),
                    github=github,
                    max_comment_bytes=60_000,
                    lease_owner="publisher-one",
                    lease_generation=claim.publication.delivery_lease_generation,
                    posted_github=github,
                )

        self.assertEqual(github.create_calls, 0)

    def test_terminal_run_publication_is_retired_without_github_delivery(self) -> None:
        run_id, batch = self.start_recorded_run(
            request_key="github:issue-comment:terminal-publication"
        )
        with self.runtime.transaction() as connection:
            prepared = publications.prepare_publication(
                connection, run_id=run_id, plan=self.plan(batch)
            )
            review_run_application.mark_superseded_in_transaction(
                connection, run_id
            )

        github = FakePostgresPublicationGitHub(self.runtime)
        worker = PublicationWorker(
            self.runtime,
            github,
            PublisherPolicy(
                lease_duration=timedelta(seconds=30),
                heartbeat_interval=timedelta(seconds=5),
                retry_delay=timedelta(seconds=1),
                poll_interval=timedelta(milliseconds=10),
                max_comment_bytes=60_000,
            ),
            lease_owner="terminal-run-test",
            stop_event=threading.Event(),
        )
        worker.run(once=True)

        with self.runtime.transaction() as connection:
            retired = publications.get_publication(connection, prepared.id)
        self.assertEqual(retired.status, publications.PublicationStatus.STALE)
        self.assertEqual(github.comments, [])

    def test_expired_final_attempt_fails_publication_and_run(self) -> None:
        run_id, batch = self.start_recorded_run(
            request_key="github:issue-comment:exhausted-publication"
        )
        with self.runtime.transaction() as connection:
            prepared = publications.prepare_publication(
                connection,
                run_id=run_id,
                plan=self.plan(batch),
                delivery_max_attempts=1,
            )
            claimed = publications.claim_publication(
                connection,
                prepared.id,
                lease_owner="dead-publisher",
                lease_duration=timedelta(seconds=30),
            )
            connection.execute(
                """
                UPDATE review_agent.publications
                SET delivery_lease_expires_at = statement_timestamp()
                    - INTERVAL '1 second'
                WHERE id = %s
                """,
                (prepared.id,),
            )
        self.assertTrue(claimed.acquired)

        worker = PublicationWorker(
            self.runtime,
            FakePostgresPublicationGitHub(self.runtime),
            PublisherPolicy(
                lease_duration=timedelta(seconds=30),
                heartbeat_interval=timedelta(seconds=5),
                retry_delay=timedelta(seconds=1),
                poll_interval=timedelta(milliseconds=10),
                max_comment_bytes=60_000,
            ),
            lease_owner="recovery-publisher",
            stop_event=threading.Event(),
        )
        worker.run(once=True)

        with self.runtime.transaction() as connection:
            failed = publications.get_publication(connection, prepared.id)
            failed_run = review_runs.get_run(connection, run_id)
        self.assertEqual(failed.status, publications.PublicationStatus.FAILED)
        self.assertEqual(failed_run.status, ReviewStatus.FAILED)
        self.assertEqual(
            failed_run.failure_code, "publication_attempts_exhausted"
        )

    def test_stale_sweep_leaves_publication_recovery_to_the_publisher(self) -> None:
        run_id, batch = self.start_recorded_run(
            request_key="github:issue-comment:stale-publication-lease"
        )
        with self.runtime.transaction() as connection:
            prepared = publications.prepare_publication(
                connection, run_id=run_id, plan=self.plan(batch)
            )
            claimed = publications.claim_publication(
                connection,
                prepared.id,
                lease_owner="stale-publisher",
                lease_duration=timedelta(minutes=5),
            )
            self.assertTrue(claimed.acquired)
            stale_at = datetime(2026, 8, 20, tzinfo=timezone.utc)
            connection.execute(
                """
                UPDATE review_agent.review_runs
                SET started_at = %s, last_heartbeat_at = %s
                WHERE id = %s
                """,
                (stale_at, stale_at, run_id),
            )
            connection.execute(
                """
                UPDATE review_agent.publications
                SET delivery_lease_expires_at = statement_timestamp()
                    - INTERVAL '1 second'
                WHERE id = %s
                """,
                (prepared.id,),
            )
            recovered = review_run_application.mark_stale_runs_failed_in_transaction(
                connection,
                cutoff=datetime(2026, 8, 24, tzinfo=timezone.utc),
                repository=None,
                pr_number=None,
            )
            expired = publications.get_publication(connection, prepared.id)
            reclaimed = publications.claim_next_publication(
                connection,
                lease_owner="recovery-publisher",
                lease_duration=timedelta(minutes=5),
            )
            retained = publications.get_publication(connection, prepared.id)

        self.assertEqual(recovered, ())
        self.assertEqual(expired.status, publications.PublicationStatus.POSTING)
        self.assertEqual(expired.delivery_lease_owner, "stale-publisher")
        self.assertIsNotNone(reclaimed)
        self.assertTrue(reclaimed.acquired)
        self.assertEqual(retained.status, publications.PublicationStatus.POSTING)
        self.assertEqual(retained.delivery_lease_owner, "recovery-publisher")

    def test_publication_intent_and_review_job_handoff_are_atomic(self) -> None:
        run_id, batch = self.start_recorded_run(
            request_key="github:issue-comment:publication-handoff"
        )
        with self.runtime.transaction() as connection:
            enqueued = jobs.enqueue_run(
                connection,
                review_run_id=run_id,
                priority=0,
                max_attempts=3,
                active_job_limit=10,
            )
        with self.runtime.transaction() as connection:
            claimed = jobs.claim_next_job(
                connection,
                lease_owner="review-worker",
                lease_duration=timedelta(seconds=30),
                priority_aging_interval=timedelta(minutes=15),
            )
        assert claimed is not None
        self.assertEqual(claimed.id, enqueued.job.id)

        with self.assertRaises(jobs.ReviewJobLeaseLost):
            with self.runtime.transaction() as connection:
                publications.prepare_publication(
                    connection,
                    run_id=run_id,
                    plan=self.plan(batch),
                    review_job_id=claimed.id,
                    review_lease_generation=claimed.lease_generation + 1,
                )
        with self.runtime.transaction() as connection:
            publication_count = connection.execute(
                "SELECT count(*) FROM review_agent.publications WHERE review_run_id = %s",
                (run_id,),
            ).fetchone()
        self.assertEqual(publication_count, (0,))

        with self.runtime.transaction() as connection:
            prepared = publications.prepare_publication(
                connection,
                run_id=run_id,
                plan=self.plan(batch),
                review_job_id=claimed.id,
                review_lease_generation=claimed.lease_generation,
            )
            handed_off = jobs.get_job(connection, claimed.id)
        self.assertEqual(prepared.status, publications.PublicationStatus.GENERATED)
        self.assertEqual(
            handed_off.status, jobs.ReviewJobStatus.AWAITING_PUBLICATION
        )
        with self.runtime.transaction() as connection:
            stale_at = datetime(2026, 8, 20, tzinfo=timezone.utc)
            connection.execute(
                "UPDATE review_agent.review_runs "
                "SET started_at = %s, last_heartbeat_at = %s WHERE id = %s",
                (stale_at, stale_at, run_id),
            )
            generated_sweep = (
                review_run_application.mark_stale_runs_failed_in_transaction(
                    connection,
                    cutoff=datetime(2026, 8, 24, tzinfo=timezone.utc),
                    repository=None,
                    pr_number=None,
                )
            )
            posting = publications.claim_publication(connection, prepared.id)
            assert posting.publication.posting_started_at is not None
            retry = publications.fail_publication(
                connection,
                publication_id=prepared.id,
                posting_started_at=posting.publication.posting_started_at,
                failure_code="retryable_provider_error",
            )
            retry_sweep = (
                review_run_application.mark_stale_runs_failed_in_transaction(
                    connection,
                    cutoff=datetime(2026, 8, 24, tzinfo=timezone.utc),
                    repository=None,
                    pr_number=None,
                )
            )
            retained_job = jobs.get_job(connection, claimed.id)

        self.assertEqual(generated_sweep, ())
        self.assertEqual(retry.status, publications.PublicationStatus.PUBLISH_FAILED)
        self.assertEqual(retry_sweep, ())
        self.assertEqual(
            retained_job.status, jobs.ReviewJobStatus.AWAITING_PUBLICATION
        )

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
        exact_body = prepared.parts[0].delivery.body
        marker = exact_body[exact_body.index("<!--") :]
        github.comments[0] = replace(
            github.comments[0],
            body=f"outdated body part=99/99\n\n{marker}",
        )

        with self.runtime.transaction() as connection:
            connection.execute(
                """
                UPDATE review_agent.publications
                SET delivery_lease_expires_at = statement_timestamp()
                    - INTERVAL '1 second'
                WHERE id = %s
                """,
                (prepared.id,),
            )

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
        self.assertEqual(github.comments[0].body, exact_body)

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

    def test_permanent_gateway_rejection_does_not_consume_retry_budget(self) -> None:
        run_id, batch = self.start_recorded_run(
            request_key="github:issue-comment:permanent-rejection"
        )
        with self.runtime.transaction() as connection:
            prepared = publications.prepare_publication(
                connection, run_id=run_id, plan=self.plan(batch)
            )
        github = FakePostgresPublicationGitHub(self.runtime)
        github.create_error = GitHubPublicationError(
            "repository_not_authorized", retryable=False
        )

        result = review_publication_application.publish_postgres_publication(
            self.runtime,
            publication_id=int(prepared.id),
            github=github,
            max_comment_bytes=60_000,
        )

        with self.runtime.transaction() as connection:
            stored = connection.execute(
                """
                SELECT status, failure_code, delivery_attempt_count
                FROM review_agent.publications
                WHERE id = %s
                """,
                (prepared.id,),
            ).fetchone()
            run = review_runs.get_run(connection, run_id)
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(result.status, "failed")
        self.assertEqual(stored, ("failed", "repository_not_authorized", 1))
        self.assertEqual(run.failure_code, "repository_not_authorized")

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
        # Recovery and failure-status cleanup precede posted supersession.
        github.fail_on_list_call = github.list_issue_comments_calls + 3

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
        self.assertEqual(cleared.delivery_status, "suppressed")

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

    def test_ordinary_publisher_delivers_one_durable_failure_status(self) -> None:
        run_id, _ = self.start_recorded_run(request_key="failure-status-worker")
        with self.runtime.transaction() as connection:
            review_runs.fail_run(connection, run_id, failure_code="review_deliver_error")
        github = FakePostgresPublicationGitHub(self.runtime)
        worker = PublicationWorker(
            self.runtime, github,
            PublisherPolicy(
                lease_duration=timedelta(seconds=30), heartbeat_interval=timedelta(seconds=5),
                retry_delay=timedelta(seconds=1), poll_interval=timedelta(milliseconds=10),
                max_comment_bytes=60_000,
            ),
            lease_owner="failure-worker", stop_event=threading.Event(),
        )

        worker.run(once=True)

        with self.runtime.transaction() as connection:
            stored = review_runs.failure_status_target(connection, run_id)
        self.assertEqual(stored.delivery_status, "posted")
        self.assertEqual(stored.comment_id, github.comments[0].comment_id)
        self.assertEqual(len(github.comments), 1)

    def test_failure_status_reclaims_after_create_without_duplicate(self) -> None:
        run_id, _ = self.start_recorded_run(request_key="failure-status-reclaim")
        with self.runtime.transaction() as connection:
            review_runs.fail_run(connection, run_id, failure_code="review_deliver_error")
            claim = review_runs.claim_failure_status(
                connection, run_id=run_id, lease_owner="dead-worker",
                lease_duration=timedelta(seconds=30),
            )
        github = FakePostgresPublicationGitHub(self.runtime)
        github.kill_after_create = True
        with self.assertRaises(ProcessDeath):
            review_publication_application.publish_postgres_run_failure_status(
                self.runtime, run_id=int(run_id), github=github,
                lease_owner="dead-worker",
                lease_generation=claim.target.delivery_lease_generation,
            )
        original_id = github.comments[0].comment_id
        with self.runtime.transaction() as connection:
            connection.execute(
                "UPDATE review_agent.review_runs SET "
                "failure_status_delivery_lease_expires_at = statement_timestamp() "
                "WHERE id = %s", (run_id,),
            )
        worker = PublicationWorker(
            self.runtime, github,
            PublisherPolicy(
                lease_duration=timedelta(seconds=30), heartbeat_interval=timedelta(seconds=5),
                retry_delay=timedelta(seconds=1), poll_interval=timedelta(milliseconds=10),
                max_comment_bytes=60_000,
            ), lease_owner="recovery-worker", stop_event=threading.Event(),
        )

        worker.run(once=True)

        self.assertEqual(len(github.comments), 1)
        self.assertEqual(github.comments[0].comment_id, original_id)
        with self.runtime.transaction() as connection:
            stored = review_runs.failure_status_target(connection, run_id)
        self.assertEqual(stored.delivery_status, "posted")

    def test_failure_status_recovery_retries_when_marker_listing_fails(self) -> None:
        run_id, _ = self.start_recorded_run(
            request_key="failure-status-list-retry"
        )
        with self.runtime.transaction() as connection:
            review_runs.fail_run(
                connection, run_id, failure_code="review_deliver_error"
            )
            claim = review_runs.claim_failure_status(
                connection,
                run_id=run_id,
                lease_owner="interrupted-publisher",
                lease_duration=timedelta(minutes=5),
            )
        github = FakePostgresPublicationGitHub(self.runtime)
        github.kill_after_create = True
        with self.assertRaises(ProcessDeath):
            review_publication_application.publish_postgres_run_failure_status(
                self.runtime,
                run_id=int(run_id),
                github=github,
                lease_owner="interrupted-publisher",
                lease_generation=claim.target.delivery_lease_generation,
            )
        with self.runtime.transaction() as connection:
            connection.execute(
                "UPDATE review_agent.review_runs SET "
                "failure_status_delivery_lease_expires_at = statement_timestamp() "
                "WHERE id = %s",
                (run_id,),
            )
            recovered = review_runs.claim_next_failure_status(
                connection,
                lease_owner="recovery-publisher",
                lease_duration=timedelta(minutes=5),
            )
        assert recovered is not None
        github.fail_on_list_call = github.list_issue_comments_calls + 1

        with self.assertRaises(GitHubPublicationError):
            review_publication_application.publish_postgres_run_failure_status(
                self.runtime,
                run_id=int(run_id),
                github=github,
                lease_owner="recovery-publisher",
                lease_generation=recovered.target.delivery_lease_generation,
            )

        self.assertEqual(len(github.comments), 1)
        with self.runtime.transaction() as connection:
            stored = review_runs.failure_status_target(connection, run_id)
        self.assertEqual(stored.delivery_status, "publish_failed")

    def test_newer_review_removes_an_unacknowledged_failure_status(self) -> None:
        failed_run, _ = self.start_recorded_run(
            request_key="failure-status-superseded-after-create"
        )
        with self.runtime.transaction() as connection:
            review_runs.fail_run(
                connection, failed_run, failure_code="review_deliver_error"
            )
            claim = review_runs.claim_failure_status(
                connection,
                run_id=failed_run,
                lease_owner="interrupted-publisher",
                lease_duration=timedelta(minutes=5),
            )
        github = FakePostgresPublicationGitHub(self.runtime)
        github.kill_after_create = True
        with self.assertRaises(ProcessDeath):
            review_publication_application.publish_postgres_run_failure_status(
                self.runtime,
                run_id=int(failed_run),
                github=github,
                lease_owner="interrupted-publisher",
                lease_generation=claim.target.delivery_lease_generation,
            )
        orphan_id = github.comments[0].comment_id

        newer_run, batch = self.start_recorded_run(
            request_key="review-after-unacknowledged-failure"
        )
        with self.runtime.transaction() as connection:
            prepared = publications.prepare_publication(
                connection, run_id=newer_run, plan=self.plan(batch)
            )
        github.kill_after_create = False
        review_publication_application.publish_postgres_publication(
            self.runtime,
            publication_id=int(prepared.id),
            github=github,
            max_comment_bytes=60_000,
        )

        self.assertNotIn(orphan_id, [item.comment_id for item in github.comments])
        with self.runtime.transaction() as connection:
            stored = review_runs.failure_status_target(connection, failed_run)
        self.assertEqual(stored.delivery_status, "suppressed")
        self.assertIsNone(stored.comment_id)

    def test_publisher_alternates_ready_review_and_failure_status_work(self) -> None:
        active_run, batch = self.start_recorded_run(
            request_key="fairness-publication", pr_number=41
        )
        with self.runtime.transaction() as connection:
            prepared = publications.prepare_publication(
                connection, run_id=active_run, plan=self.plan(batch)
            )
        failed_run, _ = self.start_recorded_run(
            request_key="fairness-failure", pr_number=42
        )
        with self.runtime.transaction() as connection:
            review_runs.fail_run(
                connection, failed_run, failure_code="review_deliver_error"
            )
        github = FakePostgresPublicationGitHub(self.runtime)
        worker = PublicationWorker(
            self.runtime, github,
            PublisherPolicy(
                lease_duration=timedelta(seconds=30), heartbeat_interval=timedelta(seconds=5),
                retry_delay=timedelta(seconds=1), poll_interval=timedelta(milliseconds=10),
                max_comment_bytes=60_000,
            ), lease_owner="fairness-worker", stop_event=threading.Event(),
        )

        worker.run(once=True)
        worker.run(once=True)

        with self.runtime.transaction() as connection:
            publication = publications.get_publication(connection, prepared.id)
            failure = review_runs.failure_status_target(connection, failed_run)
        self.assertEqual(publication.status.value, "posted")
        self.assertEqual(failure.delivery_status, "posted")
        self.assertEqual(github.posted_publications, [int(prepared.id)])

    def test_newer_success_suppresses_older_pending_failure_status(self) -> None:
        failed_run, _ = self.start_recorded_run(request_key="older-pending-failure")
        with self.runtime.transaction() as connection:
            review_runs.fail_run(connection, failed_run, failure_code="review_deliver_error")
        newer_run, batch = self.start_recorded_run(request_key="newer-success")
        with self.runtime.transaction() as connection:
            prepared = publications.prepare_publication(
                connection, run_id=newer_run, plan=self.plan(batch)
            )
        github = FakePostgresPublicationGitHub(self.runtime)

        review_publication_application.publish_postgres_publication(
            self.runtime, publication_id=int(prepared.id), github=github,
            max_comment_bytes=60_000,
        )

        with self.runtime.transaction() as connection:
            stored = review_runs.failure_status_target(connection, failed_run)
        self.assertEqual(stored.delivery_status, "suppressed")
        self.assertIsNone(stored.comment_id)

    def test_manual_failure_recovery_excludes_an_older_pull_request_run(self) -> None:
        older_run, _ = self.start_recorded_run(request_key="older-failure-recovery")
        with self.runtime.transaction() as connection:
            review_runs.fail_run(
                connection, older_run, failure_code="review_deliver_error"
            )
        self.start_recorded_run(request_key="newer-review-recovery")

        with self.runtime.transaction() as connection:
            ordinary_claim = review_runs.claim_next_failure_status(
                connection,
                lease_owner="failure-worker",
                lease_duration=timedelta(minutes=5),
            )
            queued = review_runs.failed_runs_needing_status(
                connection, repository="team/service", pr_number=41
            )
            with self.assertRaises(review_runs.FailureStatusLeaseLost):
                review_runs.claim_failure_status(
                    connection,
                    run_id=older_run,
                    lease_owner="operator",
                    lease_duration=timedelta(minutes=5),
                )

        self.assertIsNone(ordinary_claim)
        self.assertEqual(queued, ())

    def test_failure_status_retry_requires_a_nonblank_failure_code(self) -> None:
        run_id, _ = self.start_recorded_run(request_key="blank-delivery-failure")
        with self.runtime.transaction() as connection:
            review_runs.fail_run(
                connection, run_id, failure_code="review_deliver_error"
            )
            claim = review_runs.claim_failure_status(
                connection,
                run_id=run_id,
                lease_owner="failure-worker",
                lease_duration=timedelta(minutes=5),
            )

        with self.assertRaises(review_runs.ReviewRunError):
            with self.runtime.transaction() as connection:
                review_runs.retry_failure_status(
                    connection,
                    run_id=run_id,
                    lease_owner="failure-worker",
                    lease_generation=claim.target.delivery_lease_generation,
                    failure_code="   ",
                    retry_delay=timedelta(seconds=1),
                )

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

    def test_suggestion_recovery_requires_one_exact_review_comment_multiset(
        self,
    ) -> None:
        run_id, batch = self.start_recorded_run(
            request_key="github:issue-comment:exact-suggestion-recovery"
        )
        plan = self.suggestion_plan(batch, two_comments=True)
        with self.runtime.transaction() as connection:
            prepared = publications.prepare_publication(
                connection, run_id=run_id, plan=plan
            )
        key = plan.publication_key
        first_body = (
            "```suggestion\nTrue\n```\n\n"
            f"review-agent:canonical publication={key}"
        )
        second_body = (
            "```suggestion\nFalse\n```\n\n"
            f"review-agent:canonical publication={key}"
        )

        def review_comment(
            *, comment_id: int, review_id: int, body: str, line: int
        ) -> PullRequestReviewComment:
            return PullRequestReviewComment(
                comment_id=comment_id,
                review_id=review_id,
                body=body,
                author_login="review-agent[bot]",
                path="backend/changed.py",
                commit_id="a" * 40,
                line=line,
                side="RIGHT",
                start_line=None,
                start_side=None,
            )

        github = FakePostgresPublicationGitHub(self.runtime)
        github.review_comments = [
            review_comment(comment_id=770, review_id=77, body=first_body, line=7),
            review_comment(comment_id=771, review_id=77, body=first_body, line=7),
            review_comment(comment_id=780, review_id=78, body=first_body, line=7),
            review_comment(comment_id=781, review_id=78, body=second_body, line=8),
        ]

        result = review_publication_application.publish_postgres_publication(
            self.runtime,
            publication_id=int(prepared.id),
            github=github,
            max_comment_bytes=60_000,
        )

        suggestion = next(
            part
            for part in result.published_parts
            if part.part_type is PublicationPartType.SUGGESTION_REVIEW
        )
        self.assertEqual(suggestion.external_id, 78)
        self.assertEqual(result.recovered_parts, 1)

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
