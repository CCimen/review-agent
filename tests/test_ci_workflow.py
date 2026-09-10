from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import tomllib
import unittest
from pathlib import Path
from typing import cast

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "release-image.yml"
DOCS_WORKFLOW = ROOT / ".github" / "workflows" / "docs-pages.yml"
RELEASE_SBOM = ROOT / "scripts" / "generate_release_sbom.sh"
PYTHON_RUNTIME_SBOM = ROOT / "scripts" / "generate_python_runtime_sbom.sh"
RELEASE_SBOM_REQUIREMENTS = ROOT / "requirements-release-sbom.txt"
RELEASE_TAG_CHECK = ROOT / "scripts" / "validate_release_tag.py"
IMAGE_CHECK = ROOT / "scripts" / "check_image.sh"
POSTGRES_CHECK = ROOT / "scripts" / "check_postgres_schema.sh"
PYTHON_CHECK = ROOT / "scripts" / "check_bundle.sh"
PYRIGHT_CONFIG = ROOT / "pyrightconfig.json"
RUFF_CONFIG = ROOT / "ruff.toml"
DEVELOPMENT_REQUIREMENTS = ROOT / "requirements-dev.txt"
TRIVY_CONFIG = ROOT / "trivy.yaml"
ROADMAP = ROOT / "docs" / "ROADMAP.md"
HOMEPAGE = ROOT / "website" / "src" / "pages" / "index.tsx"
README = ROOT / "README.md"


def mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise AssertionError("expected a mapping")
    return cast(dict[str, object], value)


def sequence(value: object) -> list[object]:
    if not isinstance(value, list):
        raise AssertionError("expected a sequence")
    return cast(list[object], value)


