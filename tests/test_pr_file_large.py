from __future__ import annotations

import base64
from dataclasses import replace
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
            run=Mock(id=51),
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

    def test_cached_pages_are_scoped_to_repository_commit_and_path(self) -> None:
        github = Mock()
        github.request_json.return_value = self._contents(
            content=base64.b64encode(b"first\nsecond\n").decode("ascii"), size=13,
        )
        cache = source.ReviewFileCache()
        scope = self._scope()
        first = source.read_review_file_page(
            github, scope, path="a.py", side="head", start_line=1, max_lines=1,
            max_chars=100, cache=cache,
        )
        second = source.read_review_file_page(
            github, scope, path="a.py", side="head", start_line=2, max_lines=1,
            max_chars=100, cache=cache,
        )
        self.assertEqual((first.content, second.content), ("1: first", "2: second"))
        source.read_review_file_page(
            github, replace(scope, run=Mock(id=52)), path="a.py", side="head",
            start_line=1, max_lines=1, max_chars=100, cache=cache,
        )
        self.assertEqual(github.request_json.call_count, 1)
        for other, path, side in (
            (replace(scope, provider_repository_id=9002), "a.py", "head"),
            (replace(scope, head_sha="c" * 40), "a.py", "head"),
            (scope, "b.py", "head"),
            (scope, "a.py", "base"),
        ):
            source.read_review_file_page(
                github, other, path=path, side=side, start_line=1, max_lines=1,
                max_chars=100, cache=cache,
            )
        self.assertEqual(github.request_json.call_count, 5)

    def test_file_cache_evicts_least_recent_reads_within_both_memory_bounds(self) -> None:
        for max_bytes, max_entries in ((4, 10), (100, 2), (1, 10)):
            with self.subTest(max_bytes=max_bytes, max_entries=max_entries):
                github = Mock()
                github.request_json.return_value = self._contents(
                    content=base64.b64encode(b"a\n").decode("ascii"), size=2,
                )
                cache = source.ReviewFileCache(max_bytes=max_bytes, max_entries=max_entries)
                for path in ("a.py", "b.py", "a.py", "c.py", "a.py", "b.py"):
                    source.read_review_file_page(
                        github, self._scope(), path=path, side="head", start_line=1,
                        max_lines=1, max_chars=100, cache=cache,
                    )
                self.assertEqual(github.request_json.call_count, 6 if max_bytes == 1 else 4)

    def test_raw_file_within_the_gateway_memory_budget_is_pageable(self) -> None:
        github = Mock()
        github.request_json.return_value = self._contents(
            encoding="none",
            size=1_500_000,
            sha="b" * 40,
        )
        github.request.return_value = (b"line one\nline two\n", False, {})

        cache = source.ReviewFileCache()
        page = source.read_review_file_page(
            github,
            self._scope(),
            path="frontend/schema.d.ts",
            side="head",
            start_line=1,
            max_lines=200,
            max_chars=1_000,
            cache=cache,
        )

        self.assertEqual(page.state, "ok")
        self.assertEqual(page.content, "1: line one\n2: line two")
        self.assertIn("/contents/frontend/schema.d.ts?ref=", github.request.call_args.args[0])
        self.assertEqual(
            github.request.call_args.kwargs["accept"],
            "application/vnd.github.raw+json",
        )
        repeated = source.read_review_file_page(
            github, self._scope(), path="frontend/schema.d.ts", side="head",
            start_line=2, max_lines=1, max_chars=1_000, cache=cache,
        )
        self.assertEqual(repeated.content, "2: line two")
        self.assertEqual(github.request_json.call_count, 1)
        self.assertEqual(github.request.call_count, 1)

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

    def test_invalid_utf8_is_a_terminal_text_state(self) -> None:
        github = Mock()
        raw = b"valid prefix\n\xff\xfe"
        github.request_json.return_value = self._contents(
            content=base64.b64encode(raw).decode("ascii"),
            size=len(raw),
        )

        page = source.read_review_file_page(
            github,
            self._scope(),
            path="context/not-utf8.md",
            side="base",
            start_line=1,
            max_lines=200,
            max_chars=1_000,
        )

        self.assertEqual(page.state, "not_utf8")
        self.assertEqual(page.content, "")
        self.assertEqual(page.complete_lines, 0)

    def test_invalid_utf8_outside_the_requested_page_is_not_inspected(self) -> None:
        github = Mock()
        raw = b"requested line\n\xff\xfe\n"
        github.request_json.return_value = self._contents(
            content=base64.b64encode(raw).decode("ascii"),
            size=len(raw),
        )

        page = source.read_review_file_page(
            github,
            self._scope(),
            path="decisions/typed-header.md",
            side="base",
            start_line=1,
            max_lines=1,
            max_chars=1_000,
        )

        self.assertEqual(page.state, "ok")
        self.assertEqual(page.content, "1: requested line")
        self.assertEqual((page.complete_lines, page.total_lines), (1, 2))


if __name__ == "__main__":
    unittest.main()
