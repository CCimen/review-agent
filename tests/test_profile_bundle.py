from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
PROFILE_SOURCE = ROOT / "bootstrap" / "profiles" / "sundsvall-standard"
PLUGIN_SOURCE = ROOT / "bootstrap" / "plugins" / "review_agent_tools"
HERMES_IMAGE = "nousresearch/hermes-agent:test@sha256:" + "1" * 64


def load_installer():
    spec = importlib.util.spec_from_file_location(
        "review_agent_profile_installer", ROOT / "bootstrap" / "install.py"
    )
    if spec is None or spec.loader is None:
        raise AssertionError("could not load bootstrap installer")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.suffix not in {".pyc", ".pyo"}
    }


class ProfileBundleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.install = load_installer()

    def load_installed_contract(self, hermes_home: Path):
        with mock.patch.dict(
            os.environ,
            {"REVIEW_AGENT_HERMES_IMAGE": HERMES_IMAGE},
        ):
            return self.install.review_contract.load_installed_contract(hermes_home)

    def run_installer(
        self,
        hermes_home: Path,
        *arguments: str,
        profiles_source: Path | None = None,
        profile_environment: str | None = "sundsvall-standard",
    ) -> int:
        with (
            mock.patch.dict(
                os.environ,
                {
                    **(
                        {"REVIEW_AGENT_PROFILE": profile_environment}
                        if profile_environment is not None
                        else {}
                    ),
                    "REVIEW_AGENT_HERMES_IMAGE": HERMES_IMAGE,
                },
                clear=True,
            ),
            mock.patch.object(self.install, "HERMES_HOME", hermes_home),
            mock.patch.object(
                self.install,
                "PROFILES_SOURCE",
                profiles_source or self.install.PROFILES_SOURCE,
            ),
            mock.patch.object(
                sys,
                "argv",
                [str(ROOT / "bootstrap" / "install.py"), *arguments],
            ),
        ):
            result = self.install.main()

        return result

    def assert_profile_assets_installed(self, hermes_home: Path) -> None:
        self.assertEqual(
            (PROFILE_SOURCE / "SOUL.md").read_bytes(),
            (hermes_home / "SOUL.md").read_bytes(),
        )
        self.assertEqual(
            (PROFILE_SOURCE / "workspace" / "AGENTS.md").read_bytes(),
            (hermes_home / "workspace" / "AGENTS.md").read_bytes(),
        )
        self.assertEqual(
            tree_bytes(PROFILE_SOURCE / "skills" / "review-agent-pr"),
            tree_bytes(hermes_home / "skills" / "review-agent-pr"),
        )
        self.assertEqual(
            tree_bytes(PROFILE_SOURCE / "skills" / "ponytail"),
            tree_bytes(hermes_home / "skills" / "ponytail"),
        )

    def test_installer_copies_fixed_profile_and_plugin_byte_for_byte(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            hermes_home = Path(temp) / "hermes-home"
            hermes_home.mkdir()
            original_config = (
                "{\n"
                '  "model": {"default": "stale-model"},\n'
                '  "operator": {"keep": true}\n'
                "}\n"
            )
            (hermes_home / "config.yaml").write_text(
                original_config,
                encoding="utf-8",
            )
            first = self.run_installer(hermes_home)

            self.assertEqual(0, first)
            self.assert_profile_assets_installed(hermes_home)
            self.assertEqual(
                tree_bytes(PLUGIN_SOURCE),
                tree_bytes(hermes_home / "plugins" / "review_agent_tools"),
            )
            installed_config_bytes = (hermes_home / "config.yaml").read_bytes()
            self.assertEqual(
                (self.install.SOURCE / "config.yaml").read_bytes(),
                installed_config_bytes,
            )
            installed_config = installed_config_bytes.decode("utf-8")
            self.assertEqual(
                ["review-agent-tools"],
                self.install.load_yaml(hermes_home / "config.yaml")["plugins"][
                    "enabled"
                ],
            )
            self.assertNotIn("stale-model", installed_config)
            self.assertNotIn("operator:\n", installed_config)
            self.assertEqual(
                original_config,
                (hermes_home / "config.yaml.before-review-agent").read_text(
                    encoding="utf-8"
                ),
            )
            self.assertTrue((hermes_home / ".no-bundled-skills").exists())
            receipt = json.loads(
                (hermes_home / ".review-agent-profile.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual("sundsvall-standard", receipt["contract"]["profile"])
            self.assertEqual(["review-agent-pr", "ponytail"], receipt["skills"])
            self.assertEqual(2, receipt["schema_version"])
            self.assertEqual(HERMES_IMAGE, receipt["contract"]["hermes_image"])
            self.assertTrue(receipt["files"])
            self.assertNotIn("files", receipt["contract"])
            (hermes_home / "skills" / "review-agent-pr" / "stale.txt").write_text(
                "stale", encoding="utf-8"
            )
            (hermes_home / "plugins" / "review_agent_tools" / "stale.py").write_text(
                "stale = True\n", encoding="utf-8"
            )

            repeated = self.run_installer(
                hermes_home,
            )

            self.assertEqual(0, repeated)
            self.assert_profile_assets_installed(hermes_home)
            self.assertEqual(
                tree_bytes(PLUGIN_SOURCE),
                tree_bytes(hermes_home / "plugins" / "review_agent_tools"),
            )

    def test_receipt_detects_installed_behavior_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            hermes_home = Path(temp) / "hermes-home"
            installed = self.run_installer(hermes_home)

            self.assertEqual(0, installed)
            contract = self.load_installed_contract(hermes_home)
            self.assertEqual("sundsvall-standard", contract.profile)

            (hermes_home / "SOUL.md").write_text(
                "# Changed outside the selected profile\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(
                self.install.review_contract.ReviewContractError,
                "do not match the receipt",
            ):
                self.load_installed_contract(hermes_home)

    def test_packaged_and_installed_files_resolve_to_the_same_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            hermes_home = Path(temp) / "hermes-home"
            installed = self.run_installer(hermes_home)
            self.assertEqual(0, installed)

            with mock.patch.dict(
                os.environ,
                {"REVIEW_AGENT_HERMES_IMAGE": HERMES_IMAGE},
            ):
                packaged_contract = (
                    self.install.review_contract.load_packaged_contract(
                        "sundsvall-standard",
                        self.install.SOURCE,
                    )
                )

            self.assertEqual(self.load_installed_contract(hermes_home), packaged_contract)

    def test_redeploy_restores_source_controlled_soul_and_agents(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            hermes_home = Path(temp) / "hermes-home"

            installed = self.run_installer(hermes_home)
            self.assertEqual(0, installed)

            soul_target = hermes_home / "SOUL.md"
            agents_target = hermes_home / "workspace" / "AGENTS.md"
            soul_target.write_bytes(b"operator soul\n")
            agents_target.write_bytes(b"operator agents\n")

            repeated = self.run_installer(hermes_home)

            self.assertEqual(0, repeated)
            self.assertEqual(
                (PROFILE_SOURCE / "SOUL.md").read_bytes(), soul_target.read_bytes()
            )
            self.assertEqual(
                (PROFILE_SOURCE / "workspace" / "AGENTS.md").read_bytes(),
                agents_target.read_bytes(),
            )
            self.assertEqual(
                b"operator agents\n",
                (hermes_home / "workspace" / "AGENTS.md.before-review-agent").read_bytes(),
            )

    def test_selected_profile_survives_redeploy_and_removes_unlisted_skills(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            profiles = root / "profiles"
            shutil.copytree(PROFILE_SOURCE, profiles / "sundsvall-standard")
            custom = profiles / "team-standard"
            shutil.copytree(profiles / "sundsvall-standard", custom)
            (custom / "SOUL.md").write_text(
                "# Team reviewer\n\nAnswer review explanations in Swedish.\n",
                encoding="utf-8",
            )
            (custom / "workspace" / "AGENTS.md").write_text(
                "# Team review rules\n\nUse concise Swedish explanations.\n",
                encoding="utf-8",
            )
            (custom / "profile.json").write_text(
                json.dumps({"schema_version": 1, "skills": ["review-agent-pr"]}),
                encoding="utf-8",
            )
            hermes_home = root / "hermes-home"

            initial = self.run_installer(
                hermes_home,
                profiles_source=profiles,
            )
            selected = self.run_installer(
                hermes_home,
                "--profile",
                "team-standard",
                profiles_source=profiles,
                profile_environment=None,
            )
            repeated = self.run_installer(
                hermes_home,
                profiles_source=profiles,
                profile_environment=None,
            )

            self.assertEqual((0, 0, 0), (initial, selected, repeated))
            self.assertIn(
                "Answer review explanations in Swedish.",
                (hermes_home / "SOUL.md").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "Use concise Swedish explanations.",
                (hermes_home / "workspace" / "AGENTS.md").read_text(
                    encoding="utf-8"
                ),
            )
            self.assertFalse((hermes_home / "skills" / "ponytail").exists())
            receipt = json.loads(
                (hermes_home / ".review-agent-profile.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual("team-standard", receipt["contract"]["profile"])

    def test_profile_contract_rejects_unknown_and_runtime_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            profiles = Path(temp) / "profiles"
            custom = profiles / "unsafe-profile"
            shutil.copytree(PROFILE_SOURCE, custom)
            manifest = json.loads(
                (custom / "profile.json").read_text(encoding="utf-8")
            )
            manifest["platform_toolsets"] = {"webhook": ["terminal"]}
            (custom / "profile.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )

            with mock.patch.object(self.install, "PROFILES_SOURCE", profiles):
                with self.assertRaisesRegex(
                    self.install.ProfileError,
                    "may define only schema_version and skills",
                ):
                    self.install.load_profile(
                        "unsafe-profile", required_skills=()
                    )
                with self.assertRaisesRegex(
                    self.install.ProfileError, "lower-case words"
                ):
                    self.install.load_profile(
                        "../unsafe-profile", required_skills=()
                    )
                with self.assertRaisesRegex(
                    self.install.ProfileError, "unknown review profile"
                ):
                    self.install.load_profile(
                        "missing-profile", required_skills=()
                    )

                (custom / "profile.json").write_text(
                    json.dumps({"schema_version": 1, "skills": ["ponytail"]}),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(
                    self.install.ProfileError, "missing required review skill"
                ):
                    self.install.load_profile(
                        "unsafe-profile", required_skills=("review-agent-pr",)
                    )

    def test_installer_rejects_missing_review_skill_before_writing_home(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            profiles = root / "profiles"
            selected = profiles / "sundsvall-standard"
            shutil.copytree(PROFILE_SOURCE, selected)
            (selected / "profile.json").write_text(
                json.dumps({"schema_version": 1, "skills": ["ponytail"]}),
                encoding="utf-8",
            )
            hermes_home = root / "hermes-home"

            with self.assertRaises(SystemExit) as raised:
                self.run_installer(
                    hermes_home,
                    profiles_source=profiles,
                )

            self.assertEqual(2, raised.exception.code)
            self.assertFalse(hermes_home.exists())

    def test_explicit_profile_recovers_from_an_invalid_installed_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            hermes_home = Path(temp) / "hermes-home"
            hermes_home.mkdir()
            (hermes_home / ".review-agent-profile.json").write_text(
                '{"profile": "truncated"}', encoding="utf-8"
            )

            completed = self.run_installer(
                hermes_home,
                "--profile",
                "sundsvall-standard",
                profile_environment=None,
            )

            self.assertEqual(0, completed)
            receipt = json.loads(
                (hermes_home / ".review-agent-profile.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual("sundsvall-standard", receipt["contract"]["profile"])


if __name__ == "__main__":
    unittest.main()
