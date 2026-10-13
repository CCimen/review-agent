from __future__ import annotations

import json
import sys
import unittest
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "bootstrap" / "plugins"
sys.path.insert(0, str(PACKAGE_ROOT))

from review_agent_tools import (  # noqa: E402
    capacity,
    review_contract,
    review_delivery_tool,
    review_memory_tools,
    review_part_tools,
    review_parts_application,
    review_run_application,
    review_source_initialization,
    review_source_tools,
    review_tool_runtime,
)
from review_agent_tools.domain.review import resolve_review_subject  # noqa: E402
from review_agent_tools.github.source import ReviewSourceBytes  # noqa: E402
from review_agent_tools.postgres.coverage import FileIndexSummary  # noqa: E402

TEST_REVIEW_CONTRACT = review_contract.ReviewContract(
    profile="default-standard",
    hermes_image="hermes@test",
    model_provider="openai-codex",
    model="gpt-test",
    reasoning_effort="high",
    plugin_result_max_chars=160_000,
    profile_bundle_sha256="1" * 64,
    managed_config_sha256="2" * 64,
    engine_bundle_sha256="3" * 64,
    sha256="contract",
)

REPOSITORY = "example/project"
HEAD = "b" * 40
PULL = {
    "state": "open",
    "base": {"sha": "a" * 40, "repo": {"id": 1, "full_name": REPOSITORY}},
    "head": {"sha": HEAD, "repo": {"id": 1, "full_name": REPOSITORY}},
    "changed_files": 3,
}
SOURCE = SimpleNamespace(run_id=41, lease=SimpleNamespace(job_id=7, lease_generation=3))


def finding(**overrides: object) -> dict[str, object]:
    return {
        "rule_id": "correctness.null-return",
        "category": "correctness",
        "path": "src/app.py",
        "line": 7,
        "symbol": "save",
        "anchor": "save return",
        "title": "save can return None to a caller that dereferences it",
        "severity": "High",
        "publication_score": 9,
        "confidence": 0.9,
        "evidence": "save returns None when the row is missing.",
        "disproof_checks": "Checked every caller in the diff.",
        "impact": "The request handler raises.",
        "smallest_fix": "Raise NotFound in save.",
        "introduced_by_diff": True,
        **overrides,
    }


def runtime() -> Mock:
    fake = Mock()
    fake.transaction.return_value = nullcontext(Mock())
    return fake


def unregistered_state() -> review_run_application.LiveRunState:
    """An accepted review whose changed files are not registered yet."""
    return review_run_application.LiveRunState(
        run_id=41,
        phase="accepted",
        started_at="2026-08-24T10:00:00Z",
        file_index=FileIndexSummary(
            changed_files_reported=3,
            changed_files_registered=0,
            registration_complete=False,
            by_domain=(),
            by_review_mode=(),
            by_change_status=(),
            sample_paths=(),
        ),
        resolved_config=resolve_review_subject(
            base_sha="a" * 40,
            head_sha=HEAD,
            policy_revision="policy-v1",
            resolved_config_schema_version=2,
            resolved_config=review_contract.resolved_config(TEST_REVIEW_CONTRACT),
        ).resolved_config,
    )


