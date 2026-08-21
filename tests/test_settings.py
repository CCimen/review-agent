from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PLUGIN_PARENT = Path(__file__).resolve().parents[1] / "bootstrap" / "plugins"
sys.path.insert(0, str(PLUGIN_PARENT))

from review_agent_tools.settings import (  # noqa: E402
    PostgresDatabaseUrl,
    ReviewAgentSettings,
    SettingsError,
)
from review_agent_tools import memory_validation, review_publisher  # noqa: E402


class ReviewAgentSettingsTests(unittest.TestCase):
    def test_postgresql_database_url_is_required_and_typed(self) -> None:
        configured = ReviewAgentSettings(
            {
                "REVIEW_AGENT_DATABASE_URL": (
                    " postgresql://reviewer:secret@db.example.test/reviews "
                )
            }
        )

        self.assertEqual(
            configured.postgres_database_url,
            PostgresDatabaseUrl(
                "postgresql://reviewer:secret@db.example.test/reviews"
            ),
        )
        with self.assertRaisesRegex(
            SettingsError, "REVIEW_AGENT_DATABASE_URL is required"
        ):
            ReviewAgentSettings({}).postgres_database_url
        with self.assertRaisesRegex(
            SettingsError, "REVIEW_AGENT_DATABASE_URL must be a PostgreSQL URL"
        ):
            ReviewAgentSettings(
                {"REVIEW_AGENT_DATABASE_URL": "sqlite:///review.sqlite3"}
            ).postgres_database_url
        for incomplete_url in (
            "postgresql://db.example.test",
            "postgresql:///reviews",
        ):
            with self.subTest(incomplete_url=incomplete_url):
                with self.assertRaisesRegex(
                    SettingsError, "must include a host and database name"
                ):
                    ReviewAgentSettings(
                        {"REVIEW_AGENT_DATABASE_URL": incomplete_url}
                    ).postgres_database_url

    def test_repository_and_token_values_are_normalized(self) -> None:
        settings = ReviewAgentSettings(
            {
                "REVIEW_AGENT_ALLOWED_REPOSITORIES": (
                    " Sundsvallskommun/API,example-org/example-repository, "
                ),
                "GITHUB_READ_TOKEN": " read-token ",
                "REVIEW_AGENT_PUBLISH_GH_TOKEN": " publish-token ",
            }
        )

        self.assertEqual(
            settings.allowed_repositories,
            frozenset(
                {
                    "sundsvallskommun/api",
                    "example-org/example-repository",
                }
            ),
        )
        self.assertEqual(settings.github_read_token, "read-token")
        self.assertEqual(settings.github_publish_token, "publish-token")

    def test_publish_byte_limit_preserves_default_clamp_and_error(self) -> None:
        self.assertEqual(ReviewAgentSettings({}).publish_max_bytes, 60_000)
        self.assertEqual(
            ReviewAgentSettings(
                {"REVIEW_AGENT_PUBLISH_MAX_BYTES": "10"}
            ).publish_max_bytes,
            1_000,
        )
        self.assertEqual(
            ReviewAgentSettings(
                {"REVIEW_AGENT_PUBLISH_MAX_BYTES": "100000"}
            ).publish_max_bytes,
            65_000,
        )
        invalid = ReviewAgentSettings(
            {
                "GITHUB_READ_TOKEN": "read-token",
                "REVIEW_AGENT_PUBLISH_MAX_BYTES": "many",
            }
        )
        self.assertEqual(invalid.github_read_token, "read-token")
        with self.assertRaisesRegex(
            SettingsError,
            "REVIEW_AGENT_PUBLISH_MAX_BYTES must be an integer",
        ):
            invalid.publish_max_bytes

    def test_database_path_preserves_explicit_environment_and_home_fallbacks(self) -> None:
        settings = ReviewAgentSettings(
            {
                "REVIEW_AGENT_DB": "~/configured.sqlite3",
                "HERMES_HOME": "~/custom-hermes",
            }
        )

        self.assertEqual(
            settings.database_path("~/explicit.sqlite3"),
            Path("~/explicit.sqlite3").expanduser(),
        )
        self.assertEqual(
            settings.database_path(),
            Path("~/configured.sqlite3").expanduser(),
        )
        self.assertEqual(
            ReviewAgentSettings(
                {"HERMES_HOME": "~/custom-hermes"}
            ).database_path(),
            Path("~/custom-hermes/review-memory/review_memory.sqlite3").expanduser(),
        )

    def test_policy_revision_and_feedback_preserve_current_semantics(self) -> None:
        settings = ReviewAgentSettings(
            {
                "REVIEW_AGENT_POLICY_REVISION": "  policy   v2  ",
                "REVIEW_AGENT_FEEDBACK_ENABLED": " YES ",
            }
        )

        self.assertEqual(settings.policy_revision(), "policy v2")
        self.assertEqual(ReviewAgentSettings({}).policy_revision(), "policy-v1")
        self.assertTrue(settings.feedback_enabled)
        self.assertFalse(ReviewAgentSettings({}).feedback_enabled)
        with self.assertRaisesRegex(SettingsError, "policy_revision is required"):
            ReviewAgentSettings(
                {"REVIEW_AGENT_POLICY_REVISION": ""}
            ).policy_revision()
        with self.assertRaisesRegex(
            SettingsError,
            "policy_revision exceeds 120 characters",
        ):
            ReviewAgentSettings({}).policy_revision("x" * 121)

    def test_invalid_settings_keep_memory_error_contracts(self) -> None:
        with patch.dict(
            os.environ,
            {"REVIEW_AGENT_PUBLISH_MAX_BYTES": "many"},
            clear=False,
        ):
            with self.assertRaisesRegex(
                memory_validation.ReviewMemoryError,
                "REVIEW_AGENT_PUBLISH_MAX_BYTES must be an integer",
            ):
                review_publisher._max_comment_bytes()

        for revision, message in (
            (" ", "policy_revision is required"),
            ("x" * 121, "policy_revision exceeds 120 characters"),
        ):
            with self.subTest(revision=revision):
                with self.assertRaisesRegex(
                    memory_validation.ReviewMemoryError,
                    message,
                ):
                    memory_validation.current_policy_revision(revision)


if __name__ == "__main__":
    unittest.main()
