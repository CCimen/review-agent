from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "bootstrap" / "plugins" / "review_agent_tools"))

from review_agent_learning import (
    CONTRADICTORY_DECISIONS,
    DECISION_POLICIES,
    POSITIVE_DECISIONS,
    POSITIVE_FEEDBACK,
    QUALITY_POLICIES,
    build_learning_report,
    render_markdown,
)
from memory_validation import DECISIONS, REVIEW_FEEDBACK_CATEGORIES, SUPPRESSIVE_DECISIONS


def state_with(
    *,
    findings: list[dict[str, object]] | None = None,
    observations: list[dict[str, object]] | None = None,
    references: list[dict[str, object]] | None = None,
    decisions: list[dict[str, object]] | None = None,
    feedback: list[dict[str, object]] | None = None,
    schema_version: int = 4,
) -> dict[str, object]:
    return {
        "schema_version": schema_version,
        "findings": findings or [],
        "finding_observations": observations or [],
        "pr_finding_references": references or [],
        "decisions": decisions or [],
        "review_quality_feedback": feedback or [],
    }


def finding(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "fingerprint": "abcdef1234567890",
        "repository": "example-org/example-repository",
        "pr_number": 17,
        "rule_id": "migration.record-identity",
        "title": "Bulk migration can choose the wrong catalog record",
        "path": "src/catalog/record_selector.py",
        "severity": "High",
        "category": "security",
    }
    base.update(overrides)
    return base


def observation(**overrides: object) -> dict[str, object]:
    base = finding(
        id=1,
        review_subject_id=1,
        head_sha="a" * 40,
        policy_revision="policy-v1",
        observed_at="2026-06-24T00:00:00Z",
    )
    base.update(overrides)
    return base


def reference(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "repository": "example-org/example-repository",
        "pr_number": 17,
        "fingerprint": "abcdef1234567890",
        "local_reference": "F1",
    }
    base.update(overrides)
    return base