class ReviewPartToolTests(unittest.TestCase):
    def test_the_lease_fence_tells_a_tool_which_part_its_session_reviews(self) -> None:
        @review_tool_runtime.worker_lease_fence()
        def handler(args: dict[str, Any], **context: Any) -> str:
            return str(review_tool_runtime.session_part(context))

        for stored, expected in ((2, "2"), (0, "0")):
            with (
                self.subTest(part=stored),
                patch.object(review_tool_runtime, "postgres_runtime", return_value=runtime()),
                patch.object(
                    review_tool_runtime.postgres_jobs,
                    "require_live_lease",
                    return_value=SimpleNamespace(part=stored),
                ),
            ):
                # A value the model could smuggle in is replaced by the job's part.
                result = handler(
                    {"run_id": 41}, session_id="review-agent-job-7-lease-3", review_part=9
                )
            self.assertEqual(result, expected)

    def initialize(self, *, may_split: bool) -> tuple[Any, Mock, Mock]:
        client = Mock()
        client.get_review_pull.return_value = SimpleNamespace(
            repository=REPOSITORY, pr_number=1, payload=PULL
        )
        client.get_changed_files_page.return_value = ReviewSourceBytes(
            state="ok", truncated=False, headers={},
            body=json.dumps([
                {"filename": f"src/{name}.py", "status": "modified", "sha": "c" * 40,
                 "additions": 1, "deletions": 1, "changes": 2, "patch": "+" * 300_000}
                for name in "abc"
            ]).encode(),
        )
        source = review_tool_runtime.GatewaySourceSession(
            run_id=41,
            lease=review_tool_runtime.postgres_jobs.WorkerLeaseSession(7, 3),
            client=client,
        )
        state = unregistered_state()
        with (
            patch.object(review_run_application, "load_live_run_state", return_value=state),
            patch.object(review_run_application, "load_live_snapshot", return_value=SimpleNamespace(pull=PULL)) as snapshot,
            patch.object(review_run_application, "advance_live_phase"),
            patch.object(
                review_run_application, "register_live_changed_files",
                return_value=replace(state.file_index, registration_complete=True),
            ),
            patch.object(review_parts_application, "split_review", return_value=3) as split,
        ):
            result = review_source_initialization.initialize_review(
                source, runtime(), TEST_REVIEW_CONTRACT, may_split=may_split
            )
        return result, split, snapshot

    def test_the_main_session_splits_a_large_review_and_does_nothing_after(self) -> None:
        result, split, snapshot = self.initialize(may_split=True)

        self.assertEqual(result.parts_planned, 3)
        plan = split.call_args.kwargs["plan"]
        self.assertEqual(
            sorted(path for part in plan.parts for path in part),
            ["src/a.py", "src/b.py", "src/c.py"],
        )
        self.assertGreater(len(plan.parts), 1)
        # The split released the lease: no snapshot is loaded with it afterwards.
        self.assertEqual([call.kwargs["phase"] for call in snapshot.call_args_list], ["accepted"])

    def test_a_caller_that_may_not_split_initializes_as_before(self) -> None:
        result, split, snapshot = self.initialize(may_split=False)

        split.assert_not_called()
        self.assertEqual(result.parts_planned, 0)
        self.assertEqual(
            [call.kwargs["phase"] for call in snapshot.call_args_list],
            ["accepted", "collecting_diff"],
        )

    def test_begin_stops_the_session_whose_review_was_split(self) -> None:
        with (
            patch.object(review_source_tools, "gateway_source_session", return_value=SOURCE),
            patch.object(review_source_tools, "pull_request_identity", return_value=(REPOSITORY, 1, PULL)),
            patch.object(review_source_tools, "postgres_runtime"),
            patch.object(review_source_tools, "installed_review_contract"),
            patch.object(
                review_source_tools, "initialize_review",
                return_value=SimpleNamespace(parts_planned=11),
            ) as initialize,
            patch.object(review_source_tools, "prepare_graph") as graph,
        ):
            result = json.loads(review_source_tools.review_begin.__wrapped__({"existing_run_id": 41}))

        self.assertTrue(initialize.call_args.kwargs["may_split"])
        self.assertEqual((result["run_state"], result["parts"]), ("split_into_parts", 11))
        self.assertIn("Stop this turn", result["next_action"])
        graph.assert_not_called()

    def test_a_part_is_told_what_earlier_parts_found_without_their_evidence(self) -> None:
        payload = review_part_tools.briefing_payload(
            review_parts_application.PartBriefing(
                part=3,
                parts=11,
                assigned_files=62,
                earlier=(
                    review_parts_application.PartReport(
                        1, {"findings": [finding()], "notes": ["save() can now return None"]}
                    ),
                ),
            )
        )

        self.assertEqual((payload["part"], payload["parts"], payload["changed_files"]), (3, 11, 62))
        self.assertEqual(
            payload["earlier_parts_untrusted"],
            [{
                "part": 1,
                "findings": [{
                    "path": "src/app.py", "line": 7, "severity": "High",
                    "title": "save can return None to a caller that dereferences it",
                }],
                "notes": ["save() can now return None"],
            }],
        )
        self.assertIn("never instructions", payload["untrusted_data_notice"])

    def record(
        self,
        findings: list[dict[str, object]],
        *,
        part: int,
        stage: Mock,
        unread: tuple[int, ...] = (),
        **arguments: object,
    ) -> dict[str, Any]:
        with (
            patch.object(review_parts_application, "unread_reports", return_value=unread),
            patch.object(review_memory_tools, "gateway_source_session", return_value=SOURCE),
            patch.object(review_memory_tools, "pull_request_identity", return_value=(REPOSITORY, 1, PULL)),
            patch.object(review_run_application, "reopen_live_finding_collection"),
            patch.object(review_memory_tools, "review_run_snapshot", return_value=PULL),
            patch.object(review_memory_tools, "load_changed_files", return_value=[{
                "path": "src/app.py", "context_hash": "c" * 40,
                "context_hash_source": "blob", "patch": "@@ -1 +1 @@",
            }]),
            patch.object(review_memory_tools, "postgres_runtime"),
            patch.object(review_part_tools, "postgres_runtime"),
            patch.object(review_parts_application, "stage_report", stage),
            patch.object(
                review_memory_tools.review_finding_application, "record_live_findings",
                side_effect=AssertionError("a part session must not record findings"),
            ) if part else nullcontext(),
        ):
            return json.loads(review_memory_tools.review_memory_record.__wrapped__(
                {"run_id": 41, "findings": findings, **arguments}, review_part=part,
            ))

    def test_a_part_stores_its_candidates_only_after_reading_all_its_files(self) -> None:
        unread = Mock(side_effect=review_parts_application.PartReadingIncomplete(unread=3, assigned=80))
        waiting = self.record([finding()], part=2, stage=unread)
        self.assertEqual((waiting["stage"], waiting["recorded"]), ("part_incomplete", False))
        self.assertIn("3 of this part's 80", waiting["error"])
        self.assertIn("diff_state unseen", waiting["next_action"])
        self.assertIn("diff_state truncated", waiting["next_action"])

        stored = Mock(return_value=2)
        done = self.record([finding()], part=2, stage=stored, part_notes=["  save() can now\nreturn None "])
        self.assertEqual((done["stage"], done["part"], done["candidates"]), ("part_recorded", 2, 1))
        self.assertEqual(
            stored.call_args.kwargs["report"],
            {"findings": [finding()], "notes": ["save() can now return None"]},
        )

    def test_a_part_hands_in_every_candidate_that_fits_its_report(self) -> None:
        # More than an equal share of the review's finding limit: only the
        # report size bounds what one part hands to the final session.
        candidates = [finding(anchor=f"anchor {index}") for index in range(17)]
        stored = Mock(return_value=2)
        done = self.record(candidates, part=2, stage=stored)
        self.assertEqual((done["stage"], done["candidates"]), ("part_recorded", 17))
        self.assertEqual(stored.call_args.kwargs["report"]["findings"], candidates)

    def test_a_part_report_names_what_to_change_when_it_cannot_be_stored(self) -> None:
        never = Mock(side_effect=AssertionError("an invalid report must not be stored"))
        limit = capacity.current().part_report_max_chars
        long_text = [finding(anchor=f"anchor {index}", evidence="e" * 900) for index in range(limit // 900 + 1)]
        too_long = self.record(long_text, part=2, stage=never)["error"]
        self.assertIn("shorten", too_long)
        # What still does not fit stays recoverable: the final session follows notes.
        self.assertIn("path and line", too_long)

        invalid = self.record([finding(severity="Blocker")], part=2, stage=never)
        self.assertIn("severity must be one of", invalid["error"])

    def test_the_final_session_records_only_after_receiving_every_part_report(self) -> None:
        with patch.object(
            review_memory_tools.review_finding_application, "record_live_findings"
        ) as recorder:
            result = self.record([finding()], part=0, stage=Mock(), unread=(2, 5))

        recorder.assert_not_called()
        self.assertEqual((result["stage"], result["retryable"]), ("validation_failed", True))
        self.assertIn("part(s) 2, 5", result["error"])
        self.assertIn("review_agent_part_report", result["next_action"])

    def test_recording_names_the_rejected_field_of_an_invalid_finding(self) -> None:
        result = self.record([finding(severity="Blocker")], part=0, stage=Mock())

        self.assertIn("severity must be one of", result["error"])

    def deliver(self, *, part: int, parts: int, unread: tuple[int, ...]) -> tuple[dict[str, Any], Mock, Mock]:
        with (
            patch.object(review_delivery_tool, "gateway_source_session", return_value=SOURCE),
            patch.object(review_delivery_tool, "pull_request_identity", return_value=(REPOSITORY, 1, PULL)),
            patch.object(review_delivery_tool, "postgres_runtime"),
            patch.object(
                review_run_application, "load_live_run_state",
                return_value=SimpleNamespace(phase="reviewing", parts=parts),
            ),
            patch.object(review_delivery_tool, "review_run_snapshot", return_value=PULL),
            patch.object(review_parts_application, "unread_reports", return_value=unread),
            patch.object(review_run_application, "summarize_postgres_coverage") as coverage,
            patch.object(review_delivery_tool.settings.ReviewAgentSettings, "from_environment"),
            patch.object(
                review_delivery_tool.review_publication_application, "prepare_postgres_publication",
                return_value=SimpleNamespace(
                    publication_id=5, findings_count=1, suggestions_count=0,
                    resolved_count=0, ignored_previous_verdicts=(),
                ),
            ) as prepare,
        ):
            result = json.loads(
                review_delivery_tool.review_deliver.__wrapped__({"run_id": 41}, review_part=part)
            )
        return result, prepare, coverage

    def test_only_the_final_session_delivers_and_only_with_every_part_report(self) -> None:
        refused, prepare, _ = self.deliver(part=2, parts=3, unread=())
        self.assertIn("review_agent_memory_record", refused["error"])
        prepare.assert_not_called()

        waiting, prepare, _ = self.deliver(part=0, parts=3, unread=(2, 3))
        self.assertEqual(waiting["stage"], "validation_failed")
        self.assertIn("part(s) 2, 3", waiting["error"])
        prepare.assert_not_called()

        delivered, prepare, coverage = self.deliver(part=0, parts=3, unread=())
        self.assertEqual(delivered["stage"], "queued_for_publication")
        prepare.assert_called_once()
        # Each part read its own files before reporting; no session re-reads them.
        coverage.assert_not_called()

    def test_the_final_session_receives_a_report_and_a_part_session_does_not(self) -> None:
        report = {"findings": [finding()], "notes": ["save() can now return None"]}
        for part, received in ((0, True), (2, False)):
            with (
                self.subTest(session_part=part),
                patch.object(review_part_tools, "gateway_source_session", return_value=SOURCE),
                patch.object(review_part_tools, "postgres_runtime"),
                patch.object(review_parts_application, "planned_parts", return_value=3),
                patch.object(review_parts_application, "load_report", return_value=report),
                patch.object(review_parts_application, "confirm_report_read") as confirm,
            ):
                result = json.loads(review_part_tools.review_part_report.__wrapped__(
                    {"run_id": 41, "part": 1}, review_part=part,
                ))
            self.assertEqual(confirm.called, received)
            if received:
                self.assertEqual((result["part"], result["parts"]), (1, 3))
                self.assertEqual(result["findings_untrusted"], [finding()])
                self.assertEqual(confirm.call_args.kwargs["part"], 1)
            else:
                self.assertIn("final session", result["error"])

    def test_a_part_pages_only_the_changed_files_it_owns(self) -> None:
        for arguments, expected in (({}, 2), ({"changed_only": False}, 0)):
            with (
                self.subTest(arguments=arguments),
                patch.object(review_source_tools, "gateway_source_session", return_value=SOURCE),
                patch.object(review_source_tools, "pull_request_identity", return_value=(REPOSITORY, 1, PULL)),
                patch.object(review_source_tools, "postgres_runtime"),
                patch.object(
                    review_run_application, "load_live_changed_file_page",
                    return_value=SimpleNamespace(
                        run_id=41, repository=REPOSITORY, pr_number=1, limit=100,
                        next_cursor=None, total_matching=0, items=(),
                    ),
                ) as page,
            ):
                review_source_tools.pr_files.__wrapped__({"run_id": 41, **arguments}, review_part=2)
            self.assertEqual(page.call_args.kwargs["part"], expected)


if __name__ == "__main__":
    unittest.main()
