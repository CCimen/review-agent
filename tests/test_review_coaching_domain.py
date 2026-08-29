from __future__ import annotations

from dataclasses import replace
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bootstrap" / "plugins"))

from review_agent_tools.domain.coaching import (  # noqa: E402
    CoachInterventionOutcomeInput,
    CoachingDomainError,
    resolve_intervention_outcome,
)


def sha256_id(character: str) -> str:
    return "sha256:" + (character * 64)


class CoachInterventionOutcomeTests(unittest.TestCase):
    @staticmethod
    def accepted() -> CoachInterventionOutcomeInput:
        return CoachInterventionOutcomeInput(
            coach_candidate_id=17,
            candidate_key="judgment-false-positive-abc123",
            target_owner="profile",
            proposal_content_hash=sha256_id("a"),
            base_contract_hash=sha256_id("b"),
            diff_hash=sha256_id("c"),
            validation_receipt_hash=sha256_id("d"),
            outcome="accepted",
            reason="Replay and holdout cases passed.",
            actor="github:maintainer",
        )

    def test_resolves_a_stable_accepted_outcome(self) -> None:
        first = resolve_intervention_outcome(self.accepted())
        second = resolve_intervention_outcome(self.accepted())

        self.assertEqual(first, second)
        self.assertEqual(
            first.intervention_key,
            "sha256:a1a4ef409aa701829f4386cdf03dd8b1"
            "b83dff8e6923150b28c4bc319cbedfe2",
        )
        self.assertEqual(first.diff_hash, sha256_id("c"))
        self.assertEqual(first.validation_receipt_hash, sha256_id("d"))

    def test_regression_requires_evaluated_artifacts(self) -> None:
        resolved = resolve_intervention_outcome(
            replace(self.accepted(), outcome="rejected_regression")
        )

        self.assertEqual(resolved.outcome, "rejected_regression")

    def test_insufficient_evidence_may_omit_evaluated_artifacts(self) -> None:
        resolved = resolve_intervention_outcome(
            replace(
                self.accepted(),
                outcome="rejected_insufficient_evidence",
                diff_hash="",
                validation_receipt_hash="",
            )
        )

        self.assertIsNone(resolved.diff_hash)
        self.assertIsNone(resolved.validation_receipt_hash)

    def test_accepted_outcome_requires_diff_and_validation_receipt(self) -> None:
        for field in ("diff_hash", "validation_receipt_hash"):
            with self.subTest(field=field):
                with self.assertRaisesRegex(
                    CoachingDomainError, "requires diff_hash and validation_receipt_hash"
                ):
                    resolve_intervention_outcome(
                        replace(self.accepted(), **{field: ""})
                    )

    def test_rejects_malformed_hashes_and_unbounded_audit_text(self) -> None:
        for field in (
            "proposal_content_hash",
            "base_contract_hash",
            "diff_hash",
            "validation_receipt_hash",
        ):
            with self.subTest(field=field):
                with self.assertRaisesRegex(CoachingDomainError, "sha256"):
                    resolve_intervention_outcome(
                        replace(self.accepted(), **{field: "sha256:ABC"})
                    )
        with self.assertRaisesRegex(CoachingDomainError, "reason exceeds 2000"):
            resolve_intervention_outcome(
                replace(self.accepted(), reason="x" * 2_001)
            )
        with self.assertRaisesRegex(CoachingDomainError, "actor exceeds 200"):
            resolve_intervention_outcome(
                replace(self.accepted(), actor="x" * 201)
            )

    def test_live_reviewer_entrypoints_do_not_import_intervention_history(self) -> None:
        entrypoints = (
            ROOT / "bootstrap" / "plugins" / "review_agent_tools" / "worker.py",
            ROOT
            / "bootstrap"
            / "plugins"
            / "review_agent_tools"
            / "review_tool_runtime.py",
            ROOT / "tools" / "review_agent_worker.py",
            ROOT / "tools" / "review_agent_admission.py",
        )
        private_symbols = (
            "CoachInterventionHistory",
            "coach_intervention_history",
            "intervention_history",
            "review_agent_memory",
        )

        for path in entrypoints:
            source = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                for symbol in private_symbols:
                    self.assertNotIn(symbol, source)


if __name__ == "__main__":
    unittest.main()
