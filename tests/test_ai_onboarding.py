from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import unittest

import yaml

from scripts.validate_release_tag import is_release_tag


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


def _llms_generator_module():
    scripts_path = str(ROOT / "scripts")
    sys.path.insert(0, scripts_path)
    try:
        spec = importlib.util.spec_from_file_location(
            "review_agent_llms_generator_test",
            ROOT / "scripts/generate_llms_docs.py",
        )
        if spec is None or spec.loader is None:
            raise AssertionError("could not load LLM documentation generator")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(scripts_path)


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
        self.assertIn("REVIEW_AGENT_MODEL_PROVIDER", short)
        self.assertIn("use Deploy when the source revision changes", short)
        self.assertIn("AI-assisted setup", short)
        self.assertIn("## Coding-agent handoff", short)
        self.assertIn("check out the exact runtime release named above", short)
        self.assertIn("skills/install-review-agent/SKILL.md", short)
        self.assertIn("do not install a floating global copy", short)
        self.assertIn("install `requirements.txt`", short)
        self.assertIn(".venv/bin/python tools/review_agent_admin.py capabilities", short)
        self.assertIn(".venv/bin/python tools/review_agent_admin.py preflight", short)
        self.assertIn("review-agent-memory quality --days 30", short)
        self.assertIn("explicit operator triage", short)
        self.assertNotIn("`review-agent-admin capabilities`", short)
        self.assertNotIn("docs/goals/", full)
        self.assertNotIn("/Users/", full)
        self.assertNotIn("<TabItem", full)
        self.assertNotIn("import Tabs", full)
        self.assertNotIn(":::warning", full)
        release_lines = [
            line for line in short.splitlines() if line.startswith("Release state: ")
        ]
        self.assertEqual(len(release_lines), 1)
        release_tag = release_lines[0].removeprefix("Release state: ")
        self.assertTrue(is_release_tag(release_tag))
        self.assertIn(
            f"Runtime release selected by `llms.txt`: {release_tag}",
            full,
        )
        self.assertNotIn("Source: https://github.com/CCimen/review-agent/blob/", full)
        self.assertNotIn("Review Agent revision:", full)

        generator = _llms_generator_module()
        for invalid_tag in (
            "v01.2.3",
            "v1.2.3-01",
            "v1.2.3-.rc",
            "v1.2.3-rc..1",
        ):
            with self.subTest(invalid_tag=invalid_tag):
                with self.assertRaises(generator.GenerationError):
                    generator.generate(revision=invalid_tag)

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
        github_app = (ROOT / "docs/GITHUB_APP_PILOT.md").read_text(
            encoding="utf-8"
        )
        skill = (ROOT / "skills/install-review-agent/SKILL.md").read_text(
            encoding="utf-8"
        )
        homepage = (ROOT / "website/src/pages/index.tsx").read_text(encoding="utf-8")
        combined = f"{setup}\n{skill}"
        self.assertIn("skills/install-review-agent/SKILL.md", setup)
        self.assertIn("https://ccimen.github.io/review-agent/llms.txt", combined)
        self.assertIn(".claude/skills/install-review-agent/SKILL.md", skill)
        self.assertIn(".agents/skills/install-review-agent/SKILL.md", skill)
        self.assertIn("python3 -m venv .venv", combined)
        self.assertIn(
            ".venv/bin/python tools/review_agent_admin.py capabilities", combined
        )
        self.assertIn(
            ".venv/bin/python tools/review_agent_admin.py preflight", combined
        )
        self.assertIn("--homepage-url", combined)
        self.assertIn(
            "docker compose exec hermes-review review-agent-admin doctor", setup
        )
        self.assertIn(
            "docker compose exec review-github-gateway review-agent-admin installations list",
            setup,
        )
        self.assertIn(
            "docker compose exec review-github-gateway review-agent-admin repositories list",
            setup,
        )
        self.assertIn(
            "docker compose exec review-github-gateway review-agent-admin",
            skill,
        )
        release_lock = setup.split("## 1. Lock the source release", 1)[1].split(
            "## 2. Record the non-secret decisions", 1
        )[0]
        self.assertIn("sed -n 's/^Release state: //p'", release_lock)
        self.assertIn('git clone --branch "$REVIEW_AGENT_RELEASE"', release_lock)
        self.assertIn(
            'test "$(git -C review-agent rev-parse HEAD)" = \\\n'
            '  "$(git -C review-agent rev-parse "${REVIEW_AGENT_RELEASE}^{commit}")"',
            release_lock,
        )
        self.assertNotIn("blob/main", setup)
        openshift_checks = setup.split(
            '<TabItem value="openshift" label="OpenShift">', 1
        )[1].split("</TabItem>", 1)[0]
        normalized_openshift_checks = " ".join(
            openshift_checks.replace("\\\n", " ").split()
        )
        for workload, command in (
            ("hermes-review", "review-agent-admin doctor"),
            ("hermes-review", "review-agent-admin queues inspect"),
            ("review-agent-github-gateway", "review-agent-admin installations list"),
            ("review-agent-github-gateway", "review-agent-admin repositories list"),
            (
                "hermes-review",
                "review-agent-admin smoke-test --dry-run --repository <owner/repository> --pr <number>",
            ),
        ):
            with self.subTest(workload=workload, command=command):
                self.assertIn(
                    f"oc rsh deployment/{workload} {command}",
                    normalized_openshift_checks,
                )
        recovery = github_app.split(
            "## Recover access after a repository or profile change", 1
        )[1].split("## Troubleshooting", 1)[0]
        normalized_recovery = " ".join(recovery.replace("\\\n", " ").split())
        self.assertIn(
            "oc rsh deployment/review-agent-github-gateway "
            "review-agent-admin github-app onboard <owner/repository>",
            normalized_recovery,
        )
        self.assertIn(
            "oc rsh deployment/hermes-review review-agent-admin doctor",
            normalized_recovery,
        )
        normalized_setup = " ".join(setup.replace("\\\n", " ").split())
        self.assertIn(
            "oc rsh deployment/hermes-review "
            "review-agent-memory quality --days 30 --repo <owner/repository>",
            normalized_setup,
        )
        self.assertIn("removed and later reselected", skill)
        self.assertIn(
            "oc rsh deployment/review-agent-github-gateway review-agent-admin",
            skill,
        )
        local_build = homepage.split(
            '<TabItem value="compose" label="Local Compose build" default>', 1
        )[1].split("</TabItem>", 1)[0]
        self.assertIn("docker compose up -d --build", local_build)
        self.assertIn("attested release image", local_build)
        self.assertIn("IMAGE-DIGESTS.txt", local_build)
        self.assertEqual(homepage.count("--build"), local_build.count("--build"))
        self.assertNotIn('label="Compose / Dokploy"', homepage)
        self.assertIn("review-agent-memory quality --days 30", skill)
        self.assertIn("Do not classify feedback for the operator", skill)
        self.assertIn(
            "Never read, quote, or summarize the raw export",
            " ".join(skill.split()),
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
                "--homepage-url",
                "https://docs.example.org/review-agent/",
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
