from __future__ import annotations

from contextlib import ExitStack
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bootstrap" / "plugins"))

from review_agent_tools import review_source_tools as tools  # noqa: E402
from review_agent_tools.github.gateway import GitHubGatewayRetryable  # noqa: E402


class ReviewDiffReuseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.context = ExitStack()
        self.addCleanup(self.context.close)
        self.context.enter_context(patch.object(tools, "_diff_cache", tools.ReviewDiffCache()))
        self.pull = {"changed_files": 3, "base": {"sha": "a" * 40}, "head": {"sha": "b" * 40}}
        self.client = Mock()
        self.client.get_review_diff.return_value = SimpleNamespace(
            state="diff_unavailable", body=b"", truncated=False
        )
        self.files = [
            {"filename": name, "status": "added", "additions": 1, "deletions": 0,
             "changes": 1, "patch": "@@ -0,0 +1 @@\n+" + "å" * 2200}
            for name in ("a.py", "b.py", "c.py")
        ]
        self.client.get_changed_files_page.return_value = SimpleNamespace(
            state="ok", body=json.dumps(self.files).encode(), truncated=False, headers={}
        )
        self.source = SimpleNamespace(
            run_id=91, lease=SimpleNamespace(job_id=7, lease_generation=3), client=self.client
        )
        self.context.enter_context(patch.object(tools, "gateway_source_session", return_value=self.source))
        self.identity = self.context.enter_context(patch.object(
            tools, "pull_request_identity", return_value=("example/project", 1, self.pull)
        ))
        self.snapshot = self.context.enter_context(patch.object(tools, "review_run_snapshot", return_value=self.pull))
        self.context.enter_context(patch.object(tools, "postgres_runtime"))
        self.record = self.context.enter_context(patch.object(tools.review_run_application, "record_live_diff_result"))

    def read(self, path: str, start: int = 0) -> dict[str, object]:
        return json.loads(tools.pr_diff.__wrapped__({
            "run_id": self.source.run_id, "path": path, "max_chars": 1000, "start_char": start
        }))

    def test_reuses_patches_and_continuations_but_checks_each_request(self) -> None:
        pages = [self.read("a.py", offset) for offset in (0, 1000, 2000)]
        self.read("b.py")
        self.read("c.py")
        self.assertTrue(all("error" not in page for page in pages))
        expected = "diff --git a/a.py b/a.py\n--- /dev/null\n+++ b/a.py\n" + self.files[0]["patch"] + "\n"
        self.assertEqual("".join(str(page["diff"]) for page in pages), expected)
        self.assertEqual(self.client.get_review_diff.call_count, 1)
        self.assertEqual(self.client.get_changed_files_page.call_count, 1)
        self.assertEqual(self.identity.call_count, 5)
        self.assertEqual(self.record.call_count, 5)
        evidence = [call.args[2].page for call in self.record.call_args_list[:3]]
        self.assertEqual(len({page.content_sha256 for page in evidence}), 1)
        self.assertEqual([(page.start_char, page.end_char) for page in evidence], [(0, 1000), (1000, 2000), (2000, len(expected))])

    def test_truncated_rendering_and_missing_rendered_paths_keep_patch_fallback(self) -> None:
        for truncated in (False, True):
            with self.subTest(truncated=truncated), patch.object(tools, "_diff_cache", tools.ReviewDiffCache()):
                self.client.reset_mock()
                self.client.get_review_diff.return_value = SimpleNamespace(
                    state="ok", truncated=truncated,
                    body=b"diff --git a/a.py b/a.py\n--- /dev/null\n+++ b/a.py\n@@ -0,0 +1 @@\n+rendered\n",
                )
                first = self.read("a.py")
                missing = self.read("b.py")
                again = self.read("a.py")
                self.assertEqual(first, again)
                self.assertEqual(first["diff_source"], "per_file_patch" if truncated else "rendered")
                self.assertEqual(missing["diff_source"], "per_file_patch")
                self.assertEqual(self.client.get_review_diff.call_count, 1)
                self.assertEqual(self.client.get_changed_files_page.call_count, 1)

    def test_revision_or_lease_changes_require_a_new_fill_and_access_is_always_checked(self) -> None:
        self.read("a.py")
        self.source.lease.lease_generation += 1
        self.read("a.py")
        self.pull["head"]["sha"] = "c" * 40
        self.read("a.py")
        self.pull["base"]["sha"] = "d" * 40
        self.read("a.py")
        self.assertEqual(self.client.get_review_diff.call_count, 4)
        self.assertEqual(self.client.get_changed_files_page.call_count, 4)
        self.identity.side_effect = tools.ToolInputError("repository access is no longer enabled")
        self.assertIn("error", self.read("a.py"))
        self.assertEqual(self.record.call_count, 4)
        self.assertEqual(self.client.get_review_diff.call_count, 4)

    def test_failed_post_fill_validation_never_records_or_caches_the_source(self) -> None:
        self.snapshot.side_effect = [self.pull, tools.ToolInputError("snapshot superseded")]
        self.assertIn("error", self.read("a.py"))
        self.record.assert_not_called()
        self.snapshot.side_effect = None
        self.assertNotIn("error", self.read("a.py"))
        self.assertEqual(self.client.get_review_diff.call_count, 2)

    def test_transient_diff_failure_is_not_cached_or_reported_as_missing(self) -> None:
        success = self.client.get_review_diff.return_value
        self.client.get_review_diff.side_effect = [GitHubGatewayRetryable("github_read_unavailable"), success]
        failed = self.read("a.py")
        self.assertTrue(failed["retryable"])
        self.record.assert_not_called()
        self.assertNotIn("error", self.read("a.py"))
        self.assertNotIn("error", self.read("b.py"))
        self.assertEqual(self.client.get_review_diff.call_count, 2)

    def test_partial_index_is_refetched_without_claiming_absent_path_is_unchanged(self) -> None:
        self.pull["changed_files"] = 4
        self.context.enter_context(patch.object(
            tools.review_run_application, "lookup_live_run_file", return_value=SimpleNamespace(item=None)
        ))
        self.read("a.py")
        result = self.read("unknown.py")
        self.assertEqual(result["path_state"], "not_in_changed_index")
        self.assertEqual(result["changed_file_index_state"], "incomplete")
        self.assertEqual(self.client.get_changed_files_page.call_count, 2)

    def test_omitted_patch_remains_unavailable_after_reuse(self) -> None:
        del self.files[2]["patch"]
        self.client.get_changed_files_page.return_value.body = json.dumps(self.files).encode()
        self.read("a.py")
        result = self.read("c.py")
        self.assertEqual(result["path_state"], "diff_unavailable")
        self.assertEqual(result["unavailable_paths"], ["c.py"])
        self.assertEqual(self.client.get_changed_files_page.call_count, 1)

    def test_oversized_snapshot_stays_readable_without_repeating_the_406(self) -> None:
        with patch.object(tools, "_diff_cache", tools.ReviewDiffCache(max_bytes=3000)):
            self.assertNotIn("error", self.read("a.py"))
            self.assertNotIn("error", self.read("b.py"))
        self.assertEqual(self.client.get_review_diff.call_count, 1)
        self.assertEqual(self.client.get_changed_files_page.call_count, 2)