class ReviewLearningReportTests(unittest.TestCase):
    def test_false_positive_decision_becomes_calibration_candidate(self) -> None:
        report = build_learning_report(
            state_with(
                observations=[observation()],
                references=[reference()],
                decisions=[
                    {
                        "fingerprint": "abcdef1234567890",
                        "observation_id": 1,
                        "decision": "false_positive",
                        "reason": "Repository already applies verified scope before this query.",
                    }
                ],
            ),
            repository="example-org/example-repository",
        )

        self.assertEqual(len(report.decision_candidates), 1)
        candidate = report.decision_candidates[0]
        self.assertEqual(candidate.source_value, "false_positive")
        self.assertEqual(candidate.signal_strength, "strong")
        self.assertEqual(candidate.suggested_route, "judgment_or_procedure")
        self.assertTrue(candidate.promotion_eligible)
        self.assertEqual(candidate.local_reference, "F1")
        markdown = render_markdown(report)
        self.assertIn("## Decision candidates", markdown)
        self.assertIn("D1: Bulk migration", markdown)
        self.assertIn("false_positive", markdown)

    def test_resolved_decision_is_positive_pattern_not_policy_candidate(self) -> None:
        report = build_learning_report(
            state_with(
                observations=[observation()],
                decisions=[
                    {
                        "fingerprint": "abcdef1234567890",
                        "observation_id": 1,
                        "decision": "resolved",
                        "reason": "Fixed with a regression test.",
                    }
                ],
            )
        )

        self.assertEqual(report.decision_candidates, ())
        self.assertEqual(len(report.positive_patterns), 1)
        self.assertEqual(report.positive_patterns[0].suggested_route, "positive_pattern")

    def test_quality_feedback_is_separate_from_decision_candidates(self) -> None:
        report = build_learning_report(
            state_with(
                observations=[observation()],
                decisions=[
                    {
                        "fingerprint": "abcdef1234567890",
                        "observation_id": 1,
                        "decision": "false_positive",
                        "reason": "Existing guard disproves the claim.",
                    }
                ],
                feedback=[
                    {
                        "repository": "example-org/example-repository",
                        "pr_number": 17,
                        "local_reference": "F2",
                        "category": "missed_issue",
                        "reason": "The review missed an authorization-boundary regression.",
                    }
                ],
            )
        )

        self.assertEqual(len(report.decision_candidates), 1)
        self.assertEqual(len(report.quality_signals), 1)
        markdown = render_markdown(report)
        self.assertIn("### D1:", markdown)
        self.assertIn("### Q1:", markdown)
        self.assertLess(markdown.index("### D1:"), markdown.index("### Q1:"))

    def test_empty_export_does_not_fabricate_candidates(self) -> None:
        report = build_learning_report(state_with())

        self.assertEqual(report.decision_candidates, ())
        self.assertEqual(report.quality_signals, ())
        markdown = render_markdown(report)
        self.assertIn("No decision-derived learning candidates", markdown)
        self.assertIn("No review-quality signals", markdown)
        self.assertIn("Weak signals", markdown)

    def test_unknown_schema_version_fails_loudly(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported review-memory schema_version"):
            build_learning_report(state_with(schema_version=999))

    def test_learning_vocabularies_match_canonical_memory_values(self) -> None:
        handled_decisions = set(DECISION_POLICIES) | set(POSITIVE_DECISIONS)
        handled_feedback = set(QUALITY_POLICIES) | set(POSITIVE_FEEDBACK)

        self.assertEqual(handled_decisions, set(DECISIONS))
        self.assertEqual(handled_feedback, set(REVIEW_FEEDBACK_CATEGORIES))
        self.assertEqual(CONTRADICTORY_DECISIONS, set(SUPPRESSIVE_DECISIONS))

    def test_decision_chain_uses_latest_effective_state_once(self) -> None:
        report = build_learning_report(
            state_with(
                observations=[observation()],
                decisions=[
                    {
                        "id": 1,
                        "fingerprint": "abcdef1234567890",
                        "observation_id": 1,
                        "decision": "false_positive",
                        "reason": "Earlier false positive.",
                    },
                    {
                        "id": 2,
                        "fingerprint": "abcdef1234567890",
                        "observation_id": 1,
                        "decision": "reopen",
                        "reason": "The guard changed.",
                    },
                    {
                        "id": 3,
                        "fingerprint": "abcdef1234567890",
                        "observation_id": 1,
                        "decision": "false_positive",
                        "reason": "Latest reviewed guard disproves it again.",
                    },
                ],
            )
        )

        self.assertEqual(len(report.decision_candidates), 1)
        candidate = report.decision_candidates[0]
        self.assertEqual(candidate.source_id, "decision:3")
        self.assertEqual(
            candidate.related_event_ids,
            ("decision:1", "decision:2", "decision:3"),
        )
        self.assertEqual(
            candidate.decision_chain,
            ("false_positive", "reopen", "false_positive"),
        )
        markdown = render_markdown(report)
        self.assertIn("Decision chain: false_positive -> reopen -> false_positive", markdown)

    def test_resolved_after_false_positive_remains_investigation_candidate(self) -> None:
        report = build_learning_report(
            state_with(
                observations=[observation()],
                decisions=[
                    {
                        "id": 1,
                        "fingerprint": "abcdef1234567890",
                        "observation_id": 1,
                        "decision": "false_positive",
                        "reason": "Initial reviewer miss.",
                    },
                    {
                        "id": 2,
                        "fingerprint": "abcdef1234567890",
                        "observation_id": 1,
                        "decision": "resolved",
                        "reason": "Fixed with a regression test.",
                    },
                ],
            )
        )

        self.assertEqual(len(report.decision_candidates), 1)
        self.assertEqual(report.positive_patterns, ())
        candidate = report.decision_candidates[0]
        self.assertEqual(candidate.source_id, "decision:2")
        self.assertEqual(candidate.source_value, "resolved")
        self.assertEqual(candidate.suggested_route, "contradictory_outcome")
        self.assertEqual(candidate.decision_chain, ("false_positive", "resolved"))

    def test_same_fingerprint_in_different_observations_stays_separate(self) -> None:
        report = build_learning_report(
            state_with(
                observations=[
                    observation(id=1, pr_number=17, head_sha="a" * 40),
                    observation(id=2, pr_number=99, head_sha="b" * 40),
                ],
                decisions=[
                    {
                        "id": 1,
                        "fingerprint": "abcdef1234567890",
                        "observation_id": 1,
                        "decision": "false_positive",
                        "reason": "Existing guard disproves it in PR 17.",
                    },
                    {
                        "id": 2,
                        "fingerprint": "abcdef1234567890",
                        "observation_id": 2,
                        "decision": "resolved",
                        "reason": "Fixed in PR 99.",
                    },
                ],
            )
        )

        self.assertEqual(len(report.decision_candidates), 1)
        self.assertEqual(len(report.positive_patterns), 1)
        self.assertEqual(report.decision_candidates[0].pr_number, 17)
        self.assertEqual(report.positive_patterns[0].pr_number, 99)

    def test_reopen_then_resolved_is_positive_lifecycle_not_contradiction(self) -> None:
        report = build_learning_report(
            state_with(
                observations=[observation()],
                decisions=[
                    {
                        "id": 1,
                        "fingerprint": "abcdef1234567890",
                        "observation_id": 1,
                        "decision": "reopen",
                        "reason": "The issue is real after recheck.",
                    },
                    {
                        "id": 2,
                        "fingerprint": "abcdef1234567890",
                        "observation_id": 1,
                        "decision": "resolved",
                        "reason": "Fixed with a regression test.",
                    },
                ],
            )
        )

        self.assertEqual(report.decision_candidates, ())
        self.assertEqual(len(report.positive_patterns), 1)
        self.assertEqual(report.positive_patterns[0].decision_chain, ("reopen", "resolved"))

    def test_unclassified_values_are_reported_not_silently_dropped(self) -> None:
        report = build_learning_report(
            state_with(
                observations=[observation()],
                decisions=[
                    {
                        "fingerprint": "abcdef1234567890",
                        "observation_id": 1,
                        "decision": "worsened",
                        "reason": "",
                    }
                ],
                feedback=[
                    {
                        "repository": "example-org/example-repository",
                        "pr_number": 17,
                        "category": "too_many_widgets",
                        "reason": "",
                    }
                ],
            )
        )
        markdown = render_markdown(report)

        self.assertIn("Unclassified decision values", markdown)
        self.assertIn("`worsened`", markdown)
        self.assertIn("Unclassified review-quality feedback values", markdown)
        self.assertIn("`too_many_widgets`", markdown)

    def test_empty_decision_reason_does_not_abort_report_but_is_incomplete(self) -> None:
        report = build_learning_report(
            state_with(
                observations=[observation()],
                decisions=[
                    {
                        "fingerprint": "abcdef1234567890",
                        "observation_id": 1,
                        "decision": "false_positive",
                        "reason": "",
                    }
                ],
            )
        )

        self.assertEqual(len(report.decision_candidates), 1)
        self.assertEqual(report.decision_candidates[0].signal_strength, "incomplete")
        self.assertFalse(report.decision_candidates[0].promotion_eligible)
        self.assertIn("human reason", report.decision_candidates[0].missing_evidence)

    def test_legacy_decision_without_observation_is_non_promotable(self) -> None:
        report = build_learning_report(
            state_with(
                decisions=[
                    {
                        "fingerprint": "abcdef1234567890",
                        "decision": "false_positive",
                        "reason": "Old decision before provenance existed.",
                    }
                ],
            )
        )

        self.assertEqual(len(report.decision_candidates), 1)
        candidate = report.decision_candidates[0]
        self.assertEqual(candidate.signal_strength, "incomplete")
        self.assertFalse(candidate.promotion_eligible)
        self.assertIn("exact observation provenance", candidate.missing_evidence)


if __name__ == "__main__":
    unittest.main()
