from __future__ import annotations

import asyncio
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import jwt

from review_agent_tools import model_quota
from review_agent_tools.model_accounts import (
    AccountAvailability,
    ModelProvider,
    RuntimeAccount,
    account_identity,
)


class ModelQuotaAccountTests(unittest.TestCase):
    def test_refresh_binds_the_usage_request_to_the_observed_account(self) -> None:
        def token(account: str) -> str:
            return jwt.encode(
                {
                    "sub": "user-a",
                    "https://api.openai.com/auth": {
                        "chatgpt_account_id": account,
                    },
                },
                "local-test-signing-key-at-least-32-bytes",
                algorithm="HS256",
            )

        current_token = token("account-a")
        identity = account_identity(
            ModelProvider.CODEX, token=current_token, auth_type="oauth"
        )
        account = RuntimeAccount(
            ModelProvider.CODEX, AccountAvailability.AVAILABLE, 1, identity
        )
        auth = Mock()
        auth.resolve_codex_runtime_credentials.return_value = {
            "api_key": current_token,
            "base_url": "https://chatgpt.com/backend-api/codex",
        }
        usage = Mock()
        usage.fetch_account_usage.return_value = SimpleNamespace(
            fetched_at=SimpleNamespace(timestamp=lambda: 1788897600),
            plan="Pro",
            buckets=(),
            reset_credits_available=0,
            limit_reached_type=None,
            spend_control_reached=None,
        )
        with (
            patch.object(
                model_quota,
                "import_module",
                side_effect={
                    "hermes_cli.auth": auth,
                    "agent.account_usage": usage,
                }.__getitem__,
            ),
            patch.object(
                model_quota, "read_accounts", return_value=(account,)
            ) as accounts,
        ):
            self.assertIsNotNone(model_quota.fetch_account_quota(account))
            usage.fetch_account_usage.assert_called_once_with(
                "openai-codex",
                api_key=current_token,
                base_url="https://chatgpt.com/backend-api/codex",
                account_id="account-a",
                user_id="user-a",
            )
            usage.fetch_account_usage.reset_mock()
            auth.resolve_codex_runtime_credentials.return_value["api_key"] = token(
                "account-b"
            )
            self.assertIsNone(model_quota.fetch_account_quota(account))
            usage.fetch_account_usage.assert_not_called()
            auth.resolve_codex_runtime_credentials.return_value["api_key"] = (
                current_token
            )
            accounts.return_value = (
                RuntimeAccount(
                    ModelProvider.CODEX, AccountAvailability.AVAILABLE, 1, "b" * 64
                ),
            )
            self.assertIsNone(model_quota.fetch_account_quota(account))


class ModelQuotaCacheTests(unittest.IsolatedAsyncioTestCase):
    def account(self, identity: str = "a") -> RuntimeAccount:
        return RuntimeAccount(
            ModelProvider.CODEX, AccountAvailability.AVAILABLE, 1, identity * 64
        )

    def snapshot(self) -> model_quota.QuotaSnapshot:
        return model_quota.QuotaSnapshot(time.time(), "Pro", (), 0, None, None)

    async def test_concurrent_and_manual_refreshes_share_one_account_request(
        self,
    ) -> None:
        started = threading.Event()
        release = threading.Event()

        def fetch(_account: RuntimeAccount) -> model_quota.QuotaSnapshot:
            started.set()
            if not release.wait(3):
                raise TimeoutError("Test did not release quota response")
            return self.snapshot()

        with (
            patch.object(model_quota, "read_accounts", return_value=(self.account(),)),
            patch.object(
                model_quota, "fetch_account_quota", side_effect=fetch
            ) as remote,
        ):
            cache = model_quota.QuotaCache()
            first = asyncio.create_task(cache.read(ModelProvider.CODEX, wait=True))
            self.assertTrue(await asyncio.to_thread(started.wait, 3))
            other = await cache.read(ModelProvider.CODEX, refresh=True)
            self.assertTrue(other.data.refreshing)
            self.assertIsNone(other.data.snapshot)
            release.set()
            complete = await first
            self.assertIsNotNone(complete.data.snapshot)
            self.assertFalse(complete.data.refreshing)
            again = await cache.read(ModelProvider.CODEX, refresh=True)
            self.assertEqual(again.data.snapshot, complete.data.snapshot)
            self.assertFalse(again.data.refreshing)
            self.assertEqual(remote.call_count, 1)

    async def test_account_change_discards_an_inflight_old_account_result(self) -> None:
        started = threading.Event()
        release = threading.Event()

        def fetch(_account: RuntimeAccount) -> model_quota.QuotaSnapshot:
            started.set()
            if not release.wait(3):
                raise TimeoutError("Test did not release quota response")
            return self.snapshot()

        with (
            patch.object(
                model_quota, "read_accounts", return_value=(self.account(),)
            ) as accounts,
            patch.object(
                model_quota, "fetch_account_quota", side_effect=fetch
            ) as remote,
        ):
            cache = model_quota.QuotaCache()
            first = asyncio.create_task(cache.read(ModelProvider.CODEX, wait=True))
            self.assertTrue(await asyncio.to_thread(started.wait, 3))
            accounts.return_value = (self.account("b"),)
            changed = await cache.read(ModelProvider.CODEX)
            self.assertEqual(changed.identity_sha256, "b" * 64)
            self.assertIsNone(changed.data.snapshot)
            self.assertEqual(remote.call_count, 1)
            release.set()
            old = await first
            self.assertIsNone(old.data.snapshot)
            self.assertEqual(old.identity_sha256, "b" * 64)

    async def test_failed_refresh_keeps_stale_data_and_rate_limits_retries(
        self,
    ) -> None:
        snapshot = self.snapshot()
        with (
            patch.object(model_quota, "read_accounts", return_value=(self.account(),)),
            patch.object(model_quota, "monotonic", return_value=0) as clock,
            patch.object(
                model_quota, "fetch_account_quota", return_value=snapshot
            ) as remote,
        ):
            cache = model_quota.QuotaCache()
            initial = await cache.read(ModelProvider.CODEX, wait=True)
            self.assertFalse(initial.data.stale)
            clock.return_value = 31
            remote.return_value = None
            failed = await cache.read(ModelProvider.CODEX, refresh=True, wait=True)
            self.assertEqual(failed.data.snapshot, snapshot)
            self.assertTrue(failed.data.stale)
            self.assertEqual(failed.data.unavailable_reason, "provider_unavailable")
            clock.return_value = 32
            again = await cache.read(ModelProvider.CODEX, refresh=True, wait=True)
            self.assertEqual(again.data.snapshot, snapshot)
            self.assertEqual(remote.call_count, 2)
            clock.return_value = 62
            remote.return_value = self.snapshot()
            recovered = await cache.read(ModelProvider.CODEX, wait=True)
            self.assertFalse(recovered.data.stale)
            self.assertIsNone(recovered.data.unavailable_reason)
            self.assertEqual(remote.call_count, 3)
