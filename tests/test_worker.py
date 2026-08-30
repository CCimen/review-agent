from __future__ import annotations

from contextlib import nullcontext
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from http.client import HTTPMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import io
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import urllib.request
from typing import Any, cast
from unittest.mock import Mock, patch


PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "bootstrap" / "plugins"
sys.path.insert(0, str(PACKAGE_ROOT))

from review_agent_tools import review_contract, review_run_application  # noqa: E402
from review_agent_tools.source_control import SameOriginHttpsRedirectHandler  # noqa: E402
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
    WorkerConfigurationError,
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
        contract = self._contract()
        contract_loader = patch.object(
            review_contract, "load_installed_contract", return_value=contract
        )
        contract_loader.start()
        self.addCleanup(contract_loader.stop)

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

    def test_hermes_credentials_reject_cross_origin_redirects(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            skill_path = Path(directory) / "SKILL.md"
            skill_path.write_text("Review safely.\n", encoding="utf-8")
            client = HermesChatClient(
                HermesChatSettings(
                    endpoint="http://hermes-review:8642/v1/chat/completions",
                    bearer_token="internal-secret",
                    skill_path=skill_path,
                )
            )

        self.assertTrue(
            any(
                type(handler) is SameOriginHttpsRedirectHandler
                for handler in client._opener.handlers
            )
        )
        handler = SameOriginHttpsRedirectHandler()
        request = urllib.request.Request(
            "http://hermes-review:8642/v1/chat/completions",
            headers={"Authorization": "Bearer internal-secret"},
        )
        self.assertIsNone(
            handler.redirect_request(
                request,
                io.BytesIO(),
                307,
                "temporary redirect",
                HTTPMessage(),
                "http://other-service:8642/v1/chat/completions",
            )
        )

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
            patch.object(worker, "_recover_if_due"),
            patch.object(worker, "_claim", side_effect=claim),
            self.assertLogs("review_agent_tools.worker", level="WARNING") as logged,
        ):
            worker.run()

        self.assertEqual(attempts, 2)
        self.assertIn("claim deferred", "\n".join(logged.output))
        client.review.assert_not_called()

    def test_worker_claims_only_for_available_execution_slots(self) -> None:
        stop = threading.Event()
        runtime = Mock(spec=PostgreSQLRuntime)
        client = Mock(spec=HermesChatClient)
        worker = ReviewWorker(
            runtime,
            client,
            replace(self._policy(), concurrency=2),
            lease_owner="worker-test",
            stop_event=stop,
        )
        claims = [
            replace(
                self._claim(generation=1),
                job=replace(
                    self._claim(generation=1).job,
                    id=job_id,
                    review_run_id=ReviewRunId(job_id + 100),
                ),
            )
            for job_id in range(1, 5)
        ]
        two_started = threading.Event()
        recovery_while_full = threading.Event()
        release = threading.Event()
        lock = threading.Lock()
        active = 0
        maximum_active = 0
        completed: list[int] = []
        errors: list[BaseException] = []

        def claim() -> ClaimedReview | None:
            if claims:
                return claims.pop(0)
            stop.set()
            return None

        def execute(claimed: ClaimedReview) -> None:
            nonlocal active, maximum_active
            with lock:
                active += 1
                maximum_active = max(maximum_active, active)
                if active == 2:
                    two_started.set()
            release.wait()
            with lock:
                completed.append(claimed.job.id)
                active -= 1

        def run_worker() -> None:
            try:
                worker.run()
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        def recover() -> None:
            if two_started.is_set():
                recovery_while_full.set()

        with (
            patch.object(worker, "_recover_if_due", side_effect=recover),
            patch.object(worker, "_claim", side_effect=claim) as claim_job,
            patch.object(worker, "_execute", side_effect=execute),
        ):
            runner = threading.Thread(target=run_worker)
            runner.start()
            self.assertTrue(two_started.wait(timeout=2))
            self.assertTrue(recovery_while_full.wait(timeout=2))
            self.assertEqual(claim_job.call_count, 2)
            release.set()
            runner.join(timeout=2)

        self.assertFalse(runner.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(maximum_active, 2)
        self.assertCountEqual(completed, [1, 2, 3, 4])

    def test_worker_concurrency_must_be_positive(self) -> None:
        with self.assertRaisesRegex(
            WorkerConfigurationError, "concurrency must be positive"
        ):
            replace(self._policy(), concurrency=0)

    def test_unexpected_execution_error_stops_and_fails_after_active_drain(
        self,
    ) -> None:
        stop = threading.Event()
        worker = ReviewWorker(
            Mock(spec=PostgreSQLRuntime),
            Mock(spec=HermesChatClient),
            replace(self._policy(), concurrency=2),
            lease_owner="worker-test",
            stop_event=stop,
        )
        claims = [
            replace(
                self._claim(generation=1),
                job=replace(self._claim(generation=1).job, id=job_id),
            )
            for job_id in (1, 2)
        ]
        second_started = threading.Event()
        release = threading.Event()
        errors: list[BaseException] = []

        def claim() -> ClaimedReview | None:
            return claims.pop(0) if claims else None

        def execute(claimed: ClaimedReview) -> None:
            if claimed.job.id == 1:
                second_started.wait(timeout=2)
                raise RuntimeError("unexpected probe")
            second_started.set()
            release.wait()

        def run_worker() -> None:
            try:
                worker.run()
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        with (
            patch.object(worker, "_recover_if_due"),
            patch.object(worker, "_claim", side_effect=claim) as claim_job,
            patch.object(worker, "_execute", side_effect=execute),
            self.assertLogs("review_agent_tools.worker", level="ERROR") as logged,
        ):
            runner = threading.Thread(target=run_worker)
            runner.start()
            stopped_before_drain = stop.wait(timeout=2)
            still_draining = runner.is_alive()
            release.set()
            runner.join(timeout=2)

        self.assertTrue(stopped_before_drain)
        self.assertTrue(still_draining)
        self.assertFalse(runner.is_alive())
        self.assertEqual(claim_job.call_count, 2)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], RuntimeError)
        self.assertIn("Review job 1 failed unexpectedly", "\n".join(logged.output))

    def test_stop_keeps_a_blocked_hermes_review_leased_until_it_returns(
        self,
    ) -> None:
        stop = threading.Event()
        runtime = Mock(spec=PostgreSQLRuntime)
        runtime.transaction.return_value = nullcontext(Mock())
        client = Mock(spec=HermesChatClient)
        worker = ReviewWorker(
            runtime,
            client,
            replace(
                self._policy(),
                heartbeat_interval=timedelta(milliseconds=10),
                lease_duration=timedelta(seconds=1),
            ),
            lease_owner="worker-test",
            stop_event=stop,
        )
        claimed = self._claim(generation=1)
        started = threading.Event()
        release = threading.Event()
        heartbeat_after_stop = threading.Event()
        errors: list[BaseException] = []

        def review(*_args: object, **_kwargs: object) -> None:
            started.set()
            release.wait()
            raise HermesRequestError("termination probe", retryable=True)

        def heartbeat(*_args: object, **_kwargs: object) -> jobs.ReviewJob:
            if stop.is_set():
                heartbeat_after_stop.set()
            return claimed.job

        client.review.side_effect = review

        def run_worker() -> None:
            try:
                worker.run()
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        with (
            patch.object(worker, "_recover_if_due"),
            patch.object(worker, "_claim", return_value=claimed) as claim_job,
            patch.object(jobs, "heartbeat_job", side_effect=heartbeat),
            patch.object(jobs, "get_job", return_value=claimed.job),
            patch.object(
                review_run_application,
                "fail_claimed_job_in_transaction",
                return_value=Mock(
                    job=Mock(status=jobs.ReviewJobStatus.QUEUED),
                ),
            ) as fail_job,
            self.assertLogs("review_agent_tools.worker", level="INFO") as logged,
        ):
            runner = threading.Thread(target=run_worker)
            runner.start()
            try:
                self.assertTrue(started.wait(timeout=2))
                stop.set()
                self.assertTrue(heartbeat_after_stop.wait(timeout=2))
                self.assertTrue(runner.is_alive())
                fail_job.assert_not_called()
            finally:
                release.set()
                runner.join(timeout=2)

        self.assertFalse(runner.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(claim_job.call_count, 1)
        fail_job.assert_called_once()
        self.assertIn("draining 1 active review", "\n".join(logged.output))

    def test_stop_before_activation_requeues_without_starting_hermes(self) -> None:
        for once in (False, True):
            with self.subTest(once=once):
                stop = threading.Event()
                connection = Mock()
                runtime = Mock(spec=PostgreSQLRuntime)
                runtime.transaction.return_value = nullcontext(connection)
                client = Mock(spec=HermesChatClient)
                worker = ReviewWorker(
                    runtime,
                    client,
                    self._policy(),
                    lease_owner="worker-test",
                    stop_event=stop,
                )
                claimed = replace(
                    self._claim(generation=1),
                    job=replace(
                        self._claim(generation=1).job,
                        attempt_count=1,
                        max_attempts=1,
                    ),
                )
                execute = worker._execute

                def stop_before_activation(item: ClaimedReview) -> None:
                    stop.set()
                    execute(item)

                with (
                    patch.object(worker, "_recover_if_due"),
                    patch.object(worker, "_claim", return_value=claimed),
                    patch.object(
                        worker,
                        "_execute",
                        side_effect=stop_before_activation,
                    ),
                    patch.object(jobs, "requeue_unstarted_job") as requeue,
                    self.assertLogs(
                        "review_agent_tools.worker", level="INFO"
                    ) as logged,
                ):
                    worker.run(once=once)

                client.review.assert_not_called()
                requeue.assert_called_once_with(
                    connection,
                    job_id=claimed.job.id,
                    lease_owner="worker-test",
                    lease_generation=claimed.job.lease_generation,
                )
                self.assertIn(
                    "returned review job 17 to the queue without consuming an attempt",
                    "\n".join(logged.output),
                )

    def test_once_does_not_claim_when_already_stopped(self) -> None:
        stop = threading.Event()
        stop.set()
        worker = ReviewWorker(
            Mock(spec=PostgreSQLRuntime),
            Mock(spec=HermesChatClient),
            self._policy(),
            lease_owner="worker-test",
            stop_event=stop,
        )

        with patch.object(worker, "_claim") as claim:
            worker.run(once=True)

        claim.assert_not_called()

    def test_once_executes_only_one_review_at_higher_concurrency(self) -> None:
        worker = ReviewWorker(
            Mock(spec=PostgreSQLRuntime),
            Mock(spec=HermesChatClient),
            replace(self._policy(), concurrency=4),
            lease_owner="worker-test",
            stop_event=threading.Event(),
        )
        claimed = self._claim(generation=1)

        with (
            patch.object(worker, "_recover_if_due"),
            patch.object(worker, "_claim", return_value=claimed) as claim_job,
            patch.object(worker, "_execute") as execute,
        ):
            worker.run(once=True)

        claim_job.assert_called_once_with()
        execute.assert_called_once_with(claimed)

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
            patch.object(worker, "_recover_if_due"),
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

    def test_worker_rejects_a_changed_contract_before_hermes(self) -> None:
        runtime = Mock(spec=PostgreSQLRuntime)
        runtime.transaction.return_value = nullcontext(Mock())
        client = Mock(spec=HermesChatClient)
        worker = ReviewWorker(
            runtime,
            client,
            self._policy(),
            lease_owner="worker-test",
            stop_event=threading.Event(),
        )
        claimed = self._claim(generation=1)
        claimed = ClaimedReview(
            job=claimed.job,
            repository=claimed.repository,
            pr_number=claimed.pr_number,
            resolved_config={"profile": "changed"},
        )

        with patch.object(
            review_run_application,
            "fail_claimed_job_in_transaction",
        ) as fail_job:
            worker._execute(claimed)

        client.review.assert_not_called()
        self.assertEqual(
            fail_job.call_args.kwargs["run_failure_code"],
            "review_contract_changed",
        )
        self.assertFalse(fail_job.call_args.kwargs["retryable"])

    def test_worker_stops_without_terminalizing_when_contract_cannot_be_read(self) -> None:
        runtime = Mock(spec=PostgreSQLRuntime)
        runtime.transaction.return_value = nullcontext(Mock())
        client = Mock(spec=HermesChatClient)
        stop = threading.Event()
        worker = ReviewWorker(
            runtime,
            client,
            self._policy(),
            lease_owner="worker-test",
            stop_event=stop,
        )

        with (
            patch.object(
                review_contract,
                "load_installed_contract",
                side_effect=review_contract.ReviewContractError("receipt unreadable"),
            ),
            patch.object(
                review_run_application,
                "fail_claimed_job_in_transaction",
            ) as fail_job,
        ):
            worker._execute(self._claim(generation=1))

        self.assertTrue(stop.is_set())
        client.review.assert_not_called()
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
            resolved_config=review_contract.resolved_config(
                WorkerBoundaryTests._contract()
            ),
        )

    @staticmethod
    def _contract() -> review_contract.ReviewContract:
        return review_contract.ReviewContract(
            profile="sundsvall-standard",
            hermes_image="hermes@test",
            model_provider="openai-codex",
            model="gpt-test",
            reasoning_effort="high",
            plugin_result_max_chars=160_000,
            profile_bundle_sha256="1" * 64,
            managed_config_sha256="2" * 64,
            engine_bundle_sha256="3" * 64,
            sha256="4" * 64,
        )


if __name__ == "__main__":
    unittest.main()
