"""Offline model-window contract checks executed inside the pinned Hermes image."""

import importlib.util
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, "/opt/review-agent-bootstrap/plugins")

CONFIG = {"compression": {"threshold": 0.8}}


@unittest.skipUnless(importlib.util.find_spec("hermes_cli"), "requires Hermes")
class HermesModelWindowTests(unittest.TestCase):
    def resolve(self, provider: str, model: str, context_tokens: int):
        from review_agent_tools import model_window

        runtime = {
            "provider": provider,
            "base_url": "https://chatgpt.com/backend-api/codex",
            "api_key": "",
        }
        # Autospec keeps Hermes' real signatures, so an incompatible call fails.
        with (
            patch(
                "hermes_cli.runtime_provider.resolve_runtime_provider",
                autospec=True,
                return_value=runtime,
            ) as resolve_runtime,
            patch(
                "agent.model_metadata.get_model_context_length",
                autospec=True,
                return_value=context_tokens,
            ) as context_length,
            patch(
                "hermes_cli.config.load_config_readonly",
                autospec=True,
                return_value=CONFIG,
            ),
        ):
            window = model_window.resolve(provider, model)
        resolve_runtime.assert_called_once_with(requested=provider, target_model=model)
        context_length.assert_called_once_with(
            model, base_url=runtime["base_url"], api_key="", provider=provider
        )
        return window

    def test_resolves_through_the_functions_hermes_uses_for_its_sessions(self) -> None:
        from review_agent_tools import model_window

        window = self.resolve("openai-codex", "selected-model", 500_000)
        self.assertEqual(
            window, model_window.ModelWindow(500_000, 100_000, 200_000, 400_000)
        )
        self.assertEqual(model_window.default_budget(), (100_000, 200_000))

    def test_budgets_follow_hermes_for_a_small_window(self) -> None:
        from tools.budget_config import budget_for_context_window

        window = self.resolve("openai-codex", "small-model", 128_000)
        budget = budget_for_context_window(128_000)
        self.assertEqual(
            (window.result_chars, window.turn_chars),
            (budget.default_result_size, budget.turn_budget),
        )
        self.assertLess(window.turn_chars, 200_000)


if __name__ == "__main__":
    unittest.main()