def workflow(path: Path) -> dict[str, object]:
    document: object = yaml.load(
        path.read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    return mapping(document)


def uses_entries(value: object) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    if isinstance(value, dict):
        item = cast(dict[str, object], value)
        if isinstance(item.get("uses"), str):
            entries.append(item)
        for child in item.values():
            entries.extend(uses_entries(child))
    elif isinstance(value, list):
        for child in cast(list[object], value):
            entries.extend(uses_entries(child))
    return entries


def needs(job: dict[str, object]) -> set[str]:
    value = job.get("needs")
    if isinstance(value, str):
        return {value}
    return {str(item) for item in sequence(value)}


def named_step(job: dict[str, object], name: str) -> dict[str, object]:
    for value in sequence(job["steps"]):
        step = mapping(value)
        if step.get("name") == name:
            return step
    raise AssertionError(f"missing workflow step: {name}")


class PythonBundleWorkflowTests(unittest.TestCase):
    def test_pages_publishes_only_a_qualified_documented_release(self):
        document = workflow(DOCS_WORKFLOW)
        self.assertEqual(mapping(document["permissions"]), {"contents": "read"})
        release_event = mapping(mapping(document["on"])["workflow_run"])
        self.assertEqual(["Publish container image"], release_event["workflows"])
        self.assertEqual(["completed"], release_event["types"])
        jobs = mapping(document["jobs"])
        build = mapping(jobs["build"])
        self.assertEqual(
            "${{ github.event_name != 'workflow_run' || "
            "github.event.workflow_run.conclusion == 'success' }}",
            build["if"],
        )
        checkout = mapping(named_step(build, "Check out repository")["with"])
        self.assertEqual("refs/heads/main", checkout["ref"])
        self.assertEqual("false", checkout["persist-credentials"])
        gate = named_step(build, "Confirm documented release is qualified")
        self.assertEqual("${{ github.token }}", mapping(gate["env"])["GH_TOKEN"])
        self.assertEqual("${{ github.repository }}", mapping(gate["env"])["GH_REPO"])
        command = str(gate["run"])
        self.assertIn("Release state: ", command)
        self.assertIn('gh release view "$release_tag" --json assets', command)
        self.assertIn("IMAGE-DIGESTS.txt", command)
        step_names = [
            str(mapping(step).get("name", "")) for step in sequence(build["steps"])
        ]
        self.assertLess(
            step_names.index("Confirm documented release is qualified"),
            step_names.index("Upload GitHub Pages artifact"),
        )
        self.assertEqual(needs(mapping(jobs["deploy"])), {"build"})

    def test_required_quality_gates_run_independently_in_read_only_ci(self):
        self.assertTrue(WORKFLOW.is_file(), "full Python bundle CI is missing")
        source = WORKFLOW.read_text(encoding="utf-8")
        document = workflow(WORKFLOW)
        events = mapping(document["on"])
        self.assertEqual(
            {
                "pull_request",
                "merge_group",
                "push",
                "workflow_dispatch",
                "workflow_call",
            },
            set(events),
        )
        self.assertEqual(["main"], sequence(mapping(events["push"])["branches"]))
        self.assertEqual({"contents": "read"}, mapping(document["permissions"]))
        self.assertNotIn("pull_request_target", source)
        self.assertNotIn("secrets.", source)
        self.assertNotRegex(source, r"(?m)^\s+[^:#]+:\s*write\b")

        action_entries = uses_entries(document)
        external_actions = [
            str(entry["uses"])
            for entry in action_entries
            if not str(entry["uses"]).startswith("./")
        ]
        self.assertTrue(external_actions)
        for action in external_actions:
            self.assertRegex(action, r"^[^@\s]+@[0-9a-f]{40}$")

        self.assertIn("python-version: '3.14.7'", source)
        self.assertIn("python-version: '3.13.5'", source)
        checkout_entries = [
            entry
            for entry in action_entries
            if str(entry["uses"]).startswith("actions/checkout@")
        ]
        self.assertTrue(checkout_entries)
        for checkout in checkout_entries:
            inputs = mapping(checkout["with"])
            self.assertEqual("false", inputs.get("persist-credentials"))
            self.assertEqual("${{ github.sha }}", inputs.get("ref"))

        jobs = mapping(document["jobs"])
        for job_id, job_name in (
            ("python-fast", "Python fast"),
            ("postgres-contract", "PostgreSQL contract"),
            ("image-smoke", "Image smoke"),
            ("dependency-scan", "Dependency vulnerabilities"),
            ("required", "CI / required"),
        ):
            self.assertEqual(job_name, mapping(jobs[job_id])["name"])
        required = mapping(jobs["required"])
        self.assertEqual(
            {
                "python-fast",
                "postgres-contract",
                "image-smoke",
                "dependency-scan",
            },
            needs(required),
        )
        self.assertEqual("${{ always() }}", required["if"])
        self.assertIn("PYTHON_FAST_RESULT: ${{ needs.python-fast.result }}", source)
        self.assertIn("POSTGRES_RESULT: ${{ needs.postgres-contract.result }}", source)
        self.assertIn("IMAGE_RESULT: ${{ needs.image-smoke.result }}", source)
        self.assertIn(
            "DEPENDENCY_SCAN_RESULT: ${{ needs.dependency-scan.result }}",
            source,
        )
        self.assertIn("npm install --global pyright@1.1.408", source)
        self.assertIn(
            "python3 -m pip install --disable-pip-version-check "
            "--requirement requirements.txt --requirement requirements-dev.txt",
            source,
        )
        self.assertIn("./scripts/check_bundle.sh", source)
        self.assertIn("./scripts/check_postgres_schema.sh", source)
        self.assertIn("docker build --tag review-agent:ci .", source)
        self.assertIn("bash ./scripts/check_image.sh review-agent:ci", source)
        self.assertIn("sh ./scripts/check_admin_image.sh review-agent-admin:ci", source)
        image_check = IMAGE_CHECK.read_text(encoding="utf-8")
        for runtime_contract in (
            "review-agent-admission",
            "review-agent-worker",
            "review-agent-publisher",
            "review-agent-hermes-contract",
            "/opt/review-agent-bootstrap/install.sh",
            "/opt/hermes/bin/hermes",
            "gateway --help",
            "command -v curl",
            "! command -v gh",
        ):
            self.assertIn(runtime_contract, image_check)
        for duplicated_command in (
            "python3 -m compileall",
            "python3 -m unittest",
            "pyright -p",
            "validate-replay",
        ):
            self.assertNotIn(duplicated_command, source)

    def test_dependency_vulnerability_gate_covers_every_shipped_lock(self):
        document = workflow(WORKFLOW)
        jobs = mapping(document["jobs"])
        scan = mapping(jobs["dependency-scan"])
        self.assertEqual("Dependency vulnerabilities", scan["name"])

        scan_step = named_step(scan, "Scan dependency vulnerabilities")
        self.assertRegex(
            str(scan_step["uses"]),
            r"^aquasecurity/trivy-action@[0-9a-f]{40}$",
        )
        scan_inputs = mapping(scan_step["with"])
        self.assertEqual("fs", scan_inputs["scan-type"])
        self.assertEqual(".", scan_inputs["scan-ref"])
        self.assertEqual("trivy.yaml", scan_inputs["trivy-config"])
        self.assertEqual("dependency-vulnerabilities.json", scan_inputs["output"])
        self.assertEqual("v0.74.0", scan_inputs["version"])
        self.assertEqual("true", scan_step["continue-on-error"])

        upload = named_step(scan, "Retain dependency vulnerability report")
        self.assertEqual("${{ always() }}", upload["if"])
        self.assertEqual(
            "dependency-vulnerabilities.json",
            mapping(upload["with"])["path"],
        )
        enforce = named_step(scan, "Enforce dependency vulnerability policy")
        self.assertEqual("${{ always() }}", enforce["if"])
        self.assertIn("SCAN_OUTCOME", mapping(enforce["env"]))
        enforce_command = str(enforce["run"])
        self.assertIn("scripts/check_trivy_report.py", enforce_command)
        self.assertNotIn("--critical-exceptions", enforce_command)
        for manifest in (
            "requirements.txt",
            "requirements-admin.txt",
            "admin/package-lock.json",
            "install/package-lock.json",
            "website/package-lock.json",
        ):
            self.assertIn(f'--require-target "{manifest}"', enforce_command)

        required = mapping(jobs["required"])
        self.assertIn("dependency-scan", needs(required))
        self.assertIn("needs.dependency-scan.result", str(required))

        policy = mapping(yaml.safe_load(TRIVY_CONFIG.read_text(encoding="utf-8")))
        self.assertEqual(0, policy["exit-code"])
        self.assertEqual("json", policy["format"])
        self.assertEqual(["HIGH", "CRITICAL"], policy["severity"])
        self.assertEqual("/dev/null", policy["ignorefile"])
        self.assertNotIn("list-all-pkgs", policy)
        self.assertEqual(["vuln"], mapping(policy["scan"])["scanners"])
        pip_patterns = [
            pattern.removeprefix("pip:")
            for pattern in cast(list[str], mapping(policy["scan"])["file-patterns"])
            if pattern.startswith("pip:")
        ]
        for manifest in ("requirements-admin.txt", "requirements-code-graph.txt"):
            self.assertTrue(
                any(re.search(pattern, manifest) for pattern in pip_patterns),
                f"Trivy must discover {manifest}",
            )
        self.assertEqual(True, mapping(policy["pkg"])["include-dev-deps"])
        self.assertEqual(False, mapping(policy["vulnerability"])["ignore-unfixed"])

    def test_fast_quality_tools_are_pinned_and_cover_production_entrypoints(self):
        self.assertEqual(
            DEVELOPMENT_REQUIREMENTS.read_text(encoding="utf-8"),
            "ruff==0.14.4\ntypes-authlib==1.8.0.20260907\n",
        )
        self.assertIn(
            "httpx2==2.12.0",
            (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines(),
        )
        ruff = mapping(tomllib.loads(RUFF_CONFIG.read_text(encoding="utf-8")))
        self.assertEqual("py311", ruff["target-version"])
        lint = mapping(ruff["lint"])
        selected = {str(item) for item in sequence(lint["select"])}
        self.assertLessEqual({"E4", "E7", "E9", "F", "B905"}, selected)
        ignored = {
            str(item)
            for key in ("ignore", "extend-ignore")
            for item in sequence(lint.get(key, []))
        }
        for key in ("per-file-ignores", "extend-per-file-ignores"):
            per_file_ignores = mapping(lint.get(key, {}))
            ignored.update(
                str(item)
                for rules in per_file_ignores.values()
                for item in sequence(rules)
            )
        self.assertFalse(
            any(
                selector == "ALL"
                or selector == "F"
                or (selector.startswith("F") and selector[1:].isdigit())
                or "B905".startswith(selector)
                for selector in ignored
            )
        )
        self.assertIn("ruff check", PYTHON_CHECK.read_text(encoding="utf-8"))

        pyright = json.loads(PYRIGHT_CONFIG.read_text(encoding="utf-8"))
        includes = set(pyright["include"])
        self.assertIn("bootstrap/install.py", includes)
        self.assertIn("bootstrap/plugins/review_agent_tools", includes)
        self.assertIn("scripts/check_trivy_report.py", includes)
        self.assertIn("tools", includes)

    def test_release_workflow_publishes_only_versioned_release_images(self):
        source = RELEASE_WORKFLOW.read_text(encoding="utf-8")
        document = workflow(RELEASE_WORKFLOW)
        events = mapping(document["on"])
        self.assertEqual({"release"}, set(events))
        self.assertEqual(
            ["published"],
            sequence(mapping(events["release"])["types"]),
        )
        self.assertEqual({"contents": "read"}, mapping(document["permissions"]))
        self.assertEqual(
            {"group": "release-image", "cancel-in-progress": "false"},
            mapping(document["concurrency"]),
        )
        self.assertNotIn("pull_request_target", source)
        self.assertNotIn("workflow_dispatch", source)
        jobs = mapping(document["jobs"])
        verify = mapping(jobs["verify"])
        quality = mapping(jobs["quality"])
        publish = mapping(jobs["publish"])
        evidence = mapping(jobs["evidence"])
        sbom = mapping(jobs["sbom"])

        self.assertEqual({"verify"}, needs(quality))
        self.assertEqual("./.github/workflows/ci.yml", quality["uses"])
        self.assertEqual({"contents": "read"}, mapping(quality["permissions"]))
        self.assertNotIn("secrets", quality)
        self.assertEqual({"verify", "quality"}, needs(publish))
        self.assertNotIn("if", publish)
        self.assertEqual({"verify", "publish"}, needs(evidence))
        self.assertEqual({"verify", "publish", "evidence"}, needs(sbom))

        self.assertEqual(
            {
                "contents": "read",
                "packages": "write",
                "attestations": "write",
                "id-token": "write",
            },
            mapping(publish["permissions"]),
        )
        self.assertEqual(
            {
                "contents": "read",
                "packages": "read",
            },
            mapping(evidence["permissions"]),
        )
        self.assertEqual(
            {
                "contents": "write",
                "attestations": "write",
                "artifact-metadata": "write",
                "id-token": "write",
            },
            mapping(sbom["permissions"]),
        )
        for job_id, value in jobs.items():
            job = mapping(value)
            job_permissions = mapping(job["permissions"]) if "permissions" in job else {}
            if any(level == "write" for level in job_permissions.values()):
                self.assertIn(job_id, {"publish", "sbom", "promote"})

        self.assertIn("source_sha: ${{ steps.source.outputs.sha }}", source)
        self.assertIn("python3 scripts/validate_release_tag.py", source)
        self.assertIn("python3 scripts/generate_llms_docs.py --check", source)
        self.assertIn(
            'grep -Fxq "Release state: ${RELEASE_TAG}" website/static/llms.txt',
            source,
        )
        self.assertIn(
            "Generated LLM documentation does not match the release tag.",
            source,
        )
        source_step = named_step(verify, "Record verified source")
        self.assertIn('test "$source_sha" = "$GITHUB_SHA"', str(source_step["run"]))
        self.assertEqual(
            "${{ github.event.release.tag_name }}",
            mapping(named_step(verify, "Check out release tag")["with"])["ref"],
        )
        for job, step_name in (
            (publish, "Check out verified source"),
            (evidence, "Check out verified source"),
        ):
            self.assertEqual(
                "${{ needs.verify.outputs.source_sha }}",
                mapping(named_step(job, step_name)["with"])["ref"],
            )

        publish_step_names = [
            mapping(value).get("name") for value in sequence(publish["steps"])
        ]
        self.assertLess(
            publish_step_names.index(
                "Confirm release tag still targets verified source"
            ),
            publish_step_names.index("Build and publish image"),
        )
        self.assertIn(
            'test "$(git rev-list -n 1 refs/tags/release-candidate)" = "$SOURCE_SHA"',
            str(
                named_step(
                    publish,
                    "Confirm release tag still targets verified source",
                )["run"]
            ),
        )
        self.assertNotIn("docker build --tag review-agent:release-candidate .", source)

        action_entries = uses_entries(document)
        checkout_entries = [
            entry
            for entry in action_entries
            if str(entry["uses"]).startswith("actions/checkout@")
        ]
        self.assertTrue(checkout_entries)
        for checkout in checkout_entries:
            self.assertEqual(
                "false", mapping(checkout["with"]).get("persist-credentials")
            )
        for entry in action_entries:
            action = str(entry["uses"])
            if action.startswith("./"):
                continue
            self.assertRegex(action, r"^[^@\s]+@[0-9a-f]{40}$")

        self.assertIn("RELEASE_TAG: ${{ github.event.release.tag_name }}", source)
        self.assertIn(
            "Release tag must use vMAJOR.MINOR.PATCH",
            RELEASE_TAG_CHECK.read_text(encoding="utf-8"),
        )
        self.assertIn("platforms: linux/amd64,linux/arm64", source)
        self.assertIn("type=raw,value=${{ github.event.release.tag_name }}", source)
        self.assertNotIn("value=latest", source)
        self.assertIn("password: ${{ secrets.GITHUB_TOKEN }}", source)
        self.assertIn("provenance: mode=max", source)
        self.assertIn("sbom: true", source)
        for step_name in ("Build and publish image", "Build and publish admin image"):
            arguments = str(mapping(named_step(publish, step_name)["with"])["build-args"])
            self.assertIn("REVIEW_AGENT_VERSION=${{ github.event.release.tag_name }}", arguments)
            self.assertIn("REVIEW_AGENT_REVISION=${{ needs.verify.outputs.source_sha }}", arguments)
        self.assertIn("subject-name: ${{ env.IMAGE_NAME }}", source)
        self.assertIn("subject-digest: ${{ steps.push.outputs.digest }}", source)
        self.assertIn("push-to-registry: true", source)

    def test_release_tags_wait_for_qualified_image_pair(self):
        jobs = mapping(workflow(RELEASE_WORKFLOW)["jobs"])
        publish = mapping(jobs["publish"])
        for name, image in (
            ("Build and publish image", "${{ env.IMAGE_NAME }}"),
            ("Build and publish admin image", "${{ env.IMAGE_NAME }}-admin"),
        ):
            inputs = mapping(named_step(publish, name)["with"])
            self.assertNotIn("tags", inputs)
            self.assertEqual(
                f"type=image,name={image},push-by-digest=true,name-canonical=true,push=true",
                inputs["outputs"],
            )
        promote = mapping(jobs["promote"])
        self.assertEqual({"verify", "publish", "sbom"}, needs(promote))
        self.assertNotIn("if", promote)
        self.assertEqual(
            {"contents": "read", "packages": "write"},
            mapping(promote["permissions"]),
        )
        step = named_step(promote, "Publish qualified version tags")
        environment = mapping(step["env"])
        self.assertEqual(
            "${{ needs.publish.outputs.image_digest }}", environment["IMAGE_DIGEST"]
        )
        self.assertEqual(
            "${{ needs.publish.outputs.admin_image_digest }}",
            environment["ADMIN_IMAGE_DIGEST"],
        )
        step_names = [str(mapping(item).get("name")) for item in sequence(promote["steps"])]
        self.assertLess(
            step_names.index("Confirm release tag still targets verified source"),
            step_names.index("Publish qualified version tags"),
        )

    def test_release_promotion_preserves_digests_and_stops_on_registry_mismatch(self):
        job = mapping(mapping(workflow(RELEASE_WORKFLOW)["jobs"])["promote"])
        command = str(named_step(job, "Publish qualified version tags")["run"])
        runtime_digest = "sha256:" + "a" * 64
        admin_digest = "sha256:" + "b" * 64
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            docker = temporary / "docker"
            docker.write_text(
                textwrap.dedent("""\
                    #!/usr/bin/env python3
                    import json, os, sys
                    from pathlib import Path
                    if sys.argv[1:4] == ["buildx", "imagetools", "create"]:
                        with Path("published.jsonl").open("a") as output:
                            output.write(json.dumps(sys.argv[4:]) + "\\n")
                    elif sys.argv[1:4] == ["buildx", "imagetools", "inspect"]:
                        name = "ADMIN_IMAGE_DIGEST" if "-admin:" in sys.argv[-1] else "IMAGE_DIGEST"
                        print(os.environ.get("REGISTRY_DIGEST", os.environ[name]))
                    else:
                        sys.exit(2)
                    """),
                encoding="utf-8",
            )
            docker.chmod(0o755)
            environment = {
                **os.environ,
                "PATH": f"{temporary}:{os.environ['PATH']}",
                "GITHUB_REPOSITORY": "Example/Review-Agent",
                "RELEASE_TAG": "v1.2.3-rc.1",
                "IMAGE_DIGEST": runtime_digest,
                "ADMIN_IMAGE_DIGEST": admin_digest,
            }
            for mismatch in (False, True):
                with self.subTest(registry_mismatch=mismatch):
                    if mismatch:
                        environment["REGISTRY_DIGEST"] = "sha256:" + "c" * 64
                    result = subprocess.run(
                        ["bash", "-c", f"set -euo pipefail\n{command}"],
                        cwd=temporary, env=environment, capture_output=True, text=True,
                    )
                    self.assertEqual(result.returncode == 0, not mismatch, result.stderr)
                    receipts = temporary / "published.jsonl"
                    images = [("ghcr.io/example/review-agent", runtime_digest)]
                    if not mismatch:
                        images.append(("ghcr.io/example/review-agent-admin", admin_digest))
                    self.assertEqual(
                        [json.loads(line) for line in receipts.read_text().splitlines()],
                        [["--tag", f"{image}:v1.2.3-rc.1", f"{image}@{digest}"] for image, digest in images],
                    )
                    receipts.unlink()

    def test_build_metadata_and_frontend_inventory_preserve_release_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            build = temporary / "build.json"
            revision = "a" * 40
            command = [
                sys.executable,
                str(ROOT / "scripts/write_build_info.py"),
                str(build),
                "--version",
                "v1.2.3",
                "--revision",
                revision,
            ]
            subprocess.run(command, check=True, capture_output=True)
            self.assertEqual(
                json.loads(build.read_bytes()),
                {"version": "v1.2.3", "revision": revision},
            )
            rejected = subprocess.run([*command[:-1], "unknown"], capture_output=True)
            self.assertNotEqual(rejected.returncode, 0)

            lock = temporary / "package-lock.json"
            lock.write_text(
                json.dumps(
                    {
                        "packages": {
                            "": {"dependencies": {"react": "19.2.8"}},
                            "node_modules/react": {"version": "19.2.8"},
                        }
                    }
                )
            )
            native = temporary / "native.json"
            inventory = {
                "bomFormat": "CycloneDX",
                "specVersion": "1.5",
                "metadata": {},
                "components": [{"name": "react", "version": "19.2.8"}],
            }
            native.write_text(json.dumps(inventory))
            output = temporary / "release.json"
            command = [
                sys.executable,
                str(ROOT / "scripts/prepare_frontend_sbom.py"),
                str(native),
                str(lock),
                str(output),
                "--version",
                "v1.2.3",
                "--revision",
                revision,
                "--image",
                "example/admin@sha256:" + "b" * 64,
                "--platform",
                "linux/amd64",
            ]
            subprocess.run(command, check=True, capture_output=True)
            released = json.loads(output.read_bytes())
            properties = {
                item["name"]: item["value"]
                for item in released["metadata"]["properties"]
            }
            self.assertEqual(properties["review-agent:source-revision"], revision)
            self.assertEqual(
                properties["review-agent:package-lock-sha256"],
                hashlib.sha256(lock.read_bytes()).hexdigest(),
            )
            self.assertEqual(released["components"], inventory["components"])
            inventory["components"] = []
            native.write_text(json.dumps(inventory))
            rejected = subprocess.run(command, capture_output=True)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn(b"missing react@19.2.8", rejected.stderr)

    def test_release_tag_validator_enforces_semver_prerelease_identifiers(self):
        for tag in (
            "v0.1.0",
            "v0.1.0-rc.1",
            "v1.2.3-alpha.beta-2",
        ):
            with self.subTest(tag=tag):
                completed = subprocess.run(
                    [sys.executable, str(RELEASE_TAG_CHECK), tag],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(0, completed.returncode, completed.stderr)

        for tag in (
            "0.1.0",
            "v01.2.3",
            "v1.02.3",
            "v1.2.03",
            "v0.1.0-01",
            "v0.1.0-rc.01",
            "v0.1",
            "v0.1.0+build",
        ):
            with self.subTest(tag=tag):
                completed = subprocess.run(
                    [sys.executable, str(RELEASE_TAG_CHECK), tag],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertNotEqual(0, completed.returncode)

    def test_release_workflow_attaches_immutable_release_sboms(self):
        source = RELEASE_WORKFLOW.read_text(encoding="utf-8")
        jobs = mapping(workflow(RELEASE_WORKFLOW)["jobs"])
        evidence = mapping(jobs["evidence"])
        sbom = mapping(jobs["sbom"])

        self.assertEqual({"verify", "publish"}, needs(evidence))
        self.assertEqual({"verify", "publish", "evidence"}, needs(sbom))
        self.assertEqual(
            "${{ github.repository }}",
            mapping(sbom["env"])["GH_REPO"],
        )
        self.assertIn("image_digest: ${{ steps.push.outputs.digest }}", source)
        self.assertIn("scripts/generate_release_sbom.sh", source)
        self.assertIn("EXPECTED_IMAGE_DIGEST: ${{ needs.publish.outputs.image_digest }}", source)
        self.assertIn("subject-path: release-sbom/*", source)
        self.assertIn("gh release upload", source)
        self.assertIn("gh release edit", source)
        self.assertIn("--clobber", source)

        upload = named_step(evidence, "Upload release evidence")
        self.assertRegex(
            str(upload["uses"]),
            r"^actions/upload-artifact@[0-9a-f]{40}$",
        )
        self.assertEqual(
            "release-sbom-${{ github.event.release.tag_name }}",
            mapping(upload["with"])["name"],
        )
        download = named_step(sbom, "Download verified release evidence")
        self.assertRegex(
            str(download["uses"]),
            r"^actions/download-artifact@[0-9a-f]{40}$",
        )
        self.assertEqual(
            mapping(upload["with"])["name"],
            mapping(download["with"])["name"],
        )
        self.assertEqual("release-sbom", mapping(download["with"])["path"])
        self.assertEqual("error", mapping(download["with"])["digest-mismatch"])
        verify = named_step(sbom, "Verify release evidence")
        verify_command = str(verify["run"])
        self.assertIn("sha256sum --check SBOM-SHA256SUMS.txt", verify_command)
        self.assertIn("SOURCE-SHA.txt", verify_command)
        self.assertIn("${{ needs.verify.outputs.source_sha }}", str(verify["env"]))
        self.assertIn("${{ needs.publish.outputs.image_digest }}", str(verify["env"]))
        update_summary = named_step(
            sbom,
            "Update release vulnerability summary",
        )
        update_command = str(update_summary["run"])
        self.assertIn("gh release view", update_command)
        self.assertIn("review-agent-vulnerability-summary:start", update_command)
        self.assertIn("review-agent-vulnerability-summary:end", update_command)
        self.assertIn("release-sbom/VULNERABILITY-SUMMARY.md", update_command)
        self.assertIn("gh release edit", update_command)
        sbom_step_names = [
            str(mapping(step).get("name", "")) for step in sequence(sbom["steps"])
        ]
        self.assertLess(
            sbom_step_names.index("Verify release evidence"),
            sbom_step_names.index("Attest release inventories"),
        )
        self.assertLess(
            sbom_step_names.index("Attest release inventories"),
            sbom_step_names.index("Attach inventories to release"),
        )
        self.assertLess(
            sbom_step_names.index("Attach inventories to release"),
            sbom_step_names.index("Update release vulnerability summary"),
        )

        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            fake_bin = temporary / "bin"
            fake_bin.mkdir()
            fake_release_body = temporary / "release-body.md"
            original_notes = "## Existing release notes\n\nKeep this paragraph exactly.\n"
            fake_release_body.write_text(original_notes, encoding="utf-8")
            release_evidence = temporary / "release-sbom"
            release_evidence.mkdir()
            summary = textwrap.dedent(
                """\
                <!-- review-agent-vulnerability-summary:start -->
                ## Vulnerability policy

                No unapproved release-image vulnerability blocks remain.
                <!-- review-agent-vulnerability-summary:end -->
                """
            )
            (release_evidence / "VULNERABILITY-SUMMARY.md").write_text(
                summary,
                encoding="utf-8",
            )
            fake_gh = fake_bin / "gh"
            fake_gh.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env bash
                    set -euo pipefail
                    if [[ "$1 $2" == "release view" ]]; then
                      cat "$FAKE_RELEASE_BODY"
                      exit
                    fi
                    if [[ "$1 $2" == "release edit" ]]; then
                      shift 3
                      test "$1" = "--notes-file"
                      cp "$2" "$FAKE_RELEASE_BODY"
                      exit
                    fi
                    exit 2
                    """
                ),
                encoding="utf-8",
            )
            fake_gh.chmod(0o755)
            update_environment = os.environ.copy()
            update_environment.update(
                {
                    "FAKE_RELEASE_BODY": str(fake_release_body),
                    "PATH": f"{fake_bin}{os.pathsep}{update_environment['PATH']}",
                    "RELEASE_TAG": "v1.2.3",
                }
            )
            updated_bodies: list[str] = []
            for _ in range(2):
                completed = subprocess.run(
                    ["bash", "-c", f"set -euo pipefail\n{update_command}"],
                    cwd=temporary,
                    env=update_environment,
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(0, completed.returncode, completed.stderr)
                updated_bodies.append(
                    fake_release_body.read_text(encoding="utf-8")
                )
            self.assertEqual(original_notes + "\n" + summary, updated_bodies[0])
            self.assertEqual(updated_bodies[0], updated_bodies[1])

        expected_source = "a" * 40
        release_tag = "v1.2.3"
        image_name = "ghcr.io/example/review-agent"
        manifest_digest = "sha256:" + "c" * 64
        amd64_digest = "sha256:" + "d" * 64
        arm64_digest = "sha256:" + "e" * 64
        checksum_assets = [
            "IMAGE-DIGESTS.txt",
            "SOURCE-SHA.txt",
            "VULNERABILITY-POLICY.json",
            "VULNERABILITY-SUMMARY.md",
            "review-agent-v1.2.3-linux-amd64.cyclonedx.json",
            "review-agent-v1.2.3-linux-amd64.spdx.json",
            "review-agent-v1.2.3-linux-amd64.table.txt",
            "review-agent-v1.2.3-linux-arm64.cyclonedx.json",
            "review-agent-v1.2.3-linux-arm64.spdx.json",
            "review-agent-v1.2.3-linux-arm64.table.txt",
            "review-agent-python-runtime-v1.2.3-linux-amd64.cyclonedx.json",
            "vulnerability-linux-amd64.json",
            "vulnerability-linux-arm64.json",
            "review-agent-admin-v1.2.3-linux-amd64.cyclonedx.json",
            "review-agent-admin-v1.2.3-linux-amd64.spdx.json",
            "review-agent-admin-v1.2.3-linux-amd64.table.txt",
            "review-agent-admin-v1.2.3-linux-arm64.cyclonedx.json",
            "review-agent-admin-v1.2.3-linux-arm64.spdx.json",
            "review-agent-admin-v1.2.3-linux-arm64.table.txt",
            "review-agent-admin-python-runtime-v1.2.3-linux-amd64.cyclonedx.json",
            "review-agent-admin-frontend-v1.2.3-linux-amd64.cyclonedx.json",
            "review-agent-admin-frontend-v1.2.3-linux-arm64.cyclonedx.json",
            "vulnerability-admin-linux-amd64.json",
            "vulnerability-admin-linux-arm64.json",
        ]
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            release_evidence = temporary / "release-sbom"
            release_evidence.mkdir()
            for asset in checksum_assets:
                (release_evidence / asset).write_text("{}\n", encoding="utf-8")
            (release_evidence / "SOURCE-SHA.txt").write_text(
                expected_source + "\n",
                encoding="utf-8",
            )
            image_digests = release_evidence / "IMAGE-DIGESTS.txt"
            image_digests.write_text(
                textwrap.dedent(
                    f"""\
                    review-agent manifest {image_name}:{release_tag} {image_name}@{manifest_digest}
                    review-agent linux/amd64 {image_name}:{release_tag} {image_name}@{amd64_digest}
                    review-agent linux/arm64 {image_name}:{release_tag} {image_name}@{arm64_digest}
                    review-agent-admin manifest {image_name}-admin:{release_tag} {image_name}-admin@sha256:{'f' * 64}
                    review-agent-admin linux/amd64 {image_name}-admin:{release_tag} {image_name}-admin@{amd64_digest}
                    review-agent-admin linux/arm64 {image_name}-admin:{release_tag} {image_name}-admin@{arm64_digest}
                    """
                ),
                encoding="utf-8",
            )

            def refresh_checksums() -> None:
                with (release_evidence / "SBOM-SHA256SUMS.txt").open(
                    "w",
                    encoding="utf-8",
                ) as checksum_file:
                    subprocess.run(
                        ["sha256sum", "--", *checksum_assets],
                        cwd=release_evidence,
                        check=True,
                        stdout=checksum_file,
                    )

            refresh_checksums()
            environment = os.environ.copy()
            environment.update(
                {
                    "EXPECTED_IMAGE_DIGEST": manifest_digest,
                    "EXPECTED_ADMIN_IMAGE_DIGEST": "sha256:" + "f" * 64,
                    "GITHUB_REPOSITORY": "example/review-agent",
                    "RELEASE_TAG": release_tag,
                    "SOURCE_SHA": expected_source,
                }
            )
            valid = subprocess.run(
                ["bash", "-c", f"set -euo pipefail\n{verify_command}"],
                cwd=temporary,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(0, valid.returncode, valid.stderr)

            inventory = release_evidence / checksum_assets[2]
            inventory.write_text('{"changed":true}\n', encoding="utf-8")
            tampered = subprocess.run(
                ["bash", "-c", f"set -euo pipefail\n{verify_command}"],
                cwd=temporary,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(0, tampered.returncode)

            inventory.write_text("{}\n", encoding="utf-8")
            (release_evidence / "rogue.txt").write_text("unexpected\n", encoding="utf-8")
            unexpected_asset = subprocess.run(
                ["bash", "-c", f"set -euo pipefail\n{verify_command}"],
                cwd=temporary,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(0, unexpected_asset.returncode)
            (release_evidence / "rogue.txt").unlink()

            environment["SOURCE_SHA"] = "b" * 40
            wrong_source = subprocess.run(
                ["bash", "-c", f"set -euo pipefail\n{verify_command}"],
                cwd=temporary,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(0, wrong_source.returncode)

            environment["SOURCE_SHA"] = expected_source
            wrong_manifest_digest = "sha256:" + "f" * 64
            image_digests.write_text(
                image_digests.read_text(encoding="utf-8").replace(
                    manifest_digest,
                    wrong_manifest_digest,
                ),
                encoding="utf-8",
            )
            refresh_checksums()
            wrong_image = subprocess.run(
                ["bash", "-c", f"set -euo pipefail\n{verify_command}"],
                cwd=temporary,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(0, wrong_image.returncode)

        self.assertTrue(RELEASE_SBOM.is_file())
        release_sbom = RELEASE_SBOM.read_text(encoding="utf-8")
        for contract in (
            "for architecture in amd64 arm64",
            '--platform "linux/$architecture"',
            "registry:$digest_ref",
            "cyclonedx-json",
            "spdx-json",
            "syft-table",
            "IMAGE-DIGESTS.txt",
            "SBOM-SHA256SUMS.txt",
            "EXPECTED_IMAGE_DIGEST",
        ):
            self.assertIn(contract, release_sbom)

        self.assertTrue(PYTHON_RUNTIME_SBOM.is_file())
        runtime_sbom = PYTHON_RUNTIME_SBOM.read_text(encoding="utf-8")
        for contract in (
            "/opt/hermes/.venv/bin/python",
            "/cdx/requirements-release-sbom.txt",
            "--require-hashes",
            "--spec-version \"$CYCLONEDX_SPEC_VERSION\"",
            "--output-reproducible",
            "--validate",
            "installed Python distributions",
        ):
            self.assertIn(contract, runtime_sbom)
        tool_lock = RELEASE_SBOM_REQUIREMENTS.read_text(encoding="utf-8")
        self.assertIn("cyclonedx-bom==7.3.1", tool_lock)
        self.assertIn("--hash=sha256:", tool_lock)

    def test_release_policy_step_preserves_checker_failure_and_evidence(self):
        jobs = mapping(workflow(RELEASE_WORKFLOW)["jobs"])
        step = named_step(
            mapping(jobs["evidence"]), "Enforce image vulnerability policy"
        )
        for fixed_version, expected_status in (("6.5.8", 1), ("", 0)):
            with (
                self.subTest(fixed_version=fixed_version),
                tempfile.TemporaryDirectory() as directory,
            ):
                temporary = Path(directory)
                (temporary / "scripts").symlink_to(
                    ROOT / "scripts", target_is_directory=True
                )
                evidence = temporary / "release-sbom"
                evidence.mkdir()
                reports = temporary / "vulnerability-reports"
                reports.mkdir()
                (temporary / "release-image-critical-exceptions.json").write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "scope": "release-image",
                            "expires_on": "2099-01-01",
                            "exceptions": [],
                        }
                    ),
                    encoding="utf-8",
                )
                for platform in ("amd64", "arm64"):
                    (reports / f"vulnerability-linux-{platform}.json").write_text(
                        json.dumps(
                            {
                                "Results": [
                                    {
                                        "Target": "Python",
                                        "Vulnerabilities": [
                                            {
                                                "Severity": "HIGH",
                                                "FixedVersion": fixed_version,
                                                "VulnerabilityID": "CVE-2099-0001",
                                                "PkgName": "tornado",
                                                "InstalledVersion": "6.5.7",
                                            }
                                        ],
                                    }
                                ]
                            }
                        ),
                        encoding="utf-8",
                    )
                for platform in ("amd64", "arm64"):
                    (reports / f"vulnerability-admin-linux-{platform}.json").write_text(
                        json.dumps({"Results": [{"Target": "Python", "Vulnerabilities": []}]}),
                        encoding="utf-8",
                    )
                completed = subprocess.run(
                    ["bash", "-e", "-c", str(step["run"])],
                    cwd=temporary,
                    env={
                        **os.environ,
                        "AMD64_SCAN_OUTCOME": "success",
                        "ARM64_SCAN_OUTCOME": "success",
                        "ADMIN_AMD64_SCAN_OUTCOME": "success",
                        "ADMIN_ARM64_SCAN_OUTCOME": "success",
                    },
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(
                    expected_status, completed.returncode, completed.stderr
                )
                policy = json.loads(
                    (evidence / "VULNERABILITY-POLICY.json").read_text()
                )
                self.assertEqual(
                    2 if fixed_version else 0, policy["blocking_vulnerabilities"]
                )
                self.assertEqual(
                    not fixed_version, (evidence / "VULNERABILITY-SUMMARY.md").is_file()
                )

    def test_release_vulnerability_gate_scans_each_exact_platform_digest(self):
        jobs = mapping(workflow(RELEASE_WORKFLOW)["jobs"])
        evidence_job = mapping(jobs["evidence"])
        steps = [mapping(step) for step in sequence(evidence_job["steps"])]
        step_names = [str(step.get("name", "")) for step in steps]

        record = named_step(evidence_job, "Record release platform digests")
        record_command = str(record["run"])
        self.assertIn("release-sbom/IMAGE-DIGESTS.txt", record_command)
        self.assertIn("linux/amd64", record_command)
        self.assertIn("linux/arm64", record_command)
        amd64_digest = "sha256:" + "a" * 64
        arm64_digest = "sha256:" + "b" * 64
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            release_evidence = temporary / "release-sbom"
            release_evidence.mkdir()
            (release_evidence / "IMAGE-DIGESTS.txt").write_text(
                textwrap.dedent(
                    f"""\
                    review-agent manifest ghcr.io/example/review-agent:v1.2.3 ghcr.io/example/review-agent@sha256:{'c' * 64}
                    review-agent linux/amd64 ghcr.io/example/review-agent:v1.2.3 ghcr.io/example/review-agent@{amd64_digest}
                    review-agent linux/arm64 ghcr.io/example/review-agent:v1.2.3 ghcr.io/example/review-agent@{arm64_digest}
                    review-agent-admin manifest ghcr.io/example/review-agent-admin:v1.2.3 ghcr.io/example/review-agent-admin@sha256:{'f' * 64}
                    review-agent-admin linux/amd64 ghcr.io/example/review-agent-admin:v1.2.3 ghcr.io/example/review-agent-admin@{amd64_digest}
                    review-agent-admin linux/arm64 ghcr.io/example/review-agent-admin:v1.2.3 ghcr.io/example/review-agent-admin@{arm64_digest}
                    """
                ),
                encoding="utf-8",
            )
            github_environment = temporary / "github-env"
            environment = os.environ.copy()
            environment.update(
                {
                    "GITHUB_ENV": str(github_environment),
                    "IMAGE_NAME": "ghcr.io/example/review-agent",
                }
            )
            completed = subprocess.run(
                ["bash", "-c", f"set -euo pipefail\n{record_command}"],
                cwd=temporary,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertEqual(
                [
                    "AMD64_IMAGE=ghcr.io/example/review-agent@" + amd64_digest,
                    "ARM64_IMAGE=ghcr.io/example/review-agent@" + arm64_digest,
                    "ADMIN_AMD64_IMAGE=ghcr.io/example/review-agent-admin@" + amd64_digest,
                    "ADMIN_ARM64_IMAGE=ghcr.io/example/review-agent-admin@" + arm64_digest,
                ],
                github_environment.read_text(encoding="utf-8").splitlines(),
            )

        scans = (
            (
                "Scan linux/amd64 image vulnerabilities",
                "${{ env.AMD64_IMAGE }}",
                "vulnerability-reports/vulnerability-linux-amd64.json",
            ),
            (
                "Scan linux/arm64 image vulnerabilities",
                "${{ env.ARM64_IMAGE }}",
                "vulnerability-reports/vulnerability-linux-arm64.json",
            ),
            (
                "Scan linux/amd64 admin image vulnerabilities",
                "${{ env.ADMIN_AMD64_IMAGE }}",
                "vulnerability-reports/vulnerability-admin-linux-amd64.json",
            ),
            (
                "Scan linux/arm64 admin image vulnerabilities",
                "${{ env.ADMIN_ARM64_IMAGE }}",
                "vulnerability-reports/vulnerability-admin-linux-arm64.json",
            ),
        )
        for name, image_ref, report in scans:
            scan = named_step(evidence_job, name)
            self.assertRegex(
                str(scan["uses"]),
                r"^aquasecurity/trivy-action@[0-9a-f]{40}$",
            )
            self.assertEqual("true", scan["continue-on-error"])
            inputs = mapping(scan["with"])
            self.assertEqual("image", inputs["scan-type"])
            self.assertEqual(image_ref, inputs["image-ref"])
            self.assertEqual("trivy.yaml", inputs["trivy-config"])
            self.assertEqual(report, inputs["output"])
            self.assertEqual("v0.74.0", inputs["version"])

        retain = named_step(evidence_job, "Retain image vulnerability reports")
        self.assertEqual("${{ always() }}", retain["if"])
        self.assertEqual(
            "vulnerability-reports/*.json",
            mapping(retain["with"])["path"],
        )
        enforce = named_step(evidence_job, "Enforce image vulnerability policy")
        self.assertEqual("${{ always() }}", enforce["if"])
        self.assertEqual(
            {"AMD64_SCAN_OUTCOME", "ARM64_SCAN_OUTCOME", "ADMIN_AMD64_SCAN_OUTCOME", "ADMIN_ARM64_SCAN_OUTCOME"},
            set(mapping(enforce["env"])),
        )
        enforce_command = str(enforce["run"])
        self.assertIn("scripts/check_trivy_report.py", enforce_command)
        self.assertIn(
            "--critical-exceptions release-image-critical-exceptions.json",
            enforce_command,
        )
        self.assertIn(
            "--markdown-output release-sbom/VULNERABILITY-SUMMARY.md",
            enforce_command,
        )
        self.assertIn(
            "vulnerability-reports/vulnerability-linux-amd64.json",
            enforce_command,
        )
        self.assertIn(
            "vulnerability-reports/vulnerability-linux-arm64.json",
            enforce_command,
        )
        evidence = str(
            named_step(evidence_job, "Add scan reports to release evidence")["run"]
        )
        self.assertIn("SBOM-SHA256SUMS.txt", evidence)
        self.assertIn("SOURCE-SHA.txt", evidence)
        self.assertIn("VULNERABILITY-POLICY.json", evidence)
        self.assertIn("VULNERABILITY-SUMMARY.md", evidence)

        smoke = named_step(
            evidence_job,
            "Smoke exact linux/amd64 release image",
        )
        self.assertEqual(
            'bash ./scripts/check_image.sh "$AMD64_IMAGE"',
            str(smoke["run"]),
        )
        admin_smoke = named_step(evidence_job, "Smoke exact linux/amd64 admin image")
        self.assertEqual(
            'sh ./scripts/check_admin_image.sh "$ADMIN_AMD64_IMAGE"',
            str(admin_smoke["run"]),
        )

        self.assertLess(
            step_names.index("Generate release inventories"),
            step_names.index("Record release platform digests"),
        )
        self.assertLess(
            step_names.index("Record release platform digests"),
            step_names.index("Smoke exact linux/amd64 release image"),
        )
        self.assertLess(
            step_names.index("Smoke exact linux/amd64 release image"),
            step_names.index("Scan linux/amd64 image vulnerabilities"),
        )
        self.assertLess(
            step_names.index("Smoke exact linux/amd64 admin image"),
            step_names.index("Scan linux/amd64 image vulnerabilities"),
        )
        self.assertLess(
            step_names.index("Enforce image vulnerability policy"),
            step_names.index("Upload release evidence"),
        )

    def test_release_sbom_generation_uses_only_immutable_digests(self):
        manifest_digest = "sha256:" + "c" * 64
        amd64_digest = "sha256:" + "a" * 64
        arm64_digest = "sha256:" + "b" * 64
        manifest = {
            "schemaVersion": 2,
            "manifests": [
                {
                    "digest": amd64_digest,
                    "platform": {"os": "linux", "architecture": "amd64"},
                },
                {
                    "digest": arm64_digest,
                    "platform": {"os": "linux", "architecture": "arm64"},
                },
            ],
        }

        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            fake_bin = temporary / "bin"
            output = temporary / "output"
            calls = temporary / "calls.jsonl"
            fake_bin.mkdir()

            docker = fake_bin / "docker"
            docker.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import json
                    import os
                    from pathlib import Path
                    import sys

                    arguments = sys.argv[1:]
                    with open(os.environ["CALLS_LOG"], "a", encoding="utf-8") as handle:
                        handle.write(json.dumps(["docker", *arguments]) + "\\n")

                    if arguments[:3] == ["buildx", "imagetools", "inspect"]:
                        if arguments[-2:] == ["--format", "{{json .Image}}"]:
                            manifest = json.loads(os.environ["RAW_MANIFEST"])
                            expected = {
                                os.environ[key].split("@")[0] + "@" + image["digest"]
                                for key in ("EXPECTED_MANIFEST_REF", "EXPECTED_ADMIN_MANIFEST_REF")
                                for image in manifest["manifests"]
                            }
                            if arguments[3] not in expected:
                                raise SystemExit("config was not selected by immutable platform digest")
                            print(json.dumps({"config": {"Labels": {
                                "org.opencontainers.image.revision": os.environ.get("REGISTRY_SOURCE_SHA", os.environ["SOURCE_SHA"]),
                                "org.opencontainers.image.version": "v1.2.3"
                            }}}))
                            raise SystemExit(0)
                        if arguments[-1] != "--raw":
                            raise SystemExit("only the immutable raw manifest may be read")
                        if arguments[3] not in (os.environ["EXPECTED_MANIFEST_REF"], os.environ["EXPECTED_ADMIN_MANIFEST_REF"]):
                            raise SystemExit("manifest was not selected by immutable digest")
                        print(os.environ["RAW_MANIFEST"])
                        raise SystemExit(0)

                    if arguments[0] == "run":
                        output_mount = next(
                            value.split(":", 1)[0]
                            for index, value in enumerate(arguments)
                            if arguments[index - 1] == "-v" and value.endswith(":/out")
                        )
                        output_name = Path(arguments[-2]).name
                        target = Path(output_mount) / output_name
                        target.write_text(
                            json.dumps(
                                {
                                    "bomFormat": "CycloneDX",
                                    "specVersion": "1.7",
                                    "components": [{"name": "runtime", "version": "1"}],
                                }
                            ),
                            encoding="utf-8",
                        )
                        raise SystemExit(0)

                    if arguments[0] in ("pull", "rm"):
                        raise SystemExit(0)
                    if arguments[0] == "create":
                        print("release-evidence-container")
                        raise SystemExit(0)
                    if arguments[0] == "cp":
                        import hashlib
                        target = Path(arguments[2])
                        source = arguments[1]
                        if source.endswith("/_build.json"):
                            value = {"version": "v1.2.3", "revision": os.environ.get("IMAGE_SOURCE_SHA", os.environ["SOURCE_SHA"])}
                            target.write_text(json.dumps(value))
                        else:
                            lock_bytes = Path(os.environ["FRONTEND_LOCK"]).read_bytes()
                            if source.endswith("/frontend-lock.sha256"):
                                digest = os.environ.get("IMAGE_LOCK_SHA", hashlib.sha256(lock_bytes).hexdigest())
                                target.write_text(digest + "  package-lock.json\\n")
                            elif source.endswith("/frontend.cyclonedx.json"):
                                lock = json.loads(lock_bytes)["packages"]
                                target.write_text(json.dumps({"bomFormat": "CycloneDX", "specVersion": "1.5", "metadata": {}, "components": [
                                    {"name": name, "version": lock["node_modules/" + name]["version"]}
                                    for name in lock[""]["dependencies"]
                                ]}))
                            else:
                                raise SystemExit("unexpected container file")
                        raise SystemExit(0)

                    raise SystemExit("unexpected docker command")
                    """
                ),
                encoding="utf-8",
            )
            docker.chmod(0o755)

            syft = fake_bin / "syft"
            syft.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import json
                    import os
                    from pathlib import Path
                    import sys

                    arguments = sys.argv[1:]
                    with open(os.environ["CALLS_LOG"], "a", encoding="utf-8") as handle:
                        handle.write(json.dumps(["syft", *arguments]) + "\\n")
                    for index, argument in enumerate(arguments):
                        if argument != "-o":
                            continue
                        output_format, output_path = arguments[index + 1].split("=", 1)
                        if output_format == "cyclonedx-json":
                            value = {
                                "bomFormat": "CycloneDX",
                                "components": [{"name": "image", "version": "1"}],
                            }
                        elif output_format == "spdx-json":
                            value = {
                                "spdxVersion": "SPDX-2.3",
                                "packages": [{"name": "image"}],
                            }
                        else:
                            Path(output_path).write_text("NAME VERSION\\nimage 1\\n", encoding="utf-8")
                            continue
                        Path(output_path).write_text(json.dumps(value), encoding="utf-8")
                    """
                ),
                encoding="utf-8",
            )
            syft.chmod(0o755)

            environment = os.environ.copy()
            environment.update(
                {
                    "CALLS_LOG": str(calls),
                    "SOURCE_SHA": "a" * 40,
                    "FRONTEND_LOCK": str(ROOT / "admin/package-lock.json"),
                    "CYCLONEDX_SPEC_VERSION": "1.7",
                    "EXPECTED_IMAGE_DIGEST": manifest_digest,
                    "EXPECTED_ADMIN_IMAGE_DIGEST": "sha256:" + "f" * 64,
                    "EXPECTED_ADMIN_MANIFEST_REF": "ghcr.io/example/review-agent-admin@sha256:" + "f" * 64,
                    "EXPECTED_MANIFEST_REF": (
                        f"ghcr.io/example/review-agent@{manifest_digest}"
                    ),
                    "PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}",
                    "RAW_MANIFEST": json.dumps(manifest),
                    "SYFT_CMD": str(syft),
                }
            )
            completed = subprocess.run(
                [
                    str(RELEASE_SBOM),
                    "ghcr.io/example/review-agent",
                    "v1.2.3",
                    str(output),
                    "ghcr.io/example/review-agent-admin",
                ],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertEqual(0, completed.returncode, completed.stderr)

            recorded_calls = [
                json.loads(line)
                for line in calls.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(
                [
                    "docker",
                    "buildx",
                    "imagetools",
                    "inspect",
                    f"ghcr.io/example/review-agent@{manifest_digest}",
                    "--raw",
                ],
                recorded_calls[0],
            )
            syft_sources = [call[1] for call in recorded_calls if call[0] == "syft"]
            created_images = [call[-1] for call in recorded_calls if call[:2] == ["docker", "create"]]
            self.assertEqual(created_images, [
                f"ghcr.io/example/review-agent-admin@{amd64_digest}",
                f"ghcr.io/example/review-agent-admin@{arm64_digest}",
            ])
            self.assertEqual(
                [
                    f"registry:ghcr.io/example/review-agent@{amd64_digest}",
                    f"registry:ghcr.io/example/review-agent@{arm64_digest}",
                    f"registry:ghcr.io/example/review-agent-admin@{amd64_digest}",
                    f"registry:ghcr.io/example/review-agent-admin@{arm64_digest}",
                ],
                syft_sources,
            )
            runtime_call = next(
                call for call in recorded_calls if call[:2] == ["docker", "run"]
            )
            self.assertEqual(
                f"ghcr.io/example/review-agent@{amd64_digest}", runtime_call[-4]
            )
            checksums = (output / "SBOM-SHA256SUMS.txt").read_text(
                encoding="utf-8"
            )
            self.assertEqual(17, len(checksums.splitlines()))
            checksum_check = subprocess.run(
                ["sha256sum", "--check", "SBOM-SHA256SUMS.txt"],
                cwd=output,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(0, checksum_check.returncode, checksum_check.stderr)

            for key, value, message in (
                ("IMAGE_SOURCE_SHA", "b" * 40, "build metadata does not match"),
                ("IMAGE_LOCK_SHA", "f" * 64, "frontend lockfile does not match"),
                ("REGISTRY_SOURCE_SHA", "b" * 40, "registry labels do not match"),
            ):
                with self.subTest(key=key):
                    rejected = subprocess.run(
                        [str(RELEASE_SBOM), "ghcr.io/example/review-agent", "v1.2.3",
                         str(temporary / key), "ghcr.io/example/review-agent-admin"],
                        capture_output=True, text=True, env={**environment, key: value},
                    )
                    self.assertNotEqual(rejected.returncode, 0)
                    self.assertIn(message, rejected.stderr)
                    if key != "REGISTRY_SOURCE_SHA":
                        last_call = json.loads(calls.read_text().splitlines()[-1])
                        self.assertEqual(last_call, ["docker", "rm", "--volumes", "release-evidence-container"])

    def test_release_sbom_requires_one_digest_for_each_platform(self):
        manifest_digest = "sha256:" + "c" * 64
        base_manifest = {
            "schemaVersion": 2,
            "manifests": [
                {
                    "digest": "sha256:" + "b" * 64,
                    "platform": {"os": "linux", "architecture": "arm64"},
                }
            ],
        }

        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            docker = temporary / "docker"
            docker.write_text(
                "#!/bin/sh\nprintf '%s\\n' \"$RAW_MANIFEST\"\n",
                encoding="utf-8",
            )
            docker.chmod(0o755)
            syft = temporary / "syft"
            syft.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
            syft.chmod(0o755)
            environment = os.environ.copy()
            environment.update(
                {
                    "CYCLONEDX_SPEC_VERSION": "1.7",
                    "EXPECTED_IMAGE_DIGEST": manifest_digest,
                    "EXPECTED_ADMIN_IMAGE_DIGEST": "sha256:" + "f" * 64,
                    "PATH": f"{temporary}{os.pathsep}{environment['PATH']}",
                    "RAW_MANIFEST": json.dumps(base_manifest),
                    "SOURCE_SHA": "a" * 40,
                    "SYFT_CMD": str(syft),
                }
            )
            completed = subprocess.run(
                [
                    str(RELEASE_SBOM),
                    "ghcr.io/example/review-agent",
                    "v1.2.3",
                    str(temporary / "output"),
                    "ghcr.io/example/review-agent-admin",
                ],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertNotEqual(0, completed.returncode)
            self.assertIn("expected exactly one linux/amd64 image", completed.stderr)

    def test_postgresql_contract_uses_pinned_loopback_only_databases(self):
        source = POSTGRES_CHECK.read_text(encoding="utf-8")
        image = (
            "postgres:17.10-bookworm@"
            "sha256:9b18b78397054fce88a9552e9d5a3ad5bb7fd258c5b3cc1c5028e46373d6ea8f"
        )

        self.assertEqual(source.count(image), 1)
        self.assertIn('cd "$ROOT"', source)
        self.assertIn("docker run", source)
        self.assertIn("--rm", source)
        self.assertIn("docker rm --force", source)
        self.assertIn("trap ", source)
        self.assertIn('REVIEW_AGENT_POSTGRES_CONTAINER="$CONTAINER"', source)
        for test_module in (
            "tests.test_postgres_schema",
            "tests.test_postgres_migrations",
            "tests.test_postgres_runtime",
        ):
            self.assertIn(test_module, source)
        self.assertIn("--publish 127.0.0.1::5432", source)
        self.assertIn('RESTORE_CONTAINER="review-agent-postgres-restore-$$"', source)
        self.assertIn("pg_dump", source)
        self.assertIn("pg_restore", source)
        self.assertIn("--exit-on-error", source)
        self.assertIn("review_agent_admin.py database prepare", source)
        self.assertIn("review_agent_admin.py database ready", source)
        self.assertIn("recovery/probe", source)
        self.assertIn("application-state canary", source)
        self.assertNotIn("0.0.0.0", source)
        self.assertNotRegex(source, r"127\.0\.0\.1:[0-9]+:5432")


class MigrationReadinessDocumentationTests(unittest.TestCase):
    def test_public_status_names_the_current_postgresql_contract(self):
        roadmap = ROADMAP.read_text(encoding="utf-8")
        homepage = HOMEPAGE.read_text(encoding="utf-8")
        readme = README.read_text(encoding="utf-8")
        normalized_roadmap = re.sub(r"\s+", " ", roadmap)
        normalized_homepage = re.sub(r"\s+", " ", homepage)

        for current_capability in (
            "Bounded PR reads",
            "Checksum-verified PostgreSQL migrations",
            "Direct PostgreSQL review",
            "Durable PostgreSQL job records",
            "Repository-scoped exports",
        ):
            self.assertIn(current_capability, normalized_roadmap)

        for current_invariant in (
            "One PostgreSQL database per environment",
            "PostgreSQL owns application persistence",
            "Hermes `HERMES_HOME` remains separate",
            "Network and model calls never hold database connections",
        ):
            self.assertIn(current_invariant, normalized_roadmap)

        for current_reliability in (
            "exact-run continuation",
            "activated through signed admission",
            "recoverable publisher lease",
        ):
            self.assertIn(current_reliability, normalized_roadmap)
        self.assertNotIn(
            "Typed ownership and trusted project context come before PostgreSQL",
            homepage,
        )
        self.assertIn("one PostgreSQL database per environment", normalized_homepage)
        self.assertIn("durable review jobs", normalized_homepage)
        self.assertIn("transactional publication outbox", normalized_homepage)
        self.assertIn("GitHub Actions runs the same bundle", readme)


if __name__ == "__main__":
    unittest.main()
