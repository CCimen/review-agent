from __future__ import annotations

import os
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch


PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "bootstrap" / "plugins"
sys.path.insert(0, str(PACKAGE_ROOT))

from review_agent_tools import memory_db, review_run_application  # noqa: E402


REPOSITORY = "sundsvallskommun/example-repository"
BASE_SHA = "b" * 40
HEAD_SHA = "a" * 40


class ReviewRunApplicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.previous_db = os.environ.get("REVIEW_AGENT_DB")
        os.environ["REVIEW_AGENT_DB"] = str(Path(self.temp.name) / "memory.sqlite3")
        memory_db.connect(os.environ["REVIEW_AGENT_DB"]).close()

    def tearDown(self) -> None:
        if self.previous_db is None:
            os.environ.pop("REVIEW_AGENT_DB", None)
        else:
            os.environ["REVIEW_AGENT_DB"] = self.previous_db
        self.temp.cleanup()

    def start_run(self, *, pr_number: int = 1) -> review_run_application.RunSubject:
        started = review_run_application.start_run(
            review_run_application.RunRequest(
                repository=REPOSITORY,
                pr_number=pr_number,
                base_sha=BASE_SHA,
                head_sha=HEAD_SHA,
            )
        )
        self.assertIsInstance(started, review_run_application.StartedRun)
        assert isinstance(started, review_run_application.StartedRun)
        return review_run_application.RunSubject(
            repository=REPOSITORY,
            pr_number=pr_number,
            run_id=started.run_id,
        )

    @staticmethod
    def pull_snapshot() -> review_run_application.PullSnapshot[dict[str, str]]:
        return review_run_application.PullSnapshot(
            payload={"state": "open"},
            base_sha=BASE_SHA,
            head_sha=HEAD_SHA,
        )

    def test_inactive_run_is_rejected_before_pull_load(self) -> None:
        calls = 0

        def load_pull() -> review_run_application.PullSnapshot[str]:
            nonlocal calls
            calls += 1
            return review_run_application.PullSnapshot(
                payload="pull",
                base_sha=BASE_SHA,
                head_sha=HEAD_SHA,
            )

        subject = review_run_application.RunSubject(
            repository=REPOSITORY,
            pr_number=1,
            run_id=999,
        )

        with self.assertRaisesRegex(
            review_run_application.ReviewRunError,
            "run_id does not match a recorded review run",
        ):
            review_run_application.load_snapshot(
                subject,
                phase="reviewing",
                pull_loader=load_pull,
            )

        self.assertEqual(calls, 0)

    def test_expected_head_is_rejected_before_pull_load(self) -> None:
        subject = self.start_run()
        calls = 0

        def load_pull() -> review_run_application.PullSnapshot[str]:
            nonlocal calls
            calls += 1
            return review_run_application.PullSnapshot(
                payload="pull",
                base_sha=BASE_SHA,
                head_sha=HEAD_SHA,
            )

        with self.assertRaisesRegex(
            review_run_application.ReviewRunError,
            "head_sha does not match the active review run",
        ):
            review_run_application.load_snapshot(
                subject,
                phase="reviewing",
                pull_loader=load_pull,
                expected_head_sha="c" * 40,
            )

        self.assertEqual(calls, 0)

    def test_snapshot_load_uses_two_bounded_database_connections(self) -> None:
        subject = self.start_run()
        original_connect = memory_db.connect_existing
        connection_count = 0

        def counted_connect(path: str | None = None):
            nonlocal connection_count
            connection_count += 1
            return original_connect(path)

        with patch.object(
            review_run_application.memory_db,
            "connect_existing",
            side_effect=counted_connect,
        ):
            review_run_application.load_snapshot(
                subject,
                phase="reviewing",
                pull_loader=self.pull_snapshot,
            )

        self.assertEqual(connection_count, 2)

    def test_changed_snapshot_terminalizes_once_and_reuses_terminal_state(self) -> None:
        subject = self.start_run()
        calls = 0

        def load_changed_pull() -> review_run_application.PullSnapshot[str]:
            nonlocal calls
            calls += 1
            return review_run_application.PullSnapshot(
                payload="pull",
                base_sha=BASE_SHA,
                head_sha="c" * 40,
            )

        with self.assertRaises(review_run_application.ReviewRunTerminal) as first:
            review_run_application.load_snapshot(
                subject,
                phase="reviewing",
                pull_loader=load_changed_pull,
            )

        self.assertTrue(first.exception.newly_terminalized)
        self.assertEqual(calls, 1)

        with self.assertRaises(review_run_application.ReviewRunTerminal) as second:
            review_run_application.load_snapshot(
                subject,
                phase="reviewing",
                pull_loader=load_changed_pull,
            )

        self.assertFalse(second.exception.newly_terminalized)
        self.assertEqual(calls, 1)
        with closing(memory_db.connect_existing()) as connection:
            run = memory_db.get_run(connection, subject.run_id)
        self.assertIsNotNone(run)
        assert run is not None
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["failure_code"], "snapshot_superseded")

    def test_snapshot_result_and_coverage_are_owned_by_run_application(self) -> None:
        subject = self.start_run()

        result = review_run_application.load_snapshot(
            subject,
            phase="collecting_diff",
            pull_loader=self.pull_snapshot,
        )
        self.assertEqual(result.pull, {"state": "open"})
        self.assertEqual(result.run.base_sha, BASE_SHA)
        self.assertEqual(result.run.head_sha, HEAD_SHA)

        file_index = review_run_application.register_changed_files(
            subject,
            files=[{"path": "backend/api.py", "status": "modified"}],
            changed_files_reported=1,
        )
        self.assertTrue(file_index["changed_file_registration_complete"])

        review_run_application.record_diff_result(
            subject,
            review_run_application.DiffExposure(exposed_paths=("backend/api.py",)),
        )
        review_run_application.record_source_read(
            subject,
            path="backend/api.py",
            side="head",
            start_line=10,
            line_count=11,
        )

        page = review_run_application.load_changed_file_page(
            subject,
            pull_loader=self.pull_snapshot,
            limit=10,
        )
        context = review_run_application.load_file_context(
            subject,
            path="backend/api.py",
            pull_loader=self.pull_snapshot,
        )
        self.assertEqual(page["items"][0]["diff_state"], "complete")
        self.assertEqual(context.file["item"]["path"], "backend/api.py")
        with closing(memory_db.connect_existing()) as connection:
            coverage = memory_db.coverage_summary(connection, run_id=subject.run_id)
            run = memory_db.get_run(connection, subject.run_id)
        self.assertIsNotNone(coverage)
        assert coverage is not None
        self.assertEqual(coverage["diff_exposed"], 1)
        self.assertEqual(coverage["context_ranges_read"], 1)
        self.assertIsNotNone(run)
        assert run is not None
        self.assertEqual(run["phase"], "reviewing")


if __name__ == "__main__":
    unittest.main()
