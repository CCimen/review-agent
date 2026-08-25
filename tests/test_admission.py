from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import hmac
import http.client
import json
from pathlib import Path
import sys
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bootstrap" / "plugins"))
sys.path.insert(0, str(ROOT / "tools"))

import review_agent_admission as admission_entrypoint  # noqa: E402

from review_agent_tools import (  # noqa: E402
    admission,
    review_contract,
    review_run_application,
)
from review_agent_tools.domain.review import (  # noqa: E402
    PullRequestId,
    ReviewPhase,
    ReviewRunId,
    ReviewStatus,
    ReviewSubjectId,
)
from review_agent_tools.postgres import jobs, review_runs  # noqa: E402
from review_agent_tools.postgres.runtime import PostgreSQLUnavailable  # noqa: E402
from review_agent_tools.settings import PostgresDatabaseUrl, SettingsError  # noqa: E402
from review_agent_tools.source_control import (  # noqa: E402
    GitHubReadClient,
    GitHubReadError,
)


class AdmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = admission.AdmissionConfig(
            secret="secret",
            token="read-token",
            allowed_repositories=frozenset({"example/repository"}),
            database_url=PostgresDatabaseUrl(
                "postgresql://review:secret@database/review"
            ),
            profile="sundsvall-standard",
            policy_revision="policy-v1",
            active_job_limit=25,
            job_max_attempts=4,
            job_priority=2,
        )
        self.github = Mock(spec=GitHubReadClient)
        self.github.request_json.return_value = {
            "number": 42,
            "state": "open",
            "base": {
                "sha": "b" * 40,
                "repo": {"id": 9001, "full_name": "Example/Repository"},
            },
            "head": {"sha": "a" * 40},
        }
        self.runtime = Mock()
        self.contract = review_contract.ReviewContract(
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

    @staticmethod
    def payload() -> dict[str, object]:
        return {
            "repository": {"full_name": "example/repository"},
            "pull_request": {"number": 42},
            "requester": {"login": "maintainer", "association": "MEMBER"},
            "request": {"comment_id": 7001},
        }

    @staticmethod
    def admitted() -> review_run_application.AdmittedReview:
        now = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)
        run = review_runs.ReviewRun(
            id=ReviewRunId(17),
            pull_request_id=PullRequestId(11),
            review_subject_id=ReviewSubjectId(13),
            request_key="github:issue-comment:7001",
            trigger_comment_id=7001,
            trigger_user="maintainer",
            status=ReviewStatus.RUNNING,
            phase=ReviewPhase.ACCEPTED,
            findings_count=None,
            failure_code=None,
            started_at=now,
            last_heartbeat_at=now,
            completed_at=None,
        )
        job = Mock(spec=jobs.ReviewJob)
        job.id = 19
        return review_run_application.AdmittedReview(
            run=review_runs.StartedRun(run=run),
            job=jobs.EnqueuedJob(job=job),
        )

    def test_signed_payload_admits_the_exact_github_snapshot(self) -> None:
        with patch.object(
            admission, "admit_postgres_review", return_value=self.admitted()
        ) as admit, patch.object(
            admission.review_contract,
            "load_packaged_contract",
            return_value=self.contract,
        ):
            response = admission.admit_review(
                payload=self.payload(),
                delivery_id="7001",
                config=self.config,
                github=self.github,
                runtime=self.runtime,
            )

        self.assertEqual(response.status, "accepted")
        self.assertEqual((response.run_id, response.job_id), (17, 19))
        request = admit.call_args.args[1]
        self.assertEqual(request.repository, "Example/Repository")
        self.assertEqual(request.provider_repository_id, 9001)
        self.assertEqual(request.request_key, "github:issue-comment:7001")
        self.assertEqual(request.base_sha, "b" * 40)
        self.assertEqual(request.head_sha, "a" * 40)
        self.assertEqual(request.resolved_config_schema_version, 2)
        self.assertEqual(
            request.resolved_config["review_contract"], self.contract.to_json()
        )
        self.assertEqual(admit.call_args.kwargs["active_job_limit"], 25)

    def test_readiness_fails_when_configured_profile_is_not_packaged(self) -> None:
        self.runtime.database_url = self.config.database_url
        with (
            patch.object(
                admission.review_contract,
                "load_packaged_contract",
                return_value=replace(self.contract, profile="other-profile"),
            ),
            self.assertRaisesRegex(admission.AdmissionError, "configured profile"),
        ):
            admission.ready_check(self.config, self.runtime)

    def test_delivery_id_and_comment_id_must_match(self) -> None:
        with patch.object(admission, "admit_postgres_review") as admit:
            with self.assertRaisesRegex(admission.AdmissionError, "X-GitHub-Delivery"):
                admission.admit_review(
                    payload=self.payload(),
                    delivery_id="different",
                    config=self.config,
                    github=self.github,
                    runtime=self.runtime,
                )
        admit.assert_not_called()
        self.github.request_json.assert_not_called()

    def test_malformed_github_response_is_an_upstream_failure(self) -> None:
        self.github.request_json.return_value = {"number": 42, "base": []}

        with self.assertRaises(GitHubReadError) as raised:
            admission.admit_review(
                payload=self.payload(),
                delivery_id="7001",
                config=self.config,
                github=self.github,
                runtime=self.runtime,
            )

        self.assertEqual(raised.exception.kind, "invalid_json")

    def test_closed_pull_request_is_not_admitted(self) -> None:
        self.github.request_json.return_value["state"] = "closed"

        with patch.object(admission, "admit_postgres_review") as admit:
            with self.assertRaisesRegex(admission.AdmissionError, "not open"):
                admission.admit_review(
                    payload=self.payload(),
                    delivery_id="7001",
                    config=self.config,
                    github=self.github,
                    runtime=self.runtime,
                )

        admit.assert_not_called()

    def test_untrusted_requester_and_repository_fail_closed(self) -> None:
        payload = self.payload()
        requester = payload["requester"]
        assert isinstance(requester, dict)
        requester["association"] = "CONTRIBUTOR"
        with self.assertRaises(admission.UnauthorizedAdmission):
            admission.admit_review(
                payload=payload,
                delivery_id="7001",
                config=self.config,
                github=self.github,
                runtime=self.runtime,
            )

        payload = self.payload()
        repository = payload["repository"]
        assert isinstance(repository, dict)
        repository["full_name"] = "other/repository"
        with self.assertRaises(admission.UnauthorizedAdmission):
            admission.admit_review(
                payload=payload,
                delivery_id="7001",
                config=self.config,
                github=self.github,
                runtime=self.runtime,
            )

    def test_configuration_exposes_capacity_without_accepting_an_empty_scope(
        self,
    ) -> None:
        environment = {
            "REVIEW_AGENT_WEBHOOK_SECRET": "secret",
            "GITHUB_READ_TOKEN": "read-token",
            "REVIEW_AGENT_ALLOWED_REPOSITORIES": "Example/Repository",
            "REVIEW_AGENT_DATABASE_URL": "postgresql://review:secret@database/review",
            "REVIEW_AGENT_ACTIVE_JOB_LIMIT": "250",
        }
        configured = admission.load_config(environment)
        self.assertEqual(configured.active_job_limit, 250)
        self.assertEqual(
            configured.allowed_repositories, frozenset({"example/repository"})
        )

        environment["REVIEW_AGENT_ALLOWED_REPOSITORIES"] = ""
        with self.assertRaisesRegex(SettingsError, "deny by default"):
            admission.load_config(environment)


class AdmissionHttpBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.secret = "admission-test-secret"
        config = admission.AdmissionConfig(
            secret=self.secret,
            token="read-token",
            allowed_repositories=frozenset({"example/repository"}),
            database_url=PostgresDatabaseUrl(
                "postgresql://review:secret@database/review"
            ),
            profile="sundsvall-standard",
            policy_revision="policy-v1",
            active_job_limit=25,
            job_max_attempts=4,
            job_priority=2,
        )
        self.server = admission_entrypoint.AdmissionServer(
            ("127.0.0.1", 0),
            config=config,
            github=Mock(),
            runtime=Mock(),
            max_body_bytes=128,
            max_concurrent_requests=2,
            request_timeout_seconds=2,
        )
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.addCleanup(self._stop_server)

    def _stop_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def _post(
        self, body: bytes = b"{}", *, valid_signature: bool = True
    ) -> tuple[int, dict[str, str], bytes]:
        signature = "sha256=invalid"
        if valid_signature:
            signature = (
                "sha256="
                + hmac.new(
                    self.secret.encode("utf-8"), body, hashlib.sha256
                ).hexdigest()
            )
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.server.server_port}{admission.DEFAULT_PATH}",
            data=body,
            method="POST",
            headers={
                "X-GitHub-Delivery": "7001",
                "X-GitHub-Event": "issue_comment",
                "X-Hub-Signature-256": signature,
            },
        )
        try:
            response = urllib.request.urlopen(request, timeout=2)
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers.items()), exc.read()
        with response:
            return response.status, dict(response.headers.items()), response.read()

    def test_signature_and_request_size_fail_before_admission(self) -> None:
        with patch.object(admission_entrypoint.admission, "admit_review") as admit:
            status, _, body = self._post(valid_signature=False)
            self.assertEqual(
                (status, json.loads(body)), (401, {"status": "bad_signature"})
            )

            status, _, body = self._post(b"x" * 129)
            self.assertEqual(
                (status, json.loads(body)),
                (413, {"status": "payload_too_large"}),
            )
        admit.assert_not_called()

    def test_missing_content_length_is_rejected(self) -> None:
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_port, timeout=2
        )
        connection.putrequest("POST", admission.DEFAULT_PATH)
        connection.putheader("X-GitHub-Event", "issue_comment")
        connection.endheaders()
        response = connection.getresponse()
        body = response.read()
        connection.close()

        self.assertEqual(
            (response.status, json.loads(body)), (411, {"status": "missing_length"})
        )

    def test_failures_use_fixed_status_and_retry_contracts(self) -> None:
        cases = (
            (
                jobs.ReviewQueueFull("queue secret"),
                429,
                "30",
                {"status": "queue_full"},
            ),
            (
                jobs.ReviewJobBusy("lock secret"),
                503,
                "5",
                {"status": "database_busy"},
            ),
            (
                PostgreSQLUnavailable("database secret"),
                503,
                None,
                {"status": "database_unavailable"},
            ),
            (RuntimeError("internal secret"), 500, None, {"status": "internal_error"}),
        )
        for failure, expected_status, retry_after, expected_body in cases:
            with self.subTest(failure=type(failure).__name__):
                with patch.object(
                    admission_entrypoint.admission,
                    "admit_review",
                    side_effect=failure,
                ):
                    status, headers, body = self._post()
                self.assertEqual(status, expected_status)
                self.assertEqual(headers.get("Retry-After"), retry_after)
                self.assertEqual(json.loads(body), expected_body)
                self.assertNotIn(b"secret", body)


if __name__ == "__main__":
    unittest.main()
