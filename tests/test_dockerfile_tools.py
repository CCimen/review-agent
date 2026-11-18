from __future__ import annotations

import sys
import tempfile
import unittest
import importlib.util
from pathlib import Path
import tomllib
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


def _installed_commands() -> dict[str, str]:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return project["project"]["scripts"]


class DockerfileToolsTests(unittest.TestCase):
    def test_every_installed_command_is_one_cli_module(self) -> None:
        modules = []
        for command, target in _installed_commands().items():
            module, _, function = target.partition(":")
            self.assertTrue(command.startswith("review-agent-"), command)
            self.assertEqual("main", function, command)
            package, _, name = module.rpartition(".")
            self.assertEqual("review_agent.cli", package, command)
            modules.append(name)

        self.assertEqual(
            sorted(modules),
            sorted(
                path.stem
                for path in (ROOT / "src" / "review_agent" / "cli").glob("*.py")
                if path.name != "__init__.py"
            ),
        )

    def test_container_keeps_stable_operator_command_names(self) -> None:
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        commands = set(_installed_commands())

        self.assertLessEqual(
            {
                "review-agent-memory",
                "review-agent-admin",
                "review-agent-github-app-worker",
                "review-agent-github-gateway",
            },
            commands,
        )
        self.assertNotIn("review-agent-database", commands)
        self.assertNotIn("review-agent-database", dockerfile)
        # Compose files and the Helm chart call the commands in /usr/local/bin.
        self.assertIn(
            "ln -s /opt/hermes/.venv/bin/review-agent-* /usr/local/bin/", dockerfile
        )

    def test_container_installs_the_pinned_python_dependencies(self) -> None:
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

        self.assertIn(
            "COPY --chown=root:root requirements.txt /opt/review-agent-requirements.txt",
            dockerfile,
        )
        self.assertIn(
            "uv pip install --no-config --no-cache --python /opt/hermes/.venv/bin/python \\\n"
            "        --requirement /opt/review-agent-requirements.txt",
            dockerfile,
        )

    def test_docker_build_context_excludes_python_bytecode(self) -> None:
        lines = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()

        # Later lines win, so every exclusion must follow the last include.
        self.assertEqual("*", lines[0])
        last_include = max(
            index for index, line in enumerate(lines) if line.startswith("!")
        )
        for exclusion in (
            "src/review_agent/_build.json",
            "**/__pycache__/",
            "**/*.py[cod]",
        ):
            self.assertGreater(lines.index(exclusion), last_include, exclusion)

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


if __name__ == "__main__":
    unittest.main()
