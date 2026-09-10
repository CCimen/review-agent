"""Account-bound quota observations and coalesced refresh inside Hermes."""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib import import_module
import logging
from time import monotonic, time
from typing import Literal, Protocol, cast
from uuid import UUID

from .model_accounts import (
    AccountAvailability,
    ModelProvider,
    RuntimeAccount,
    account_identity,
    codex_account_claims,
    read_accounts,
)


logger = logging.getLogger(__name__)
REFRESH_SECONDS = 300
MANUAL_REFRESH_SECONDS = 30
PREFLIGHT_WAIT_SECONDS = 5
QuotaUnavailable = Literal[
    "not_supported", "account_unavailable", "provider_unavailable"
]


@dataclass(frozen=True, slots=True)
class QuotaWindow:
    kind: Literal["primary", "secondary"]
    used_percent: float | None
    duration_seconds: int | None
    resets_at: float | None


@dataclass(frozen=True, slots=True)
class QuotaBucket:
    id: str
    name: str | None
    normal_model_slug: str | None
    allowed: bool | None
    limit_reached: bool | None
    windows: tuple[QuotaWindow, ...]


@dataclass(frozen=True, slots=True)
class QuotaSnapshot:
    fetched_at: float
    plan: str | None
    buckets: tuple[QuotaBucket, ...]
    reset_credits_available: int | None
    limit_reached_type: str | None
    spend_control_reached: bool | None


@dataclass(frozen=True, slots=True)
class AccountQuota:
    provider: ModelProvider
    snapshot: QuotaSnapshot | None
    refreshing: bool
    stale: bool
    next_refresh_at: float | None
    unavailable_reason: QuotaUnavailable | None


@dataclass(frozen=True, slots=True)
class ObservedQuota:
    identity_sha256: str | None
    data: AccountQuota


@dataclass(frozen=True, slots=True)
class ManagedQuota:
    runtime_key: str
    instance_id: UUID
    observation: ObservedQuota


@dataclass(frozen=True, slots=True)
class QuotaCheck:
    observed_at: datetime
    allowed: bool
    retry_at: datetime


def execution_quota(data: AccountQuota) -> QuotaCheck | None:
    """Only the provider's explicit account allowance can stop or resume work."""
    snapshot = data.snapshot
    if data.provider is not ModelProvider.CODEX or snapshot is None:
        return None
    main = next((bucket for bucket in snapshot.buckets if bucket.id == "codex"), None)
    if main is None or main.allowed is None:
        return None
    now = time()
    if main.allowed and (
        data.unavailable_reason is not None
        or now - snapshot.fetched_at >= REFRESH_SECONDS
    ):
        return None
    return QuotaCheck(
        datetime.fromtimestamp(snapshot.fetched_at, timezone.utc),
        main.allowed,
        datetime.fromtimestamp(
            max(
                now + MANUAL_REFRESH_SECONDS,
                min(data.next_refresh_at or now, now + REFRESH_SECONDS),
            ),
            timezone.utc,
        ),
    )


class _UsageWindow(Protocol):
    kind: str
    used_percent: float | None
    duration_seconds: int | None
    reset_at: datetime | None


class _UsageBucket(Protocol):
    id: str
    name: str | None
    normal_model_slug: str | None
    allowed: bool | None
    limit_reached: bool | None
    windows: tuple[_UsageWindow, ...]


class _UsageSnapshot(Protocol):
    fetched_at: datetime
    plan: str | None
    buckets: tuple[_UsageBucket, ...]
    reset_credits_available: int | None
    limit_reached_type: str | None
    spend_control_reached: bool | None


class _UsageModule(Protocol):
    def fetch_account_usage(
        self,
        provider: str,
        *,
        api_key: str,
        base_url: str,
        account_id: str,
        user_id: str,
    ) -> _UsageSnapshot | None: ...


class _AuthModule(Protocol):
    def resolve_codex_runtime_credentials(
        self, *, refresh_if_expiring: bool
    ) -> dict[str, object]: ...


def fetch_account_quota(account: RuntimeAccount) -> QuotaSnapshot | None:
    """Refresh through Hermes, then bind its existing usage reader to that token."""
    if account.provider is not ModelProvider.CODEX or account.identity_sha256 is None:
        return None
    auth = cast(_AuthModule, import_module("hermes_cli.auth"))
    credentials = auth.resolve_codex_runtime_credentials(refresh_if_expiring=True)
    token = credentials.get("api_key")
    base_url = credentials.get("base_url", "")
    if not isinstance(token, str) or not isinstance(base_url, str):
        return None
    if (
        account_identity(account.provider, token=token, auth_type="oauth")
        != account.identity_sha256
    ):
        return None
    claims = codex_account_claims(token)
    if claims is None:
        return None
    usage = cast(_UsageModule, import_module("agent.account_usage"))
    snapshot = usage.fetch_account_usage(
        account.provider.value,
        api_key=token,
        base_url=base_url,
        account_id=claims[0],
        user_id=claims[1],
    )
    current = next(
        item for item in read_accounts() if item.provider is account.provider
    )
    if snapshot is None or current.identity_sha256 != account.identity_sha256:
        return None
    buckets: list[QuotaBucket] = []
    for bucket in snapshot.buckets:
        windows: list[QuotaWindow] = []
        for window in bucket.windows:
            if window.kind not in ("primary", "secondary"):
                raise ValueError("Unsupported quota window")
            windows.append(
                QuotaWindow(
                    window.kind,
                    window.used_percent,
                    window.duration_seconds,
                    window.reset_at.timestamp()
                    if window.reset_at is not None
                    else None,
                )
            )
        buckets.append(
            QuotaBucket(
                bucket.id,
                bucket.name,
                bucket.normal_model_slug,
                bucket.allowed,
                bucket.limit_reached,
                tuple(windows),
            )
        )
    return QuotaSnapshot(
        snapshot.fetched_at.timestamp(),
        snapshot.plan,
        tuple(buckets),
        snapshot.reset_credits_available,
        snapshot.limit_reached_type,
        snapshot.spend_control_reached,
    )


