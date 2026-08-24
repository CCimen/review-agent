from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

PLUGINS = Path(__file__).resolve().parents[1] / "bootstrap" / "plugins"
sys.path.insert(0, str(PLUGINS))

from review_agent_tools import (  # noqa: E402
    memory_db,
    review_publication_application,
    review_publisher,
)
from review_agent_tools.github import publication as github_publication  # noqa: E402


class FakeGateway:
    """Records create/update/delete calls so idempotency can be asserted."""

    def __init__(self, *, login: str = "review-agent-bot", comments=None):
        self.login = login
        self._comments: list[github_publication.IssueComment] = list(comments or [])
        self.created: list[tuple[int, str]] = []
        self.updated: list[tuple[int, str]] = []
        self.deleted: list[int] = []
        self._next_id = 5000

    def current_user_login(self) -> str:
        return self.login

    def get_pull_request(self, repository: str, pr_number: int):
        del repository, pr_number
        return github_publication.PullRequestState(
            state="open", draft=False, base_sha="b" * 40, head_sha="a" * 40
        )

    def list_issue_comments(
        self, repository: str, issue_number: int, *, max_pages: int = 3
    ):
        del repository, issue_number
        # Simulate GitHub paging (100/page, oldest-first) so the >300-comment
        # failure-status fallback can be exercised.
        return list(self._comments[: max_pages * 100])

    def update_issue_comment(self, repository: str, comment_id: int, body: str):
        del repository
        self.updated.append((comment_id, body))
        return github_publication.IssueComment(
            comment_id=comment_id, body=body, author_login=self.login
        )

    def create_issue_comment(self, repository: str, issue_number: int, body: str):
        del repository, issue_number
        self._next_id += 1
        self.created.append((self._next_id, body))
        comment = github_publication.IssueComment(
            comment_id=self._next_id, body=body, author_login=self.login
        )
        self._comments.append(comment)
        return comment

    def delete_issue_comment(self, repository: str, comment_id: int) -> None:
        del repository
        self.deleted.append(comment_id)


class FailureStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / "memory.sqlite3")
        self._env = dict(os.environ)
        os.environ["REVIEW_AGENT_DB"] = self.db
        os.environ["REVIEW_AGENT_ALLOWED_REPOSITORIES"] = "sundsvallskommun/example-repository"
        memory_db.connect(self.db).close()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        self.temp.cleanup()

    def _failed_run(
        self,
        *,
        head_sha: str = "a" * 40,
        failure_code: str = "review_failed",
    ) -> int:
        with closing(memory_db.connect_existing(self.db)) as conn:
            run = memory_db.start_run(
                conn, "sundsvallskommun/example-repository", 12, base_sha="b" * 40, head_sha=head_sha
            )
            run_id = int(run["id"])
            memory_db.complete_run(
                conn,
                run_id,
                repository="sundsvallskommun/example-repository",
                pr_number=12,
                status="failed",
                failure_code=failure_code,
            )
        return run_id

    def test_review_runs_has_failure_status_columns(self):
        with closing(memory_db.connect(self.db)) as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(review_runs)")}
        self.assertIn("failure_detail", cols)
        self.assertIn("failure_status_comment_id", cols)
        self.assertIn("failure_status_posted_at", cols)

    def test_publish_failure_status_posts_marker_and_persists_on_failed_run(self):
        run_id = self._failed_run()  # terminal status='failed' row
        fake = FakeGateway()
        with closing(memory_db.connect_existing(self.db)) as conn:
            result = review_publisher.publish_run_failure_status(
                conn,
                run_id=run_id,
                failure_code="github_diff_406",
                github=fake,
            )
        self.assertTrue(result["posted"])
        self.assertEqual(len(fake.created), 1)
        body = fake.created[0][1]
        self.assertIn(f"review-agent:failure-status run={run_id}", body)
        self.assertIn("GitHub could not render this pull request's diff", body)
        self.assertIn("post `/review` again as a new top-level PR comment", body)
        self.assertIn("share the status code with the reviewer operator", body)
        with closing(memory_db.connect_existing(self.db)) as conn:
            run = memory_db.get_run(conn, run_id)
        assert run is not None
        self.assertEqual(run["failure_status_comment_id"], fake.created[0][0])
        self.assertTrue(run["failure_status_posted_at"])

    def test_installed_flat_import_path_posts_failure_status(self):
        plugin = PLUGINS / "review_agent_tools"
        tools = Path(__file__).resolve().parents[1] / "tools"
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join((str(plugin), str(tools)))
        script = r'''
from contextlib import closing
from types import SimpleNamespace
import os

import memory_db
import review_agent_memory


class Gateway:
    def __init__(self):
        self.created = []

    def current_user_login(self):
        return "review-agent-bot"

    def get_pull_request(self, repository, pr_number):
        del repository, pr_number
        return SimpleNamespace(
            state="open", draft=False, base_sha="b" * 40, head_sha="a" * 40
        )

    def list_issue_comments(self, repository, issue_number, *, max_pages=3):
        del repository, issue_number, max_pages
        return []

    def create_issue_comment(self, repository, issue_number, body):
        del repository, issue_number
        comment = SimpleNamespace(
            comment_id=9001, body=body, author_login="review-agent-bot"
        )
        self.created.append(comment)
        return comment

    def update_issue_comment(self, repository, comment_id, body):
        del repository
        return SimpleNamespace(
            comment_id=comment_id, body=body, author_login="review-agent-bot"
        )

    def delete_issue_comment(self, repository, comment_id):
        del repository, comment_id


database = os.environ["REVIEW_AGENT_DB"]
with closing(memory_db.connect(database)) as connection:
    run = memory_db.start_run(
        connection,
        "sundsvallskommun/example-repository",
        12,
        base_sha="b" * 40,
        head_sha="a" * 40,
    )
    memory_db.complete_run(
        connection,
        int(run["id"]),
        repository="sundsvallskommun/example-repository",
        pr_number=12,
        status="failed",
        failure_code="github_diff_406",
    )

gateway = Gateway()
publisher = review_agent_memory.load_review_publisher()
with closing(memory_db.connect_existing(database)) as connection:
    result = publisher.publish_run_failure_status(
        connection,
        run_id=int(run["id"]),
        failure_code="github_diff_406",
        github=gateway,
    )

assert result["posted"] is True
assert len(gateway.created) == 1
assert "review-agent:failure-status" in gateway.created[0].body
'''
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=Path(__file__).resolve().parents[1],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_publish_failure_status_is_idempotent_stored_id_first(self):
        run_id = self._failed_run()
        fake = FakeGateway()
        with closing(memory_db.connect_existing(self.db)) as conn:
            review_publisher.publish_run_failure_status(
                conn, run_id=run_id, failure_code="c", github=fake
            )
        first_id = fake.created[0][0]
        # Second call with an EMPTY comment listing: stored-id-first must PATCH the same
        # comment without listing/creating, so a noisy PR can't spawn duplicates.
        fake2 = FakeGateway(comments=[])
        with closing(memory_db.connect_existing(self.db)) as conn:
            review_publisher.publish_run_failure_status(
                conn, run_id=run_id, failure_code="c", github=fake2
            )
        self.assertEqual(fake2.created, [])
        self.assertEqual([cid for cid, _ in fake2.updated], [first_id])

    def test_publish_failure_status_no_stored_id_updates_marker_beyond_300(self):
        # No stored comment id (e.g. a pre-migration/degraded earlier post). The prior
        # failure-status comment sits beyond comment #300 — GitHub returns oldest-first,
        # so the recent status comment is on a late page. The deep scan must FIND and
        # UPDATE it, never create a duplicate.
        run_id = self._failed_run()
        marker = review_publication_application._failure_status_marker(
            run_id, "a" * 40
        )
        fillers = [
            github_publication.IssueComment(
                comment_id=i, body=f"chatter {i}", author_login="review-agent-bot"
            )
            for i in range(304)
        ]
        marker_comment = github_publication.IssueComment(
            comment_id=9999, body=f"earlier status\n{marker}\n", author_login="review-agent-bot"
        )
        fake = FakeGateway(comments=fillers + [marker_comment])
        with closing(memory_db.connect_existing(self.db)) as conn:
            review_publisher.publish_run_failure_status(
                conn, run_id=run_id, failure_code="c", github=fake
            )
        self.assertEqual(fake.created, [])
        self.assertEqual([cid for cid, _ in fake.updated], [9999])

    def test_snapshot_status_explains_base_or_head_change_without_delivery_error(self):
        run_id = self._failed_run(failure_code="snapshot_superseded")
        fake = FakeGateway()

        with closing(memory_db.connect_existing(self.db)) as conn:
            review_publisher.publish_run_failure_status(
                conn,
                run_id=run_id,
                failure_code="snapshot_superseded",
                github=fake,
            )

        body = fake.created[0][1]
        self.assertIn("review snapshot was superseded", body.lower())
        self.assertIn("base or head changed", body.lower())
        self.assertIn("If a newer review is not already running", body)
        self.assertNotIn("failed during delivery", body)

    def test_reaper_uses_publisher_owned_snapshot_status_copy(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
        import review_agent_memory as cli

        self._failed_run(failure_code="snapshot_superseded")
        fake = FakeGateway()

        with closing(memory_db.connect_existing(self.db)) as conn:
            summary = cli.reap_and_publish(
                conn,
                memory_db,
                review_publisher,
                repository="sundsvallskommun/example-repository",
                github=fake,
            )

        self.assertEqual(summary["status_posted"], 1)
        body = fake.created[0][1]
        self.assertIn("review snapshot was superseded", body.lower())
        self.assertIn("base or head changed", body.lower())
        self.assertNotIn("see operator logs", body)

    def test_reaper_skips_superseded_run_when_a_newer_review_exists(self):
        with closing(memory_db.connect_existing(self.db)) as conn:
            first = memory_db.start_run(
                conn,
                "sundsvallskommun/example-repository",
                12,
                base_sha="b" * 40,
                head_sha="a" * 40,
            )
            second = memory_db.start_run(
                conn,
                "sundsvallskommun/example-repository",
                12,
                base_sha="b" * 40,
                head_sha="c" * 40,
            )
            memory_db.complete_run(
                conn,
                int(second["id"]),
                status="generated",
                posted_comment_id=9001,
            )
            first_row = memory_db.get_run(conn, int(first["id"]))
        assert first_row is not None
        self.assertEqual(first_row["failure_code"], "snapshot_superseded")

        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
        import review_agent_memory as cli

        fake = FakeGateway()
        with closing(memory_db.connect_existing(self.db)) as conn:
            summary = cli.reap_and_publish(
                conn,
                memory_db,
                review_publisher,
                repository="sundsvallskommun/example-repository",
                github=fake,
            )

        self.assertEqual(summary["status_posted"], 0)
        self.assertEqual(fake.created, [])

    def test_cleanup_removes_marker_only_failure_status_without_stored_id(self):
        # A failure-status comment posted in degraded mode has the marker but no stored
        # DB id. The stored-id sweep can't see it; the marker fallback must delete it so a
        # successful retry never leaves a stale failure comment.
        marker = review_publication_application._failure_status_marker(
            123, "a" * 40
        )
        orphan = github_publication.IssueComment(
            comment_id=4242, body=f"status\n{marker}\n", author_login="review-agent-bot"
        )
        fake = FakeGateway(comments=[orphan])
        with closing(memory_db.connect_existing(self.db)) as conn:
            review_publication_application._cleanup_prior_failure_status(
                conn, fake, "sundsvallskommun/example-repository", 12
            )
        self.assertIn(4242, fake.deleted)

    def _finding(self) -> dict[str, object]:
        return {
            "rule_id": "authorization.missing-context",
            "category": "security",
            "path": "backend/changed.py",
            "line": 10,
            "symbol": "handler",
            "anchor": "PUT /resources/{resource_id}",
            "title": "Authorization context omitted",
            "severity": "Critical",
            "publication_score": 9,
            "confidence": 0.9,
            "evidence": "Concrete evidence.",
            "disproof_checks": "Checked the guard.",
            "impact": "Cross-scope write.",
            "smallest_fix": "Derive resource scope from verified context.",
            "introduced_by_diff": True,
        }

    def test_publish_review_success_cleans_up_prior_failure_status(self):
        # Run A failed and left a failure-status comment (id 7777) on PR 12.
        run_a = self._failed_run()
        with closing(memory_db.connect_existing(self.db)) as conn:
            memory_db.record_failure_status_comment(
                conn, run_a, comment_id=7777, posted_at="t"
            )
        finding = self._finding()
        fake = FakeGateway()
        # Run B reviews the same PR and publishes successfully.
        with closing(memory_db.connect_existing(self.db)) as conn:
            run = memory_db.start_run(
                conn, "sundsvallskommun/example-repository", 12, base_sha="b" * 40, head_sha="a" * 40
            )
            run_b = int(run["id"])
            memory_db.record_findings(
                conn,
                "sundsvallskommun/example-repository",
                12,
                "a" * 40,
                [finding],
                review_run_id=run_b,
                base_sha="b" * 40,
                context_hashes={"backend/changed.py": "d" * 40},
            )
            memory_db.update_run_phase(
                conn, run_b, "rendering", repository="sundsvallskommun/example-repository", pr_number=12
            )
            final = memory_db.finalize_review(
                conn, "sundsvallskommun/example-repository", 12, "a" * 40, review_run_id=run_b
            )
            result = review_publisher.publish_review(
                conn,
                publication_id=int(final["publication_id"]),
                review_run_id=run_b,
                github=fake,
            )
        self.assertTrue(result["published"])
        self.assertIn(7777, fake.deleted)
        with closing(memory_db.connect_existing(self.db)) as conn:
            run_a_row = memory_db.get_run(conn, run_a)
        assert run_a_row is not None
        self.assertIsNone(run_a_row["failure_status_comment_id"])

    def test_reaper_marks_stale_then_posts_failure_status(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
        import review_agent_memory as cli

        with closing(memory_db.connect_existing(self.db)) as conn:
            run = memory_db.start_run(
                conn, "sundsvallskommun/example-repository", 12, base_sha="b" * 40, head_sha="a" * 40
            )
            run_id = int(run["id"])
            conn.execute(
                "UPDATE review_runs SET last_heartbeat_at = ? WHERE id = ?",
                ("2020-01-01T00:00:00+00:00", run_id),
            )
            conn.commit()

        fake = FakeGateway()
        with closing(memory_db.connect_existing(self.db)) as conn:
            summary = cli.reap_and_publish(
                conn,
                memory_db,
                review_publisher,
                repository="sundsvallskommun/example-repository",
                older_than_minutes=30,
                github=fake,
            )
        self.assertEqual(summary["marked_failed"], 1)
        self.assertEqual(summary["status_posted"], 1)
        self.assertEqual(summary["status_failed"], [])
        self.assertEqual(len(fake.created), 1)
        with closing(memory_db.connect_existing(self.db)) as conn:
            row = memory_db.get_run(conn, run_id)
        assert row is not None
        self.assertEqual(row["status"], "failed")
        self.assertIsNotNone(row["failure_status_comment_id"])

    def test_memory_runs_does_not_import_review_publisher(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "bootstrap/plugins/review_agent_tools/memory_runs.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("review_publisher", source)


if __name__ == "__main__":
    unittest.main()
