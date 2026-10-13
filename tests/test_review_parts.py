from __future__ import annotations

import sys
import unittest
from pathlib import Path

import yaml

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "bootstrap" / "plugins"
sys.path.insert(0, str(PACKAGE_ROOT))

from review_agent_tools.domain import review as review_domain  # noqa: E402


class ReviewPartPlanTests(unittest.TestCase):
    def test_the_managed_config_compacts_at_its_threshold_of_the_window(self) -> None:
        # Part sizing assumes Hermes compacts at window x threshold: no output
        # reservation, per-model threshold, or absolute cap moves that point.
        config = yaml.safe_load(
            (PACKAGE_ROOT.parent / "config.yaml").read_text(encoding="utf-8")
        )
        self.assertEqual(set(config["compression"]), {"threshold", "codex_gpt55_autoraise"})
        self.assertNotIn("max_tokens", config["model"])
        self.assertNotIn("context_length", config["model"])

    def test_part_sizes_follow_the_session_window(self) -> None:
        # At 500K tokens the preferred part matches the measured ~390K-character
        # parts; the largest stays below the 80% compaction point.
        self.assertEqual(
            review_domain.part_sizes(context_tokens=500_000, compaction_tokens=400_000),
            (371_653, 607_874),
        )
        # A smaller window gets proportionally smaller parts instead of compacting.
        self.assertEqual(
            review_domain.part_sizes(context_tokens=272_000, compaction_tokens=217_600),
            (192_125, 320_629),
        )
        # The preferred part never exceeds the largest, even with an early threshold.
        target, limit = review_domain.part_sizes(
            context_tokens=272_000, compaction_tokens=100_000
        )
        self.assertEqual(target, limit)

    def test_a_pull_request_one_session_can_read_is_not_split(self) -> None:
        files = [(f"src/file{index:02}.py", 1_000) for index in range(50)]

        plan = review_domain.plan_review_parts(
            files, part_target=40_000, part_limit=60_000, max_parts=8
        )

        self.assertEqual(plan.parts, ())
        self.assertEqual(plan.unassigned, ())

    def test_a_large_pull_request_is_split_in_path_order_within_the_part_limit(self) -> None:
        sizes = {
            f"{directory}/file{index:02}.py": 10_000
            for directory in ("web", "api", "docs")
            for index in range(10)
        }

        plan = review_domain.plan_review_parts(
            sizes.items(), part_target=80_000, part_limit=120_000, max_parts=8
        )

        self.assertEqual(len(plan.parts), 4)
        self.assertEqual(
            [path for part in plan.parts for path in part], sorted(sizes)
        )
        self.assertTrue(
            all(sum(sizes[path] for path in part) <= 120_000 for part in plan.parts)
        )
        self.assertEqual(plan.unassigned, ())

    def test_files_beyond_the_part_cap_stay_unassigned(self) -> None:
        paths = [f"src/file{index:03}.py" for index in range(100)]

        plan = review_domain.plan_review_parts(
            ((path, 10_000) for path in paths),
            part_target=80_000,
            part_limit=100_000,
            max_parts=4,
        )

        self.assertEqual([len(part) for part in plan.parts], [10, 10, 10, 10])
        self.assertEqual(plan.unassigned, tuple(paths[40:]))

    def test_whole_file_packing_does_not_leave_a_readable_file_unassigned(self) -> None:
        plan = review_domain.plan_review_parts(
            [("a.py", 6), ("b.py", 6), ("c.py", 6)],
            part_target=10,
            part_limit=17,
            max_parts=2,
        )

        self.assertEqual(plan.parts, (("a.py", "b.py"), ("c.py",)))
        self.assertEqual(plan.unassigned, ())

    def test_a_file_no_session_can_read_is_left_unassigned(self) -> None:
        files = [("db/dump.sql", 70)] + [(f"src/file{index:02}.py", 5) for index in range(20)]

        plan = review_domain.plan_review_parts(
            files, part_target=40, part_limit=60, max_parts=8
        )

        self.assertEqual([len(part) for part in plan.parts], [8, 8, 4])
        self.assertEqual(plan.unassigned, ("db/dump.sql",))

    def test_readable_files_that_fit_one_session_are_not_split_around_a_huge_file(self) -> None:
        # The single session then reads the huge file as far as it gets, which
        # covers more than a split review that excludes it.
        plan = review_domain.plan_review_parts(
            [("db/dump.sql", 70), ("src/small.py", 5)],
            part_target=40,
            part_limit=60,
            max_parts=8,
        )

        self.assertEqual(plan.parts, ())
        self.assertEqual(plan.unassigned, ())

    def test_a_missing_patch_is_sized_from_its_changed_lines(self) -> None:
        self.assertEqual(review_domain.changed_file_diff_size(patch="abc", changes=9), 3)
        self.assertEqual(
            review_domain.changed_file_diff_size(patch=None, changes=100),
            100 * review_domain.MISSING_PATCH_CHARS_PER_LINE,
        )
        self.assertEqual(review_domain.changed_file_diff_size(patch=None, changes=0), 1)


if __name__ == "__main__":
    unittest.main()
