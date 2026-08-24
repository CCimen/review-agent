from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "bootstrap" / "plugins"
sys.path.insert(0, str(PACKAGE_ROOT))

from review_agent_tools import (  # noqa: E402
    capacity,
    memory_db,
    review_finding_application,
    review_publisher,
    schemas,
    source_control,
    tools,
)
from review_agent_tools.github import publication as github_publication  # noqa: E402
import review_agent_tools  # noqa: E402


class _FakeRegistry:
    def __init__(self, settings=None):
        self.tools = {}
        self.settings = settings or {}

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def register_tool(self, *, name, toolset, schema, handler):
        self.tools[name] = {
            "toolset": toolset,
            "schema": schema,
            "handler": handler,
        }


class _FakeGitHub:
    def current_user_login(self):
        return "review-agent-bot"

    def get_pull_request(self, repository, pr_number):
        del repository, pr_number
        return github_publication.PullRequestState(
            state="open",
            draft=False,
            base_sha="b" * 40,
            head_sha="a" * 40,
        )

    def list_issue_comments(self, repository, issue_number, *, max_pages=3):
        del repository, issue_number, max_pages
        return []

    def update_issue_comment(self, repository, comment_id, body):
        del repository, body
        return github_publication.IssueComment(
            comment_id=comment_id,
            body="updated",
            author_login="review-agent-bot",
        )

    def create_issue_comment(self, repository, issue_number, body):
        del repository, issue_number
        return github_publication.IssueComment(
            comment_id=123,
            body=body,
            author_login="review-agent-bot",
        )

    def delete_issue_comment(self, repository, comment_id):
        del repository, comment_id


