from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path
from typing import cast

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bootstrap" / "plugins"))

from review_agent_tools import review_contract  # noqa: E402


def read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def words(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise AssertionError("expected a mapping")
    return cast(dict[str, object], value)


def sequence(value: object) -> list[object]:
    if not isinstance(value, list):
        raise AssertionError("expected a sequence")
    return cast(list[object], value)


class DeploymentConfigTests(unittest.TestCase):
    def test_citation_declares_the_project_license(self):
        citation = mapping(yaml.safe_load(read("CITATION.cff")))
        self.assertEqual(citation["license"], "EUPL-1.2")

    def test_compose_keeps_github_access_on_isolated_networks(self):
        compose = read("compose.yaml")
        review_worker = compose.split("\n  review-worker:\n", 1)[1].split(
            "\n  review-github-gateway:\n", 1
        )[0]
        worker = compose.split("\n  review-github-app-worker:\n", 1)[1].split(
            "\n  review-publisher:", 1
        )[0]
        gateway = compose.split("\n  review-github-gateway:\n", 1)[1].split(
            "\n  review-github-app-worker:", 1
        )[0]

        self.assertIn("review-github-control", review_worker)
        self.assertNotIn("review-egress", worker)
        self.assertIn("review-github-control", worker)
        self.assertIn("review-github-egress", gateway)
        self.assertNotIn("review-egress", gateway)
        self.assertIn("review-github-control", gateway)
        self.assertNotIn("review-runtime", gateway)
        self.assertNotIn("review-ingress", gateway)

    def test_openshift_runtime_matches_the_app_only_credential_boundary(self):
        template = read("examples/openshift/review-agent-template.yaml")

        document = mapping(yaml.safe_load(template))
        containers: dict[str, dict[str, object]] = {}
        job_containers: dict[str, dict[str, object]] = {}
        pod_specs: dict[str, dict[str, object]] = {}
        for raw_object in sequence(document["objects"]):
            resource = mapping(raw_object)
            if resource.get("kind") == "Job":
                name = str(mapping(resource["metadata"])["name"])
                spec = mapping(resource["spec"])
                pod_template = mapping(spec["template"])
                pod_spec = mapping(pod_template["spec"])
                job_containers[name] = mapping(sequence(pod_spec["containers"])[0])
                continue
            if resource.get("kind") != "Deployment":
                continue
            name = str(mapping(resource["metadata"])["name"])
            spec = mapping(resource["spec"])
            pod_template = mapping(spec["template"])
            pod_spec = mapping(pod_template["spec"])
            container = mapping(sequence(pod_spec["containers"])[0])
            containers[name] = container
            pod_specs[name] = pod_spec

        self.assertEqual(len(containers), 6)
        for name, container in containers.items():
            resources = mapping(container["resources"])
            for field in ("requests", "limits"):
                values = mapping(resources[field])
                self.assertIn("cpu", values, name)
                self.assertIn("memory", values, name)
        self.assertEqual(
            set(job_containers),
            {"review-agent-db-migrate", "review-agent-profile-install"},
        )
        for name, container in job_containers.items():
            resources = mapping(container["resources"])
            for field in ("requests", "limits"):
                values = mapping(resources[field])
                self.assertIn("cpu", values, name)
                self.assertIn("memory", values, name)

        env_entries = {
            name: {
                str(mapping(item)["name"]): mapping(item)
                for item in sequence(container.get("env", []))
            }
            for name, container in containers.items()
        }
        env_names = {name: set(entries) for name, entries in env_entries.items()}
        app_credentials = {
            "REVIEW_AGENT_GITHUB_APP_ID",
            "REVIEW_AGENT_GITHUB_APP_PRIVATE_KEY_FILE",
        }
        self.assertTrue(app_credentials <= env_names["review-agent-github-gateway"])
        for name, names in env_names.items():
            if name != "review-agent-github-gateway":
                self.assertTrue(app_credentials.isdisjoint(names), name)
        self.assertIn(
            "REVIEW_AGENT_GITHUB_APP_WEBHOOK_SECRET",
            env_names["review-agent-admission"],
        )
        for name, names in env_names.items():
            if name != "review-agent-admission":
                self.assertNotIn("REVIEW_AGENT_GITHUB_APP_WEBHOOK_SECRET", names)
        app_id = env_entries["review-agent-github-gateway"][
            "REVIEW_AGENT_GITHUB_APP_ID"
        ]
        app_id_ref = mapping(mapping(app_id["valueFrom"])["secretKeyRef"])
        self.assertEqual(app_id_ref["name"], "review-agent-github-app")
        webhook = env_entries["review-agent-admission"][
            "REVIEW_AGENT_GITHUB_APP_WEBHOOK_SECRET"
        ]
        webhook_ref = mapping(mapping(webhook["valueFrom"])["secretKeyRef"])
        self.assertEqual(webhook_ref["name"], "review-agent-github-app")
        self.assertEqual(
            {
                name
                for name, names in env_names.items()
                if "REVIEW_AGENT_GITHUB_GATEWAY_URL" in names
            },
            {
                "hermes-review",
                "review-agent-worker",
                "review-agent-github-app-worker",
                "review-agent-publisher",
            },
        )
        gateway_policy = next(
            mapping(resource)
            for resource in sequence(document["objects"])
            if mapping(resource).get("kind") == "NetworkPolicy"
            and mapping(mapping(resource)["metadata"])["name"]
            == "review-agent-github-gateway-ingress"
        )
        gateway_peers = {
            mapping(mapping(mapping(peer)["podSelector"])["matchLabels"])["app"]
            for rule in sequence(mapping(gateway_policy["spec"])["ingress"])
            for peer in sequence(mapping(rule)["from"])
        }
        self.assertEqual(
            gateway_peers,
            {
                "hermes-review",
                "review-agent-worker",
                "review-agent-github-app-worker",
                "review-agent-publisher",
            },
        )
        key_mounts = {
            name
            for name, pod_spec in pod_specs.items()
            if any(
                mapping(volume).get("name") == "app-key"
                for volume in sequence(pod_spec.get("volumes", []))
            )
        }
        self.assertEqual(key_mounts, {"review-agent-github-gateway"})
        for name in ("review-agent-worker", "review-agent-publisher"):
            for source in sequence(containers[name].get("envFrom", [])):
                self.assertNotIn("secretRef", mapping(source), name)
        gateway_volumes = {
            str(mapping(volume)["name"]): mapping(volume)
            for volume in sequence(
                pod_specs["review-agent-github-gateway"].get("volumes", [])
            )
        }
        key_secret = mapping(gateway_volumes["app-key"]["secret"])
        self.assertEqual(key_secret["secretName"], "review-agent-github-app")

        self.assertNotIn("ALLOWED_REPOSITORIES", template)
        self.assertNotIn("REVIEW_AGENT_ALLOWED_REPOSITORIES", template)
        self.assertNotIn("REVIEW_AGENT_ADMISSION_MAX_BODY_BYTES", template)

    def test_default_profile_has_no_repository_specific_assumptions(self):
        soul = read("bootstrap/profiles/default-standard/SOUL.md")
        canonical = read("bootstrap/profiles/default-standard/workspace/AGENTS.md")
        skill = read(
            "bootstrap/profiles/default-standard/skills/review-agent-pr/SKILL.md"
        )
        baseline = words(f"{soul}\n{canonical}\n{skill}")

        for repository_specific_assumption in [
            "FastAPI",
            "SQLAlchemy",
            "PostgreSQL",
            "pgvector",
            "Redis/ARQ",
            "SvelteKit",
            "OIDC/JWT",
            "LiteLLM",
            "MCP",
            "tenant isolation",
            "tenant.missing-scope",
            "auth.jwt-claim-validation",
            "rbac.missing-check",
            "contract.openapi-break",
            "POST /api/v1/documents",
            "verify_access_token",
            "enqueue_transcription",
            "tenant document query",
        ]:
            with self.subTest(assumption=repository_specific_assumption):
                self.assertNotIn(repository_specific_assumption, baseline)

    def test_worker_shutdown_grace_matches_across_deployments(self):
        compose = read("compose.yaml")
        openshift_template = mapping(
            yaml.safe_load(read("examples/openshift/review-agent-template.yaml"))
        )
        compose_worker = compose.split("\n  review-worker:\n", 1)[1].split(
            "\n  review-github-gateway:", 1
        )[0]
        compose_grace = re.search(
            r'stop_grace_period: "\$\{REVIEW_AGENT_WORKER_TERMINATION_GRACE_SECONDS:-(\d+)\}s"',
            compose_worker,
        )
        self.assertIsNotNone(compose_grace)
        assert compose_grace is not None
        grace_parameter = next(
            mapping(item)
            for item in sequence(openshift_template["parameters"])
            if mapping(item).get("name") == "WORKER_TERMINATION_GRACE_SECONDS"
        )
        worker_deployment = next(
            mapping(item)
            for item in sequence(openshift_template["objects"])
            if mapping(mapping(item).get("metadata", {})).get("name")
            == "review-agent-worker"
        )
        worker_pod = mapping(
            mapping(mapping(worker_deployment["spec"])["template"])["spec"]
        )

        self.assertEqual(grace_parameter["value"], compose_grace.group(1))
        self.assertEqual(
            worker_pod["terminationGracePeriodSeconds"],
            "${{WORKER_TERMINATION_GRACE_SECONDS}}",
        )

    def test_app_runtime_uses_least_privilege_deployment(self):
        compose = read("compose.yaml")
        profile_section = compose.split("  review-profile-install:", 1)[1].split(
            "\n  review-db-migrate:", 1
        )[0]
        migration_section = compose.split("  review-db-migrate:", 1)[1].split(
            "\n  hermes-review:", 1
        )[0]
        hermes_section = compose.split("  hermes-review:", 1)[1].split(
            "\n  review-admission:", 1
        )[0]
        admission_section = compose.split("  review-admission:", 1)[1].split(
            "\n  review-worker:", 1
        )[0]
        reviewer_section = compose.split("  hermes-review:", 1)[1].split(
            "\n  review-admission:", 1
        )[0]
        publisher_section = compose.split("\n  review-publisher:\n", 1)[1].split(
            "\nnetworks:", 1
        )[0]

        self.assertNotIn("env_file:", reviewer_section)
        self.assertNotIn("REVIEW_AGENT_FEEDBACK_GH_TOKEN", reviewer_section)
        self.assertNotIn("REVIEW_AGENT_FEEDBACK_WEBHOOK_SECRET", reviewer_section)
        self.assertIn("REVIEW_AGENT_GITHUB_GATEWAY_URL", reviewer_section)
        self.assertNotIn("GITHUB_READ_TOKEN", reviewer_section)
        self.assertNotIn("PUBLISH_GH_TOKEN", reviewer_section)
        self.assertIn("REVIEW_AGENT_GITHUB_GATEWAY_URL", publisher_section)
        self.assertNotIn("PUBLISH_GH_TOKEN", publisher_section)
        self.assertIn("review-github-control", publisher_section)
        self.assertNotIn("review-egress", publisher_section)
        self.assertIn("REVIEW_AGENT_DATABASE_URL", reviewer_section)
        self.assertIn("review-egress", hermes_section)
        self.assertNotIn("review-ingress", hermes_section)
        self.assertIn("no-new-privileges:true", hermes_section)
        self.assertNotIn("\n      GH_TOKEN:", reviewer_section)
        self.assertIn("PYTHONDONTWRITEBYTECODE", reviewer_section)
        self.assertNotIn("hermes_review_data:/opt/data", admission_section)
        self.assertNotIn("hermes-review-feedback", compose)
        self.assertNotIn("review-agent-feedback-bridge", compose)
        self.assertNotIn("REVIEW_AGENT_FEEDBACK_GH_TOKEN", compose)
        self.assertNotIn("REVIEW_AGENT_FEEDBACK_WEBHOOK_SECRET", compose)
        self.assertNotIn("REVIEW_AGENT_FEEDBACK_ALLOWED_ACTOR_IDS", compose)
        self.assertIn(
            'REVIEW_AGENT_FEEDBACK_ENABLED: "${REVIEW_AGENT_FEEDBACK_ENABLED:-false}"',
            compose,
        )
        self.assertIn("condition: service_completed_successfully", compose)
        self.assertIn(
            'entrypoint: ["/opt/review-agent-bootstrap/install.sh"]', profile_section
        )
        self.assertNotIn("--force-agents", profile_section)
        self.assertIn("HERMES_HOME: /opt/data", profile_section)
        self.assertIn("REVIEW_AGENT_PROFILE", profile_section)
        self.assertIn("hermes_review_data:/opt/data", profile_section)
        self.assertIn(
            'entrypoint: ["/usr/local/bin/review-agent-admin"]', migration_section
        )
        self.assertIn('command: ["database", "prepare"]', migration_section)
        self.assertIn("REVIEW_AGENT_DATABASE_URL", migration_section)
        self.assertIn("REVIEW_AGENT_RUNTIME_DATABASE_URL", migration_section)
        self.assertIn("review-postgres:", migration_section)
        self.assertIn("condition: service_healthy", migration_section)
        self.assertIn("read_only: true", migration_section)
        self.assertNotIn("ports:", compose)
        self.assertIn("review-database:\n    internal: true", compose)
        self.assertNotIn("/opt/review-agent-bootstrap/install.sh", reviewer_section)

    def test_live_reviewer_keeps_unsafe_toolsets_disabled(self):
        config = read("bootstrap/config.yaml")
        api_server_match = re.search(r"(?m)^  api_server:\n((?:    .+\n)+)", config)
        self.assertIsNotNone(api_server_match)
        assert api_server_match is not None
        api_server_tools = [
            line.strip() for line in api_server_match.group(1).splitlines()
        ]
        disabled = config.split("  disabled_toolsets:", 1)[1].split("\nmemory:", 1)[0]
        self.assertEqual(api_server_tools, ["- review_agent"])
        self.assertIn("- file", disabled)
        self.assertIn("- skills", disabled)
        self.assertIn("- memory", disabled)
        self.assertIn("- terminal", disabled)
        self.assertIn("- code_execution", disabled)
        self.assertNotIn("platforms:\n  webhook:", config)
        self.assertNotIn("deliver: github_comment", config)

    def test_hermes_base_image_is_one_immutable_digest(self):
        compose = read("compose.yaml")
        env_example = read(".env.example")
        dockerfile_image = re.search(
            r"(?m)^ARG HERMES_IMAGE=(\S+)$", read("Dockerfile")
        )
        env_image = re.search(r"(?m)^HERMES_IMAGE=(\S+)$", env_example)
        self.assertIsNotNone(dockerfile_image)
        self.assertIsNotNone(env_image)
        assert dockerfile_image is not None and env_image is not None
        image = dockerfile_image.group(1)

        self.assertRegex(image, r"^nousresearch/hermes-agent:\S+@sha256:[0-9a-f]{64}$")
        self.assertEqual(image, env_image.group(1))
        compose_hermes_defaults = re.findall(
            r"HERMES_IMAGE: \$\{HERMES_IMAGE:-([^}]+)\}", compose
        )
        self.assertTrue(compose_hermes_defaults)
        self.assertEqual({image}, set(compose_hermes_defaults))
        self.assertNotIn("nousresearch/hermes-agent:latest", compose)
        self.assertNotIn("nousresearch/hermes-agent:latest", env_example)

    def test_deployment_requires_the_model_and_a_supported_effort(self):
        compose = read("compose.yaml")
        compose_admin = read("compose.admin.yaml")
        install_schema = mapping(
            yaml.safe_load(read("install/review-agent.schema.json"))
        )

        required_model = (
            'REVIEW_AGENT_MODEL: "${REVIEW_AGENT_MODEL:?Set REVIEW_AGENT_MODEL to the review model}"'
        )
        self.assertIn(required_model, compose)
        self.assertIn(required_model, compose_admin)
        self.assertNotIn("REVIEW_AGENT_MODEL:-", compose + compose_admin)
        self.assertIn("REVIEW_AGENT_GITHUB_APP_WEBHOOK_SECRET:?", compose)
        openshift = read("examples/openshift/review-agent-template.yaml")
        model_parameter = openshift.split("  - name: MODEL\n", 1)[1].split(
            "  - name:", 1
        )[0]
        self.assertIn("required: true", model_parameter)
        self.assertNotIn("value:", model_parameter)
        deployment_schema = mapping(mapping(install_schema["properties"])["deployment"])
        deployment_properties = mapping(deployment_schema["properties"])
        effort_schema = mapping(deployment_properties["reasoning_effort"])
        self.assertEqual(
            review_contract.REASONING_EFFORTS,
            frozenset(sequence(effort_schema["enum"])),
        )


if __name__ == "__main__":
    unittest.main()
