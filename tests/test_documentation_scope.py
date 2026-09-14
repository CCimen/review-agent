from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = ROOT / "bootstrap" / "plugins"
sys.path.insert(0, str(PACKAGE_ROOT))

from review_agent_tools import documentation_scope  # noqa: E402
from review_agent_tools.domain import documentation_policy  # noqa: E402


POLICY = """version = 1
[[area]]
id = "configuration"
sources = ["src/**"]
documents = ["docs/configuration.md"]
intent = "Keep settings accurate."
[[ignore_changes]]
paths = ["tests/fixtures/**"]
reason = "Internal fixtures are not shipped."
"""


class DocumentationScopeTests(unittest.TestCase):
    def test_git_invocation_fixes_repository_authority_and_denies_transport(self) -> None:
        inherited = {
            "GIT_DIR": "/other/repository",
            "GIT_WORK_TREE": "/other/tree",
            "GIT_COMMON_DIR": "/other/common",
            "GIT_INDEX_FILE": "/other/index",
            "GIT_OBJECT_DIRECTORY": "/other/objects",
            "GIT_ALTERNATE_OBJECT_DIRECTORIES": "/other/alternates",
        }
        with (
            mock.patch.dict(os.environ, inherited),
            mock.patch.object(
                documentation_scope.subprocess,
                "Popen",
                side_effect=OSError("stopped before execution"),
            ) as popen,
            self.assertRaises(documentation_scope.DocumentationScopeError),
        ):
            documentation_scope.preview_repository_scope(
                Path("."), base="HEAD", head="HEAD"
            )

        arguments = popen.call_args.args[0]
        environment = popen.call_args.kwargs["env"]
        self.assertEqual(arguments[1:3], ("--literal-pathspecs", "--no-replace-objects"))
        self.assertEqual(environment["GIT_NO_LAZY_FETCH"], "1")
        self.assertEqual(environment["GIT_ALLOW_PROTOCOL"], "")
        for name in inherited:
            self.assertNotIn(name, environment)

    def test_explicit_mapping_wins_over_change_exclusion(self) -> None:
        policy = documentation_policy.parse_policy(
            POLICY.replace(
                'paths = ["tests/fixtures/**"]', 'paths = ["src/**"]'
            )
        )

        scope = documentation_scope.plan_scope(
            base_sha="a" * 40,
            comparison_sha="a" * 40,
            head_sha="b" * 40,
            changed_files=(documentation_scope.ChangedPath("M", "src/config.py"),),
            policy=policy,
            policy_hash="sha256:" + "c" * 64,
        )

        self.assertEqual(scope.areas[0].matched_paths, ("src/config.py",))
        self.assertEqual(scope.exclusions, ())

    def _git(self, root: Path, *arguments: str) -> str:
        return subprocess.run(
            ("git", "-C", str(root), *arguments), check=True,
            stdout=subprocess.PIPE, text=True,
        ).stdout.strip()

    def _commit(self, root: Path, message: str) -> str:
        self._git(root, "add", "-A")
        self._git(
            root, "-c", "user.name=Test", "-c", "user.email=test@example.com",
            "commit", "-m", message,
        )
        return self._git(root, "rev-parse", "HEAD")

    def test_preview_uses_committed_refs_maps_renames_and_ignores_dirty_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._git(root, "init", "-q")
            (root / ".review-agent").mkdir()
            (root / ".review-agent/documentation.toml").write_text(POLICY)
            (root / "src").mkdir()
            (root / "src/config.py").write_text("OLD = True\n")
            (root / "docs").mkdir()
            (root / "docs/configuration.md").write_text("# Configuration\n")
            base = self._commit(root, "base")
            (root / "src/config.py").rename(root / "src/settings\nname.py")
            head = self._commit(root, "rename")

            first = documentation_scope.preview_repository_scope(
                root, base=base, head=head
            )
            (root / ".review-agent/documentation.toml").write_text("invalid dirty tree")
            second = documentation_scope.preview_repository_scope(
                root, base=base, head=head
            )

            self.assertEqual(first.to_json_obj(), second.to_json_obj())
            self.assertEqual(first.documents, ("docs/configuration.md",))
            self.assertEqual(first.areas[0].matched_paths, ("src/settings\nname.py", "src/config.py"))
            self.assertEqual(first.changed_files[0].previous_path, "src/config.py")

    def test_base_rules_win_over_invalid_head_policy_and_exclusions_are_explained(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._git(root, "init", "-q")
            (root / ".review-agent").mkdir()
            (root / ".review-agent/documentation.toml").write_text(POLICY)
            (root / "docs").mkdir()
            (root / "docs/configuration.md").write_text("# Configuration\n")
            (root / "tests/fixtures").mkdir(parents=True)
            (root / "tests/fixtures/example.txt").write_text("old\n")
            base = self._commit(root, "base")
            (root / "tests/fixtures/example.txt").write_text("new\n")
            (root / ".review-agent/documentation.toml").write_text("version = 2\narea = []\n")
            head = self._commit(root, "proposal")

            scope = documentation_scope.preview_repository_scope(root, base=base, head=head)

            self.assertTrue(scope.active_policy)
            self.assertEqual(scope.proposal_status, "invalid")
            self.assertEqual(scope.exclusions[0].reason, "Internal fixtures are not shipped.")
            self.assertIn(".review-agent/documentation.toml", scope.unmapped_paths)

    def test_valid_base_scope_survives_an_oversized_head_policy_object(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._git(root, "init", "-q")
            (root / ".review-agent").mkdir()
            policy_path = root / ".review-agent/documentation.toml"
            policy_path.write_text(POLICY)
            (root / "docs").mkdir()
            (root / "docs/configuration.md").write_text("# Configuration\n")
            (root / "src").mkdir()
            (root / "src/config.py").write_text("OLD = True\n")
            base = self._commit(root, "base")
            (root / "src/config.py").write_text("NEW = True\n")
            policy_path.write_bytes(b"x" * (documentation_policy.MAX_CONFIG_BYTES + 1))
            head = self._commit(root, "oversized proposal")

            scope = documentation_scope.preview_repository_scope(root, base=base, head=head)

            self.assertEqual(scope.status, "scoped")
            self.assertTrue(scope.active_policy)
            self.assertEqual(scope.proposal_status, "invalid")
            self.assertEqual(scope.documents, ("docs/configuration.md",))

    def test_symlink_policy_proposal_does_not_replace_a_valid_base(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._git(root, "init", "-q")
            (root / ".review-agent").mkdir()
            policy_path = root / ".review-agent/documentation.toml"
            policy_path.write_text(POLICY)
            (root / "docs").mkdir()
            (root / "docs/configuration.md").write_text("# Configuration\n")
            base = self._commit(root, "base")
            policy_path.unlink()
            policy_path.symlink_to("elsewhere.toml")
            head = self._commit(root, "symlink proposal")

            scope = documentation_scope.preview_repository_scope(root, base=base, head=head)

            self.assertEqual(scope.status, "scoped")
            self.assertTrue(scope.active_policy)
            self.assertEqual(scope.proposal_status, "invalid")

    def test_oversized_first_adoption_is_an_invalid_inactive_proposal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._git(root, "init", "-q")
            (root / "README.md").write_text("# Repository\n")
            base = self._commit(root, "base")
            (root / ".review-agent").mkdir()
            (root / ".review-agent/documentation.toml").write_bytes(
                b"x" * (documentation_policy.MAX_CONFIG_BYTES + 1)
            )
            head = self._commit(root, "invalid first adoption")

            scope = documentation_scope.preview_repository_scope(root, base=base, head=head)

            self.assertEqual(scope.status, "not_configured")
            self.assertFalse(scope.active_policy)
            self.assertEqual(scope.proposal_status, "invalid")

    def test_head_only_policy_is_a_proposal_without_active_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._git(root, "init", "-q")
            (root / "docs").mkdir()
            (root / "docs/configuration.md").write_text("# Configuration\n")
            base = self._commit(root, "base")
            (root / ".review-agent").mkdir()
            (root / ".review-agent/documentation.toml").write_text(POLICY)
            (root / "src").mkdir()
            (root / "src/config.py").write_text("VALUE = 1\n")
            head = self._commit(root, "add policy")

            scope = documentation_scope.preview_repository_scope(root, base=base, head=head)

            self.assertEqual(scope.status, "not_configured")
            self.assertFalse(scope.active_policy)
            self.assertEqual(scope.proposal_status, "valid")
            self.assertFalse(scope.semantic_inference_used)
            self.assertEqual(scope.documents, ("docs/configuration.md",))

    def test_exact_document_path_with_glob_metacharacters_is_read_literally(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._git(root, "init", "-q")
            (root / ".review-agent").mkdir()
            (root / ".review-agent/documentation.toml").write_text(
                POLICY.replace("docs/configuration.md", "docs/guide[1].md")
            )
            (root / "docs").mkdir()
            (root / "docs/guide[1].md").write_text("# Literal path\n")
            (root / "docs/guide1.md").write_text("# Pathspec decoy\n")
            base = self._commit(root, "base")

            scope = documentation_scope.preview_repository_scope(
                root, base=base, head=base
            )

            self.assertEqual(scope.status, "scoped")
            self.assertTrue(scope.active_policy)

    def test_invalid_base_policy_returns_an_invalid_configuration_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._git(root, "init", "-q")
            (root / ".review-agent").mkdir()
            (root / ".review-agent/documentation.toml").write_text(
                "version = 2\narea = []\n"
            )
            base = self._commit(root, "invalid policy")

            scope = documentation_scope.preview_repository_scope(
                root, base=base, head=base
            )

            self.assertEqual(scope.status, "invalid_configuration")
            self.assertFalse(scope.active_policy)
            self.assertTrue(scope.incomplete_reasons)

    def test_changed_document_deletion_selects_its_area_and_cli_explains_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._git(root, "init", "-q")
            (root / ".review-agent").mkdir()
            (root / ".review-agent/documentation.toml").write_text(POLICY)
            (root / "docs").mkdir()
            document = root / "docs/configuration.md"
            document.write_text("# Configuration\n")
            base = self._commit(root, "base")
            document.unlink()
            head = self._commit(root, "delete document")

            command = subprocess.run(
                (
                    sys.executable,
                    str(ROOT / "tools/review_agent_admin.py"),
                    "repository-context", "docs-scope", str(root),
                    "--base", base, "--head", head,
                ),
                check=True,
                stdout=subprocess.PIPE,
                text=True,
                env={**os.environ, "GIT_EXTERNAL_DIFF": "must-not-run"},
            )
            scope = json.loads(command.stdout)

            self.assertEqual(scope["status"], "scoped")
            self.assertEqual(scope["documents"], ["docs/configuration.md"])
            self.assertEqual(scope["areas"][0]["matched_paths"], ["docs/configuration.md"])

    def test_git_output_is_stopped_at_the_protocol_bound(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._git(root, "init", "-q")
            oversized = root / "oversized.bin"
            oversized.write_bytes(b"x" * (documentation_scope.MAX_GIT_OUTPUT_BYTES + 1))
            self._commit(root, "oversized blob")
            object_id = self._git(root, "rev-parse", "HEAD:oversized.bin")

            with self.assertRaisesRegex(
                documentation_scope.DocumentationScopeError,
                "git_output_too_large",
            ):
                runner = getattr(documentation_scope, "_run_git")
                runner(root, ("cat-file", "blob", object_id))


if __name__ == "__main__":
    unittest.main()
