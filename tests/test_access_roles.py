from __future__ import annotations

import itertools
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bootstrap" / "plugins"))

from review_agent_tools.domain.access import Role  # noqa: E402


class PlatformRoleTests(unittest.TestCase):
    def test_the_highest_account_flag_decides_the_role(self) -> None:
        for owner, superuser, viewer in itertools.product((False, True), repeat=3):
            expected = (
                Role.OWNER if owner
                else Role.ADMIN if superuser
                else Role.VIEWER if viewer
                else Role.MEMBER
            )
            with self.subTest(owner=owner, superuser=superuser, viewer=viewer):
                self.assertIs(
                    expected,
                    Role.from_flags(
                        platform_owner=owner, superuser=superuser, global_viewer=viewer
                    ),
                )


if __name__ == "__main__":
    unittest.main()
