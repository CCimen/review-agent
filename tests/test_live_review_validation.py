import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from examples.live_review_validation import read_report


class ReadReportTests(unittest.TestCase):
    def test_reads_only_a_direct_child_of_the_report_directory(self) -> None:
        with (
            TemporaryDirectory() as directory,
            TemporaryDirectory() as outside_directory,
        ):
            root = Path(directory)
            (root / "report.txt").write_text("reviewed", encoding="utf-8")
            outside = Path(outside_directory) / "outside.txt"
            outside.write_text("outside", encoding="utf-8")
            (root / "link.txt").symlink_to(outside)

            self.assertEqual(read_report(root, "report.txt"), "reviewed")
            for requested_name in ("../outside.txt", str(outside), "link.txt"):
                with (
                    self.subTest(requested_name=requested_name),
                    self.assertRaises(ValueError),
                ):
                    read_report(root, requested_name)


if __name__ == "__main__":
    unittest.main()
