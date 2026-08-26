from __future__ import annotations

import base64
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "bootstrap" / "plugins"
sys.path.insert(0, str(PACKAGE_ROOT))

from review_agent_tools.github import source  # noqa: E402
from review_agent_tools.postgres.review_runs import ReviewRunScope  # noqa: E402


class ReviewSourceFilePageTests(unittest.TestCase):
    @staticmethod
    def _scope() -> ReviewRunScope:
        return ReviewRunScope(
            run=Mock(),
            provider_repository_id=9001,
            repository="example-org/example-repository",
            pr_number=42,
            base_sha="a" * 40,
            head_sha="b" * 40,
            resolved_config=Mock(),
        )

    @staticmethod
    def _contents(**over: object) -> dict[str, object]:
        value: dict[str, object] = {
            "type": "file",
            "encoding": "base64",
            "content": "",
            "size": 0,
            "sha": "a" * 40,
        }
        value.update(over)
        return value

    def test_small_file_returns_only_the_requested_numbered_page(self) -> None:
        github = Mock()
        raw = b"first\nsecond\nthird\n"
        github.request_json.return_value = self._contents(
            content=base64.b64encode(raw).decode("ascii"),
            size=len(raw),
        )

        page = source.read_review_file_page(
            github,
            self._scope(),
            path="backend/a.py",
            side="head",
            start_line=2,
            max_lines=1,
            max_chars=1_000,
        )

        self.assertEqual(page.state, "ok")
        self.assertEqual(page.content, "2: second")
        self.assertEqual((page.complete_lines, page.total_lines), (1, 3))
        github.request.assert_not_called()

    def test_raw_file_within_the_gateway_memory_budget_is_pageable(self) -> None:
        github = Mock()
        github.request_json.return_value = self._contents(
            encoding="none",
            size=1_500_000,
            sha="b" * 40,
        )
        github.request.return_value = (b"line one\nline two\n", False, {})

        page = source.read_review_file_page(
            github,
            self._scope(),
            path="frontend/schema.d.ts",
            side="head",
            start_line=1,
            max_lines=200,
            max_chars=1_000,
        )

        self.assertEqual(page.state, "ok")
        self.assertEqual(page.content, "1: line one\n2: line two")
        self.assertIn("/contents/frontend/schema.d.ts?ref=", github.request.call_args.args[0])
        self.assertEqual(
            github.request.call_args.kwargs["accept"],
            "application/vnd.github.raw+json",
        )

    def test_provider_size_and_truncation_return_a_terminal_state(self) -> None:
        for metadata, response in (
            (self._contents(encoding="none", size=2_000_001), None),
            (self._contents(encoding="none", size=2_000_000), (b"x", True, {})),
        ):
            with self.subTest(metadata=metadata):
                github = Mock()
                github.request_json.return_value = metadata
                if response is not None:
                    github.request.return_value = response
                page = source.read_review_file_page(
                    github,
                    self._scope(),
                    path="data/huge.json",
                    side="head",
                    start_line=7,
                    max_lines=200,
                    max_chars=1_000,
                )
                self.assertEqual(page.state, "too_large")
                self.assertEqual(page.start_line, 7)

    def test_non_regular_file_is_terminal_without_a_raw_fetch(self) -> None:
        github = Mock()
        github.request_json.return_value = {
            "type": "dir",
            "encoding": "none",
            "content": "",
            "size": 0,
        }

        page = source.read_review_file_page(
            github,
            self._scope(),
            path="backend",
            side="head",
            start_line=1,
            max_lines=200,
            max_chars=1_000,
        )

        self.assertEqual(page.state, "not_regular")
        github.request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
