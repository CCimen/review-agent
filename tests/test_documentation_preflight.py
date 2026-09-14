from __future__ import annotations

import hashlib
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bootstrap" / "plugins"))

from review_agent_tools.documentation_preflight import deterministic_outcome, policy_scope  # noqa: E402
from review_agent_tools.documentation_scope import ChangedPath  # noqa: E402
from review_agent_tools.domain.documentation_review import DocumentationOutcome  # noqa: E402
from review_agent_tools.github.source import ReviewPolicySource  # noqa: E402


POLICY = '''version = 1
[[area]]
id = "api"
sources = ["src/api/**"]
documents = ["docs/api.md"]
intent = "Describe the public API."
[[ignore_changes]]
paths = ["tests/**"]
reason = "Tests do not change the public contract."
'''


def policy(content: str | None, revision: str) -> ReviewPolicySource:
    return ReviewPolicySource(
        "ok" if content is not None else "not_found_at_revision", revision, content,
        "d" * 40 if content is not None else None,
        hashlib.sha256(content.encode()).hexdigest() if content is not None else None,
    )


class DocumentationPreflightTests(unittest.TestCase):
    def scope(self, path: str, *, base: str | None = POLICY,
              head: str | None = POLICY, complete: bool = True):
        return policy_scope(
            base_sha="a" * 40, comparison_sha="c" * 40, head_sha="b" * 40,
            changed_files=(ChangedPath("modified", path),), inventory_complete=complete,
            base=policy(base, "a" * 40), head=policy(head, "b" * 40),
        )

    def test_only_complete_explicit_exclusions_skip_inference(self) -> None:
        self.assertEqual(deterministic_outcome(self.scope("tests/api.py")), DocumentationOutcome.NOT_NEEDED)
        self.assertIsNone(deterministic_outcome(self.scope("tests/api.py", complete=False)))
        self.assertIsNone(deterministic_outcome(self.scope("src/api/users.py")))
        self.assertIsNone(deterministic_outcome(self.scope("src/unknown.py")))

    def test_proposed_rules_preview_without_becoming_authority(self) -> None:
        proposed = self.scope("src/api/users.py", base=None)
        self.assertEqual(deterministic_outcome(proposed), DocumentationOutcome.NOT_CONFIGURED)
        self.assertFalse(proposed.active_policy)
        self.assertEqual(proposed.documents, ("docs/api.md",))
        removed = self.scope("src/api/users.py", head=None)
        self.assertTrue(removed.active_policy)
        self.assertEqual(removed.proposal_status, "removed")
        self.assertEqual(removed.documents, ("docs/api.md",))

    def test_invalid_proposal_does_not_hide_valid_base_rules(self) -> None:
        scope = self.scope("src/api/users.py", head="not valid TOML")
        self.assertTrue(scope.active_policy)
        self.assertEqual(scope.proposal_status, "invalid")
        self.assertEqual(scope.documents, ("docs/api.md",))
        invalid_base = self.scope("src/api/users.py", base="not valid TOML")
        self.assertEqual(deterministic_outcome(invalid_base), DocumentationOutcome.INVALID_CONFIGURATION)


if __name__ == "__main__":
    unittest.main()
