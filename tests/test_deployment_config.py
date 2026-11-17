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

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bootstrap" / "plugins"))

from review_agent_tools import review_contract  # noqa: E402

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
                    'running="$(docker compose ps --status running --services)"',
                    "docker compose run --rm --no-deps --no-build review-profile-install",
                    "docker compose run --rm --no-deps --no-build review-db-migrate",
                    "docker compose up -d --no-deps --no-build --force-recreate",
                ),
            ),
            "bash -euo pipefail <<'OPENSHIFT_UPGRADE'": (
                tab("openshift-upgrade", "OpenShift"),
                (
                    "oc process -f examples/openshift/review-agent-template.yaml",
                    'oc scale deployment "${DEPLOYMENTS[@]}" --replicas=0',
                    "oc wait --for=delete $pods",
                    "oc delete job review-agent-profile-install",
                    'oc apply -f "$RENDERED"',
                    "oc wait --for=condition=complete job/review-agent-profile-install",
                    "oc scale deployment/hermes-review",
                    "oc scale deployment/review-agent-admission",
                ),
            ),
            "bash -euo pipefail <<'HELM_DEPLOY'": (
                tab("kubernetes-upgrade", "Kubernetes (Helm)"),
                (
                    "--set drained=true --set initialize=false",
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

    def test_documented_openshift_commands_supply_required_parameters(self):
        template = mapping(
            yaml.safe_load(read("examples/openshift/review-agent-template.yaml"))
        )
        required = [
            str(parameter["name"])
            for parameter in map(mapping, sequence(template["parameters"]))
            if parameter.get("required") and "value" not in parameter
        ]
        self.assertIn("MODEL", required)
        render = "oc process -f examples/openshift/review-agent-template.yaml"
        for document in ("docs/DEPLOYMENT.md", "website/src/pages/index.tsx"):
            commands = read(document).split(render)[1:]
            self.assertTrue(commands, document)
            for command in commands:
                arguments = command.split("oc apply", 1)[0].split('"$RENDERED"', 1)[0]
                for name in required:
                    with self.subTest(document=document, parameter=name):
                        self.assertIn(f"-p {name}=", arguments)

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

    def test_helm_chart_deploys_the_same_workloads_as_the_openshift_template(self):
        parameters: dict[str, object] = {
            "${IMAGE}": HELM_IMAGE,
            "${MODEL}": "example-model",
            "${{WORKER_TERMINATION_GRACE_SECONDS}}": 150,
        }
        template = mapping(
            yaml.safe_load(read("examples/openshift/review-agent-template.yaml"))
        )
        for parameter in map(mapping, sequence(template["parameters"])):
            parameters.setdefault("${" + str(parameter["name"]) + "}", parameter.get("value"))

        def resolved(value: object) -> object:
            if isinstance(value, dict):
                return {key: resolved(item) for key, item in mapping(value).items()}
            if isinstance(value, list):
                return [resolved(item) for item in sequence(value)]
            return parameters.get(value, value) if isinstance(value, str) else value

        def pod_template(item: dict[str, object]) -> dict[str, object]:
            return mapping(mapping(item["spec"])["template"])

        expected = {
            (str(item["kind"]), str(mapping(item["metadata"])["name"])): item
            for item in map(mapping, sequence(resolved(template["objects"])))
            if item["kind"] != "Route"
        }
        # The initialization phase is the template's own starting state: every
        # workload at zero replicas and both Jobs present.
        actual: dict[tuple[str, str], dict[str, object]] = {}
        for (kind, name), item in self._chart_objects(
            "--set", "drained=true", "--set", "initialize=true"
        ).items():
            if kind == "Job":
                # Jobs are immutable, so every release revision gets its own.
                self.assertTrue(name.endswith("-1"), name)
                name = name.removesuffix("-1")
                mapping(item["metadata"])["name"] = name
            actual[(kind, name)] = item
        self.assertEqual(set(expected), set(actual))

        # Everything the chart adds to the template is asserted here and then
        # removed, so the comparison below covers the rest exactly.
        colocated = {
            "podAffinity": {
                "requiredDuringSchedulingIgnoredDuringExecution": [
                    {
                        "labelSelector": {"matchLabels": {"app": "hermes-review"}},
                        "topologyKey": "kubernetes.io/hostname",
                    }
                ]
            }
        }
        for key, item in actual.items():
            kind, name = key
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
                pod["securityContext"] = {"seccompProfile": {"type": "RuntimeDefault"}}
            if kind == "Job":
                self.assertEqual(
                    {"labels": {"app": "review-agent-init"}},
                    pod_template(item).pop("metadata"),
                )
            if name == "review-agent-worker":
                # The worker shares the ReadWriteOnce Hermes volume.
                self.assertEqual(
                    colocated, mapping(pod_template(item)["spec"]).pop("affinity")
                )
            if kind == "PersistentVolumeClaim":
                self.assertEqual(
                    {"helm.sh/resource-policy": "keep"},
                    mapping(item["metadata"]).pop("annotations"),
                )
            with self.subTest(kind=kind, name=name):
                self.assertEqual(expected[key], item)

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
        worker = mapping(
            mapping(mapping(shared[("Deployment", "review-agent-worker")]["spec"])["template"])["spec"]
        )
        self.assertNotIn("affinity", worker)
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
                calls = Path(directory) / "helm-calls"
                helm = Path(directory) / "helm"
                helm.write_text(
                    "#!/bin/sh\n"
                    f'echo "$*" >> "{calls}"\n'
                    f'case "$*" in *"{failing}"*) exit 1;; esac\n',
                    encoding="utf-8",
                )
                kubectl = Path(directory) / "kubectl"
                kubectl.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                helm.chmod(0o755)
                kubectl.chmod(0o755)
                result = subprocess.run(
                    ["bash", "-c", scripts[0]],
                    env={**os.environ, "PATH": f"{directory}{os.pathsep}{os.environ['PATH']}"},
                    capture_output=True, text=True, check=False,
                )
                logged = calls.read_text(encoding="utf-8").splitlines()
                return result.returncode, logged

        code, calls = run("no phase fails")
        self.assertEqual(0, code)
        self.assertEqual(3, len(calls))
        self.assertIn("drained=true --set initialize=false", calls[0])
        self.assertIn("drained=true --set initialize=true", calls[1])
        self.assertNotIn("drained=true", calls[2])

        for failing, reached in (("initialize=false", 1), ("initialize=true", 2)):
            with self.subTest(failing=failing):
                code, calls = run(failing)
                self.assertNotEqual(0, code)
                self.assertEqual(reached, len(calls))
                self.assertTrue(all("drained=true" in call for call in calls))


if __name__ == "__main__":
    unittest.main()
