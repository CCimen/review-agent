from __future__ import annotations

import ast
import importlib
import sys
import unittest
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "bootstrap" / "plugins"
PLUGIN = PACKAGE_ROOT / "review_agent_tools"
sys.path.insert(0, str(PACKAGE_ROOT))


OWNER_MODULES = [
    "feedback_authorization",
    "feedback_commands",
    "memory_validation",
    "memory_schema",
    "memory_migration",
    "memory_identity",
    "memory_decisions",
    "memory_findings",
    "memory_suggestions",
    "memory_verification",
    "memory_publications",
    "memory_feedback",
    "memory_reporting",
    "memory_runs",
    "memory_coach",
]
OFFLINE_OPERATOR_MODULES = {
    "review_agent_learning",
    "review_agent_coach",
    "review_agent_coach_proposals",
    "review_agent_replay",
    "review_agent_export",
}


class MemoryModuleBoundaryTests(unittest.TestCase):
    def test_owner_modules_import_without_facade_cycles(self):
        for module in OWNER_MODULES:
            with self.subTest(module=module):
                importlib.import_module(f"review_agent_tools.{module}")

    def test_owner_modules_do_not_import_memory_db_facade(self):
        for module in OWNER_MODULES:
            with self.subTest(module=module):
                source = (PLUGIN / f"{module}.py").read_text(encoding="utf-8")
                tree = ast.parse(source)
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        imported = {alias.name for alias in node.names}
                        self.assertNotIn("memory_db", imported)
                        self.assertNotIn("review_agent_tools.memory_db", imported)
                    elif isinstance(node, ast.ImportFrom):
                        imported = {alias.name for alias in node.names}
                        if node.level == 1 and node.module is None:
                            self.assertNotIn("memory_db", imported)
                        self.assertNotEqual(node.module, "memory_db")
                        self.assertNotEqual(node.module, "review_agent_tools.memory_db")
                        self.assertFalse(
                            node.level == 1 and node.module == "memory_db"
                        )

    def test_public_plugin_does_not_import_offline_learning_modules(self):
        for path in sorted(PLUGIN.glob("*.py")):
            with self.subTest(path=path.name):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        imported = {alias.name.split(".")[0] for alias in node.names}
                        self.assertTrue(OFFLINE_OPERATOR_MODULES.isdisjoint(imported))
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        imported = node.module.split(".")[0]
                        self.assertNotIn(imported, OFFLINE_OPERATOR_MODULES)

    def test_review_run_application_keeps_infrastructure_in_tool_adapters(self):
        forbidden = {
            "json",
            "review_publisher",
            "schemas",
            "settings",
            "source_control",
            "urllib",
        }

        def imported_roots(source: str) -> set[str]:
            roots: set[str] = set()
            for node in ast.walk(ast.parse(source)):
                if isinstance(node, ast.Import):
                    roots.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    if node.module:
                        roots.add(node.module.split(".")[0])
                    elif node.level:
                        roots.update(alias.name.split(".")[0] for alias in node.names)
            return roots

        source = (PLUGIN / "review_run_application.py").read_text(encoding="utf-8")
        self.assertTrue(forbidden.isdisjoint(imported_roots(source)))
        self.assertIn("settings", imported_roots("from . import settings as config"))

    def test_tools_delegate_run_and_coverage_operations_to_application_owner(self):
        moved_operations = {
            "complete_run",
            "file_index_summary",
            "get_run",
            "list_run_files",
            "lookup_run_file",
            "record_diff_exposure",
            "record_file_range",
            "register_changed_files",
            "start_run",
            "update_run_phase",
            "validate_run_snapshot",
        }

        def moved_calls(source: str) -> set[str]:
            tree = ast.parse(source)
            facade_names: set[str] = set()
            direct_names: dict[str, str] = {}
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name in {
                            "memory_db",
                            "review_agent_tools.memory_db",
                        }:
                            facade_names.add(alias.asname or alias.name.split(".")[-1])
                elif isinstance(node, ast.ImportFrom):
                    if node.module in {
                        "memory_db",
                        "review_agent_tools.memory_db",
                    } or (node.level == 1 and node.module == "memory_db"):
                        for alias in node.names:
                            if alias.name in moved_operations:
                                direct_names[alias.asname or alias.name] = alias.name
                    elif node.module == "review_agent_tools" or (
                        node.level == 1 and node.module is None
                    ):
                        for alias in node.names:
                            if alias.name == "memory_db":
                                facade_names.add(alias.asname or alias.name)
            calls: set[str] = set()
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if (
                    isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id in facade_names
                    and node.func.attr in moved_operations
                ):
                    calls.add(node.func.attr)
                elif isinstance(node.func, ast.Name) and node.func.id in direct_names:
                    calls.add(direct_names[node.func.id])
            return calls

        source = (PLUGIN / "tools.py").read_text(encoding="utf-8")
        self.assertFalse(moved_calls(source))
        self.assertEqual(
            moved_calls("from . import memory_db as db\ndb.start_run()"),
            {"start_run"},
        )
        self.assertEqual(
            moved_calls(
                "from .memory_db import record_file_range as save_range\n"
                "save_range()"
            ),
            {"record_file_range"},
        )


if __name__ == "__main__":
    unittest.main()
