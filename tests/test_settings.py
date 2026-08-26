from __future__ import annotations

import sys
import unittest
from pathlib import Path

PLUGIN_PARENT = Path(__file__).resolve().parents[1] / "bootstrap" / "plugins"
sys.path.insert(0, str(PLUGIN_PARENT))

from review_agent_tools.settings import (  # noqa: E402
    PostgresDatabaseUrl,
    ReviewAgentSettings,
    SettingsError,
)
from review_agent_tools import memory_validation  # noqa: E402


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
                {"REVIEW_AGENT_DATABASE_URL": "https://db.example.test/reviews"}
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

    def test_gateway_and_transitional_publish_values_are_normalized(self) -> None:
        settings = ReviewAgentSettings(
            {
                "REVIEW_AGENT_GITHUB_GATEWAY_URL": " http://gateway:8646 ",
                "REVIEW_AGENT_PUBLISH_GH_TOKEN": " publish-token ",
            }
        )

        self.assertEqual(settings.github_gateway_url, "http://gateway:8646")
        self.assertEqual(settings.github_publish_token, "publish-token")
        with self.assertRaisesRegex(
            SettingsError, "REVIEW_AGENT_GITHUB_GATEWAY_URL is required"
        ):
            ReviewAgentSettings({}).github_gateway_url

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
                "REVIEW_AGENT_PUBLISH_MAX_BYTES": "many",
            }
        )
        with self.assertRaisesRegex(
            SettingsError,
            "REVIEW_AGENT_PUBLISH_MAX_BYTES must be an integer",
        ):
            invalid.publish_max_bytes

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

    def test_invalid_policy_revisions_keep_validation_error_contract(self) -> None:
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
