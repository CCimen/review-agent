from __future__ import annotations

import base64
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "bootstrap" / "plugins"
sys.path.insert(0, str(PACKAGE_ROOT))

from review_agent_tools import tools  # noqa: E402


class FileAtRevisionLargeFileTests(unittest.TestCase):
    """File reads use base64 for small files and raw Contents API media up to its limit."""

    def setUp(self):
        tools._file_at_revision.cache_clear()

    def _contents(self, **over):
        base = {"type": "file", "encoding": "base64", "content": "", "size": 0, "sha": "a" * 40}
        base.update(over)
        return base

    def test_small_file_uses_contents_base64(self):
        raw = b"def handler():\n    return 1\n"
        contents = self._contents(encoding="base64", content=base64.b64encode(raw).decode(), size=len(raw))
        with patch.object(tools, "_request_json", side_effect=[contents]), \
             patch.object(tools, "_request") as raw_get:
            result = tools._file_at_revision("example-org/example-repository", "backend/a.py", "a" * 40)
        self.assertEqual(result, raw)
        raw_get.assert_not_called()  # no second raw fetch for a small (<=1 MB) file

    def test_large_file_reads_contents_raw(self):
        raw = b"line one\nline two\nline three\n"
        contents = self._contents(
            encoding="none",
            content="",
            size=50_000_000,
            sha="b" * 40,
        )
        with patch.object(tools, "_request_json", side_effect=[contents]), \
             patch.object(tools, "_request", return_value=(raw, False, {})) as raw_get:
            result = tools._file_at_revision("example-org/example-repository", "frontend/schema.d.ts", "a" * 40)
            repeated = tools._file_at_revision("example-org/example-repository", "frontend/schema.d.ts", "a" * 40)
        self.assertEqual(result, raw)
        self.assertEqual(repeated, raw)
        self.assertEqual(raw_get.call_count, 1)
        self.assertIn("/contents/frontend/schema.d.ts?ref=", raw_get.call_args.args[0])
        self.assertEqual(raw_get.call_args.kwargs.get("accept"), "application/vnd.github.raw+json")
        self.assertEqual(
            raw_get.call_args.kwargs.get("max_bytes"),
            tools.GITHUB_CONTENTS_FILE_MAX_BYTES,
        )

    def test_file_over_cap_punts_to_diff_without_blob_fetch(self):
        contents = self._contents(
            encoding="none",
            content="",
            size=tools.GITHUB_CONTENTS_FILE_MAX_BYTES + 1,
            sha="b" * 40,
        )
        with patch.object(tools, "_request_json", side_effect=[contents]), \
             patch.object(tools, "_request") as raw_get:
            with self.assertRaises(tools.ToolInputError) as ctx:
                tools._file_at_revision("example-org/example-repository", "data/huge.json", "a" * 40)
        self.assertIn("review_agent_pr_diff", str(ctx.exception))
        raw_get.assert_not_called()  # provider limit checked before a raw fetch

    def test_truncated_raw_response_punts_to_diff(self):
        # size metadata is within the cap, but the raw response truncates -> treat as too large.
        contents = self._contents(
            encoding="none",
            content="",
            size=tools.GITHUB_CONTENTS_FILE_MAX_BYTES - 10,
            sha="b" * 40,
        )
        with patch.object(tools, "_request_json", side_effect=[contents]), \
             patch.object(tools, "_request", return_value=(b"x" * 4096, True, {})):
            with self.assertRaises(tools.ToolInputError) as ctx:
                tools._file_at_revision("example-org/example-repository", "frontend/big.d.ts", "a" * 40)
        self.assertIn("review_agent_pr_diff", str(ctx.exception))

    def test_non_regular_file_is_rejected(self):
        contents = {"type": "dir", "encoding": "none", "content": "", "size": 0}
        with patch.object(tools, "_request_json", side_effect=[contents]), \
             patch.object(tools, "_request") as raw_get:
            with self.assertRaises(tools.ToolInputError) as ctx:
                tools._file_at_revision("example-org/example-repository", "backend", "a" * 40)
        self.assertIn("not a regular file", str(ctx.exception))
        raw_get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
