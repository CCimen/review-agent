from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGINS = ROOT / "bootstrap" / "plugins"
sys.path.insert(0, str(PLUGINS))

from review_agent_tools import failure_codes  # noqa: E402


class FailureCodesTests(unittest.TestCase):
    def test_every_named_code_is_enumerated_once(self):
        codes = [
            value
            for name, value in vars(failure_codes).items()
            if name.isupper() and isinstance(value, str)
        ]
        self.assertEqual(len(codes), len(set(codes)))
        self.assertEqual(failure_codes.ALL | failure_codes.JOB_ALL, frozenset(codes))

    def test_console_explains_every_run_failure_code(self):
        # The console showed raw tokens for most real failures while carrying
        # sentences for codes nothing writes.
        source = (ROOT / "admin" / "src" / "ui.tsx").read_text(encoding="utf-8")
        block = source.split("const failureSentences", 1)[1].split("};", 1)[0]
        explained = set(re.findall(r"^  ([a-z0-9_]+):", block, flags=re.M))
        self.assertEqual(set(failure_codes.ALL), explained)

    def test_failure_detail_is_one_bounded_line(self):
        self.assertEqual(
            failure_codes.failure_detail("API call failed\n\x00after  3 retries\t"),
            "API call failed after 3 retries",
        )
        self.assertEqual(len(failure_codes.failure_detail("x" * 400) or ""), 300)
        self.assertIsNone(failure_codes.failure_detail(" \n "))
        self.assertIsNone(failure_codes.failure_detail(None))

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
