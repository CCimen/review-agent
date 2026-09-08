"""Offline behavior checks run inside the pinned Hermes image during its build."""

from __future__ import annotations

import importlib.util
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch


@unittest.skipUnless(importlib.util.find_spec("hermes_cli"), "requires Hermes image")
class HermesAuxiliaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        from agent import auxiliary_client

        self.aux = auxiliary_client
        self.runtime = {
            "provider": "openai-codex",
            "model": "test-selected-model",
            "api_key": "synthetic-selected-account",
        }
        self.enterContext(
            patch(
                "hermes_cli.config.load_config_readonly",
                return_value={
                    "auxiliary": {
                        "allow_fallback": False,
                        "compression": {
                            "fallback_chain": [
                                {"provider": "anthropic", "model": "other-model"}
                            ],
                        },
                    },
                    "fallback_providers": [
                        {"provider": "anthropic", "model": "other-model"}
                    ],
                },
            )
        )
        self.enterContext(
            patch.object(self.aux, "_is_provider_unhealthy", return_value=False)
        )

    def test_missing_selected_account_never_discovers_another_provider(self) -> None:
        other_account = Mock()
        with (
            patch.object(
                self.aux, "resolve_provider_client", return_value=(None, None)
            ) as selected,
            patch.object(
                self.aux, "_try_openrouter", return_value=(other_account, "other-model")
            ) as fallback,
        ):
            result = self.aux._resolve_auto_route(self.runtime, task="compression")
        self.assertEqual(result, (None, None, ""))
        fallback.assert_not_called()
        selected.assert_called_once()
        self.assertEqual(
            selected.call_args.args[:2], ("openai-codex", "test-selected-model")
        )
        self.assertEqual(
            selected.call_args.kwargs["explicit_api_key"], "synthetic-selected-account"
        )

    def test_healthy_selected_account_keeps_its_provider_model_and_key(self) -> None:
        client = Mock()
        with patch.object(
            self.aux,
            "resolve_provider_client",
            return_value=(client, "test-selected-model"),
        ) as selected:
            result = self.aux._resolve_auto_route(self.runtime, task="compression")
        self.assertEqual(result, (client, "test-selected-model", "openai-codex"))
        self.assertEqual(
            selected.call_args.kwargs["explicit_api_key"], "synthetic-selected-account"
        )

    def test_missing_fallback_policy_denies_provider_discovery(self) -> None:
        with (
            patch("hermes_cli.config.load_config_readonly", return_value={}),
            patch.object(
                self.aux, "resolve_provider_client", return_value=(None, None)
            ),
            patch.object(
                self.aux, "_try_openrouter", return_value=(Mock(), "other-model")
            ) as fallback,
        ):
            self.assertEqual(
                self.aux._resolve_auto_route(self.runtime, task="compression"),
                (None, None, ""),
            )
        fallback.assert_not_called()

    async def test_exhausted_selected_account_never_uses_request_time_fallback(
        self,
    ) -> None:
        for async_mode in (False, True):
            with self.subTest(async_mode=async_mode):
                create = AsyncMock() if async_mode else Mock()
                create.side_effect = RuntimeError("insufficient credits")
                client = SimpleNamespace(
                    api_key="synthetic-selected-account",
                    base_url="https://api.openai.com/v1",
                    chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
                )
                with (
                    patch.object(
                        self.aux,
                        "_get_cached_client",
                        return_value=(client, "test-selected-model"),
                    ),
                    patch.object(
                        self.aux,
                        "_effective_provider_for_client",
                        return_value="openai-codex",
                    ),
                    patch.object(
                        self.aux, "_recover_provider_pool", return_value=False
                    ),
                    patch.object(
                        self.aux, "_recoverable_pool_provider", return_value=None
                    ),
                    patch.object(
                        self.aux,
                        "_try_openrouter",
                        return_value=(Mock(), "other-model"),
                    ) as fallback,
                ):
                    with self.assertRaisesRegex(RuntimeError, "insufficient credits"):
                        if async_mode:
                            await self.aux._async_call_llm_impl(
                                task="compression",
                                main_runtime=self.runtime,
                                messages=[{"role": "user", "content": "test"}],
                            )
                        else:
                            self.aux._call_llm_impl(
                                task="compression",
                                main_runtime=self.runtime,
                                messages=[{"role": "user", "content": "test"}],
                            )
                fallback.assert_not_called()


if __name__ == "__main__":
    if importlib.util.find_spec("hermes_cli") is None:
        raise SystemExit("The image isolation checks require the Hermes runtime")
    unittest.main()
