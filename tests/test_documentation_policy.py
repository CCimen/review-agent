from __future__ import annotations

import sys
import unittest
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "bootstrap" / "plugins"
sys.path.insert(0, str(PACKAGE_ROOT))

from review_agent_tools.domain import documentation_policy  # noqa: E402


class DocumentationPolicyTests(unittest.TestCase):
    def test_standalone_policy_parses_and_keeps_explanations(self) -> None:
        policy = documentation_policy.parse_policy(
            """version = 1
[[area]]
id = "configuration"
sources = ["src/config/**"]
documents = ["docs/configuration.md"]
intent = "Keep settings and recovery accurate."
[[ignore_changes]]
paths = ["tests/fixtures/**"]
reason = "Internal fixtures are not shipped."
[[ignore_documents]]
paths = ["docs/archive/**"]
reason = "These documents describe old releases."
"""
        )

        self.assertEqual(policy.areas[0].documents, ("docs/configuration.md",))
        self.assertEqual(policy.ignore_changes[0].reason, "Internal fixtures are not shipped.")

    def test_contract_rejects_unknown_duplicate_escaping_and_ignored_documents(self) -> None:
        invalid = (
            """version = 1
owner = "model"
area = []
""",
            """version = 1
[[area]]
id = "same"
sources = ["src/**"]
documents = ["docs/a.md"]
intent = "First."
[[area]]
id = "same"
sources = ["lib/**"]
documents = ["docs/b.md"]
intent = "Second."
""",
            """version = 1
[[area]]
id = "escape"
sources = ["../src/**"]
documents = ["docs/a.md"]
intent = "Invalid."
""",
            """version = 1
[[area]]
id = "archive"
sources = ["src/**"]
documents = ["docs/archive/old.md"]
intent = "Invalid."
[[ignore_documents]]
paths = ["docs/archive/**"]
reason = "Old releases."
""",
        )
        for content in invalid:
            with self.subTest(content=content[:30]):
                with self.assertRaises(documentation_policy.DocumentationPolicyError):
                    documentation_policy.parse_policy(content)

    def test_review_agent_starter_uses_existing_source_and_document_paths(self) -> None:
        root = PACKAGE_ROOT.parents[1]
        policy = documentation_policy.parse_policy(
            (root / "examples/documentation-review/review-agent.toml").read_text()
        )
        self.assertEqual({area.id for area in policy.areas}, {
            "review-commands", "repository-context", "deployment",
        })
        for area in policy.areas:
            for path in (*area.sources, *area.documents):
                self.assertTrue((root / path).is_file(), path)

    def test_contract_is_bounded(self) -> None:
        with self.assertRaisesRegex(
            documentation_policy.DocumentationPolicyError, "64 KiB"
        ):
            documentation_policy.parse_policy(
                "x" * (documentation_policy.MAX_CONFIG_BYTES + 1)
            )


if __name__ == "__main__":
    unittest.main()
