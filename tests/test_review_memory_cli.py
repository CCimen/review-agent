from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import psycopg


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
PLUGIN_PARENT = ROOT / "bootstrap" / "plugins"
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(PLUGIN_PARENT))

import review_agent_memory as memory  # noqa: E402
from review_agent_tools import operator_application  # noqa: E402
from review_agent_tools.postgres import quality_reporting, quality_triage  # noqa: E402
from review_agent_tools.postgres.runtime import (  # noqa: E402
    PostgreSQLNotReady,
    PostgreSQLUnavailable,
)


class ReviewAgentMemoryCliTests(unittest.TestCase):
    def test_nonpositive_limit_fails_in_parser_before_resources_are_opened(
        self,
    ) -> None:
        stderr = io.StringIO()
        with (
            patch.object(memory, "_runtime") as runtime,
            redirect_stderr(stderr),
            self.assertRaises(SystemExit) as raised,
        ):
            memory.main(["list", "--limit", "0"])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("usage:", stderr.getvalue())
        with self.assertRaises(json.JSONDecodeError):
            json.loads(stderr.getvalue())
        runtime.assert_not_called()

    def test_high_cost_limits_fail_before_resources_are_opened(self) -> None:
        environment = {
            "REVIEW_AGENT_OPERATOR_PAGE_MAX_ITEMS": "100",
            "REVIEW_AGENT_OPERATOR_EXPORT_MAX_ROWS": "10000",
        }
        commands = (
            ["list", "--limit", "101"],
            [
                "export",
                "--repo",
                "example/repository",
                "--row-limit",
                "10001",
            ],
        )

        for command in commands:
            with self.subTest(command=command):
                stderr = io.StringIO()
                with (
                    patch.dict(os.environ, environment, clear=True),
                    patch.object(memory, "_runtime") as runtime,
                    redirect_stderr(stderr),
                ):
                    exit_code = memory.main(command)

                self.assertEqual(exit_code, os.EX_USAGE)
                runtime.assert_not_called()
                self.assertEqual(
                    json.loads(stderr.getvalue()),
                    {"error": {"code": "invalid_command_input", "retryable": False}},
                )

    def test_database_failure_is_retryable_and_secret_safe(self) -> None:
        secret = "postgresql://operator:private-value@database/reviews"
        stderr = io.StringIO()
        with (
            patch.object(
                memory,
                "_runtime",
                side_effect=PostgreSQLUnavailable(secret),
            ),
            redirect_stderr(stderr),
        ):
            exit_code = memory.main(["list"])

        self.assertEqual(exit_code, os.EX_TEMPFAIL)
        self.assertNotIn(secret, stderr.getvalue())
        self.assertEqual(
            json.loads(stderr.getvalue()),
            {"error": {"code": "database_unavailable", "retryable": True}},
        )

    def test_database_not_ready_is_terminal_and_secret_safe(self) -> None:
        secret = "pending migration contained private-value"
        stderr = io.StringIO()
        with (
            patch.object(memory, "_runtime", side_effect=PostgreSQLNotReady(secret)),
            redirect_stderr(stderr),
        ):
            exit_code = memory.main(["list"])

        self.assertEqual(exit_code, os.EX_CONFIG)
        self.assertNotIn(secret, stderr.getvalue())
        self.assertEqual(
            json.loads(stderr.getvalue()),
            {"error": {"code": "database_not_ready", "retryable": False}},
        )

    def test_transient_database_contention_is_retryable(self) -> None:
        secret = "lock failure contained private-value"
        runtime = Mock()
        stderr = io.StringIO()
        with (
            patch.object(memory, "_runtime", return_value=runtime),
            patch.object(
                memory,
                "_run_live",
                side_effect=psycopg.errors.LockNotAvailable(secret),
            ),
            redirect_stderr(stderr),
        ):
            exit_code = memory.main(["list"])

        self.assertEqual(exit_code, os.EX_TEMPFAIL)
        self.assertNotIn(secret, stderr.getvalue())
        self.assertEqual(
            json.loads(stderr.getvalue()),
            {"error": {"code": "database_busy", "retryable": True}},
        )
        runtime.close.assert_called_once_with()

    def test_operator_rejection_is_terminal_and_secret_safe(self) -> None:
        secret = "operator input contained private-value"
        runtime = Mock()
        stderr = io.StringIO()
        with (
            patch.object(memory, "_runtime", return_value=runtime),
            patch.object(
                memory,
                "_run_live",
                side_effect=operator_application.OperatorInputError(secret),
            ),
            redirect_stderr(stderr),
        ):
            exit_code = memory.main(["list"])

        self.assertEqual(exit_code, os.EX_DATAERR)
        self.assertNotIn(secret, stderr.getvalue())
        self.assertEqual(
            json.loads(stderr.getvalue()),
            {"error": {"code": "command_rejected", "retryable": False}},
        )
        runtime.close.assert_called_once_with()

    def test_unexpected_failure_reports_type_without_message_and_closes(self) -> None:
        secret = "unexpected private-value"
        runtime = Mock()
        stderr = io.StringIO()
        with (
            patch.object(memory, "_runtime", return_value=runtime),
            patch.object(memory, "_run_live", side_effect=ValueError(secret)),
            redirect_stderr(stderr),
        ):
            exit_code = memory.main(["list"])

        self.assertEqual(exit_code, os.EX_SOFTWARE)
        self.assertNotIn(secret, stderr.getvalue())
        self.assertEqual(
            json.loads(stderr.getvalue()),
            {
                "error": {
                    "code": "internal_error",
                    "exception_type": "ValueError",
                    "retryable": False,
                }
            },
        )
        runtime.close.assert_called_once_with()

    def test_internal_store_failure_is_not_reported_as_operator_rejection(self) -> None:
        secret = "stored quality invariant contained private-value"
        runtime = Mock()
        stderr = io.StringIO()
        with (
            patch.object(memory, "_runtime", return_value=runtime),
            patch.object(
                memory,
                "_run_live",
                side_effect=quality_reporting.QualityReportingError(secret),
            ),
            redirect_stderr(stderr),
        ):
            exit_code = memory.main(["quality"])

        self.assertEqual(exit_code, os.EX_SOFTWARE)
        self.assertNotIn(secret, stderr.getvalue())
        self.assertEqual(
            json.loads(stderr.getvalue()),
            {
                "error": {
                    "code": "internal_error",
                    "exception_type": "QualityReportingError",
                    "retryable": False,
                }
            },
        )
        runtime.close.assert_called_once_with()

    def test_triage_input_rejections_are_reported_as_command_rejections(self) -> None:
        secret = "triage input contained private-value"
        command = [
            "triage-feedback",
            "1",
            "--status",
            "insufficient",
            "--actor",
            "github:operator",
            "--reason",
            "The evidence is insufficient.",
        ]
        error_types = (
            quality_triage.QualityFeedbackNotFound,
            quality_triage.QualityFeedbackNotTriageable,
        )
        for error_type in error_types:
            with self.subTest(error_type=error_type.__name__):
                runtime = Mock()
                stderr = io.StringIO()
                with (
                    patch.object(memory, "_runtime", return_value=runtime),
                    patch.object(
                        memory,
                        "_run_live",
                        side_effect=error_type(secret),
                    ),
                    redirect_stderr(stderr),
                ):
                    exit_code = memory.main(command)

                self.assertEqual(exit_code, os.EX_DATAERR)
                self.assertNotIn(secret, stderr.getvalue())
                self.assertEqual(
                    json.loads(stderr.getvalue()),
                    {"error": {"code": "command_rejected", "retryable": False}},
                )
                runtime.close.assert_called_once_with()

    def test_internal_triage_store_failure_remains_internal(self) -> None:
        secret = "triage store invariant contained private-value"
        command = [
            "triage-feedback",
            "1",
            "--status",
            "insufficient",
            "--actor",
            "github:operator",
            "--reason",
            "The evidence is insufficient.",
        ]
        runtime = Mock()
        stderr = io.StringIO()
        with (
            patch.object(memory, "_runtime", return_value=runtime),
            patch.object(
                memory,
                "_run_live",
                side_effect=quality_triage.QualityTriageStoreError(secret),
            ),
            redirect_stderr(stderr),
        ):
            exit_code = memory.main(command)

        self.assertEqual(exit_code, os.EX_SOFTWARE)
        self.assertNotIn(secret, stderr.getvalue())
        self.assertEqual(
            json.loads(stderr.getvalue()),
            {
                "error": {
                    "code": "internal_error",
                    "exception_type": "QualityTriageStoreError",
                    "retryable": False,
                }
            },
        )
        runtime.close.assert_called_once_with()

    def test_runs_modes_that_do_not_page_ignore_the_page_limit(self) -> None:
        environment = {"REVIEW_AGENT_OPERATOR_PAGE_MAX_ITEMS": "10"}
        for command in (["runs", "--stats"], ["runs", "--mark-stalled"]):
            with self.subTest(command=command):
                runtime = Mock()
                with (
                    patch.dict(os.environ, environment, clear=True),
                    patch.object(memory, "_runtime", return_value=runtime),
                    patch.object(memory, "_run_live", return_value=0) as run_live,
                ):
                    exit_code = memory.main(command)

                self.assertEqual(exit_code, 0)
                run_live.assert_called_once()
                runtime.close.assert_called_once_with()

    def test_close_failure_does_not_replace_a_completed_command_receipt(self) -> None:
        secret = "close failure contained private-value"
        runtime = Mock()
        runtime.close.side_effect = RuntimeError(secret)
        stdout = io.StringIO()
        stderr = io.StringIO()

        def completed_command(*_args: object) -> int:
            print('{"status":"complete"}')
            return 0

        with (
            patch.object(memory, "_runtime", return_value=runtime),
            patch.object(memory, "_run_live", side_effect=completed_command),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            exit_code = memory.main(["list"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(stdout.getvalue()), {"status": "complete"})
        self.assertEqual(stderr.getvalue(), "")
        runtime.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