class DiffCacheBoundsTests(unittest.TestCase):
    def test_concurrent_runs_remain_isolated_while_evicting(self) -> None:
        cache = tools.ReviewDiffCache(max_entries=3)
        keys = [tools.DiffSnapshotKey("example/project", 1, run, 7, 3, "a" * 40, "b" * 40, 1) for run in range(8)]
        values = [tools.DiffSnapshot(tools.diff_render.prepare_rendered_diff(
            f"diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -0,0 +1 @@\n+{key.run_id}\n"
        )) for key in keys]

        def use(index: int) -> None:
            for _ in range(30):
                cache.put(keys[index], "rendered", values[index])
                found = cache.get(keys[index], "rendered")
                self.assertTrue(found is None or found is values[index])

        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(use, range(8)))
        self.assertEqual(sum(cache.get(key, "rendered") is not None for key in keys), 3)

    def test_evicts_oldest_snapshot_on_byte_or_entry_limit(self) -> None:
        key = tools.DiffSnapshotKey("example/project", 1, 91, 7, 3, "a" * 40, "b" * 40, 1)
        value = tools.DiffSnapshot(tools.diff_render.prepare_rendered_diff(
            "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -0,0 +1 @@\n+" + "x" * 4000
        ))
        for cache in (tools.ReviewDiffCache(max_entries=1), tools.ReviewDiffCache(max_bytes=9000)):
            with self.subTest(cache=cache):
                cache.put(key, "rendered", value)
                self.assertIs(cache.get(key, "rendered"), value)
                newer = replace(key, run_id=92)
                cache.put(newer, "rendered", value)
                self.assertIsNone(cache.get(key, "rendered"))
                self.assertIs(cache.get(newer, "rendered"), value)
