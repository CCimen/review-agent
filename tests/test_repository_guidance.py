from __future__ import annotations

import sys
import unittest
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "bootstrap" / "plugins"
sys.path.insert(0, str(PACKAGE_ROOT))

from review_agent_tools.domain import repository_guidance  # noqa: E402


class RepositoryGuidanceContractTests(unittest.TestCase):
    def test_config_preserves_the_explicit_context_order(self) -> None:
        config = repository_guidance.parse_config(
            """
version = 1
enabled = true
context = [
  "context/platform.md",
  "context/backend/fastapi.md",
]
""".strip()
        )

        self.assertTrue(config.enabled)
        self.assertEqual(
            config.context_paths,
            (
                ".review-agent/context/platform.md",
                ".review-agent/context/backend/fastapi.md",
            ),
        )

    def test_config_defaults_to_enabled_with_no_context_files(self) -> None:
        config = repository_guidance.parse_config("version = 1")

        self.assertTrue(config.enabled)
        self.assertEqual(config.context_paths, ())

    def test_config_rejects_unknown_fields_unsafe_paths_and_duplicates(self) -> None:
        invalid = (
            ("version = true", "version"),
            ("version = 1.0", "version"),
            ("version = 1\npersonality = 'friendly'", "fields"),
            ("version = 1\ncontext = ['../secret.md']", "context/"),
            ("version = 1\ncontext = ['instructions.md']", "context/"),
            ("version = 1\ncontext = ['context/platform.txt']", "Markdown"),
            (
                "version = 1\ncontext = ['context/a.md', 'context/a.md']",
                "duplicates",
            ),
        )
        for content, message in invalid:
            with self.subTest(content=content):
                with self.assertRaisesRegex(
                    repository_guidance.RepositoryGuidanceError,
                    message,
                ):
                    repository_guidance.parse_config(content)

    def test_config_bounds_the_number_of_explicit_files(self) -> None:
        paths = ", ".join(
            f"'context/{index}.md'"
            for index in range(repository_guidance.MAX_CONTEXT_FILES + 1)
        )

        with self.assertRaisesRegex(
            repository_guidance.RepositoryGuidanceError,
            f"at most {repository_guidance.MAX_CONTEXT_FILES}",
        ):
            repository_guidance.parse_config(
                f"version = 1\ncontext = [{paths}]"
            )


if __name__ == "__main__":
    unittest.main()
