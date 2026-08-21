from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import review_status  # noqa: E402


class HealthResponse:
    status = 200

    def __enter__(self) -> HealthResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        del size
        return b'{"status":"ok"}'


class ReviewStatusTests(unittest.TestCase):
    def test_successful_workflow_run_is_not_reported_as_dispatched(self) -> None:
        workflow_runs = {
            "workflow_runs": [
                {
                    "conclusion": "success",
                    "created_at": "2026-08-21T06:00:00Z",
                    "display_title": "Review request",
                    "triggering_actor": {"login": "alice"},
                }
            ]
        }
        completed = subprocess.CompletedProcess(
            args=["gh"],
            returncode=0,
            stdout=json.dumps(workflow_runs),
            stderr="",
        )
        stdout = io.StringIO()

        with (
            patch.object(review_status.urllib.request, "urlopen", return_value=HealthResponse()),
            patch.object(review_status.subprocess, "run", return_value=completed),
            redirect_stdout(stdout),
        ):
            result = review_status.main(
                [
                    "--repo",
                    "example-org/example-repository",
                    "--health-url",
                    "https://review.example.org/health",
                ]
            )

        output = stdout.getvalue()
        self.assertEqual(result, 0)
        self.assertIn("1 succeeded, 0 skipped", output)
        self.assertIn("WORKFLOW SUCCEEDED", output)
        self.assertNotIn("REVIEW DISPATCHED", output)
        self.assertNotIn("dispatched a review", output)

    def test_github_query_timeout_is_bounded_and_actionable(self) -> None:
        stdout = io.StringIO()

        def timeout_run(
            *args: object,
            timeout: int,
            **kwargs: object,
        ) -> subprocess.CompletedProcess[str]:
            del args, kwargs
            self.assertEqual(timeout, review_status.GITHUB_QUERY_TIMEOUT_SECONDS)
            raise subprocess.TimeoutExpired(cmd=["gh"], timeout=timeout)

        with (
            patch.object(review_status.urllib.request, "urlopen", return_value=HealthResponse()),
            patch.object(
                review_status.subprocess,
                "run",
                side_effect=timeout_run,
            ),
            redirect_stdout(stdout),
        ):
            result = review_status.main(
                [
                    "--repo",
                    "example-org/example-repository",
                    "--health-url",
                    "https://review.example.org/health",
                ]
            )

        self.assertEqual(result, 1)
        self.assertIn(
            "GitHub query timed out after 15 seconds",
            stdout.getvalue(),
        )


if __name__ == "__main__":
    unittest.main()
