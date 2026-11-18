from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import cast
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from review_agent.review import contract as review_contract  # noqa: E402

HELM_IMAGE = "registry.example/review-agent@sha256:" + "0" * 64


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

    def test_helm_chart_matches_the_app_only_credential_boundary(self):
        objects = self._chart_objects(
            "--set", "drained=true", "--set", "initialize=true"
        )
        containers: dict[str, dict[str, object]] = {}
        job_containers: dict[str, dict[str, object]] = {}
        pod_specs: dict[str, dict[str, object]] = {}
        for (kind, name), resource in objects.items():
            if kind == "Job":
                spec = mapping(resource["spec"])
                pod_template = mapping(spec["template"])
                pod_spec = mapping(pod_template["spec"])
                # Jobs are immutable, so every release revision gets its own.
                job_containers[name.removesuffix("-1")] = mapping(
                    sequence(pod_spec["containers"])[0]
                )
                continue
            if kind != "Deployment":
                continue
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
        gateway_policy = objects[
            ("NetworkPolicy", "review-agent-github-gateway-ingress")
        ]
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

    def test_default_profile_has_no_repository_specific_assumptions(self):
        soul = read("profiles/default-standard/SOUL.md")
        canonical = read("profiles/default-standard/workspace/AGENTS.md")
        skill = read(
            "profiles/default-standard/skills/review-agent-pr/SKILL.md"
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
        helm_values = mapping(
            yaml.safe_load(read("examples/helm/review-agent/values.yaml"))
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
        helm_grace = helm_values["workerTerminationGraceSeconds"]

        self.assertEqual(helm_grace, int(compose_grace.group(1)))
        worker_deployment = self._chart_objects()[("Deployment", "review-agent-worker")]
        worker_pod = mapping(
            mapping(mapping(worker_deployment["spec"])["template"])["spec"]
        )
        self.assertEqual(worker_pod["terminationGracePeriodSeconds"], helm_grace)

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

    def test_admin_service_serves_the_frontend_the_image_installs(self):
        from review_agent.console import app as console_app

        environment = {
            "REVIEW_AGENT_DATABASE_URL": "postgresql://localhost/not_connected",
            "REVIEW_AGENT_ADMIN_PUBLIC_URL": "https://admin.example.test",
        }
        with tempfile.TemporaryDirectory() as temp:
            image_dist = Path(temp) / "dist"
            image_dist.mkdir()
            with (
                patch.dict(os.environ, environment),
                patch.object(console_app, "create_app") as create,
                patch.object(console_app, "IMAGE_STATIC_DIR", image_dist),
            ):
                # An image frontend on the same machine does not capture an
                # API that runs from a checkout.
                console_app.app_from_environment()
                with patch.object(console_app, "source_checkout", return_value=None):
                    console_app.app_from_environment()
        checkout, image = (call.kwargs["static_dir"] for call in create.call_args_list)
        # A checkout serves the frontend built beside src/; an installed
        # package serves the directory the Dockerfile copies the build to.
        self.assertEqual(ROOT / "console" / "dist", checkout)
        self.assertEqual(image_dist, image)
        self.assertIn(
            f"COPY --from=frontend /build/dist {console_app.IMAGE_STATIC_DIR}\n",
            read("console/Dockerfile"),
        )

    def test_live_reviewer_keeps_unsafe_toolsets_disabled(self):
        config = read("hermes/config.yaml")
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
        deployment_schema = mapping(mapping(install_schema["properties"])["deployment"])
        deployment_properties = mapping(deployment_schema["properties"])
        effort_schema = mapping(deployment_properties["reasoning_effort"])
        self.assertEqual(
            review_contract.REASONING_EFFORTS,
            frozenset(sequence(effort_schema["enum"])),
        )

    def test_upgrade_procedures_stop_writers_before_migrating(self):
        upgrade = read("docs/DEPLOYMENT.md").split(
            "## Upgrade and roll back production", 1
        )[1]

        def tab(value: str, label: str) -> str:
            opening = f'<TabItem value="{value}" label="{label}">'
            return upgrade.split(opening, 1)[1].split("</TabItem>", 1)[0]

        procedures = {
            "bash -euo pipefail <<'COMPOSE_UPGRADE'": (
                tab("compose-upgrade", "Compose"),
                (
                    "docker compose stop $APP_SERVICES",
                    "remaining=\"$(docker compose ps --all --format '{{.Service}} {{.State}}' |",
                    "docker compose run --rm --no-deps review-profile-install",
                    "docker compose run --rm --no-deps review-db-migrate",
                    "docker compose up -d --no-deps --no-build --force-recreate",
                ),
            ),
            "bash -euo pipefail <<'HELM_DEPLOY'": (
                tab("kubernetes-upgrade", "Kubernetes (Helm)"),
                (
                    "--set drained=true --set initialize=false",
                    "--for=jsonpath='{.status.observedGeneration}'=\"$generation\"",
                    "kubectl delete job -l app=review-agent-init --ignore-not-found",
                    "kubectl wait --for=delete $pods",
                    "--set drained=true --set initialize=true --wait --wait-for-jobs",
                    'helm upgrade review-agent "$CHART" -f "$VALUES" --wait',
                ),
            ),
        }
        for fail_fast_shell, (procedure, steps) in procedures.items():
            with self.subTest(shell=fail_fast_shell):
                self.assertIn(fail_fast_shell, procedure)
                positions = [procedure.index(step) for step in steps]
                self.assertEqual(positions, sorted(positions))

    def _helm(self) -> str:
        helm = shutil.which("helm")
        if helm is None:
            if os.environ.get("CI"):
                self.fail("helm is required to check the chart in CI")
            self.skipTest("helm is not installed")
        return helm

    def _render_chart(
        self, *extra: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                self._helm(), "template", "review-agent", "examples/helm/review-agent",
                "--set", f"image={HELM_IMAGE}", "--set", "model=example-model", *extra,
            ],
            cwd=ROOT, check=check, capture_output=True, text=True,
        )

    def _chart_objects(self, *extra: str) -> dict[tuple[str, str], dict[str, object]]:
        return {
            (str(item["kind"]), str(mapping(item["metadata"])["name"])): item
            for item in map(
                mapping, filter(None, yaml.safe_load_all(self._render_chart(*extra).stdout))
            )
        }

    def test_helm_chart_sets_pod_user_job_labels_and_shared_volume(self):
        def pod_template(item: dict[str, object]) -> dict[str, object]:
            return mapping(mapping(item["spec"])["template"])

        colocated = {
            "podAffinity": {
                "requiredDuringSchedulingIgnoredDuringExecution": [
                    {
                        "labelSelector": {
                            "matchLabels": {"review-agent/volume": "hermes-data"}
                        },
                        "topologyKey": "kubernetes.io/hostname",
                    }
                ]
            }
        }
        # The initialization phase renders every object: every workload at
        # zero replicas and both Jobs present.
        objects = self._chart_objects(
            "--set", "drained=true", "--set", "initialize=true"
        )
        for (kind, name), item in objects.items():
            if kind in {"Deployment", "Job"}:
                pod = mapping(pod_template(item)["spec"])
                self.assertEqual(
                    {
                        "runAsNonRoot": True,
                        "runAsUser": 10000,
                        "runAsGroup": 10000,
                        "fsGroup": 10000,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    pod["securityContext"],
                )
            if kind == "Job":
                # Jobs are immutable, so every release revision gets its own.
                self.assertTrue(name.endswith("-1"), name)
                # The deploy script finds earlier attempts by this label.
                initialization = {"app": "review-agent-init"}
                self.assertEqual(initialization, mapping(item["metadata"])["labels"])
                self.assertEqual(
                    {"labels": initialization}, pod_template(item)["metadata"]
                )
            if name in {"hermes-review", "review-agent-worker"} and kind == "Deployment":
                # Both mount the ReadWriteOnce Hermes volume, so each must
                # land on the node of the other, whichever is replaced.
                labels = mapping(mapping(pod_template(item)["metadata"])["labels"])
                self.assertEqual("hermes-data", labels["review-agent/volume"])
                self.assertEqual(
                    colocated, mapping(pod_template(item)["spec"])["affinity"]
                )
        volume = objects[("PersistentVolumeClaim", "review-agent-hermes-data")]
        self.assertEqual(
            {"helm.sh/resource-policy": "keep"},
            mapping(volume["metadata"])["annotations"],
        )

    def test_helm_chart_runs_initialization_only_while_drained(self):
        def phase(*extra: str) -> tuple[int, set[object]]:
            objects = self._chart_objects(*extra)
            return (
                sum(kind == "Job" for kind, _ in objects),
                {
                    mapping(item["spec"])["replicas"]
                    for (kind, _), item in objects.items()
                    if kind == "Deployment"
                },
            )

        self.assertEqual((0, {1}), phase())
        self.assertEqual((0, {0}), phase("--set", "drained=true"))
        self.assertEqual(
            (2, {0}), phase("--set", "drained=true", "--set", "initialize=true")
        )
        refused = self._render_chart("--set", "initialize=true", check=False)
        self.assertNotEqual(0, refused.returncode)
        self.assertIn("initialize=true requires drained=true", refused.stderr)

        shared = self._chart_objects("--set", "persistence.accessMode=ReadWriteMany")
        for name in ("hermes-review", "review-agent-worker"):
            consumer = mapping(
                mapping(mapping(shared[("Deployment", name)]["spec"])["template"])["spec"]
            )
            self.assertNotIn("affinity", consumer)
        exposed = self._chart_objects(
            "--set", "ingress.enabled=true", "--set", "ingress.host=review.example.org"
        )
        ingress = mapping(exposed[("Ingress", "review-agent")]["spec"])
        self.assertEqual(
            [{"name": "review-agent-admission", "port": {"name": "http"}}],
            [
                mapping(mapping(path)["backend"])["service"]
                for rule in map(mapping, sequence(ingress["rules"]))
                for path in sequence(mapping(rule["http"])["paths"])
            ],
        )

    def test_documented_helm_script_never_starts_workloads_after_a_failed_phase(self):
        deployment = read("docs/DEPLOYMENT.md")
        scripts = re.findall(
            r"```bash\n(bash -euo pipefail <<'HELM_DEPLOY'\n.*?\nHELM_DEPLOY)\n```",
            deployment,
            flags=re.S,
        )
        # The installation and the upgrade follow one and the same script.
        self.assertEqual(2, len(scripts))
        self.assertEqual(scripts[0], scripts[1])

        def run(failing: str) -> tuple[int, list[str]]:
            with tempfile.TemporaryDirectory() as directory:
                calls = Path(directory) / "calls"
                for tool in ("helm", "kubectl"):
                    stub = Path(directory) / tool
                    stub.write_text(
                        "#!/bin/sh\n"
                        f'echo "{tool} $*" >> "{calls}"\n'
                        f'case "{tool} $*" in *"{failing}"*) exit 1;; esac\n',
                        encoding="utf-8",
                    )
                    stub.chmod(0o755)
                result = subprocess.run(
                    ["bash", "-c", scripts[0]],
                    env={**os.environ, "PATH": f"{directory}{os.pathsep}{os.environ['PATH']}"},
                    capture_output=True, text=True, check=False,
                )
                return result.returncode, calls.read_text(encoding="utf-8").splitlines()

        cleanup = "kubectl delete job -l app=review-agent-init --ignore-not-found"
        code, calls = run("no phase fails")
        self.assertEqual(0, code)
        helm = [call for call in calls if call.startswith("helm ")]
        self.assertEqual(3, len(helm))
        drain, initialize, start = helm
        self.assertIn("drained=true --set initialize=false", drain)
        # Waiting here would wait for a volume that binds on first use.
        self.assertNotIn("--wait", drain)
        self.assertIn("drained=true --set initialize=true", initialize)
        self.assertIn("--wait-for-jobs", initialize)
        self.assertNotIn("drained=true", start)
        # Every Deployment has scaled down, Jobs of an earlier attempt are
        # deleted, and every application and initialization label is checked
        # for pods, before initialization.
        # The check is that the controller has seen the scale-down, not that
        # an earlier rollout was healthy: rollout status fails after a
        # rollout that exceeded its deadline, which is when a rerun is needed.
        self.assertFalse([call for call in calls if "rollout status" in call])
        scaled_down = [
            index for index, call in enumerate(calls)
            if call.startswith("kubectl wait deployment/")
            and "--for=jsonpath={.status.observedGeneration}=" in call
        ]
        self.assertEqual(6, len(scaled_down))
        self.assertLess(calls.index(drain), scaled_down[0])
        self.assertLess(scaled_down[-1], calls.index(cleanup))
        self.assertLess(calls.index(cleanup), calls.index(initialize))
        checked = [
            call for call in calls[calls.index(cleanup): calls.index(initialize)]
            if call.startswith("kubectl get pods -l app=")
        ]
        self.assertEqual(7, len(checked))
        self.assertIn("kubectl get pods -l app=review-agent-init -o name", checked)

        for failing, reached in (
            ("initialize=false", 1),
            ("kubectl get deployment/", 1),
            ("kubectl wait deployment/", 1),
            ("kubectl delete job", 1),
            ("kubectl get pods", 1),
            ("initialize=true", 2),
        ):
            with self.subTest(failing=failing):
                code, calls = run(failing)
                self.assertNotEqual(0, code)
                helm = [call for call in calls if call.startswith("helm ")]
                self.assertEqual(reached, len(helm))
                self.assertTrue(all("drained=true" in call for call in helm))


if __name__ == "__main__":
    unittest.main()