class ToolValidationTests(unittest.TestCase):
    def setUp(self):
        tools._file_at_revision.cache_clear()
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / "memory.sqlite3")
        self.env = {
            "REVIEW_AGENT_ALLOWED_REPOSITORIES": "sundsvallskommun/example-repository",
            "REVIEW_AGENT_DB": self.db,
        }
        memory_db.connect(self.db).close()
        self.finding = {
            "rule_id": "correctness.boolean-default",
            "category": "correctness",
            "path": "backend/changed.py",
            "line": 10,
            "symbol": "handler",
            "anchor": "feature default",
            "title": "Boolean default remains disabled",
            "severity": "Critical",
            "publication_score": 9,
            "confidence": 0.9,
            "evidence": "Concrete evidence.",
            "disproof_checks": "Checked the guard.",
            "impact": "The feature remains unavailable.",
            "smallest_fix": "Restore the enabled default.",
            "introduced_by_diff": True,
        }

    def tearDown(self):
        self.temp.cleanup()

    def start_run(self, *, head_sha: str = "a" * 40, base_sha: str = "b" * 40) -> int:
        connection = memory_db.connect_existing(self.db)
        try:
            run = memory_db.start_run(
                connection,
                "sundsvallskommun/example-repository",
                1,
                base_sha=base_sha,
                head_sha=head_sha,
            )
            return int(run["id"])
        finally:
            connection.close()

    def select_optional_suggestions(
        self,
        findings: list[dict[str, object]],
        *,
        patch_text: str,
        head_text: str,
        suppressed_indices: frozenset[int] = frozenset(),
    ) -> tuple[int, dict[int, str]]:
        run_id = self.start_run()
        connection = memory_db.connect_existing(self.db)
        try:
            recorded = memory_db.record_findings(
                connection,
                "sundsvallskommun/example-repository",
                1,
                "a" * 40,
                findings,
                review_run_id=run_id,
                base_sha="b" * 40,
                context_hashes={"backend/changed.py": "c" * 40},
            )
        finally:
            connection.close()
        for index in suppressed_indices:
            recorded[index]["suppressed"] = True
        with patch.dict(os.environ, self.env, clear=False):
            return review_finding_application._record_optional_suggestions(
                review_finding_application.FindingRecordSubject(
                    repository="sundsvallskommun/example-repository",
                    pr_number=1,
                    run_id=run_id,
                    base_sha="b" * 40,
                    head_sha="a" * 40,
                ),
                findings,
                recorded,
                {
                    "backend/changed.py": review_finding_application.ChangedFile(
                        path="backend/changed.py",
                        patch=patch_text,
                    )
                },
                lambda _path: head_text,
            )

    def test_empty_allowlist_denies_by_default(self):
        with patch.dict(
            os.environ, {"REVIEW_AGENT_ALLOWED_REPOSITORIES": ""}, clear=False
        ):
            result = json.loads(
                tools.review_begin({"repository": "sundsvallskommun/example-repository", "pr_number": 1})
            )
        self.assertIn("deny by default", result["error"])

    def test_allowlist_is_accepted_before_other_input_validation(self):
        with patch.dict(
            os.environ,
            {"REVIEW_AGENT_ALLOWED_REPOSITORIES": "sundsvallskommun/example-repository"},
            clear=True,
        ):
            result = json.loads(
                tools.review_begin({"repository": "sundsvallskommun/example-repository", "pr_number": 0})
            )
        self.assertEqual(result["error"], "pr_number must be positive")

    def test_schema_severities_come_from_memory_owner(self):
        severity_schema = schemas.REVIEW_AGENT_MEMORY_RECORD["parameters"]["properties"][
            "findings"
        ]["items"]["properties"]["severity"]
        self.assertEqual(severity_schema["enum"], sorted(memory_db.SEVERITIES))

    def test_schema_finding_text_limits_come_from_validation_owner(self):
        finding_properties = schemas.REVIEW_AGENT_MEMORY_RECORD["parameters"][
            "properties"
        ]["findings"]["items"]["properties"]

        for field, maximum in memory_db.FINDING_TEXT_LIMITS.items():
            with self.subTest(field=field):
                self.assertEqual(finding_properties[field]["maxLength"], maximum)

    def test_schema_exposes_bounded_optional_atomic_suggestion(self):
        properties = schemas.REVIEW_AGENT_MEMORY_RECORD["parameters"]["properties"][
            "findings"
        ]["items"]["properties"]
        suggestion = properties["suggestion"]

        self.assertFalse(suggestion["additionalProperties"])
        self.assertEqual(
            set(suggestion["required"]),
            {"start_line", "end_line", "expected_text", "replacement_text"},
        )
        self.assertEqual(
            suggestion["properties"]["replacement_text"]["maxLength"],
            memory_db.MAX_SUGGESTION_TEXT_CHARS,
        )

    def test_schema_describes_demonstrated_paths_and_complete_remediation(self):
        finding_properties = schemas.REVIEW_AGENT_MEMORY_RECORD["parameters"][
            "properties"
        ]["findings"]["items"]["properties"]

        evidence_contract = finding_properties["evidence"]["description"]
        remediation_contract = finding_properties["smallest_fix"]["description"]

        self.assertIn("primary executed failure path", evidence_contract)
        self.assertIn(
            "fallback or secondary path unless it is independently traced",
            evidence_contract,
        )
        self.assertIn("every proven sibling lifecycle path", remediation_contract)
        self.assertIn("One lowest-risk owner-aligned remediation", remediation_contract)
        self.assertIn(
            "real behavior boundary implicated by the finding",
            remediation_contract,
        )
        self.assertIn(
            "actual downstream consumer rather than only a helper property",
            remediation_contract,
        )
        self.assertIn(
            "Offer alternatives only when an external contract requires a developer "
            "decision",
            remediation_contract,
        )

    def test_read_schemas_expose_non_retryable_terminal_contract(self):
        for schema in (schemas.REVIEW_AGENT_PR_DIFF, schemas.REVIEW_AGENT_PR_FILE):
            with self.subTest(tool=schema["name"]):
                description = schema["description"]
                self.assertIn("terminal: true", description)
                self.assertIn("retryable: false", description)
                self.assertIn("next_action", description)

    def test_schema_prior_verdicts_come_from_memory_owner(self):
        deliver_verdict_schema = schemas.REVIEW_AGENT_DELIVER["parameters"]["properties"][
            "previous_verdicts"
        ]["items"]["properties"]["verdict"]
        self.assertEqual(
            deliver_verdict_schema["enum"],
            list(memory_db.PRIOR_FINDING_VERDICTS),
        )

    def test_plugin_registers_all_declared_tools(self):
        registry = _FakeRegistry()

        review_agent_tools.register(registry)

        manifest_text = (
            PACKAGE_ROOT / "review_agent_tools" / "plugin.yaml"
        ).read_text(encoding="utf-8")
        tool_block = manifest_text.partition("provides_tools:")[2].partition(
            "requires_env:"
        )[0]
        declared = {
            line.strip().removeprefix("- ").strip()
            for line in tool_block.splitlines()
            if line.strip().startswith("- ")
        }
        self.assertTrue(
            declared,
            "plugin manifest must declare at least one tool",
        )
        self.assertEqual(set(registry.tools), declared)

        for name, item in registry.tools.items():
            self.assertEqual(item["toolset"], "review_agent")
            self.assertIsInstance(item["schema"], dict)
            self.assertEqual(item["schema"]["name"], name)
            self.assertTrue(callable(item["handler"]))

    def test_plugin_text_page_capacity_is_operator_configurable(self):
        registry = _FakeRegistry({"result_max_chars": 320_000})

        def restore_default_capacity():
            limits = capacity.configure(
                result_max_chars=capacity.DEFAULT_RESULT_MAX_CHARS
            )
            schemas.apply_capacity(limits)

        self.addCleanup(restore_default_capacity)

        review_agent_tools.register(registry)

        self.assertEqual(capacity.current().result_max_chars, 320_000)
        self.assertEqual(capacity.current().text_page_max_chars, 45_714)
        max_chars = registry.tools["review_agent_pr_diff"]["schema"]["parameters"][
            "properties"
        ]["max_chars"]
        self.assertEqual(max_chars["maximum"], 45_714)
        self.assertEqual(max_chars["default"], 45_714)

    def test_non_allowlisted_repository_is_denied_before_network(self):
        with patch.dict(os.environ, self.env, clear=False):
            result = json.loads(tools.pr_diff({"repository": "other/project", "pr_number": 1}))
        self.assertEqual(result["error"], "repository is not allowlisted")

    def test_invalid_path_is_denied_before_network(self):
        with patch.dict(os.environ, self.env, clear=False):
            result = json.loads(
                tools.pr_file(
                    {
                        "repository": "sundsvallskommun/example-repository",
                        "pr_number": 1,
                        "path": "../../etc/passwd",
                    }
                )
            )
        self.assertIn("traversal", result["error"])

    def test_record_returns_terminal_handoff_for_stale_snapshot(self):
        pull = {
            "state": "open",
            "draft": False,
            "head": {"sha": "a" * 40},
            "base": {"sha": "b" * 40},
            "changed_files": 1,
        }
        with (
            patch.dict(os.environ, self.env, clear=False),
            patch.object(tools, "_pr", return_value=pull),
        ):
            run_id = self.start_run(head_sha="b" * 40)
            result = json.loads(
                tools.review_memory_record(
                    {
                        "repository": "sundsvallskommun/example-repository",
                        "pr_number": 1,
                        "head_sha": "b" * 40,
                        "run_id": run_id,
                        "findings": [],
                    }
                )
            )
        self.assertEqual(result["run_state"], "snapshot_superseded")
        self.assertTrue(result["terminal"])
        self.assertFalse(result["retryable"])

    def test_record_rejects_finding_outside_changed_files(self):
        pull = {
            "state": "open",
            "draft": False,
            "head": {"sha": "a" * 40},
            "base": {"sha": "b" * 40},
            "changed_files": 1,
        }
        finding = dict(self.finding, path="backend/unchanged.py")
        with (
            patch.dict(os.environ, self.env, clear=False),
            patch.object(tools, "_pr", return_value=pull),
            patch.object(
                tools,
                "_changed_files",
                return_value=[
                    {
                        "path": "backend/changed.py",
                        "context_hash": "c" * 40,
                        "context_hash_source": "blob",
                    }
                ],
            ),
        ):
            run_id = self.start_run()
            result = json.loads(
                tools.review_memory_record(
                    {
                        "repository": "sundsvallskommun/example-repository",
                        "pr_number": 1,
                        "head_sha": "a" * 40,
                        "run_id": run_id,
                        "findings": [finding],
                    }
                )
            )
        self.assertIn("changed pull-request file", result["error"])

    def test_record_partial_enumeration_still_records_enumerated_finding(self):
        # GitHub reports more changed files than were enumerated (e.g. a PR beyond the
        # ~3000-file files-API ceiling). A finding on an ENUMERATED file must still
        # record (honest-partial) rather than be hard-refused; coverage stays
        # incomplete (surfaced by the renderer banner) but is never silently dropped.
        pull = {
            "state": "open",
            "draft": False,
            "head": {"sha": "a" * 40},
            "base": {"sha": "b" * 40},
            "changed_files": 2,
        }
        with (
            patch.dict(os.environ, self.env, clear=False),
            patch.object(tools, "_pr", return_value=pull),
            patch.object(
                tools,
                "_changed_files",
                return_value=[
                    {
                        "path": "backend/changed.py",
                        "context_hash": "c" * 40,
                        "context_hash_source": "blob",
                    }
                ],
            ),
        ):
            run_id = self.start_run()
            result = json.loads(
                tools.review_memory_record(
                    {
                        "repository": "sundsvallskommun/example-repository",
                        "pr_number": 1,
                        "head_sha": "a" * 40,
                        "run_id": run_id,
                        "findings": [self.finding],
                    }
                )
            )
        self.assertNotIn("error", result)
        self.assertEqual(result["recorded"][0]["context_hash"], "c" * 40)

    def test_record_uses_trusted_blob_hash(self):
        pull = {
            "state": "open",
            "draft": False,
            "head": {"sha": "a" * 40},
            "base": {"sha": "b" * 40},
            "changed_files": 1,
        }
        with (
            patch.dict(os.environ, self.env, clear=False),
            patch.object(tools, "_pr", return_value=pull),
            patch.object(
                tools,
                "_changed_files",
                return_value=[
                    {
                        "path": "backend/changed.py",
                        "context_hash": "c" * 40,
                        "context_hash_source": "blob",
                    }
                ],
            ),
        ):
            run_id = self.start_run()
            result = json.loads(
                tools.review_memory_record(
                    {
                        "repository": "sundsvallskommun/example-repository",
                        "pr_number": 1,
                        "head_sha": "a" * 40,
                        "run_id": run_id,
                        "findings": [self.finding],
                    }
                )
            )
        self.assertEqual(result["recorded"][0]["context_hash"], "c" * 40)
        self.assertFalse(result["recorded"][0]["suppressed"])

    def test_record_falls_back_to_exact_head_when_blob_hash_is_missing(self):
        pull = {
            "state": "open",
            "draft": False,
            "head": {"sha": "a" * 40},
            "base": {"sha": "b" * 40},
            "changed_files": 1,
        }
        with (
            patch.dict(os.environ, self.env, clear=False),
            patch.object(tools, "_pr", return_value=pull),
            patch.object(
                tools,
                "_changed_files",
                return_value=[
                    {
                        "path": "backend/changed.py",
                        "context_hash": "c" * 64,
                        "context_hash_source": "patch",
                    }
                ],
            ),
        ):
            run_id = self.start_run()
            result = json.loads(
                tools.review_memory_record(
                    {
                        "repository": "sundsvallskommun/example-repository",
                        "pr_number": 1,
                        "head_sha": "a" * 40,
                        "run_id": run_id,
                        "findings": [self.finding],
                    }
                )
            )
        self.assertEqual(result["recorded"][0]["context_hash"], "a" * 40)

    def test_record_validates_and_persists_atomic_suggestion(self):
        pull = {
            "state": "open",
            "draft": False,
            "head": {
                "sha": "a" * 40,
                "repo": {"full_name": "sundsvallskommun/example-repository"},
            },
            "base": {"sha": "b" * 40},
            "changed_files": 1,
        }
        changed = {
            "path": "backend/changed.py",
            "context_hash": "c" * 40,
            "context_hash_source": "blob",
            "patch": "@@ -9,2 +9,2 @@\n context\n-old = None\n+enabled = False",
        }
        finding = dict(
            self.finding,
            suggestion={
                "start_line": 10,
                "end_line": 10,
                "expected_text": "enabled = False",
                "replacement_text": "enabled = True",
            },
        )
        head = "\n".join([*(f"line {number}" for number in range(1, 10)), "enabled = False"])
        with (
            patch.dict(os.environ, self.env, clear=False),
            patch.object(tools, "_pr", return_value=pull),
            patch.object(tools, "_changed_files", return_value=[changed]),
            patch.object(tools, "_file_at_revision", return_value=head.encode()),
        ):
            run_id = self.start_run()
            result = json.loads(
                tools.review_memory_record(
                    {
                        "repository": "sundsvallskommun/example-repository",
                        "pr_number": 1,
                        "head_sha": "a" * 40,
                        "run_id": run_id,
                        "findings": [finding],
                    }
                )
            )

        self.assertNotIn("error", result)
        self.assertEqual(result["suggestions_recorded"], 1)
        self.assertEqual(result["recorded"][0]["suggestion"], {"status": "recorded"})
        connection = memory_db.connect_existing(self.db)
        try:
            row = connection.execute("SELECT * FROM review_suggestions").fetchone()
        finally:
            connection.close()
        self.assertIsNotNone(row)
        self.assertEqual(row["start_line"], 10)
        self.assertEqual(row["replacement_text"], "enabled = True")

    def test_high_risk_finding_never_persists_atomic_suggestion(self):
        pull = {
            "state": "open",
            "draft": False,
            "head": {
                "sha": "a" * 40,
                "repo": {"full_name": "sundsvallskommun/example-repository"},
            },
            "base": {"sha": "b" * 40},
            "changed_files": 1,
        }
        changed = {
            "path": "backend/changed.py",
            "context_hash": "c" * 40,
            "context_hash_source": "blob",
            "patch": "@@ -9,2 +9,2 @@\n context\n-old = None\n+enabled = False",
        }
        finding = dict(
            self.finding,
            rule_id="authorization.missing-context",
            category="security",
            title="Authorization context omitted",
            suggestion={
                "start_line": 10,
                "end_line": 10,
                "expected_text": "enabled = False",
                "replacement_text": "enabled = True",
            },
        )
        head = "\n".join(
            [*(f"line {number}" for number in range(1, 10)), "enabled = False"]
        )
        with (
            patch.dict(os.environ, self.env, clear=False),
            patch.object(tools, "_pr", return_value=pull),
            patch.object(tools, "_changed_files", return_value=[changed]),
            patch.object(tools, "_file_at_revision", return_value=head.encode()) as read,
        ):
            run_id = self.start_run()
            result = json.loads(
                tools.review_memory_record(
                    {
                        "repository": "sundsvallskommun/example-repository",
                        "pr_number": 1,
                        "head_sha": "a" * 40,
                        "run_id": run_id,
                        "findings": [finding],
                    }
                )
            )

        self.assertEqual(result["suggestions_recorded"], 0)
        self.assertEqual(
            result["recorded"][0]["suggestion"]["reason"],
            "suggestion_high_risk_category",
        )
        read.assert_not_called()

    def test_invalid_optional_suggestion_does_not_drop_finding(self):
        pull = {
            "state": "open",
            "draft": False,
            "head": {
                "sha": "a" * 40,
                "repo": {"full_name": "sundsvallskommun/example-repository"},
            },
            "base": {"sha": "b" * 40},
            "changed_files": 1,
        }
        changed = {
            "path": "backend/changed.py",
            "context_hash": "c" * 40,
            "context_hash_source": "blob",
            "patch": "@@ -9,2 +9,2 @@\n context\n-old = None\n+enabled = False",
        }
        finding = dict(
            self.finding,
            suggestion={
                "start_line": 10,
                "end_line": 10,
                "expected_text": "not the head text",
                "replacement_text": "enabled = True",
            },
        )
        head = "\n".join([*(f"line {number}" for number in range(1, 10)), "enabled = False"])
        with (
            patch.dict(os.environ, self.env, clear=False),
            patch.object(tools, "_pr", return_value=pull),
            patch.object(tools, "_changed_files", return_value=[changed]),
            patch.object(tools, "_file_at_revision", return_value=head.encode()),
        ):
            run_id = self.start_run()
            result = json.loads(
                tools.review_memory_record(
                    {
                        "repository": "sundsvallskommun/example-repository",
                        "pr_number": 1,
                        "head_sha": "a" * 40,
                        "run_id": run_id,
                        "findings": [finding],
                    }
                )
            )

        self.assertNotIn("error", result)
        self.assertEqual(result["suggestions_recorded"], 0)
        self.assertEqual(result["recorded"][0]["suggestion"]["status"], "omitted")
        self.assertEqual(
            result["recorded"][0]["suggestion"]["reason"],
            "suggestion_expected_text_mismatch",
        )
        connection = memory_db.connect_existing(self.db)
        try:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM finding_observations").fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM review_suggestions").fetchone()[0],
                0,
            )
        finally:
            connection.close()

    def test_overlapping_candidates_keep_higher_priority_patch(self):
        medium = dict(
            self.finding,
            rule_id="correctness.medium-default",
            severity="Medium",
            publication_score=8,
            symbol="medium_default",
            anchor="medium default",
            suggestion={
                "start_line": 10,
                "end_line": 10,
                "expected_text": "enabled = False",
                "replacement_text": "enabled = maybe",
            },
        )
        high = dict(
            self.finding,
            rule_id="correctness.high-default",
            severity="High",
            publication_score=9,
            symbol="high_default",
            anchor="high default",
            suggestion={
                "start_line": 10,
                "end_line": 10,
                "expected_text": "enabled = False",
                "replacement_text": "enabled = True",
            },
        )

        count, statuses = self.select_optional_suggestions(
            [medium, high],
            patch_text="@@ -9,2 +9,2 @@\n context\n-old = None\n+enabled = False",
            head_text="\n".join(
                [*(f"line {number}" for number in range(1, 10)), "enabled = False"]
            ),
        )

        self.assertEqual(count, 1)
        self.assertEqual(statuses[1], "recorded")
        self.assertEqual(statuses[0], "suggestion_overlaps_higher_priority_patch")
        connection = memory_db.connect_existing(self.db)
        try:
            row = connection.execute(
                "SELECT fingerprint, replacement_text FROM review_suggestions"
            ).fetchone()
            high_fingerprint = connection.execute(
                "SELECT fingerprint FROM findings WHERE rule_id = ?",
                ("correctness.high-default",),
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(row["fingerprint"], high_fingerprint)
        self.assertEqual(row["replacement_text"], "enabled = True")

    def test_same_head_canonical_patch_precedes_new_overlap_selection(self):
        head_text = "\n".join(f"value_{line} = 0" for line in range(1, 21))
        patch_text = "@@ -0,0 +1,20 @@\n" + "\n".join(
            f"+value_{line} = 0" for line in range(1, 21)
        )
        first_finding = dict(
            self.finding,
            rule_id="correctness.canonical-owner",
            line=10,
            symbol="canonical_owner",
            anchor="canonical owner",
            suggestion={
                "start_line": 10,
                "end_line": 10,
                "expected_text": "value_10 = 0",
                "replacement_text": "value_10 = 1",
            },
        )

        first_count, first_statuses = self.select_optional_suggestions(
            [first_finding], patch_text=patch_text, head_text=head_text
        )
        self.assertEqual(first_count, 1)
        self.assertEqual(first_statuses[0], "recorded")

        connection = memory_db.connect_existing(self.db)
        try:
            first_run_id = int(
                connection.execute(
                    "SELECT id FROM review_runs WHERE status = 'running'"
                ).fetchone()[0]
            )
            memory_db.complete_run(
                connection,
                first_run_id,
                repository="sundsvallskommun/example-repository",
                pr_number=1,
                status="generated",
                findings_count=1,
            )
        finally:
            connection.close()

        repeated_finding = dict(
            first_finding,
            line=20,
            suggestion={
                "start_line": 20,
                "end_line": 20,
                "expected_text": "value_20 = 0",
                "replacement_text": "value_20 = 1",
            },
        )
        newly_overlapping = dict(
            self.finding,
            rule_id="correctness.new-overlap",
            severity="High",
            line=10,
            symbol="new_overlap",
            anchor="new overlap",
            suggestion={
                "start_line": 10,
                "end_line": 10,
                "expected_text": "value_10 = 0",
                "replacement_text": "value_10 = 2",
            },
        )

        second_count, second_statuses = self.select_optional_suggestions(
            [repeated_finding, newly_overlapping],
            patch_text=patch_text,
            head_text=head_text,
        )

        self.assertEqual(second_count, 1)
        self.assertEqual(second_statuses[0], "recorded")
        self.assertEqual(
            second_statuses[1], "suggestion_overlaps_higher_priority_patch"
        )
        connection = memory_db.connect_existing(self.db)
        try:
            second_run_id = int(
                connection.execute(
                    "SELECT id FROM review_runs WHERE status = 'running'"
                ).fetchone()[0]
            )
            rows = connection.execute(
                """
                SELECT start_line, end_line, replacement_text
                FROM review_suggestions
                WHERE review_run_id = ?
                """,
                (second_run_id,),
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["start_line"], 10)
        self.assertEqual(rows[0]["end_line"], 10)
        self.assertEqual(rows[0]["replacement_text"], "value_10 = 1")

    def test_atomic_suggestion_review_is_capped_at_twelve(self):
        findings: list[dict[str, object]] = []
        for line in range(1, 14):
            findings.append(
                dict(
                    self.finding,
                    rule_id=f"correctness.atomic-{line:02d}",
                    line=line,
                    symbol=f"atomic_{line}",
                    anchor=f"atomic line {line}",
                    severity="Medium",
                    publication_score=8,
                    suggestion={
                        "start_line": line,
                        "end_line": line,
                        "expected_text": f"value_{line} = 0",
                        "replacement_text": f"value_{line} = 1",
                    },
                )
            )
        patch_text = "@@ -0,0 +1,13 @@\n" + "\n".join(
            f"+value_{line} = 0" for line in range(1, 14)
        )
        head_text = "\n".join(f"value_{line} = 0" for line in range(1, 14))

        count, statuses = self.select_optional_suggestions(
            findings, patch_text=patch_text, head_text=head_text
        )

        self.assertEqual(count, memory_db.MAX_ATOMIC_SUGGESTIONS_PER_REVIEW)
        self.assertEqual(
            sum(reason == "suggestion_review_limit" for reason in statuses.values()),
            1,
        )
        connection = memory_db.connect_existing(self.db)
        try:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM review_suggestions").fetchone()[0],
                memory_db.MAX_ATOMIC_SUGGESTIONS_PER_REVIEW,
            )
        finally:
            connection.close()

    def test_suppressed_candidates_do_not_consume_the_atomic_patch_limit(self):
        findings: list[dict[str, object]] = []
        for line in range(1, 14):
            findings.append(
                dict(
                    self.finding,
                    rule_id=f"correctness.suppression-{line:02d}",
                    line=line,
                    symbol=f"suppression_{line}",
                    anchor=f"suppression line {line}",
                    severity="High" if line <= 12 else "Medium",
                    suggestion={
                        "start_line": line,
                        "end_line": line,
                        "expected_text": f"value_{line} = 0",
                        "replacement_text": f"value_{line} = 1",
                    },
                )
            )
        patch_text = "@@ -0,0 +1,13 @@\n" + "\n".join(
            f"+value_{line} = 0" for line in range(1, 14)
        )
        head_text = "\n".join(f"value_{line} = 0" for line in range(1, 14))

        count, statuses = self.select_optional_suggestions(
            findings,
            patch_text=patch_text,
            head_text=head_text,
            suppressed_indices=frozenset(range(12)),
        )

        self.assertEqual(count, 1)
        self.assertTrue(
            all(statuses[index] == "suggestion_finding_suppressed" for index in range(12))
        )
        self.assertEqual(statuses[12], "recorded")

    def test_atomic_patch_limit_stops_additional_head_file_reads(self):
        findings: list[dict[str, object]] = []
        changed_by_path: dict[str, dict[str, object]] = {}
        context_hashes: dict[str, str] = {}
        for index in range(1, 14):
            path = f"src/atomic_{index:02d}.py"
            findings.append(
                dict(
                    self.finding,
                    rule_id=f"correctness.atomic-file-{index:02d}",
                    path=path,
                    line=1,
                    symbol=f"atomic_file_{index}",
                    anchor=f"atomic file {index}",
                    severity="Medium",
                    suggestion={
                        "start_line": 1,
                        "end_line": 1,
                        "expected_text": "value = 0",
                        "replacement_text": "value = 1",
                    },
                )
            )
            changed_by_path[path] = {
                "path": path,
                "patch": "@@ -0,0 +1 @@\n+value = 0",
            }
            context_hashes[path] = "c" * 40
        run_id = self.start_run()
        connection = memory_db.connect_existing(self.db)
        try:
            recorded = memory_db.record_findings(
                connection,
                "sundsvallskommun/example-repository",
                1,
                "a" * 40,
                findings,
                review_run_id=run_id,
                base_sha="b" * 40,
                context_hashes=context_hashes,
            )
        finally:
            connection.close()
        reads: list[str] = []

        def load_head(path: str) -> str:
            reads.append(path)
            return "value = 0"

        with patch.dict(os.environ, self.env, clear=False):
            count, statuses = review_finding_application._record_optional_suggestions(
                review_finding_application.FindingRecordSubject(
                    repository="sundsvallskommun/example-repository",
                    pr_number=1,
                    run_id=run_id,
                    base_sha="b" * 40,
                    head_sha="a" * 40,
                ),
                findings,
                recorded,
                {
                    path: review_finding_application.ChangedFile(
                        path=path,
                        patch=str(item["patch"]),
                    )
                    for path, item in changed_by_path.items()
                },
                load_head,
            )

        self.assertEqual(count, memory_db.MAX_ATOMIC_SUGGESTIONS_PER_REVIEW)
        self.assertEqual(len(reads), memory_db.MAX_ATOMIC_SUGGESTIONS_PER_REVIEW)
        self.assertEqual(statuses[12], "suggestion_review_limit")

    def test_deliver_returns_terminal_handoff_when_snapshot_changed(self):
        pull = {
            "state": "open",
            "draft": False,
            "head": {"sha": "a" * 40},
            "base": {"sha": "b" * 40},
            "changed_files": 1,
        }
        with (
            patch.dict(os.environ, self.env, clear=False),
            patch.object(tools, "_pr", return_value=pull),
        ):
            run_id = self.start_run(head_sha="b" * 40)
            result = json.loads(
                tools.review_deliver(
                    {
                        "repository": "sundsvallskommun/example-repository",
                        "pr_number": 1,
                        "head_sha": "b" * 40,
                        "run_id": run_id,
                    }
                )
            )
        self.assertEqual(result["run_state"], "snapshot_superseded")
        self.assertTrue(result["terminal"])
        self.assertFalse(result["retryable"])
        self.assertEqual(result["failure_code"], "snapshot_superseded")

    def test_deliver_keeps_wrong_model_head_as_hard_error(self):
        pull = {
            "state": "open",
            "draft": False,
            "head": {"sha": "a" * 40},
            "base": {"sha": "b" * 40},
            "changed_files": 1,
        }
        with (
            patch.dict(os.environ, self.env, clear=False),
            patch.object(tools, "_pr", return_value=pull) as reader,
        ):
            run_id = self.start_run(head_sha="a" * 40)
            result = json.loads(
                tools.review_deliver(
                    {
                        "repository": "sundsvallskommun/example-repository",
                        "pr_number": 1,
                        "head_sha": "c" * 40,
                        "run_id": run_id,
                    }
                )
            )

        self.assertIn("error", result)
        self.assertIn("active review run", result["error"])
        reader.assert_not_called()

    def test_pr_diff_returns_terminal_handoff_for_changed_base_snapshot(self):
        initial = {
            "state": "open",
            "draft": False,
            "head": {"sha": "a" * 40},
            "base": {"sha": "b" * 40},
            "changed_files": 1,
        }
        moved_base = {
            "state": "open",
            "draft": False,
            "head": {"sha": "a" * 40},
            "base": {"sha": "c" * 40},
            "changed_files": 1,
        }
        with patch.dict(os.environ, self.env, clear=False):
            with patch.object(tools, "_pr", return_value=initial):
                run_id = self.start_run()
            with (
                patch.object(tools, "_pr", return_value=moved_base),
                patch.object(tools, "_request") as requester,
            ):
                result = json.loads(
                    tools.pr_diff(
                        {
                            "repository": "sundsvallskommun/example-repository",
                            "pr_number": 1,
                            "run_id": run_id,
                        }
                    )
                )
        self.assertEqual(result["run_state"], "snapshot_superseded")
        self.assertTrue(result["terminal"])
        self.assertFalse(result["retryable"])
        requester.assert_not_called()

    def test_snapshot_terminal_handoff_is_reused_without_more_github_reads(self):
        initial = {
            "state": "open",
            "draft": False,
            "head": {"sha": "a" * 40, "repo": {"full_name": "sundsvallskommun/example-repository"}},
            "base": {"sha": "b" * 40, "repo": {"full_name": "sundsvallskommun/example-repository"}},
            "changed_files": 1,
        }
        moved_head = {
            "state": "open",
            "draft": False,
            "head": {"sha": "c" * 40, "repo": {"full_name": "sundsvallskommun/example-repository"}},
            "base": {"sha": "b" * 40, "repo": {"full_name": "sundsvallskommun/example-repository"}},
            "changed_files": 1,
        }
        with patch.dict(os.environ, self.env, clear=False):
            with patch.object(tools, "_pr", return_value=initial):
                run_id = self.start_run()
            with (
                patch.object(tools, "_pr", return_value=moved_head),
                patch.object(tools, "_file_at_revision") as reader,
                patch.object(tools, "_publish_failure_status_safe") as publish_status,
            ):
                first = json.loads(
                    tools.pr_file(
                        {
                            "repository": "sundsvallskommun/example-repository",
                            "pr_number": 1,
                            "path": "backend/changed.py",
                            "run_id": run_id,
                        }
                    )
                )
            with patch.object(
                tools, "_pr", side_effect=AssertionError("unexpected GitHub read")
            ) as github_reader:
                second = json.loads(
                    tools.review_deliver(
                        {
                            "repository": "sundsvallskommun/example-repository",
                            "pr_number": 1,
                            "head_sha": "a" * 40,
                            "run_id": run_id,
                        }
                    )
                )

        self.assertEqual(first, second)
        self.assertEqual(first["run_state"], "snapshot_superseded")
        self.assertTrue(first["terminal"])
        self.assertFalse(first["retryable"])
        reader.assert_not_called()
        github_reader.assert_not_called()
        publish_status.assert_called_once_with(
            run_id=run_id,
            failure_code="snapshot_superseded",
        )
        with closing(memory_db.connect_existing(self.db)) as connection:
            run = memory_db.get_run(connection, run_id)
        assert run is not None
        self.assertEqual(run["failure_code"], "snapshot_superseded")

    def test_delivery_race_reuses_terminal_contract_without_failure_status(self):
        pull = {
            "state": "open",
            "draft": False,
            "head": {"sha": "a" * 40},
            "base": {"sha": "b" * 40},
            "changed_files": 1,
        }
        original_update = (
            tools.review_run_application.memory_runs.update_run_phase
        )

        def supersede_before_publish(connection, candidate_run_id, phase, **kwargs):
            if phase == "publishing":
                memory_db.complete_run(
                    connection,
                    candidate_run_id,
                    repository="sundsvallskommun/example-repository",
                    pr_number=1,
                    status="failed",
                    failure_code="snapshot_superseded",
                )
                return None
            return original_update(
                connection, candidate_run_id, phase, **kwargs
            )

        with (
            patch.dict(os.environ, self.env, clear=False),
            patch.object(tools, "_pr", return_value=pull),
            patch.object(
                memory_db,
                "finalize_review",
                return_value={
                    "publication_id": 99,
                    "findings_count": 0,
                    "resolved_count": 0,
                },
            ),
            patch.object(
                tools.review_run_application.memory_runs,
                "update_run_phase",
                side_effect=supersede_before_publish,
            ),
            patch.object(tools, "_publish_failure_status_safe") as publish_status,
            patch.object(review_publisher, "publish_review") as publish_review,
        ):
            run_id = self.start_run()
            result = json.loads(
                tools.review_deliver(
                    {
                        "repository": "sundsvallskommun/example-repository",
                        "pr_number": 1,
                        "head_sha": "a" * 40,
                        "run_id": run_id,
                    }
                )
            )

        self.assertEqual(result["run_state"], "snapshot_superseded")
        self.assertTrue(result["terminal"])
        publish_status.assert_not_called()
        publish_review.assert_not_called()
        with closing(memory_db.connect_existing(self.db)) as connection:
            run = memory_db.get_run(connection, run_id)
        assert run is not None
        self.assertEqual(run["failure_code"], "snapshot_superseded")

    def test_deliver_finalizes_and_publishes_recorded_findings(self):
        pull = {
            "state": "open",
            "draft": False,
            "head": {"sha": "a" * 40},
            "base": {"sha": "b" * 40},
            "changed_files": 1,
        }
        with (
            patch.dict(os.environ, self.env, clear=False),
            patch.object(tools, "_pr", return_value=pull),
            patch.object(
                tools,
                "_changed_files",
                return_value=[
                    {
                        "path": "backend/changed.py",
                        "context_hash": "c" * 40,
                        "context_hash_source": "blob",
                    }
                ],
            ),
            patch.object(review_publisher, "_default_gateway", return_value=_FakeGitHub()),
        ):
            run_id = self.start_run()
            record_result = json.loads(
                tools.review_memory_record(
                    {
                        "repository": "sundsvallskommun/example-repository",
                        "pr_number": 1,
                        "head_sha": "a" * 40,
                        "run_id": run_id,
                        "findings": [self.finding],
                    }
                )
            )
            deliver_result = json.loads(
                tools.review_deliver(
                    {
                        "repository": "sundsvallskommun/example-repository",
                        "pr_number": 1,
                        "head_sha": "a" * 40,
                        "run_id": run_id,
                    }
                )
            )

        self.assertEqual(deliver_result["findings_count"], 1)
        self.assertTrue(deliver_result["published"])
        with closing(memory_db.connect_existing(self.db)) as connection:
            publication = memory_db.list_publications(
                connection, repository="sundsvallskommun/example-repository", pr_number=1
            )[0]
            rendered = connection.execute(
                "SELECT rendered_markdown FROM review_publications WHERE id = ?",
                (publication["id"],),
            ).fetchone()["rendered_markdown"]
        self.assertIn(
            "### F1 · Critical (P0): Boolean default remains disabled",
            rendered,
        )
        self.assertIn("Copyable fix brief for a coding agent", rendered)
        self.assertNotIn(
            record_result["recorded"][0]["fingerprint"],
            rendered.split("<!--", 1)[0],
        )

    def test_pr_file_reads_head_from_fork_repository(self):
        pull = {
            "head": {
                "sha": "a" * 40,
                "repo": {"full_name": "contributor/platform-fork"},
            },
            "base": {
                "sha": "b" * 40,
                "repo": {"full_name": "sundsvallskommun/example-repository"},
            },
        }
        with (
            patch.dict(os.environ, self.env, clear=False),
            patch.object(tools, "_pr", return_value=pull),
            patch.object(tools, "_changed_files", return_value=[]),
            patch.object(tools, "_file_at_revision", return_value=b"line one\n") as reader,
        ):
            run_id = self.start_run()
            result = json.loads(
                tools.pr_file(
                    {
                        "repository": "sundsvallskommun/example-repository",
                        "pr_number": 1,
                        "path": "backend/app.py",
                        "side": "head",
                        "run_id": run_id,
                    }
                )
            )
        reader.assert_called_once_with("contributor/platform-fork", "backend/app.py", "a" * 40)
        self.assertEqual(result["source_repository"], "contributor/platform-fork")

    def test_pr_file_reports_single_line_character_truncation_honestly(self):
        raw = ("x" * (capacity.current().text_page_max_chars * 2)).encode()
        with (
            patch.dict(os.environ, self.env, clear=False),
            patch.object(tools, "_pr", return_value=self._pull()),
            patch.object(tools, "_changed_files", return_value=[]),
            patch.object(tools, "_file_at_revision", return_value=raw),
        ):
            run_id = self.start_run()
            result = json.loads(
                tools.pr_file(
                    {
                        "repository": "sundsvallskommun/example-repository",
                        "pr_number": 1,
                        "path": "frontend/minified.js",
                        "side": "head",
                        "run_id": run_id,
                    }
                )
            )

        self.assertEqual(
            result["characters_returned"], capacity.current().text_page_max_chars
        )
        self.assertEqual(
            len(result["content"]), capacity.current().text_page_max_chars
        )
        self.assertEqual(result["complete_lines_returned"], 0)
        self.assertEqual(result["end_line"], 1)
        self.assertTrue(result["truncated"])

    def test_pr_file_exact_page_fill_still_reports_omitted_line(self):
        line = "x" * (capacity.current().text_page_max_chars - len("1: "))
        raw = f"{line}\ntail\n".encode()
        with (
            patch.dict(os.environ, self.env, clear=False),
            patch.object(tools, "_pr", return_value=self._pull()),
            patch.object(tools, "_changed_files", return_value=[]),
            patch.object(tools, "_file_at_revision", return_value=raw),
        ):
            run_id = self.start_run()
            result = json.loads(
                tools.pr_file(
                    {
                        "repository": "sundsvallskommun/example-repository",
                        "pr_number": 1,
                        "path": "frontend/exact.js",
                        "run_id": run_id,
                    }
                )
            )

        self.assertEqual(result["complete_lines_returned"], 1)
        self.assertEqual(
            result["characters_returned"], capacity.current().text_page_max_chars
        )
        self.assertTrue(result["truncated"])

    # --- stable not-found and path/side contract ---

    def _pull(self):
        return {
            "head": {"sha": "a" * 40, "repo": {"full_name": "sundsvallskommun/example-repository"}},
            "base": {"sha": "b" * 40, "repo": {"full_name": "sundsvallskommun/example-repository"}},
        }

    def test_file_not_found_message_is_stable_and_pathless(self):
        client = Mock()
        client.request_json.side_effect = source_control.GitHubReadError(
            "not_found", "not found"
        )
        with patch.object(tools, "_github_read_client", return_value=client):
            with self.assertRaises(tools.ToolInputError) as ctx:
                tools._file_at_revision("sundsvallskommun/example-repository", "backend/guessed/path.py", "a" * 40)
        message = str(ctx.exception)
        self.assertIn("do not retry guessed paths", message)
        self.assertNotIn("backend/guessed/path.py", message)

    def test_pr_file_not_found_returns_terminal_non_failure(self):
        with (
            patch.dict(os.environ, self.env, clear=False),
            patch.object(tools, "_pr", return_value=self._pull()),
            patch.object(tools, "_changed_files", return_value=[]),
            patch.object(
                tools,
                "_request_json",
                side_effect=tools.NotFoundError("not found"),
            ),
        ):
            run_id = self.start_run()
            result = json.loads(
                tools.pr_file(
                    {
                        "repository": "sundsvallskommun/example-repository",
                        "pr_number": 1,
                        "path": "backend/context.py",
                        "side": "head",
                        "run_id": run_id,
                    }
                )
            )

        self.assertNotIn("error", result)
        self.assertEqual(result["file_state"], "not_found_at_revision")
        self.assertEqual(result["content"], "")
        self.assertTrue(result["terminal"])
        self.assertFalse(result["retryable"])
        self.assertIn("Do not retry review_agent_pr_file", result["next_action"])

    def test_pr_file_missing_blob_returns_terminal_non_failure(self):
        metadata = {
            "type": "file",
            "encoding": "none",
            "content": "",
            "size": 1_100_000,
            "sha": "c" * 40,
        }
        with (
            patch.dict(os.environ, self.env, clear=False),
            patch.object(tools, "_pr", return_value=self._pull()),
            patch.object(tools, "_changed_files", return_value=[]),
            patch.object(tools, "_request_json", return_value=metadata),
            patch.object(
                tools,
                "_request",
                side_effect=tools.NotFoundError("not found"),
            ),
        ):
            run_id = self.start_run()
            result = json.loads(
                tools.pr_file(
                    {
                        "repository": "sundsvallskommun/example-repository",
                        "pr_number": 1,
                        "path": "backend/context.py",
                        "side": "head",
                        "run_id": run_id,
                    }
                )
            )

        self.assertNotIn("error", result)
        self.assertEqual(result["file_state"], "not_found_at_revision")
        self.assertTrue(result["terminal"])
        self.assertFalse(result["retryable"])

    def test_pr_file_missing_head_repository_returns_terminal_non_failure(self):
        pull = self._pull()
        pull["head"]["repo"] = None
        with (
            patch.dict(os.environ, self.env, clear=False),
            patch.object(tools, "_pr", return_value=pull),
            patch.object(tools, "_changed_files") as changed_reader,
            patch.object(tools, "_file_at_revision") as file_reader,
        ):
            run_id = self.start_run()
            result = json.loads(
                tools.pr_file(
                    {
                        "repository": "sundsvallskommun/example-repository",
                        "pr_number": 1,
                        "path": "backend/context.py",
                        "side": "head",
                        "run_id": run_id,
                    }
                )
            )

        self.assertNotIn("error", result)
        self.assertEqual(result["file_state"], "source_repository_unavailable")
        self.assertTrue(result["terminal"])
        self.assertFalse(result["retryable"])
        changed_reader.assert_not_called()
        file_reader.assert_not_called()

    def test_pr_file_redirects_added_file_to_head_without_tool_failure(self):
        files = [{"path": "backend/new.py", "status": "added", "previous_path": None}]
        with (
            patch.dict(os.environ, self.env, clear=False),
            patch.object(tools, "_pr", return_value=self._pull()),
            patch.object(tools, "_changed_files", return_value=files),
            patch.object(tools, "_file_at_revision") as reader,
        ):
            run_id = self.start_run()
            result = json.loads(
                tools.pr_file({"repository": "sundsvallskommun/example-repository", "pr_number": 1, "path": "backend/new.py", "side": "base", "run_id": run_id})
            )
        self.assertNotIn("error", result)
        self.assertEqual(result["file_state"], "side_unavailable")
        self.assertEqual(result["valid_side"], "head")
        self.assertTrue(result["terminal"])
        self.assertFalse(result["retryable"])
        self.assertIn("Do not retry side: base", result["next_action"])
        reader.assert_not_called()

    def test_pr_file_redirects_deleted_file_to_base_without_tool_failure(self):
        files = [{"path": "backend/gone.py", "status": "removed", "previous_path": None}]
        with (
            patch.dict(os.environ, self.env, clear=False),
            patch.object(tools, "_pr", return_value=self._pull()),
            patch.object(tools, "_changed_files", return_value=files),
            patch.object(tools, "_file_at_revision") as reader,
        ):
            run_id = self.start_run()
            result = json.loads(
                tools.pr_file({"repository": "sundsvallskommun/example-repository", "pr_number": 1, "path": "backend/gone.py", "side": "head", "run_id": run_id})
            )
        self.assertNotIn("error", result)
        self.assertEqual(result["file_state"], "side_unavailable")
        self.assertEqual(result["valid_side"], "base")
        self.assertTrue(result["terminal"])
        self.assertFalse(result["retryable"])
        self.assertIn("Do not retry side: head", result["next_action"])
        reader.assert_not_called()

    def test_pr_file_base_side_of_renamed_file_uses_previous_path(self):
        files = [{"path": "backend/new_name.py", "status": "renamed", "previous_path": "backend/old_name.py"}]
        with (
            patch.dict(os.environ, self.env, clear=False),
            patch.object(tools, "_pr", return_value=self._pull()),
            patch.object(tools, "_changed_files", return_value=files),
            patch.object(tools, "_file_at_revision", return_value=b"prior\n") as reader,
        ):
            run_id = self.start_run()
            json.loads(
                tools.pr_file({"repository": "sundsvallskommun/example-repository", "pr_number": 1, "path": "backend/new_name.py", "side": "base", "run_id": run_id})
            )
        reader.assert_called_once_with("sundsvallskommun/example-repository", "backend/old_name.py", "b" * 40)

    def test_pr_file_reuses_run_owned_rename_metadata_without_remote_file_index(self):
        with patch.dict(os.environ, self.env, clear=False):
            run_id = self.start_run()
            with closing(memory_db.connect_existing(self.db)) as connection:
                memory_db.register_changed_files(
                    connection,
                    run_id=run_id,
                    repository="sundsvallskommun/example-repository",
                    pr_number=1,
                    files=[
                        {
                            "path": "backend/new_name.py",
                            "status": "renamed",
                            "previous_path": "backend/old_name.py",
                        }
                    ],
                )
            with (
                patch.object(tools, "_pr", return_value=self._pull()),
                patch.object(tools, "_changed_files") as remote_index,
                patch.object(
                    tools,
                    "_file_at_revision",
                    return_value=b"prior\n",
                ) as reader,
            ):
                result = json.loads(
                    tools.pr_file(
                        {
                            "repository": "sundsvallskommun/example-repository",
                            "pr_number": 1,
                            "path": "backend/new_name.py",
                            "side": "base",
                            "run_id": run_id,
                        }
                    )
                )

        self.assertNotIn("error", result)
        remote_index.assert_not_called()
        reader.assert_called_once_with(
            "sundsvallskommun/example-repository", "backend/old_name.py", "b" * 40
        )

    def test_pr_file_redirects_unchanged_base_read_to_head_without_tool_failure(self):
        with (
            patch.dict(os.environ, self.env, clear=False),
            patch.object(tools, "_pr", return_value=self._pull()),
            patch.object(tools, "_changed_files", return_value=[]),
            patch.object(tools, "_file_at_revision") as reader,
        ):
            run_id = self.start_run()
            result = json.loads(
                tools.pr_file(
                    {
                        "repository": "sundsvallskommun/example-repository",
                        "pr_number": 1,
                        "path": "backend/context.py",
                        "side": "base",
                        "run_id": run_id,
                    }
                )
            )

        self.assertNotIn("error", result)
        self.assertEqual(result["file_state"], "side_unavailable")
        self.assertEqual(result["valid_side"], "head")
        self.assertTrue(result["terminal"])
        self.assertFalse(result["retryable"])
        reader.assert_not_called()

    def test_pr_file_redirects_rename_without_prior_path_to_head(self):
        files = [
            {
                "path": "backend/new_name.py",
                "status": "renamed",
                "previous_path": None,
            }
        ]
        with (
            patch.dict(os.environ, self.env, clear=False),
            patch.object(tools, "_pr", return_value=self._pull()),
            patch.object(tools, "_changed_files", return_value=files),
            patch.object(tools, "_file_at_revision") as reader,
        ):
            run_id = self.start_run()
            result = json.loads(
                tools.pr_file(
                    {
                        "repository": "sundsvallskommun/example-repository",
                        "pr_number": 1,
                        "path": "backend/new_name.py",
                        "side": "base",
                        "run_id": run_id,
                    }
                )
            )

        self.assertNotIn("error", result)
        self.assertEqual(result["file_state"], "side_unavailable")
        self.assertEqual(result["valid_side"], "head")
        self.assertTrue(result["terminal"])
        self.assertFalse(result["retryable"])
        reader.assert_not_called()

    def test_pr_file_non_regular_path_returns_terminal_non_failure(self):
        metadata = {
            "type": "dir",
            "encoding": "none",
            "content": "",
            "size": 0,
        }
        with (
            patch.dict(os.environ, self.env, clear=False),
            patch.object(tools, "_pr", return_value=self._pull()),
            patch.object(tools, "_changed_files", return_value=[]),
            patch.object(tools, "_request_json", return_value=metadata),
        ):
            run_id = self.start_run()
            result = json.loads(
                tools.pr_file(
                    {
                        "repository": "sundsvallskommun/example-repository",
                        "pr_number": 1,
                        "path": "backend",
                        "run_id": run_id,
                    }
                )
            )

        self.assertNotIn("error", result)
        self.assertEqual(result["file_state"], "not_regular")
        self.assertEqual(result["content"], "")
        self.assertTrue(result["terminal"])
        self.assertFalse(result["retryable"])

    def test_pr_file_oversized_path_returns_terminal_non_failure(self):
        metadata = {
            "type": "file",
            "encoding": "none",
            "content": "",
            "size": tools.GITHUB_CONTENTS_FILE_MAX_BYTES + 1,
            "sha": "a" * 40,
        }
        with (
            patch.dict(os.environ, self.env, clear=False),
            patch.object(tools, "_pr", return_value=self._pull()),
            patch.object(tools, "_changed_files", return_value=[]),
            patch.object(tools, "_request_json", return_value=metadata),
            patch.object(tools, "_request") as raw_reader,
        ):
            run_id = self.start_run()
            result = json.loads(
                tools.pr_file(
                    {
                        "repository": "sundsvallskommun/example-repository",
                        "pr_number": 1,
                        "path": "data/huge.json",
                        "run_id": run_id,
                    }
                )
            )

        self.assertNotIn("error", result)
        self.assertEqual(result["file_state"], "too_large")
        self.assertEqual(result["content"], "")
        self.assertTrue(result["terminal"])
        self.assertFalse(result["retryable"])
        raw_reader.assert_not_called()

    def test_pr_file_binary_path_returns_terminal_non_failure(self):
        with (
            patch.dict(os.environ, self.env, clear=False),
            patch.object(tools, "_pr", return_value=self._pull()),
            patch.object(tools, "_changed_files", return_value=[]),
            patch.object(tools, "_file_at_revision", return_value=b"\x00binary"),
        ):
            run_id = self.start_run()
            result = json.loads(
                tools.pr_file(
                    {
                        "repository": "sundsvallskommun/example-repository",
                        "pr_number": 1,
                        "path": "assets/image.bin",
                        "run_id": run_id,
                    }
                )
            )

        self.assertNotIn("error", result)
        self.assertEqual(result["file_state"], "binary")
        self.assertEqual(result["content"], "")
        self.assertTrue(result["terminal"])
        self.assertFalse(result["retryable"])


if __name__ == "__main__":
    unittest.main()