@dataclass(slots=True)
class _CacheEntry:
    identity_sha256: str | None = None
    snapshot: QuotaSnapshot | None = None
    task: asyncio.Task[None] | None = None
    next_refresh: float = 0
    next_manual_refresh: float = 0
    failures: int = 0


class QuotaCache:
    """One bounded refresh task per provider, retained when a caller disconnects."""

    def __init__(self) -> None:
        self._entries = {provider: _CacheEntry() for provider in ModelProvider}

    async def close(self) -> None:
        await asyncio.gather(
            *(
                asyncio.shield(entry.task)
                for entry in self._entries.values()
                if entry.task is not None
            )
        )

    async def read(
        self, provider: ModelProvider, *, refresh: bool = False, wait: bool = False
    ) -> ObservedQuota:
        accounts = await asyncio.to_thread(read_accounts)
        account = next(item for item in accounts if item.provider is provider)
        entry = self._entries[provider]
        if entry.identity_sha256 != account.identity_sha256:
            entry.identity_sha256 = account.identity_sha256
            entry.snapshot = None
            entry.next_refresh = entry.next_manual_refresh = 0
            entry.failures = 0
        if account.availability is not AccountAvailability.AVAILABLE:
            entry.snapshot = None
            return ObservedQuota(
                account.identity_sha256,
                AccountQuota(
                    provider,
                    None,
                    False,
                    True,
                    None,
                    "account_unavailable",
                ),
            )
        if provider is not ModelProvider.CODEX:
            return ObservedQuota(
                account.identity_sha256,
                AccountQuota(
                    provider,
                    None,
                    False,
                    True,
                    None,
                    "not_supported",
                ),
            )
        now = monotonic()
        if entry.task is None and (
            now >= entry.next_refresh or (refresh and now >= entry.next_manual_refresh)
        ):
            entry.task = asyncio.create_task(self._refresh(entry, account))
        if wait and entry.task is not None:
            try:
                await asyncio.wait_for(
                    asyncio.shield(entry.task), PREFLIGHT_WAIT_SECONDS
                )
            except TimeoutError:
                pass
        return self._result(provider, entry)

    async def _refresh(self, entry: _CacheEntry, account: RuntimeAccount) -> None:
        try:
            snapshot = await asyncio.to_thread(fetch_account_quota, account)
        except Exception as exc:
            logger.warning("Provider quota read failed: %s", type(exc).__name__)
            snapshot = None
        finally:
            entry.task = None
        if account.identity_sha256 != entry.identity_sha256:
            return
        now = monotonic()
        entry.failures = min(entry.failures + 1, 5) if snapshot is None else 0
        if snapshot is not None:
            entry.snapshot = snapshot
        delay = (
            min(30 * 2 ** (entry.failures - 1), REFRESH_SECONDS)
            if entry.failures
            else REFRESH_SECONDS
        )
        if snapshot is not None:
            main = next(
                (bucket for bucket in snapshot.buckets if bucket.id == "codex"), None
            )
            if main is not None and main.allowed is False:
                resets = [
                    window.resets_at
                    for window in main.windows
                    if window.resets_at is not None and window.resets_at > time()
                ]
                if resets:
                    delay = max(
                        MANUAL_REFRESH_SECONDS, min(delay, min(resets) - time())
                    )
        entry.next_refresh = now + delay
        entry.next_manual_refresh = now + (
            delay if entry.failures else MANUAL_REFRESH_SECONDS
        )

    def _result(self, provider: ModelProvider, entry: _CacheEntry) -> ObservedQuota:
        snapshot = entry.snapshot
        wall_time = time()
        stale = (
            snapshot is None
            or bool(entry.failures)
            or wall_time - snapshot.fetched_at >= REFRESH_SECONDS
        )
        if snapshot is not None:
            stale = stale or any(
                window.resets_at is not None and window.resets_at <= wall_time
                for bucket in snapshot.buckets
                for window in bucket.windows
            )
        return ObservedQuota(
            entry.identity_sha256,
            AccountQuota(
                provider,
                snapshot,
                entry.task is not None,
                stale,
                wall_time + max(0, entry.next_refresh - monotonic()),
                "provider_unavailable" if entry.failures else None,
            ),
        )
