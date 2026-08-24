from __future__ import annotations

import tempfile
import sys
import unittest
from collections.abc import Sequence
from pathlib import Path

PLUGIN_PARENT = Path(__file__).resolve().parents[1] / "bootstrap" / "plugins"
sys.path.insert(0, str(PLUGIN_PARENT))

from review_agent_tools import (  # noqa: E402
    memory_db,
    publication_partition,
    review_publication_application,
)
from review_agent_tools.github.publication import (  # noqa: E402
    InlineReviewComment,
    IssueComment,
    PullRequestReview,
    PullRequestReviewComment,
    PullRequestState,
)


class RecordingGitHub:
    def __init__(self) -> None:
        self.created: list[IssueComment] = []

    def current_user_login(self) -> str:
        return "review-agent-bot"

    def get_pull_request(self, repository: str, pr_number: int) -> PullRequestState:
        del repository, pr_number
        return PullRequestState(
            state="open",
            draft=False,
            base_sha="b" * 40,
            head_sha="a" * 40,
        )

    def list_issue_comments(
        self, repository: str, issue_number: int, *, max_pages: int = 3
    ) -> list[IssueComment]:
        del repository, issue_number, max_pages
        return list(self.created)

    def create_issue_comment(
        self, repository: str, issue_number: int, body: str
    ) -> IssueComment:
        del repository, issue_number
        comment = IssueComment(
            comment_id=1000 + len(self.created),
            body=body,
            author_login="review-agent-bot",
        )
        self.created.append(comment)
        return comment

    def update_issue_comment(
        self, repository: str, comment_id: int, body: str
    ) -> IssueComment:
        del repository
        return IssueComment(
            comment_id=comment_id,
            body=body,
            author_login="review-agent-bot",
        )

    def delete_issue_comment(self, repository: str, comment_id: int) -> None:
        del repository, comment_id

    def create_pull_request_review(
        self,
        repository: str,
        pr_number: int,
        *,
        commit_id: str,
        body: str,
        comments: Sequence[InlineReviewComment],
    ) -> PullRequestReview:
        del repository, pr_number, commit_id, body, comments
        raise AssertionError("a publication without suggestions creates no review")

    def list_pull_request_review_comments(
        self, repository: str, pr_number: int, *, max_pages: int = 3
    ) -> list[PullRequestReviewComment]:
        del repository, pr_number, max_pages
        return []


class ReviewPublicationApplicationTests(unittest.TestCase):
    def test_application_publishes_the_exact_partitioned_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            connection = memory_db.connect(str(Path(temporary) / "memory.sqlite3"))
            self.addCleanup(connection.close)
            run = memory_db.start_run(
                connection,
                "example-org/example-repository",
                17,
                base_sha="b" * 40,
                head_sha="a" * 40,
            )
            memory_db.record_findings(
                connection,
                "example-org/example-repository",
                17,
                "a" * 40,
                [
                    {
                        "rule_id": "authorization.missing-context",
                        "category": "security",
                        "path": "src/api/resources.py",
                        "line": 42,
                        "symbol": "update_resource",
                        "anchor": "PUT /v1/resources/{resource_id}",
                        "title": "Resource update omits authorization context",
                        "severity": "High",
                        "publication_score": 9,
                        "confidence": 0.93,
                        "evidence": "The changed query writes a caller-controlled scope.",
                        "disproof_checks": "Checked the dependency and repository layer.",
                        "impact": "Cross-scope write.",
                        "smallest_fix": "Bind scope from verified context.",
                        "introduced_by_diff": True,
                    }
                ],
                review_run_id=int(run["id"]),
                base_sha="b" * 40,
                context_hashes={"src/api/resources.py": "d" * 40},
            )
            publication = memory_db.finalize_review(
                connection,
                "example-org/example-repository",
                17,
                "a" * 40,
                review_run_id=int(run["id"]),
            )
            expected = publication_partition.split_publication_body(
                str(publication["markdown"]),
                publication_key=str(publication["publication_key"]),
                max_comment_bytes=65_536,
                rendered_blocks_json=str(publication["rendered_blocks_json"]),
            )
            github = RecordingGitHub()

            result = review_publication_application.publish_review(
                connection,
                publication_id=int(publication["publication_id"]),
                review_run_id=int(run["id"]),
                github=github,
                max_comment_bytes=65_536,
            )

        self.assertTrue(result["published"])
        self.assertEqual([comment.body for comment in github.created], [expected[0].body])


if __name__ == "__main__":
    unittest.main()
