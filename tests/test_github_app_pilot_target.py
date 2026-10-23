from pathlib import Path
from tempfile import TemporaryDirectory
import sys
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from examples import github_app_pilot_target  # noqa: E402


class ReadReportTests(unittest.TestCase):
    def test_reads_only_a_direct_child_of_the_report_directory(self) -> None:
        with (
            TemporaryDirectory() as directory,
            TemporaryDirectory() as outside_dir,
        ):
            root = Path(directory)
            (root / "report.txt").write_text("reviewed", encoding="utf-8")
            outside = Path(outside_dir) / "outside.txt"
            outside.write_text("outside", encoding="utf-8")
            (root / "link.txt").symlink_to(outside)
            with patch.object(github_app_pilot_target, "_REPORT_ROOT", root):
                self.assertEqual(
                    github_app_pilot_target.read_report("report.txt"),
                    "reviewed",
                )
                for invalid in ("../outside.txt", "/tmp/outside.txt", "link.txt"):
                    with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                        github_app_pilot_target.read_report(invalid)


if __name__ == "__main__":
    unittest.main()
