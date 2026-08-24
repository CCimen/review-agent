from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
PROFILE_SOURCE = ROOT / "bootstrap" / "profiles" / "sundsvall-standard"
PLUGIN_SOURCE = ROOT / "bootstrap" / "plugins" / "review_agent_tools"


def load_installer():
    spec = importlib.util.spec_from_file_location(
        "review_agent_profile_installer", ROOT / "bootstrap" / "install.py"
    )
    if spec is None or spec.loader is None:
        raise AssertionError("could not load bootstrap installer")
    module = importlib.util.module_from_spec(spec)
    previous_yaml = sys.modules.get("yaml")
    yaml_stub = types.ModuleType("yaml")
    yaml_stub.safe_load = json.loads
    yaml_stub.safe_dump = lambda value, **_kwargs: json.dumps(value, indent=2) + "\n"
    sys.modules["yaml"] = yaml_stub
    try:
        spec.loader.exec_module(module)
    finally:
        if previous_yaml is None:
            sys.modules.pop("yaml", None)
        else:
            sys.modules["yaml"] = previous_yaml
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
        self.managed_config = {
            "model": {"provider": "openai-codex", "default": "gpt-5.6-sol"},
            "agent": {"reasoning_effort": "xhigh"},
            "platforms": {
                "webhook": {
                    "extra": {
                        "routes": {
                            "review-agent": {"skills": ["review-agent-pr"]}
                        }
                    }
                }
            },
        }

    def run_installer(
        self,
        hermes_home: Path,
        *arguments: str,
        existing_config: dict[str, object] | None = None,
        profiles_source: Path | None = None,
        profile_environment: str | None = "sundsvall-standard",
    ) -> tuple[int, mock.Mock, mock.Mock]:
        existing_config = existing_config or {}

        def load_config(path: Path) -> dict[str, object]:
            if path == hermes_home / "config.yaml":
                return existing_config
            if path == self.install.SOURCE / "config.yaml":
                return self.managed_config
            raise AssertionError(f"unexpected config path: {path}")

        connection = mock.Mock()
        connect = mock.Mock(return_value=connection)
        memory_db = types.ModuleType("memory_db")
        memory_db.connect = connect
        completed = subprocess.CompletedProcess([], 0, stdout="", stderr="")

        with (
            mock.patch.dict(
                os.environ,
                (
                    {"REVIEW_AGENT_PROFILE": profile_environment}
                    if profile_environment is not None
                    else {}
                ),
                clear=True,
            ),
            mock.patch.object(self.install, "HERMES_HOME", hermes_home),
            mock.patch.object(
                self.install,
                "PROFILES_SOURCE",
                profiles_source or self.install.PROFILES_SOURCE,
            ),
            mock.patch.object(self.install, "load_yaml", side_effect=load_config),
            mock.patch.object(
                self.install.subprocess,
                "run",
                return_value=completed,
            ) as run,
            mock.patch.dict(sys.modules, {"memory_db": memory_db}),
            mock.patch.object(
                sys,
                "argv",
                [str(ROOT / "bootstrap" / "install.py"), *arguments],
            ),
        ):
            result = self.install.main()

        connection.close.assert_called_once_with()
        return result, run, connect

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
            existing_config = {
                "model": {"default": "stale-model"},
                "operator": {"keep": True},
            }

            first, enable, _connect = self.run_installer(
                hermes_home,
                existing_config=existing_config,
            )

            self.assertEqual(0, first)
            self.assert_profile_assets_installed(hermes_home)
            self.assertEqual(
                tree_bytes(PLUGIN_SOURCE),
                tree_bytes(hermes_home / "plugins" / "review_agent_tools"),
            )
            installed_config = json.loads(
                (hermes_home / "config.yaml").read_text(encoding="utf-8")
            )
            self.assertEqual("gpt-5.6-sol", installed_config["model"]["default"])
            self.assertTrue(installed_config["operator"]["keep"])
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
            self.assertEqual("sundsvall-standard", receipt["profile"])
            self.assertEqual(["review-agent-pr", "ponytail"], receipt["skills"])
            enable.assert_called_once_with(
                ["hermes", "plugins", "enable", "review-agent-tools"],
                check=False,
                text=True,
                capture_output=True,
                env=mock.ANY,
            )
            self.assertEqual(
                str(hermes_home),
                enable.call_args.kwargs["env"]["HERMES_HOME"],
            )

            (hermes_home / "skills" / "review-agent-pr" / "stale.txt").write_text(
                "stale", encoding="utf-8"
            )
            (hermes_home / "plugins" / "review_agent_tools" / "stale.py").write_text(
                "stale = True\n", encoding="utf-8"
            )

            repeated, repeated_enable, _connect = self.run_installer(
                hermes_home,
                existing_config=installed_config,
            )

            self.assertEqual(0, repeated)
            self.assert_profile_assets_installed(hermes_home)
            self.assertEqual(
                tree_bytes(PLUGIN_SOURCE),
                tree_bytes(hermes_home / "plugins" / "review_agent_tools"),
            )
            repeated_enable.assert_called_once()

    def test_preserve_soul_and_force_agents_keep_existing_flag_behavior(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            hermes_home = Path(temp) / "hermes-home"

            installed, _enable, _connect = self.run_installer(hermes_home)
            self.assertEqual(0, installed)

            soul_target = hermes_home / "SOUL.md"
            agents_target = hermes_home / "workspace" / "AGENTS.md"
            soul_target.write_bytes(b"operator soul\n")
            agents_target.write_bytes(b"operator agents\n")

            preserved, _enable, _connect = self.run_installer(
                hermes_home,
                "--preserve-soul",
            )

            self.assertEqual(0, preserved)
            self.assertEqual(b"operator soul\n", soul_target.read_bytes())
            self.assertEqual(b"operator agents\n", agents_target.read_bytes())

            forced, _enable, _connect = self.run_installer(
                hermes_home,
                "--preserve-soul",
                "--force-agents",
            )

            self.assertEqual(0, forced)
            self.assertEqual(b"operator soul\n", soul_target.read_bytes())
            self.assertEqual(
                (PROFILE_SOURCE / "workspace" / "AGENTS.md").read_bytes(),
                agents_target.read_bytes(),
            )
            self.assertEqual(
                b"operator agents\n",
                (hermes_home / "workspace" / "AGENTS.md.before-review-agent").read_bytes(),
            )

    def test_skip_plugin_enable_still_installs_the_complete_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            hermes_home = Path(temp) / "hermes-home"

            completed, enable, _connect = self.run_installer(
                hermes_home,
                "--skip-plugin-enable",
            )

            self.assertEqual(0, completed)
            enable.assert_not_called()
            self.assert_profile_assets_installed(hermes_home)
            self.assertEqual(
                tree_bytes(PLUGIN_SOURCE),
                tree_bytes(hermes_home / "plugins" / "review_agent_tools"),
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

            initial, _enable, _connect = self.run_installer(
                hermes_home,
                profiles_source=profiles,
            )
            selected, _enable, _connect = self.run_installer(
                hermes_home,
                "--profile",
                "team-standard",
                profiles_source=profiles,
                profile_environment=None,
            )
            repeated, _enable, _connect = self.run_installer(
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
            self.assertEqual("team-standard", receipt["profile"])

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
                    self.install.ProfileError, "missing managed route skill"
                ):
                    self.install.load_profile(
                        "unsafe-profile", required_skills=("review-agent-pr",)
                    )

    def test_installer_rejects_missing_route_skill_before_writing_home(self) -> None:
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

            completed, _enable, _connect = self.run_installer(
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
            self.assertEqual("sundsvall-standard", receipt["profile"])


if __name__ == "__main__":
    unittest.main()
