from __future__ import annotations

import sys
import unittest
from pathlib import Path

PLUGINS = Path(__file__).resolve().parents[1] / "bootstrap" / "plugins"
sys.path.insert(0, str(PLUGINS))

from review_agent_tools import failure_codes  # noqa: E402


class FailureCodesTests(unittest.TestCase):
    def test_canonical_values_are_stable(self):
        self.assertEqual(failure_codes.REVIEW_FAILED, "review_failed")
        self.assertEqual(failure_codes.STALE_TIMEOUT, "stale_timeout")
        self.assertEqual(failure_codes.SNAPSHOT_SUPERSEDED, "snapshot_superseded")
        self.assertEqual(failure_codes.REVIEW_DELIVER_ERROR, "review_deliver_error")
        self.assertEqual(
            failure_codes.UNEXPECTED_REVIEW_DELIVER_FAILURE,
            "unexpected_review_deliver_failure",
        )
        self.assertEqual(failure_codes.GITHUB_DIFF_UNAVAILABLE, "github_diff_406")
        self.assertEqual(failure_codes.JOB_RETRY_EXHAUSTED, "job_retry_exhausted")
        self.assertEqual(failure_codes.JOB_EXECUTION_FAILED, "job_execution_failed")
        self.assertEqual(
            failure_codes.REVIEW_CONTRACT_CHANGED,
            "review_contract_changed",
        )
        self.assertEqual(
            failure_codes.PUBLICATION_ATTEMPTS_EXHAUSTED,
            "publication_attempts_exhausted",
        )
        self.assertEqual(failure_codes.JOB_LEASE_EXPIRED, "job_lease_expired")
        self.assertEqual(
            failure_codes.JOB_RETRYABLE_EXECUTION,
            "job_retryable_execution",
        )
        self.assertEqual(
            failure_codes.JOB_TERMINAL_EXECUTION,
            "job_terminal_execution",
        )
        self.assertEqual(failure_codes.JOB_RUN_FAILED, "job_run_failed")

    def test_all_enumerates_every_code_without_duplicates(self):
        codes = [
            failure_codes.REVIEW_FAILED,
            failure_codes.STALE_TIMEOUT,
            failure_codes.SNAPSHOT_SUPERSEDED,
            failure_codes.SUPERSEDED_DUPLICATE_MIGRATION,
            failure_codes.REVIEW_DELIVER_ERROR,
            failure_codes.UNEXPECTED_REVIEW_DELIVER_FAILURE,
            failure_codes.GITHUB_DIFF_UNAVAILABLE,
            failure_codes.JOB_RETRY_EXHAUSTED,
            failure_codes.JOB_EXECUTION_FAILED,
            failure_codes.PUBLICATION_ATTEMPTS_EXHAUSTED,
            failure_codes.OPERATOR_CANCELLED,
            failure_codes.REVIEW_CONTRACT_CHANGED,
        ]
        self.assertEqual(failure_codes.ALL, frozenset(codes))
        self.assertEqual(len(failure_codes.ALL), len(codes))

    def test_job_all_is_separate_from_run_codes(self):
        job_codes = {
            failure_codes.JOB_LEASE_EXPIRED,
            failure_codes.JOB_RETRYABLE_EXECUTION,
            failure_codes.JOB_TERMINAL_EXECUTION,
            failure_codes.JOB_RUN_FAILED,
        }

        self.assertEqual(failure_codes.JOB_ALL, frozenset(job_codes))
        self.assertTrue(failure_codes.ALL.isdisjoint(failure_codes.JOB_ALL))


if __name__ == "__main__":
    unittest.main()
