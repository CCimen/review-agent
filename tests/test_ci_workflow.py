from __future__ import annotations

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

        self.assertIn("python-version: '3.11'", source)
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
        for manifest in (
            "requirements.txt",
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
        self.assertEqual(True, mapping(policy["pkg"])["include-dev-deps"])
        self.assertEqual(False, mapping(policy["vulnerability"])["ignore-unfixed"])

    def test_fast_quality_tools_are_pinned_and_cover_production_entrypoints(self):
        self.assertEqual(
            DEVELOPMENT_REQUIREMENTS.read_text(encoding="utf-8"),
            "ruff==0.14.4\n",
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
                self.assertIn(job_id, {"publish", "sbom"})

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
        self.assertIn(
            "type=raw,value=latest,enable=${{ github.event.release.prerelease == false }}",
            source,
        )
        self.assertIn("password: ${{ secrets.GITHUB_TOKEN }}", source)
        self.assertIn("provenance: mode=max", source)
        self.assertIn("sbom: true", source)
        self.assertIn("subject-name: ${{ env.IMAGE_NAME }}", source)
        self.assertIn("subject-digest: ${{ steps.push.outputs.digest }}", source)
        self.assertIn("push-to-registry: true", source)

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
        self.assertIn("image_digest: ${{ steps.push.outputs.digest }}", source)
        self.assertIn("scripts/generate_release_sbom.sh", source)
        self.assertIn("EXPECTED_IMAGE_DIGEST: ${{ needs.publish.outputs.image_digest }}", source)
        self.assertIn("subject-path: release-sbom/*", source)
        self.assertIn("gh release upload", source)
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
        sbom_step_names = [
            str(mapping(step).get("name", "")) for step in sequence(sbom["steps"])
        ]
        self.assertLess(
            sbom_step_names.index("Verify release evidence"),
            sbom_step_names.index("Attest release inventories"),
        )
        self.assertLess(
            sbom_step_names.index("Verify release evidence"),
            sbom_step_names.index("Attach inventories to release"),
        )

        expected_source = "a" * 40
        release_tag = "v1.2.3"
        image_name = "ghcr.io/example/review-agent"
        manifest_digest = "sha256:" + "c" * 64
        amd64_digest = "sha256:" + "d" * 64
        arm64_digest = "sha256:" + "e" * 64
        checksum_assets = [
            "IMAGE-DIGESTS.txt",
            "SOURCE-SHA.txt",
            "review-agent-v1.2.3-linux-amd64.cyclonedx.json",
            "review-agent-v1.2.3-linux-amd64.spdx.json",
            "review-agent-v1.2.3-linux-amd64.table.txt",
            "review-agent-v1.2.3-linux-arm64.cyclonedx.json",
            "review-agent-v1.2.3-linux-arm64.spdx.json",
            "review-agent-v1.2.3-linux-arm64.table.txt",
            "review-agent-python-runtime-v1.2.3-linux-amd64.cyclonedx.json",
            "vulnerability-linux-amd64.json",
            "vulnerability-linux-arm64.json",
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
            {"AMD64_SCAN_OUTCOME", "ARM64_SCAN_OUTCOME"},
            set(mapping(enforce["env"])),
        )
        enforce_command = str(enforce["run"])
        self.assertIn("scripts/check_trivy_report.py", enforce_command)
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

        self.assertLess(
            step_names.index("Generate release inventories"),
            step_names.index("Record release platform digests"),
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
                        if arguments[-1] != "--raw":
                            raise SystemExit("only the immutable raw manifest may be read")
                        if arguments[3] != os.environ["EXPECTED_MANIFEST_REF"]:
                            raise SystemExit("manifest was not selected by immutable digest")
                        print(os.environ["RAW_MANIFEST"])
                        raise SystemExit(0)

                    if arguments[0] == "run":
                        output_mount = next(
                            value.split(":", 1)[0]
                            for index, value in enumerate(arguments)
                            if arguments[index - 1] == "-v" and value.endswith(":/out")
                        )
                        output_name = Path(arguments[-1]).name
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
                    "CYCLONEDX_SPEC_VERSION": "1.7",
                    "EXPECTED_IMAGE_DIGEST": manifest_digest,
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
            self.assertEqual(
                [
                    f"registry:ghcr.io/example/review-agent@{amd64_digest}",
                    f"registry:ghcr.io/example/review-agent@{arm64_digest}",
                ],
                syft_sources,
            )
            runtime_call = next(
                call for call in recorded_calls if call[:2] == ["docker", "run"]
            )
            self.assertEqual(
                f"ghcr.io/example/review-agent@{amd64_digest}", runtime_call[-3]
            )
            checksums = (output / "SBOM-SHA256SUMS.txt").read_text(
                encoding="utf-8"
            )
            self.assertEqual(8, len(checksums.splitlines()))
            checksum_check = subprocess.run(
                ["sha256sum", "--check", "SBOM-SHA256SUMS.txt"],
                cwd=output,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(0, checksum_check.returncode, checksum_check.stderr)

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
                    "PATH": f"{temporary}{os.pathsep}{environment['PATH']}",
                    "RAW_MANIFEST": json.dumps(base_manifest),
                    "SYFT_CMD": str(syft),
                }
            )
            completed = subprocess.run(
                [
                    str(RELEASE_SBOM),
                    "ghcr.io/example/review-agent",
                    "v1.2.3",
                    str(temporary / "output"),
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
        self.assertIn("review_agent_admin.py database migrate", source)
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
