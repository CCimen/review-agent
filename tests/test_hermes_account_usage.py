"""Offline quota contract checks executed inside the pinned Hermes image."""

import importlib.util
import unittest
from unittest.mock import patch


@unittest.skipUnless(importlib.util.find_spec("hermes_cli"), "requires Hermes")
class HermesAccountUsageTests(unittest.TestCase):
    def fetch(self, payload: object):
        import httpx
        from agent.account_usage import fetch_account_usage

        client_class = httpx.Client

        def respond(request: httpx.Request) -> httpx.Response:
            self.assertEqual(
                str(request.url), "https://chatgpt.com/backend-api/wham/usage"
            )
            self.assertEqual(request.headers["ChatGPT-Account-Id"], "account-a")
            self.assertEqual(request.headers["Authorization"], "Bearer test-token")
            if isinstance(payload, bytes):
                return httpx.Response(200, content=payload)
            return httpx.Response(200, json=payload)

        transport = httpx.MockTransport(respond)
        with (
            patch(
                "agent.account_usage.httpx.Client",
                side_effect=lambda **kwargs: client_class(
                    transport=transport, **kwargs
                ),
            ),
            patch(
                "agent.account_usage.resolve_codex_runtime_credentials",
                side_effect=AssertionError("Explicit usage must keep its account"),
            ),
            patch(
                "agent.account_usage._read_codex_tokens",
                side_effect=AssertionError("Do not read another account header"),
            ),
        ):
            return fetch_account_usage(
                "openai-codex",
                api_key="test-token",
                base_url="https://chatgpt.com/backend-api/codex",
                account_id="account-a",
                user_id="user-a",
            )

    def test_account_quota_preserves_buckets_units_and_missing_values(self) -> None:
        snapshot = self.fetch(
            {
                "account_id": "account-a",
                "user_id": "user-a",
                "plan_type": "pro",
                "rate_limit": {
                    "allowed": False,
                    "limit_reached": True,
                    "primary_window": {
                        "used_percent": 100,
                        "limit_window_seconds": 7200,
                        "reset_at": 1_900_000_000,
                    },
                    "secondary_window": None,
                },
                "additional_rate_limits": [
                    {
                        "metered_feature": "codex_other",
                        "limit_name": "Other model",
                        "normal_model_slug": "other-model",
                        "rate_limit": {
                            "allowed": True,
                            "limit_reached": False,
                            "primary_window": {
                                "used_percent": 0.5,
                                "limit_window_seconds": 900,
                            },
                            "secondary_window": {"reset_at": 1_900_500_000},
                        },
                    }
                ],
                "rate_limit_reset_credits": {"available_count": 0},
                "spend_control": {"reached": False},
            }
        )
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.reset_credits_available, 0)
        self.assertIs(snapshot.spend_control_reached, False)
        self.assertEqual(len(snapshot.buckets), 2)
        main, other = snapshot.buckets
        self.assertEqual(main.id, "codex")
        self.assertIs(main.allowed, False)
        self.assertIs(main.limit_reached, True)
        self.assertEqual(len(main.windows), 1)
        self.assertEqual(main.windows[0].kind, "primary")
        self.assertEqual(main.windows[0].duration_seconds, 7200)
        self.assertEqual(main.windows[0].reset_at.timestamp(), 1_900_000_000)
        self.assertEqual(other.id, "codex_other")
        self.assertEqual(other.name, "Other model")
        self.assertEqual(other.normal_model_slug, "other-model")
        self.assertEqual(other.windows[0].used_percent, 0.5)
        self.assertEqual(other.windows[0].duration_seconds, 900)
        self.assertIsNone(other.windows[0].reset_at)
        self.assertIsNone(other.windows[1].used_percent)
        self.assertEqual(other.windows[1].kind, "secondary")
        self.assertIsNone(other.windows[1].duration_seconds)

    def test_wrong_account_response_is_unavailable(self) -> None:
        self.assertIsNone(self.fetch({"account_id": "account-b"}))
        self.assertIsNone(self.fetch({"account_id": "account-a", "user_id": "user-b"}))

    def test_oversized_response_is_unavailable(self) -> None:
        self.assertIsNone(
            self.fetch(
                b'{"account_id":"account-a","padding":"' + b"a" * (256 * 1024) + b'"}'
            )
        )


if __name__ == "__main__":
    if importlib.util.find_spec("hermes_cli") is None:
        raise SystemExit("The image quota checks require the Hermes runtime")
    unittest.main()
