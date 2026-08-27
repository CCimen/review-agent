from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]


def _admin_module():
    spec = importlib.util.spec_from_file_location(
        "review_agent_admin_onboarding_test", ROOT / "tools/review_agent_admin.py"
    )
    if spec is None or spec.loader is None:
        raise AssertionError("could not load review-agent-admin")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AiOnboardingContractTests(unittest.TestCase):
    def test_generated_llm_context_and_skill_mirrors_are_current(self) -> None:
        for script in (
            "scripts/generate_llms_docs.py",
            "scripts/sync_install_skill.py",
        ):
            with self.subTest(script=script):
                subprocess.run(
                    [sys.executable, str(ROOT / script), "--check"],
                    check=True,
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                )

        short = (ROOT / "website/static/llms.txt").read_text(encoding="utf-8")
        full = (ROOT / "website/static/llms-full.txt").read_text(encoding="utf-8")
        self.assertIn("Authentication: GitHub App only", short)
        self.assertIn("AI-assisted setup", short)
        self.assertIn(
            "python3 tools/review_agent_admin.py capabilities", short
        )
        self.assertIn("python3 tools/review_agent_admin.py preflight", short)
        self.assertNotIn("`review-agent-admin capabilities`", short)
        self.assertNotIn("docs/goals/", full)
        self.assertNotIn("/Users/", full)
        self.assertNotIn("<TabItem", full)
        self.assertNotIn("import Tabs", full)
        self.assertNotIn(":::warning", full)

    def test_installation_plan_is_non_secret_and_has_no_scale_ceiling(self) -> None:
        plan = yaml.safe_load(
            (ROOT / "install/review-agent.example.yaml").read_text(encoding="utf-8")
        )
        schema = json.loads(
            (ROOT / "install/review-agent.schema.json").read_text(encoding="utf-8")
        )
        rendered = json.dumps(plan, sort_keys=True)
        for forbidden in (
            "api_key",
            "database_url",
            "password",
            "private_key",
            "secret",
            "token",
        ):
            self.assertNotIn(forbidden, rendered.casefold())

        runtime_properties = schema["properties"]["runtime"]["properties"]
        self.assertTrue(
            all("maximum" not in definition for definition in runtime_properties.values())
        )
        self.assertIs(plan["deployment"]["advisory_only"], True)
        self.assertEqual(plan["github"]["installation_mode"], "selected_repositories")
        self.assertEqual(
            {repository["trigger_mode"] for repository in plan["github"]["repositories"]},
            {"manual"},
        )

    def test_public_setup_uses_runnable_operator_commands(self) -> None:
        setup = (ROOT / "docs/AI_ASSISTED_SETUP.md").read_text(encoding="utf-8")
        skill = (ROOT / "skills/install-review-agent/SKILL.md").read_text(
            encoding="utf-8"
        )
        combined = f"{setup}\n{skill}"
        self.assertIn("python3 tools/review_agent_admin.py capabilities", combined)
        self.assertIn("python3 tools/review_agent_admin.py preflight", combined)
        self.assertIn(
            "docker compose exec hermes-review review-agent-admin doctor", setup
        )
        self.assertIn(
            "docker compose exec review-github-gateway review-agent-admin",
            skill,
        )

        parser = _admin_module()._parser()
        commands = (
            ("capabilities",),
            ("preflight",),
            ("doctor",),
            ("queues", "inspect"),
            ("installations", "list"),
            ("repositories", "list"),
            (
                "smoke-test",
                "--dry-run",
                "--repository",
                "owner/repository",
                "--pr",
                "1",
            ),
            (
                "github-app",
                "registration-url",
                "--owner",
                "owner",
                "--owner-type",
                "user",
                "--public-url",
                "https://review.example.org",
            ),
            (
                "github-app",
                "onboard",
                "owner/repository",
                "--actor",
                "github:operator",
            ),
        )
        for command in commands:
            with self.subTest(command=command):
                parser.parse_args(command)
        self.assertNotIn("preflight --json", combined)
        self.assertNotIn("doctor --json", combined)
        self.assertNotIn("installations list --json", combined)

    def test_docs_ci_runs_installation_contract(self) -> None:
        package = json.loads((ROOT / "install/package.json").read_text(encoding="utf-8"))
        command = package["scripts"]["test"]
        workflow = (ROOT / ".github/workflows/docs-check.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("validate.mjs review-agent.example.yaml", command)
        self.assertNotIn("--check-example-rejection", command)
        self.assertIn("npm --prefix install test", workflow)


if __name__ == "__main__":
    unittest.main()
