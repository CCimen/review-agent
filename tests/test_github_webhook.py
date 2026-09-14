from __future__ import annotations

import hashlib
import hmac
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bootstrap" / "plugins"))

from review_agent_tools import github_webhook  # noqa: E402


class GitHubWebhookTests(unittest.TestCase):
    def test_documentation_commands_and_only_relevant_pr_actions_are_normalized(self) -> None:
        from tests.test_github_app_processor import GitHubAppProcessorTests
        for body, expected in (("/review docs", "review"), ("@review DOCS", "review"),
                               ("/review docs extra", "invalid"),
                               ("/review docs false-positive F2 because A current guard proves this.", "finding_feedback")):
            payload = GitHubAppProcessorTests.review_payload()
            payload["comment"]["body"] = body
            normalized = github_webhook.normalize_event("issue_comment", payload)
            self.assertEqual(normalized.command_kind.value, expected)
            self.assertEqual(normalized.normalized["purpose"], "documentation")
        base = {"installation": {"id": 7}, "repository": {"id": 42, "full_name": "owner/repo"},
                "pull_request": {"number": 99, "body": "untrusted content", "head": {"sha": "ignored"}},
                "sender": {"id": 5, "login": "author"}}
        for action, changes, accepted in (("opened", {}, True), ("reopened", {}, True),
            ("ready_for_review", {}, True), ("synchronize", {}, True), ("converted_to_draft", {}, True),
            ("closed", {}, True), ("edited", {"base": {"ref": {"from": "old"}}}, True),
            ("edited", {"body": {"from": "old"}}, False), ("labeled", {}, False)):
            normalized = github_webhook.normalize_event("pull_request", {**base, "action": action, "changes": changes})
            self.assertEqual(normalized.command_kind is github_webhook.CommandKind.REVIEW, accepted)
            self.assertEqual(normalized.normalized["pr_number"], 99)
            self.assertNotIn("head", normalized.normalized)
            self.assertNotIn("body", normalized.normalized)

    def test_signature_covers_the_untouched_body(self) -> None:
        body = b'{"action":"created"}'
        signature = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()

        self.assertTrue(github_webhook.verify_signature(body, signature, "secret"))
        self.assertFalse(
            github_webhook.verify_signature(body + b"\n", signature, "secret")
        )

    def test_installation_and_repository_changes_keep_only_resume_fields(self) -> None:
        installation = github_webhook.normalize_event(
            "installation",
            {
                "action": "created",
                "installation": {
                    "id": 7001,
                    "account": {"id": 8001, "login": "CCimen", "type": "User"},
                    "repository_selection": "selected",
                    "permissions": {
                        "contents": "read",
                        "issues": "write",
                        "pull_requests": "write",
                        "administration": "read",
                    },
                },
                "repositories": [{"id": 42, "full_name": "CCimen/review-agent"}],
                "sender": {"id": 9001, "login": "ignored-source-field"},
            },
        )
        repositories = github_webhook.normalize_event(
            "installation_repositories",
            {
                "action": "added",
                "installation": {
                    "id": 7001,
                },
                "repository_selection": "selected",
                "repositories_added": [
                    {"id": 42, "full_name": "CCimen/review-agent", "private": True}
                ],
                "repositories_removed": [],
            },
        )

        self.assertEqual(installation.provider_installation_id, 7001)
        self.assertIsNone(installation.provider_repository_id)
        self.assertEqual(
            installation.normalized,
            {
                "account_id": 8001,
                "account_login": "CCimen",
                "account_type": "user",
                "contents_permission": "read",
                "issues_permission": "write",
                "kind": "installation",
                "pull_requests_permission": "write",
                "checks_permission": "none",
                "repository_selection": "selected",
                "repositories": [{"full_name": "CCimen/review-agent", "id": 42}],
            },
        )
        self.assertEqual(
            repositories.normalized,
            {
                "kind": "installation_repositories",
                "repository_selection": "selected",
                "repositories": [{"full_name": "CCimen/review-agent", "id": 42}],
            },
        )

    def test_installation_created_accepts_an_omitted_repository_list(self) -> None:
        normalized = github_webhook.normalize_event(
            "installation",
            {
                "action": "created",
                "installation": {
                    "id": 7001,
                    "account": {"id": 8001, "login": "CCimen", "type": "User"},
                    "repository_selection": "selected",
                    "permissions": {
                        "contents": "read",
                        "issues": "write",
                        "pull_requests": "write",
                    },
                },
            },
        )

        self.assertEqual(normalized.normalized["repositories"], [])

    def test_installation_repositories_deduplicate_or_reject_conflicts(self) -> None:
        def normalize(repositories: list[dict[str, object]]) -> list[object]:
            event = github_webhook.normalize_event(
                "installation",
                {
                    "action": "created",
                    "installation": {
                        "id": 7001,
                        "account": {
                            "id": 8001,
                            "login": "CCimen",
                            "type": "User",
                        },
                        "repository_selection": "selected",
                        "permissions": {
                            "contents": "read",
                            "issues": "write",
                            "pull_requests": "write",
                        },
                    },
                    "repositories": repositories,
                },
            )
            value = event.normalized["repositories"]
            assert isinstance(value, list)
            return value

        repository = {"id": 42, "full_name": "CCimen/review-agent"}
        self.assertEqual(
            normalize([repository, repository.copy()]),
            [{"full_name": "CCimen/review-agent", "id": 42}],
        )

        with self.assertRaisesRegex(
            github_webhook.GitHubWebhookError,
            "repository id maps to conflicting names",
        ):
            normalize(
                [
                    repository,
                    {"id": 42, "full_name": "CCimen/another-repository"},
                ]
            )
        with self.assertRaisesRegex(
            github_webhook.GitHubWebhookError,
            "repository name maps to conflicting ids",
        ):
            normalize(
                [
                    repository,
                    {"id": 43, "full_name": "ccimen/review-agent"},
                ]
            )

    def test_issue_comment_normalizes_review_and_typed_feedback(self) -> None:
        review = github_webhook.normalize_event(
            "issue_comment", self.issue_comment("/review")
        )
        feedback = github_webhook.normalize_event(
            "issue_comment",
            self.issue_comment(
                "/review false-positive F2 because Existing validation covers it."
            ),
        )

        self.assertEqual(review.command_kind, "review")
        self.assertNotIn("body", review.normalized)
        self.assertEqual(feedback.command_kind, "finding_feedback")
        self.assertEqual(
            feedback.normalized["command"],
            {
                "decision": "false_positive",
                "local_reference": "F2",
                "reason": "Existing validation covers it.",
            },
        )
        self.assertNotIn("/review false-positive", str(feedback.normalized))

    def test_non_pr_edited_bot_and_unknown_commands_are_bounded_ignores(self) -> None:
        cases = (
            (self.issue_comment("/review", action="edited"), "unsupported_action"),
            (self.issue_comment("/review", pull_request=False), "not_pull_request"),
            (self.issue_comment("/review", sender_type="Bot"), "bot_sender"),
            (self.issue_comment("hello from a developer"), "not_review_command"),
        )

        for payload, reason in cases:
            with self.subTest(reason=reason):
                normalized = github_webhook.normalize_event("issue_comment", payload)
                self.assertEqual(normalized.command_kind, "ignored")
                self.assertEqual(normalized.normalized["reason"], reason)
                self.assertNotIn("body", normalized.normalized)

    def test_malformed_identity_and_unsupported_event_fail_closed(self) -> None:
        malformed = self.issue_comment("/review")
        repository = malformed["repository"]
        assert isinstance(repository, dict)
        repository["id"] = 0

        with self.assertRaisesRegex(github_webhook.GitHubWebhookError, "repository.id"):
            github_webhook.normalize_event("issue_comment", malformed)
        with self.assertRaisesRegex(
            github_webhook.GitHubWebhookError, "unsupported GitHub event"
        ):
            github_webhook.normalize_event("workflow_run", {})

    @staticmethod
    def issue_comment(
        body: str,
        *,
        action: str = "created",
        pull_request: bool = True,
        sender_type: str = "User",
    ) -> dict[str, object]:
        issue: dict[str, object] = {"number": 42}
        if pull_request:
            issue["pull_request"] = {"url": "https://api.github.test/pulls/42"}
        return {
            "action": action,
            "installation": {
                "id": 7001,
                "repository_selection": "selected",
            },
            "repository": {"id": 9001, "full_name": "CCimen/review-agent"},
            "issue": issue,
            "comment": {
                "id": 6001,
                "body": body,
                "author_association": "MEMBER",
            },
            "sender": {"id": 5001, "login": "ccimen", "type": sender_type},
        }


if __name__ == "__main__":
    unittest.main()
