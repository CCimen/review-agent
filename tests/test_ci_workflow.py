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
            ("required", "CI / required"),
        ):
            self.assertEqual(job_name, mapping(jobs[job_id])["name"])
        required = mapping(jobs["required"])
        self.assertEqual(
            {"python-fast", "postgres-contract", "image-smoke"},
            needs(required),
        )
        self.assertEqual("${{ always() }}", required["if"])
        self.assertIn("PYTHON_FAST_RESULT: ${{ needs.python-fast.result }}", source)
        self.assertIn("POSTGRES_RESULT: ${{ needs.postgres-contract.result }}", source)
        self.assertIn("IMAGE_RESULT: ${{ needs.image-smoke.result }}", source)
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
        sbom = mapping(jobs["sbom"])

        self.assertEqual({"verify"}, needs(quality))
        self.assertEqual("./.github/workflows/ci.yml", quality["uses"])
        self.assertEqual({"contents": "read"}, mapping(quality["permissions"]))
        self.assertNotIn("secrets", quality)
        self.assertEqual({"verify", "quality"}, needs(publish))
        self.assertNotIn("if", publish)
        self.assertEqual({"verify", "publish"}, needs(sbom))

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
                "contents": "write",
                "packages": "read",
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
            (sbom, "Check out verified source"),
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

        self.assertEqual({"verify", "publish"}, needs(mapping(jobs["sbom"])))
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
