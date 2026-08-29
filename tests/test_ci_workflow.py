from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


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
ROADMAP = ROOT / "docs" / "ROADMAP.md"
HOMEPAGE = ROOT / "website" / "src" / "pages" / "index.tsx"
README = ROOT / "README.md"


class PythonBundleWorkflowTests(unittest.TestCase):
    def test_required_quality_gates_run_independently_in_read_only_ci(self):
        self.assertTrue(WORKFLOW.is_file(), "full Python bundle CI is missing")
        source = WORKFLOW.read_text(encoding="utf-8")

        expected_header = """name: Python bundle

on:
  pull_request:
  merge_group:
  push:
    branches: [main]
  workflow_dispatch:

permissions:
  contents: read

concurrency:
  group: ci-${{ github.workflow }}-${{ github.event.pull_request.number || github.ref }}
  cancel-in-progress: true
"""
        self.assertTrue(source.startswith(expected_header))
        self.assertNotIn("pull_request_target", source)
        self.assertNotIn("secrets.", source)
        self.assertEqual(source.count("permissions:"), 1)
        self.assertNotRegex(source, r"(?m)^\s+[^:#]+:\s*write\b")

        expected_actions = [
            "actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803",
            "actions/setup-python@e797f83bcb11b83ae66e0230d6156d7c80228e7c",
            "actions/setup-node@a0853c24544627f65ddf259abe73b1d18a591444",
            "actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803",
            "actions/setup-python@e797f83bcb11b83ae66e0230d6156d7c80228e7c",
            "actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803",
        ]
        actions = re.findall(r"(?m)^\s+uses: ([^\s]+)$", source)
        self.assertEqual(actions, expected_actions)
        for action in actions:
            self.assertRegex(action, r"@[0-9a-f]{40}$")

        self.assertIn("python-version: '3.11'", source)
        checkout_stanza = re.compile(
            r"(?m)^      - name: Check out repository\n"
            r"        uses: actions/checkout@"
            r"d23441a48e516b6c34aea4fa41551a30e30af803\n"
            r"        with:\n"
            r"          persist-credentials: false$"
        )
        self.assertEqual(len(checkout_stanza.findall(source)), 3)
        self.assertEqual(source.count("persist-credentials: false"), 3)
        for job_id, job_name in (
            ("python-fast", "Python fast"),
            ("postgres-contract", "PostgreSQL contract"),
            ("image-smoke", "Image smoke"),
            ("required", "CI / required"),
        ):
            self.assertRegex(
                source,
                rf"(?m)^  {re.escape(job_id)}:\n    name: {re.escape(job_name)}$",
            )
        self.assertIn("needs: [python-fast, postgres-contract, image-smoke]", source)
        self.assertIn("if: ${{ always() }}", source)
        self.assertIn("PYTHON_FAST_RESULT: ${{ needs.python-fast.result }}", source)
        self.assertIn("POSTGRES_RESULT: ${{ needs.postgres-contract.result }}", source)
        self.assertIn("IMAGE_RESULT: ${{ needs.image-smoke.result }}", source)
        self.assertEqual(source.count("cache: pip"), 2)
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

    def test_fast_quality_tools_are_pinned_and_cover_production_entrypoints(self):
        self.assertEqual(
            DEVELOPMENT_REQUIREMENTS.read_text(encoding="utf-8"),
            "ruff==0.14.4\n",
        )
        ruff = RUFF_CONFIG.read_text(encoding="utf-8")
        self.assertIn('target-version = "py311"', ruff)
        self.assertIn('select = ["E4", "E7", "E9", "F"]', ruff)
        self.assertNotIn("ignore = [\"F", ruff)
        self.assertIn("ruff check", PYTHON_CHECK.read_text(encoding="utf-8"))

        pyright = json.loads(PYRIGHT_CONFIG.read_text(encoding="utf-8"))
        includes = set(pyright["include"])
        self.assertIn("bootstrap/install.py", includes)
        self.assertIn("bootstrap/plugins/review_agent_tools", includes)
        self.assertIn("tools", includes)

    def test_release_workflow_publishes_only_versioned_release_images(self):
        source = RELEASE_WORKFLOW.read_text(encoding="utf-8")

        expected_header = """name: Publish container image

on:
  release:
    types: [published]

permissions:
  contents: read
"""
        self.assertTrue(source.startswith(expected_header))
        self.assertNotIn("pull_request_target", source)
        self.assertNotIn("workflow_dispatch", source)
        self.assertEqual(source.count("packages: write"), 1)
        self.assertIn("group: release-image", source)
        self.assertIn("needs: verify", source)
        self.assertIn("source_sha: ${{ steps.source.outputs.sha }}", source)
        self.assertIn("ref: ${{ needs.verify.outputs.source_sha }}", source)
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
        self.assertEqual(source.count("persist-credentials: false"), 3)
        self.assertEqual(
            source.count("ref: ${{ github.event.release.tag_name }}"), 1
        )
        self.assertEqual(source.count("ref: ${{ needs.verify.outputs.source_sha }}"), 2)
        self.assertIn("git rev-list -n 1 refs/tags/release-candidate", source)
        self.assertIn("docker build --tag review-agent:release-candidate .", source)
        self.assertIn(
            "bash ./scripts/check_image.sh review-agent:release-candidate", source
        )
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

        actions = re.findall(r"(?m)^\s+uses: ([^\s]+)$", source)
        self.assertEqual(
            actions,
            [
                "actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803",
                "actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803",
                "docker/setup-qemu-action@c7c53464625b32c7a7e944ae62b3e17d2b600130",
                "docker/setup-buildx-action@8d2750c68a42422c14e847fe6c8ac0403b4cbd6f",
                "docker/login-action@c94ce9fb468520275223c153574b00df6fe4bcc9",
                "docker/metadata-action@c299e40c65443455700f0fdfc63efafe5b349051",
                "docker/build-push-action@10e90e3645eae34f1e60eeb005ba3a3d33f178e8",
                "actions/attest@1e69f48acb82d1966a394da916b4c1698aa569d6",
                "actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803",
                "docker/setup-buildx-action@8d2750c68a42422c14e847fe6c8ac0403b4cbd6f",
                "docker/login-action@c94ce9fb468520275223c153574b00df6fe4bcc9",
                "anchore/sbom-action/download-syft@e22c389904149dbc22b58101806040fa8d37a610",
                "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02",
                "actions/attest@1e69f48acb82d1966a394da916b4c1698aa569d6",
            ],
        )
        for action in actions:
            self.assertRegex(action, r"@[0-9a-f]{40}$")

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

        self.assertIn("needs: [verify, publish]", source)
        self.assertIn("image_digest: ${{ steps.push.outputs.digest }}", source)
        self.assertIn("contents: write", source)
        self.assertIn("packages: read", source)
        self.assertIn("artifact-metadata: write", source)
        self.assertIn("scripts/generate_release_sbom.sh", source)
        self.assertIn("EXPECTED_IMAGE_DIGEST: ${{ needs.publish.outputs.image_digest }}", source)
        self.assertIn("subject-path: release-sbom/*", source)
        self.assertIn("gh release upload", source)
        self.assertIn("--clobber", source)

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
