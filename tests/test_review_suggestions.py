from __future__ import annotations

import hashlib
import sys
import unittest
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "bootstrap" / "plugins"
sys.path.insert(0, str(PACKAGE_ROOT))

from review_agent_tools import suggestion_validation  # noqa: E402


class SuggestionValidationTests(unittest.TestCase):
    repository = "example-org/example-repository"
    pr_number = 17
    head_sha = "a" * 40
    fingerprint = "f" * 64
    path = "src/flags.py"
    head_text = "before\nsafe = False\nafter\n"
    patch = (
        "@@ -1,3 +1,3 @@\n"
        " before\n"
        "-safe = None\n"
        "+safe = False\n"
        " after"
    )

    def raw(self, **overrides: object) -> dict[str, object]:
        value: dict[str, object] = {
            "start_line": 2,
            "end_line": 2,
            "expected_text": "safe = False",
            "replacement_text": "safe = True",
        }
        value.update(overrides)
        return value

    def validate(
        self, **overrides: object
    ) -> suggestion_validation.SuggestionValidation:
        return suggestion_validation.validate_suggestion(
            self.raw(**overrides),
            repository=self.repository,
            pr_number=self.pr_number,
            head_sha=self.head_sha,
            fingerprint=self.fingerprint,
            path=self.path,
            finding_line=2,
            patch=self.patch,
            head_text=self.head_text,
        )

    def test_accepts_exact_atomic_changed_range(self) -> None:
        result = self.validate()

        self.assertEqual(result.rejection_reason, "")
        assert result.suggestion is not None
        self.assertEqual(result.suggestion["start_line"], 2)
        self.assertEqual(result.suggestion["end_line"], 2)
        self.assertEqual(result.suggestion["replacement_text"], "safe = True")
        self.assertEqual(
            result.suggestion["expected_hash"],
            hashlib.sha256(b"safe = False").hexdigest(),
        )

    def test_overlap_requires_the_same_path_and_intersecting_lines(self) -> None:
        first = self.validate().suggestion
        assert first is not None
        second = dict(first, start_line=2, end_line=3)
        separate = dict(first, start_line=3, end_line=3)
        other_path = dict(first, path="src/other.py")

        self.assertTrue(suggestion_validation.ranges_overlap(first, second))
        self.assertFalse(suggestion_validation.ranges_overlap(first, separate))
        self.assertFalse(suggestion_validation.ranges_overlap(first, other_path))

    def test_rejects_ambiguous_terminal_newline(self) -> None:
        result = self.validate(
            expected_text="safe = False\n", replacement_text="safe = True\n"
        )

        self.assertIsNone(result.suggestion)
        self.assertEqual(result.rejection_reason, "suggestion_text_invalid")

    def test_rejects_expected_text_that_does_not_match_trusted_head(self) -> None:
        result = self.validate(expected_text="safe = maybe")

        self.assertIsNone(result.suggestion)
        self.assertEqual(result.rejection_reason, "suggestion_expected_text_mismatch")

    def test_rejects_context_only_range(self) -> None:
        result = suggestion_validation.validate_suggestion(
            self.raw(
                start_line=1,
                end_line=1,
                expected_text="before",
                replacement_text="before = True",
            ),
            repository=self.repository,
            pr_number=self.pr_number,
            head_sha=self.head_sha,
            fingerprint=self.fingerprint,
            path=self.path,
            finding_line=1,
            patch=self.patch,
            head_text=self.head_text,
        )

        self.assertIsNone(result.suggestion)
        self.assertEqual(result.rejection_reason, "suggestion_range_not_in_changed_hunk")

    def test_rejects_range_that_does_not_include_finding_line(self) -> None:
        result = self.validate(start_line=1, end_line=1)

        self.assertIsNone(result.suggestion)
        self.assertEqual(result.rejection_reason, "suggestion_must_include_finding_line")

    def test_rejects_noop_placeholder_and_markdown_fence(self) -> None:
        cases = (
            (self.raw(replacement_text="safe = False"), "suggestion_has_no_change"),
            (self.raw(replacement_text="TODO"), "suggestion_contains_placeholder"),
            (self.raw(replacement_text="```python"), "suggestion_text_invalid"),
            (
                self.raw(
                    replacement_text=(
                        "<!-- review-agent:suggestion key=sha256:"
                        + ("0" * 64)
                        + " -->"
                    )
                ),
                "suggestion_text_invalid",
            ),
        )
        for raw, reason in cases:
            with self.subTest(reason=reason):
                result = suggestion_validation.validate_suggestion(
                    raw,
                    repository=self.repository,
                    pr_number=self.pr_number,
                    head_sha=self.head_sha,
                    fingerprint=self.fingerprint,
                    path=self.path,
                    finding_line=2,
                    patch=self.patch,
                    head_text=self.head_text,
                )
                self.assertIsNone(result.suggestion)
                self.assertEqual(result.rejection_reason, reason)

    def test_key_is_stable_for_one_finding_and_head(self) -> None:
        first = suggestion_validation.suggestion_key(
            self.repository, self.pr_number, self.head_sha, self.fingerprint
        )
        second = suggestion_validation.suggestion_key(
            self.repository.upper(), self.pr_number, self.head_sha, self.fingerprint
        )

        self.assertEqual(first, second)
        self.assertRegex(first, r"^sha256:[0-9a-f]{64}$")
    def test_high_risk_finding_metadata_is_ineligible(self) -> None:
        self.assertEqual(
            suggestion_validation.suggestion_eligibility_rejection(
                rule_id="multitenancy.missing-resource-scope",
                category="correctness",
                path="src/resources.py",
                symbol="load_resource",
                anchor="resource lookup",
                title="Tenant boundary is bypassed",
                evidence="The resource lookup is not scoped to the current tenant.",
                impact="Cross-account access is possible.",
                smallest_fix="Scope the lookup.",
            ),
            "suggestion_high_risk_domain",
        )
        self.assertEqual(
            suggestion_validation.suggestion_eligibility_rejection(
                rule_id="correctness.default",
                category="security",
                path="src/flags.py",
                symbol="safe",
                anchor="safe default",
                title="Safe mode defaults to disabled",
                evidence="The changed default is false.",
                impact="Requests use the wrong mode.",
                smallest_fix="Restore the default.",
            ),
            "suggestion_high_risk_category",
        )
        for category in ("migration", "contracts", "data_contract"):
            with self.subTest(category=category):
                self.assertEqual(
                    suggestion_validation.suggestion_eligibility_rejection(
                        rule_id="correctness.local-change",
                        category=category,
                        path="src/change.py",
                        symbol="apply_change",
                        anchor="local change",
                        title="Local value is incorrect",
                        evidence="The value differs from the configured default.",
                        impact="The result is incorrect.",
                        smallest_fix="Use the configured default.",
                    ),
                    "suggestion_high_risk_category",
                )
if __name__ == "__main__":
    unittest.main()
