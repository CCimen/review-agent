from __future__ import annotations

import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from fnmatch import fnmatch
import importlib.util
from pathlib import Path
import types


ROOT = Path(__file__).resolve().parents[1]


def _load_install_module():
    spec = importlib.util.spec_from_file_location(
        "review_agent_bootstrap_install", ROOT / "bootstrap" / "install.py"
    )
    if spec is None or spec.loader is None:
        raise AssertionError("could not load bootstrap installer")
    module = importlib.util.module_from_spec(spec)
    previous_yaml = sys.modules.get("yaml")
    yaml_stub = types.ModuleType("yaml")
    yaml_stub.safe_load = lambda _text: {}
    yaml_stub.safe_dump = lambda *_args, **_kwargs: ""
    sys.modules["yaml"] = yaml_stub
    try:
        spec.loader.exec_module(module)
    finally:
        if previous_yaml is None:
            sys.modules.pop("yaml", None)
        else:
            sys.modules["yaml"] = previous_yaml
    return module


def _docker_copy_sources() -> list[str]:
    sources: list[str] = []
    for raw_line in (ROOT / "Dockerfile").read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line.startswith("COPY "):
            continue
        parts = shlex.split(line)
        index = 1
        while index < len(parts) and parts[index].startswith("--"):
            index += 1
        sources.extend(parts[index:-1])
    return sources


def _copy_source_covers(source: str, relative_path: str) -> bool:
    if "*" in source:
        return fnmatch(relative_path, source)
    if source.endswith("/"):
        return relative_path.startswith(source)
    return relative_path == source


class DockerfileToolsTests(unittest.TestCase):
    def tearDown(self) -> None:
        for name in (
            "memory_validation",
            "feedback_commands",
            "feedback_contract",
            "review_agent_memory",
        ):
            sys.modules.pop(name, None)

    def test_container_installs_every_review_memory_runtime_module(self) -> None:
        sources = _docker_copy_sources()
        modules = [
            str(path.relative_to(ROOT))
            for path in sorted((ROOT / "tools").glob("review_agent_*.py"))
        ]

        missing = [
            module
            for module in modules
            if not any(_copy_source_covers(source, module) for source in sources)
        ]

        self.assertEqual([], missing)

    def test_container_keeps_stable_operator_command_names(self) -> None:
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

        self.assertIn(
            "cp /usr/local/bin/review_agent_memory.py /usr/local/bin/review-agent-memory",
            dockerfile,
        )
        self.assertIn(
            "cp /usr/local/bin/review_agent_admin.py /usr/local/bin/review-agent-admin",
            dockerfile,
        )
        self.assertNotIn("review-agent-database", dockerfile)
        self.assertIn(
            "cp /usr/local/bin/review_agent_github_app_worker.py /usr/local/bin/review-agent-github-app-worker",
            dockerfile,
        )
        self.assertIn(
            "cp /usr/local/bin/review_agent_github_gateway.py /usr/local/bin/review-agent-github-gateway",
            dockerfile,
        )

    def test_container_installs_the_pinned_python_dependencies(self) -> None:
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")

        self.assertEqual(
            requirements.splitlines(),
            [
                "psycopg[binary]==3.3.4",
                "psycopg-pool==3.3.1",
                "PyYAML==6.0.3",
                "PyJWT==2.13.0",
                "cryptography==50.0.0",
            ],
        )
        self.assertIn(
            "COPY --chown=root:root requirements.txt /opt/review-agent-requirements.txt",
            dockerfile,
        )
        self.assertIn(
            "uv pip install --no-cache --python /opt/hermes/.venv/bin/python \\\n"
            "        --requirement /opt/review-agent-requirements.txt",
            dockerfile,
        )

    def test_docker_build_context_excludes_python_bytecode(self) -> None:
        dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")

        self.assertEqual(
            dockerignore.splitlines(),
            [
                "*",
                "!requirements.txt",
                "!bootstrap/",
                "!bootstrap/**",
                "!tools/",
                "!tools/review_agent_*.py",
                "**/__pycache__/",
                "**/*.py[cod]",
            ],
        )

    def test_container_refreshes_base_security_without_installing_tools(self) -> None:
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

        self.assertIn("apt-get -o Acquire::Retries=3 upgrade", dockerfile)
        self.assertNotIn("apt-get -o Acquire::Retries=3 install", dockerfile)
        self.assertIn("/usr/local/lib/node_modules/npm", dockerfile)

    def test_installer_replaces_managed_trees_and_ignores_bytecode(self) -> None:
        install = _load_install_module()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            target = root / "target"
            (source / "__pycache__").mkdir(parents=True)
            (source / "operator_application.py").write_text(
                "OWNER = 'postgresql'\n", encoding="utf-8"
            )
            (source / "__pycache__" / "operator_application.cpython-313.pyc").write_bytes(
                b"stale"
            )
            (target / "__pycache__").mkdir(parents=True)
            (target / "old_module.py").write_text("OWNER = 'retired'\n", encoding="utf-8")
            (target / "__pycache__" / "old_module.cpython-313.pyc").write_bytes(b"old")

            install.copy_managed_tree(source, target)

            self.assertEqual(
                "OWNER = 'postgresql'\n",
                (target / "operator_application.py").read_text(encoding="utf-8"),
            )
            self.assertFalse((target / "old_module.py").exists())
            self.assertFalse((target / "__pycache__").exists())

    def test_container_records_the_pinned_hermes_base_for_contracts(self) -> None:
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

        self.assertIn("ARG HERMES_IMAGE=", dockerfile)
        self.assertRegex(
            dockerfile,
            r"FROM \$\{HERMES_IMAGE\}\s+ARG HERMES_IMAGE\s+"
            r"ENV REVIEW_AGENT_HERMES_IMAGE=\$\{HERMES_IMAGE\}",
        )

    def test_installed_memory_cli_imports_support_modules(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            install_dir = Path(temp)
            for module in (ROOT / "tools").glob("review_agent_*.py"):
                shutil.copy2(module, install_dir / module.name)
            shutil.copy2(
                install_dir / "review_agent_memory.py",
                install_dir / "review-agent-memory",
            )

            completed = subprocess.run(
                [sys.executable, str(install_dir / "review-agent-memory"), "--help"],
                check=False,
                capture_output=True,
                text=True,
                env={
                    "HERMES_HOME": str(ROOT / "bootstrap"),
                    "PYTHONPATH": str(ROOT / "bootstrap" / "plugins"),
                },
            )
        self.assertEqual(0, completed.returncode, completed.stderr)

if __name__ == "__main__":
    unittest.main()
