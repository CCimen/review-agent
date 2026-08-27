from __future__ import annotations

import sys
import unittest
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "bootstrap" / "plugins"
sys.path.insert(0, str(PACKAGE_ROOT))

from review_agent_tools.domain import repository_decisions  # noqa: E402


class RepositoryDecisionContractTests(unittest.TestCase):
    def test_index_matches_recursive_globs_without_widening_single_segment_globs(
        self,
    ) -> None:
        index = repository_decisions.parse_index(
            """
version = 1

[[decision]]
id = "ADR-0007"
adr_path = "docs/decisions/ADR-0007-rag-chunking.md"
applies_to = ["src/rag/**", "tests/rag/*.py"]
""".strip()
        )

        matches = repository_decisions.matching_entries(
            index,
            changed_paths=("src/rag/nested/config.py",),
        )
        self.assertEqual(tuple(match.entry for match in matches), index.entries)
        self.assertEqual(matches[0].matched_path_count, 1)
        self.assertEqual(
            repository_decisions.matching_entries(
                index,
                changed_paths=("tests/rag/nested/test_config.py",),
            ),
            (),
        )

    def test_recursive_globs_match_zero_or_many_path_segments(self) -> None:
        index = repository_decisions.parse_index(
            """version = 1

[[decision]]
id = "ADR-0007"
adr_path = "docs/decisions/ADR-0007.md"
applies_to = ["**/settings.py", "src/**/config?.py"]
"""
        )

        matches = repository_decisions.matching_entries(
            index,
            changed_paths=(
                "settings.py",
                "packages/app/settings.py",
                "src/config1.py",
                "src/nested/config2.py",
            ),
        )

        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].matched_path_count, 4)

    def test_index_rejects_unknown_fields_and_more_than_the_review_guard(self) -> None:
        with self.assertRaisesRegex(
            repository_decisions.RepositoryDecisionError,
            "fields",
        ):
            repository_decisions.parse_index(
                """
version = 1
owner = "model"
decision = []
""".strip()
            )

        entries = "\n".join(
            f"""
[[decision]]
id = "ADR-{number:04d}"
adr_path = "docs/decisions/ADR-{number:04d}.md"
applies_to = ["src/{number}/**"]
""".strip()
            for number in range(repository_decisions.MAX_INDEX_ENTRIES + 1)
        )
        with self.assertRaisesRegex(
            repository_decisions.RepositoryDecisionError,
            "at most 200",
        ):
            repository_decisions.parse_index(f"version = 1\n{entries}")

    def test_adr_frontmatter_is_typed_hashed_and_keeps_provenance(self) -> None:
        entry = repository_decisions.DecisionIndexEntry(
            id="ADR-0007",
            adr_path="docs/decisions/ADR-0007-rag-chunking.md",
            applies_to=("src/rag/**",),
        )

        decision = repository_decisions.parse_adr(
            """+++
id = "ADR-0007"
title = "Keep retrieval inside the embedding budget"
status = "accepted"
"invariant" = "Chunk size, overlap, and top-k form one retrieval budget."
on_change = ["Recalculate the budget.", "Run the retrieval evaluation."]
evidence = "docs/evaluations/rag.md"
origin_pr = 1234
supersedes = "ADR-0006"
+++

# Context

Human rationale.
""",
            match=repository_decisions.DecisionIndexMatch(
                entry=entry,
                matched_path_count=2,
            ),
        )

        self.assertEqual(decision.id, "ADR-0007")
        self.assertEqual(decision.invariant_line, 5)
        self.assertEqual(decision.applies_to, ("src/rag/**",))
        self.assertEqual(decision.status, "accepted")
        self.assertEqual(decision.origin_pr, 1234)
        self.assertEqual(decision.supersedes, "ADR-0006")
        self.assertEqual(decision.matched_path_count, 2)
        self.assertRegex(decision.metadata_hash, r"^sha256:[0-9a-f]{64}$")

    def test_adr_requires_matching_identity_and_bounded_frontmatter(self) -> None:
        entry = repository_decisions.DecisionIndexEntry(
            id="ADR-0007",
            adr_path="docs/decisions/ADR-0007.md",
            applies_to=("src/**",),
        )
        with self.assertRaisesRegex(
            repository_decisions.RepositoryDecisionError,
            "does not match",
        ):
            repository_decisions.parse_adr(
                """+++
id = "ADR-0008"
title = "Other decision"
status = "accepted"
invariant = "A stable contract."
on_change = ["Run its evaluation."]
+++
""",
                match=repository_decisions.DecisionIndexMatch(
                    entry=entry,
                    matched_path_count=1,
                ),
            )

        unclosed = "+++\n" + "\n".join(
            f"comment_{line} = \"value\""
            for line in range(repository_decisions.MAX_FRONTMATTER_LINES)
        )
        with self.assertRaisesRegex(
            repository_decisions.RepositoryDecisionError,
            "first 60 lines",
        ):
            repository_decisions.parse_adr(
                unclosed,
                match=repository_decisions.DecisionIndexMatch(
                    entry=entry,
                    matched_path_count=1,
                ),
            )

    def test_contract_rejects_unsupported_globs_and_oversized_values(self) -> None:
        with self.assertRaisesRegex(
            repository_decisions.RepositoryDecisionError,
            "simple glob syntax",
        ):
            repository_decisions.parse_index(
                """version = 1
[[decision]]
id = "ADR-0007"
adr_path = "docs/decisions/ADR-0007.md"
applies_to = ["src/[ab]/**"]
"""
            )

        with self.assertRaisesRegex(
            repository_decisions.RepositoryDecisionError,
            "consecutive",
        ):
            repository_decisions.parse_index(
                """version = 1
[[decision]]
id = "ADR-0007"
adr_path = "docs/decisions/ADR-0007.md"
applies_to = ["src/**/**/config.py"]
"""
            )

        oversized = "x" * (repository_decisions.MAX_INDEX_BYTES + 1)
        with self.assertRaisesRegex(
            repository_decisions.RepositoryDecisionError,
            "64 KiB",
        ):
            repository_decisions.parse_index(oversized)


if __name__ == "__main__":
    unittest.main()
