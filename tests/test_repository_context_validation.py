from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "bootstrap" / "plugins"
sys.path.insert(0, str(PACKAGE_ROOT))

from review_agent_tools import (  # noqa: E402
    repository_context_validation,
    repository_guidance_context,
)
from review_agent_tools.domain import repository_decisions  # noqa: E402


class RepositoryContextValidationTests(unittest.TestCase):
    def test_missing_repository_root_is_a_bounded_validation_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing"

            with self.assertRaises(
                repository_context_validation.RepositoryContextValidationError
            ) as raised:
                repository_context_validation.validate_repository_context(missing)

        self.assertEqual(raised.exception.code, "repository_context_root_invalid")
        self.assertEqual(raised.exception.path, ".")

    def test_validates_only_the_explicit_copyable_repository_package(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / ".review-agent"
            (package / "context").mkdir(parents=True)
            (package / "decisions").mkdir()
            (package / "config.toml").write_text(
                'version = 1\ncontext = ["context/platform.md"]\n',
                encoding="utf-8",
            )
            (package / "instructions.md").write_text(
                "Prefer clear ownership and bounded failure modes.\n",
                encoding="utf-8",
            )
            (package / "context" / "platform.md").write_text(
                "The platform owns authentication.\n", encoding="utf-8"
            )
            (package / "context" / "ignored.md").write_text(
                "This file is not indexed.\n", encoding="utf-8"
            )
            (package / "decisions.toml").write_text(
                """version = 1
[[decision]]
id = "ADR-0001"
adr_path = ".review-agent/decisions/ADR-0001.md"
applies_to = ["src/**"]
""",
                encoding="utf-8",
            )
            (package / "decisions" / "ADR-0001.md").write_text(
                """+++
id = "ADR-0001"
title = "Keep authentication at the platform boundary"
status = "accepted"
invariant = "Application modules do not implement their own authentication."
on_change = ["Re-run the platform integration tests."]
+++

# Decision
""",
                encoding="utf-8",
            )

            receipt = repository_context_validation.validate_repository_context(root)
            value = receipt.to_json_obj()

        self.assertTrue(value["ready"])
        self.assertTrue(value["configured"])
        self.assertEqual(
            [item["path"] for item in value["context_files"]],
            [".review-agent/context/platform.md"],
        )
        self.assertEqual(value["decisions"][0]["path"], ".review-agent/decisions/ADR-0001.md")
        self.assertNotIn("authentication", str(value).lower())

    def test_reports_the_exact_missing_indexed_file_without_its_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / ".review-agent"
            package.mkdir()
            (package / "config.toml").write_text(
                'version = 1\ncontext = ["context/missing.md"]\n',
                encoding="utf-8",
            )

            with self.assertRaises(
                repository_context_validation.RepositoryContextValidationError
            ) as raised:
                repository_context_validation.validate_repository_context(root)

        self.assertEqual(raised.exception.code, "repository_context_file_missing")
        self.assertEqual(raised.exception.path, ".review-agent/context/missing.md")

    def test_offline_validation_enforces_the_runtime_line_ceiling(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            context = root / ".review-agent" / "context"
            context.mkdir(parents=True)
            (root / ".review-agent" / "config.toml").write_text(
                'version = 1\ncontext = ["context/platform.md"]\n',
                encoding="utf-8",
            )
            (context / "platform.md").write_text(
                "\n".join(
                    "bounded line"
                    for _ in range(
                        repository_guidance_context.MAX_GUIDANCE_FILE_LINES + 1
                    )
                ),
                encoding="utf-8",
            )

            with self.assertRaises(
                repository_context_validation.RepositoryContextValidationError
            ) as raised:
                repository_context_validation.validate_repository_context(root)

        self.assertEqual(raised.exception.code, "repository_context_file_too_large")
        self.assertEqual(
            raised.exception.path, ".review-agent/context/platform.md"
        )

    def test_offline_validation_uses_the_runtime_character_capacity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            context = root / ".review-agent" / "context"
            context.mkdir(parents=True)
            (root / ".review-agent" / "config.toml").write_text(
                'version = 1\ncontext = ["context/platform.md"]\n',
                encoding="utf-8",
            )
            (context / "platform.md").write_text("å" * 10, encoding="utf-8")

            receipt = repository_context_validation.validate_repository_context(
                root,
                content_max_chars=10,
            )

        self.assertEqual(receipt.guidance_chars, 10)

    def test_offline_validation_caps_an_oversized_operator_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / ".review-agent"
            package.mkdir()
            (package / "config.toml").write_text("version = 1\n", encoding="utf-8")
            (package / "instructions.md").write_text(
                "x" * (repository_guidance_context.MAX_GUIDANCE_CONTENT_CHARS + 1),
                encoding="utf-8",
            )

            with self.assertRaises(
                repository_context_validation.RepositoryContextValidationError
            ) as raised:
                repository_context_validation.validate_repository_context(
                    root,
                    content_max_chars=(
                        repository_guidance_context.MAX_GUIDANCE_CONTENT_CHARS
                        + 10_000
                    ),
                )

        self.assertEqual(raised.exception.code, "repository_context_file_too_large")
        self.assertEqual(
            raised.exception.path, ".review-agent/instructions.md"
        )

    def test_invalid_utf8_is_rejected_by_offline_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / ".review-agent"
            package.mkdir()
            (package / "config.toml").write_bytes(b"version = 1\n\xff")

            with self.assertRaises(
                repository_context_validation.RepositoryContextValidationError
            ) as raised:
                repository_context_validation.validate_repository_context(root)

        self.assertEqual(raised.exception.code, "repository_context_file_not_utf8")
        self.assertEqual(raised.exception.path, ".review-agent/config.toml")

    def test_valid_adr_header_is_not_rejected_when_body_utf8_crosses_read_limit(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            decisions = root / ".review-agent" / "decisions"
            decisions.mkdir(parents=True)
            (root / ".review-agent" / "decisions.toml").write_text(
                '''version = 1
[[decision]]
id = "ADR-0001"
adr_path = ".review-agent/decisions/ADR-0001.md"
applies_to = ["src/**"]
''',
                encoding="utf-8",
            )
            header = '''+++
id = "ADR-0001"
title = "Keep one transaction owner"
status = "accepted"
invariant = "One module owns each multi-step write."
on_change = ["Run the transaction contract tests."]
+++
'''
            remaining = repository_decisions.MAX_ADR_HEADER_BYTES - len(
                header.encode("utf-8")
            )
            body = "x" * (remaining - 1) + "å"
            (decisions / "ADR-0001.md").write_text(
                header + body,
                encoding="utf-8",
            )

            receipt = repository_context_validation.validate_repository_context(root)

        self.assertEqual(receipt.decisions[0].id, "ADR-0001")

    def test_adr_validation_ignores_invalid_bytes_after_the_header_page(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            decisions = root / ".review-agent" / "decisions"
            decisions.mkdir(parents=True)
            (root / ".review-agent" / "decisions.toml").write_text(
                '''version = 1
[[decision]]
id = "ADR-0001"
adr_path = ".review-agent/decisions/ADR-0001.md"
applies_to = ["src/**"]
''',
                encoding="utf-8",
            )
            header = '''+++
id = "ADR-0001"
title = "Keep one transaction owner"
status = "accepted"
invariant = "One module owns each multi-step write."
on_change = ["Run the transaction contract tests."]
+++
'''.encode()
            body_lines = b"body\n" * repository_decisions.MAX_FRONTMATTER_LINES
            (decisions / "ADR-0001.md").write_bytes(
                header + body_lines + b"\xff\xfe\n"
            )

            receipt = repository_context_validation.validate_repository_context(root)

        self.assertEqual(receipt.decisions[0].id, "ADR-0001")


if __name__ == "__main__":
    unittest.main()
