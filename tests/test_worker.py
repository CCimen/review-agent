from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from typing import Any, cast
from unittest.mock import Mock, patch


PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "bootstrap" / "plugins"
sys.path.insert(0, str(PACKAGE_ROOT))

from review_agent_tools import review_run_application  # noqa: E402
from review_agent_tools.domain.review import ReviewRunId  # noqa: E402
from review_agent_tools.postgres import jobs  # noqa: E402
from review_agent_tools.postgres.runtime import (  # noqa: E402
    PostgreSQLRuntime,
    PostgreSQLUnavailable,
)
from review_agent_tools.worker import (  # noqa: E402
    ClaimedReview,
    HermesChatClient,
    HermesRequestError,
    HermesChatSettings,
    ReviewWorker,
    WorkerPolicy,
)


class _ChatHandler(BaseHTTPRequestHandler):
    requests: list[tuple[dict[str, str], dict[str, object]]] = []
    response_status = 200

    def do_POST(self) -> None:
        length = int(self.headers["Content-Length"])
        body = json.loads(self.rfile.read(length))
        self.requests.append((dict(self.headers), body))
        response = b'{"choices":[{"message":{"role":"assistant","content":"done"}}]}'
        self.send_response(self.response_status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, format: str, *args: object) -> None:
        del format, args


class WorkerBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        _ChatHandler.requests = []
        _ChatHandler.response_status = 200
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _ChatHandler)
        self.server_thread = threading.Thread(target=self.server.serve_forever)
        self.server_thread.start()
        self.addCleanup(self._stop_server)

    def _stop_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join()

    def test_reclaim_changes_identity_while_each_generation_is_stable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            skill_path = Path(directory) / "SKILL.md"
            skill_path.write_text(
                "---\nname: review-agent-pr\n---\nFollow the review procedure.\n",
                encoding="utf-8",
            )
            client = HermesChatClient(
                HermesChatSettings(
                    endpoint=(
                        f"http://127.0.0.1:{self.server.server_port}"
                        "/v1/chat/completions"
                    ),
                    bearer_token="test-token",
                    skill_path=skill_path,
                )
            )
            first = self._claim(generation=1)
            second = self._claim(generation=2)
            client.review(first, timeout=self._timeout())
            client.review(first, timeout=self._timeout())
            client.review(second, timeout=self._timeout())

        self.assertEqual(len(_ChatHandler.requests), 3)
        first_headers, first_body = _ChatHandler.requests[0]
        retry_headers, _ = _ChatHandler.requests[1]
        second_headers, second_body = _ChatHandler.requests[2]
        self.assertEqual(
            first_headers["Idempotency-Key"],
            "review-agent-job-17-lease-1",
        )
        self.assertEqual(
            retry_headers["Idempotency-Key"],
            first_headers["Idempotency-Key"],
        )
        self.assertEqual(
            second_headers["Idempotency-Key"],
            "review-agent-job-17-lease-2",
        )
        self.assertEqual(
            first_headers["X-Hermes-Session-Id"],
            "review-agent-job-17-lease-1",
        )
        self.assertEqual(
            second_headers["X-Hermes-Session-Id"],
            "review-agent-job-17-lease-2",
        )
        self.assertEqual(
            second_headers["Idempotency-Key"],
            second_headers["X-Hermes-Session-Id"],
        )
        self.assertNotIn("model", first_body)
        self.assertEqual(second_body["stream"], False)
        messages = cast(list[dict[str, Any]], second_body["messages"])
        self.assertEqual(messages[0]["content"], "Follow the review procedure.\n")
        self.assertIn("existing_run_id 23", messages[1]["content"])

    def test_http_status_classifies_retryable_and_terminal_failures(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            skill_path = Path(directory) / "SKILL.md"
            skill_path.write_text("Review safely.\n", encoding="utf-8")
            client = HermesChatClient(
                HermesChatSettings(
                    endpoint=(
                        f"http://127.0.0.1:{self.server.server_port}"
                        "/v1/chat/completions"
                    ),
                    bearer_token="test-token",
                    skill_path=skill_path,
                )
            )
            for status, retryable in ((429, True), (400, False)):
                with self.subTest(status=status):
                    _ChatHandler.response_status = status
                    with self.assertRaises(HermesRequestError) as caught:
                        client.review(
                            self._claim(generation=1), timeout=self._timeout()
                        )
                    self.assertEqual(caught.exception.retryable, retryable)

    def test_worker_retries_after_a_transient_claim_failure(self) -> None:
        stop = threading.Event()
        runtime = Mock(spec=PostgreSQLRuntime)
        client = Mock(spec=HermesChatClient)
        worker = ReviewWorker(
            runtime,
            client,
            self._policy(),
            lease_owner="worker-test",
            stop_event=stop,
        )
        attempts = 0

        def claim() -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise PostgreSQLUnavailable("temporary outage")
            stop.set()
            return None

        with (
            patch.object(worker, "_claim", side_effect=claim),
            self.assertLogs("review_agent_tools.worker", level="WARNING") as logged,
        ):
            worker.run()

        self.assertEqual(attempts, 2)
        self.assertIn("claim deferred", "\n".join(logged.output))
        client.review.assert_not_called()

    def test_worker_does_not_guess_outcome_after_transient_state_read(self) -> None:
        stop = threading.Event()
        runtime = Mock(spec=PostgreSQLRuntime)
        runtime.transaction.side_effect = PostgreSQLUnavailable("temporary outage")
        client = Mock(spec=HermesChatClient)
        worker = ReviewWorker(
            runtime,
            client,
            self._policy(),
            lease_owner="worker-test",
            stop_event=stop,
        )
        claimed = self._claim(generation=1)
        attempts = 0

        def claim() -> ClaimedReview | None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return claimed
            stop.set()
            return None

        with (
            patch.object(worker, "_claim", side_effect=claim),
            patch.object(
                review_run_application,
                "fail_claimed_job_in_transaction",
            ) as fail_job,
            self.assertLogs("review_agent_tools.worker", level="WARNING"),
        ):
            worker.run()

        self.assertEqual(attempts, 2)
        client.review.assert_called_once_with(
            claimed, timeout=self._policy().request_timeout
        )
        fail_job.assert_not_called()

    def test_heartbeat_survives_transient_database_failure(self) -> None:
        runtime = Mock(spec=PostgreSQLRuntime)
        runtime.transaction.return_value = nullcontext(Mock())
        worker = ReviewWorker(
            runtime,
            Mock(spec=HermesChatClient),
            WorkerPolicy(
                lease_duration=timedelta(seconds=1),
                heartbeat_interval=timedelta(milliseconds=1),
                retry_delay=timedelta(seconds=1),
                poll_interval=timedelta(milliseconds=1),
                request_timeout=timedelta(seconds=2),
                recovery_interval=timedelta(seconds=30),
                recovery_batch_size=10,
                priority_aging_interval=timedelta(minutes=15),
            ),
            lease_owner="worker-test",
            stop_event=threading.Event(),
        )
        job = self._claim(generation=1).job
        lease_lost = threading.Event()

        with (
            patch.object(
                jobs,
                "heartbeat_job",
                side_effect=(
                    PostgreSQLUnavailable("temporary outage"),
                    jobs.ReviewJobLeaseLost(job),
                ),
            ) as heartbeat,
            self.assertLogs("review_agent_tools.worker", level="WARNING"),
        ):
            worker._heartbeat(job, threading.Event(), lease_lost)

        self.assertTrue(lease_lost.is_set())
        self.assertEqual(heartbeat.call_count, 2)

    @staticmethod
    def _policy() -> WorkerPolicy:
        return WorkerPolicy(
            lease_duration=timedelta(seconds=5),
            heartbeat_interval=timedelta(seconds=1),
            retry_delay=timedelta(seconds=1),
            poll_interval=timedelta(milliseconds=1),
            request_timeout=timedelta(seconds=2),
            recovery_interval=timedelta(seconds=30),
            recovery_batch_size=10,
            priority_aging_interval=timedelta(minutes=15),
        )

    @staticmethod
    def _timeout() -> timedelta:
        return timedelta(seconds=2)

    @staticmethod
    def _claim(*, generation: int) -> ClaimedReview:
        now = datetime.now(timezone.utc)
        return ClaimedReview(
            job=jobs.ReviewJob(
                id=17,
                review_run_id=ReviewRunId(23),
                status=jobs.ReviewJobStatus.LEASED,
                priority=0,
                available_at=now,
                attempt_count=generation,
                max_attempts=3,
                lease_owner="worker",
                lease_generation=generation,
                lease_expires_at=now,
                last_heartbeat_at=now,
                failure_code=None,
                created_at=now,
                started_at=now,
                completed_at=None,
            ),
            repository="example/repository",
            pr_number=5,
        )


if __name__ == "__main__":
    unittest.main()
